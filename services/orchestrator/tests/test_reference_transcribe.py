"""Reference-clip transcription: auto on set, manual re-run, and the handler
that writes projects.reference_transcript.

The reference is not a segment — the handler submits a single synthetic segment
(id "reference") to the transcription service and stores the returned text on the
project row. Read-only surface; status-exempt so it never drives project status.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _set_reference(conn, project_id, path="reference.wav", transcript=None):
    conn.execute(
        "UPDATE projects SET reference_path=?, reference_transcript=? WHERE id=?",
        (path, transcript, project_id),
    )
    conn.commit()


def _run_job(project_id, job_id):
    import jobs
    loop = asyncio.new_event_loop()
    loop.run_until_complete(jobs._execute_job(project_id, job_id))
    loop.close()


def _enqueue_and_run(project_id, job_type, params=None):
    import jobs
    job_id = jobs.enqueue(project_id, job_type, params=params)
    try:
        jobs._queues[project_id].get_nowait()
    except Exception:
        pass
    _run_job(project_id, job_id)
    return job_id


def _mock_poll(transcript="hello world", confidence=0.92, error=None):
    entry = {"id": "reference"}
    if error is not None:
        entry["error"] = error
    else:
        entry["transcript"] = transcript
        entry["transcript_confidence"] = confidence

    async def poll(service, job_id, interval_secs=2.0, on_progress=None):
        return {"status": "complete", "completed_segments": [entry]}

    return poll


class TestReferenceTranscribeHandler:
    def test_writes_transcript_to_project(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project)

        captured = {}

        async def mock_submit(service, payload):
            captured["service"] = service
            captured["payload"] = payload
            return {"job_id": payload["job_id"]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=_mock_poll("the quick brown fox"))):
            job_id = _enqueue_and_run(project, "reference_transcribe")

        assert captured["service"] == "transcription"
        seg = captured["payload"]["segments"][0]
        assert seg["id"] == "reference"
        assert seg["wav_path"].endswith(f"projects/{project}/reference.wav")
        assert captured["payload"]["batch_size"] == 1

        row = conn.execute(
            "SELECT reference_transcript FROM projects WHERE id=?", (project,)
        ).fetchone()
        assert row["reference_transcript"] == "the quick brown fox"

        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "complete"

    def test_no_reference_fails_job(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        # reference_path stays NULL

        with patch("service_client.submit_job", new=AsyncMock()), \
             patch("service_client.poll_until_complete", new=AsyncMock()):
            job_id = _enqueue_and_run(project, "reference_transcribe")

        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "no_reference" in job["error"]

    def test_per_entry_error_fails_job_and_leaves_transcript_null(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, transcript=None)

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=_mock_poll(error="whisper exploded"))):
            job_id = _enqueue_and_run(project, "reference_transcribe")

        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert job["error"].startswith("transcription_error:")
        row = conn.execute("SELECT reference_transcript FROM projects WHERE id=?", (project,)).fetchone()
        assert row["reference_transcript"] is None

    def test_poll_failed_fails_job(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project)

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "failed", "error": "boom"}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "reference_transcribe")

        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert job["error"] == "boom"

    def test_status_exempt_does_not_flip_project(self, client, project, isolated_data_dir):
        import db
        from status import recompute_project_status
        conn = db.get_conn(project)
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, params, created_at) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), project, "reference_transcribe", "running", "{}", _now()),
        )
        conn.commit()

        recompute_project_status(project)
        row = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert row["status"] != "processing"


class TestAutoEnqueueOnFinalise:
    def test_upload_enqueues_when_transcription_healthy(self, client, project, test_wav, isolated_data_dir):
        import db
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)), \
             patch("service_client.submit_job", new=AsyncMock(return_value={"job_id": "x"})), \
             patch("service_client.poll_until_complete",
                   new=AsyncMock(return_value={"status": "complete", "completed_segments": []})):
            with open(test_wav, "rb") as f:
                resp = client.post(
                    f"/projects/{project}/reference",
                    files={"file": ("ref.wav", f, "audio/wav")},
                )
        assert resp.status_code == 200
        conn = db.get_conn(project)
        row = conn.execute(
            "SELECT id FROM jobs WHERE project_id=? AND type='reference_transcribe'", (project,)
        ).fetchone()
        assert row is not None

    def test_upload_skips_enqueue_when_transcription_down(self, client, project, test_wav, isolated_data_dir):
        import db
        with patch("service_client.is_healthy", new=AsyncMock(return_value=False)):
            with open(test_wav, "rb") as f:
                resp = client.post(
                    f"/projects/{project}/reference",
                    files={"file": ("ref.wav", f, "audio/wav")},
                )
        assert resp.status_code == 200
        conn = db.get_conn(project)
        row = conn.execute(
            "SELECT id FROM jobs WHERE project_id=? AND type='reference_transcribe'", (project,)
        ).fetchone()
        assert row is None

    def test_replacing_reference_clears_stale_transcript(self, client, project, test_wav, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        # Pretend a previous transcript exists.
        conn.execute(
            "UPDATE projects SET reference_transcript='stale text' WHERE id=?", (project,)
        )
        conn.commit()

        with patch("service_client.is_healthy", new=AsyncMock(return_value=False)):
            with open(test_wav, "rb") as f:
                client.post(
                    f"/projects/{project}/reference",
                    files={"file": ("ref.wav", f, "audio/wav")},
                )

        row = conn.execute("SELECT reference_transcript FROM projects WHERE id=?", (project,)).fetchone()
        assert row["reference_transcript"] is None


class TestTranscribeEndpoint:
    def test_202_when_reference_and_healthy(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)), \
             patch("service_client.submit_job", new=AsyncMock(return_value={"job_id": "x"})), \
             patch("service_client.poll_until_complete",
                   new=AsyncMock(return_value={"status": "complete", "completed_segments": []})):
            resp = client.post(f"/projects/{project}/reference/transcribe")

        assert resp.status_code == 202
        body = resp.json()
        assert body["enqueued_job"]["type"] == "reference_transcribe"
        assert body["enqueued_job"]["id"]

    def test_409_when_no_reference(self, client, project, isolated_data_dir):
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/reference/transcribe")
        assert resp.status_code == 409
        assert resp.json()["error"] == "no_reference"

    def test_503_when_transcription_down(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=False)):
            resp = client.post(f"/projects/{project}/reference/transcribe")
        assert resp.status_code == 503
        assert resp.json()["error"] == "transcription_unavailable"


class TestProjectDetailExposesTranscript:
    def test_reference_transcript_in_project_body(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, transcript="surfaced text")

        resp = client.get(f"/projects/{project}")
        assert resp.status_code == 200
        assert resp.json()["reference_transcript"] == "surfaced text"
