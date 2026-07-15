"""Tuning-preview endpoint + ephemeral cleanup A/B job.

Stage-generic ephemeral preview: render ONE segment through the cleanup
service with draft params, output to a scratch dir under
projects/{id}/tuning_previews/, never touching segment tables and never
driving project status.
"""

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _insert_source(conn, project_id, status="separation_pending"):
    sid = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO sources
           (id, project_id, filename, file_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (sid, project_id, "ep01.mkv", "source/ep01.mkv", status, now, now),
    )
    conn.commit()
    return sid


def _insert_seg(conn, project_id, source_id, status="pending", confidence=0.9,
                start=0.0, end=10.0, transcript=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments
           (id, project_id, source_id, raw_path, start_secs, end_secs,
            speaker_label, match_confidence, status, transcript, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, f"segments/raw/{seg_id}.wav",
         start, end, "SPEAKER_00", confidence, status, transcript, now, now),
    )
    conn.commit()
    return seg_id


def _set_config(conn, project_id, **cols):
    set_clause = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE projects SET {set_clause} WHERE id=?", (*cols.values(), project_id))
    conn.commit()


def _insert_job(conn, project_id, job_type, status="running"):
    jid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO jobs (id, project_id, type, status, params, created_at) VALUES (?,?,?,?,?,?)",
        (jid, project_id, job_type, status, "{}", _now()),
    )
    conn.commit()
    return jid


def _run_job(project_id, job_id):
    import jobs
    loop = asyncio.new_event_loop()
    loop.run_until_complete(jobs._execute_job(project_id, job_id))
    loop.close()


def _enqueue_and_run(project_id, job_type, source_id=None, params=None):
    import jobs
    job_id = jobs.enqueue(project_id, job_type, source_id=source_id, params=params)
    try:
        jobs._queues[project_id].get_nowait()
    except Exception:
        pass
    _run_job(project_id, job_id)
    return job_id


def _capture():
    captured = {}

    async def mock_submit(service, payload):
        captured["service"] = service
        captured["payload"] = payload
        return {"job_id": payload["job_id"]}

    return captured, mock_submit


VALID_PARAMS = {
    "target_lufs": -20.0,
    "highpass_hz": 100,
    "do_trim_silence": True,
    "silence_threshold_db": -40.0,
    "silence_min_duration_secs": 0.2,
    "silence_pad_start_secs": 0.05,
    "silence_pad_end_secs": 0.2,
}


def _valid_body(segment_id):
    return {
        "stage": "cleanup",
        "params": dict(VALID_PARAMS),
        "target": {"segment_id": segment_id},
    }


class TestCreateTuningPreviewRouter:
    def test_valid_request_202_and_job_row(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        sid = _insert_source(conn, project)
        seg = _insert_seg(conn, project, sid)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)), \
             patch("service_client.submit_job", new=AsyncMock(return_value={"job_id": "x"})), \
             patch("service_client.poll_until_complete", new=AsyncMock(return_value={"status": "complete", "results": []})):
            resp = client.post(f"/projects/{project}/tuning-preview", json=_valid_body(seg))

        assert resp.status_code == 202
        body = resp.json()
        job_id = body["enqueued_job"]["id"]
        assert job_id

        row = conn.execute("SELECT type FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row is not None
        assert row["type"] == "tuning_preview"

    def test_out_of_range_param_422(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        sid = _insert_source(conn, project)
        seg = _insert_seg(conn, project, sid)

        body = _valid_body(seg)
        body["params"]["target_lufs"] = 0.0

        resp = client.post(f"/projects/{project}/tuning-preview", json=body)
        assert resp.status_code == 422

    def test_stage_separation_422(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        sid = _insert_source(conn, project)
        seg = _insert_seg(conn, project, sid)

        body = _valid_body(seg)
        body["stage"] = "separation"

        resp = client.post(f"/projects/{project}/tuning-preview", json=body)
        assert resp.status_code == 422

    def test_unknown_segment_404(self, client, project, isolated_data_dir):
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/tuning-preview", json=_valid_body(str(uuid.uuid4())))
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    def test_unhealthy_cleanup_503(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        sid = _insert_source(conn, project)
        seg = _insert_seg(conn, project, sid)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=False)):
            resp = client.post(f"/projects/{project}/tuning-preview", json=_valid_body(seg))
        assert resp.status_code == 503
        assert resp.json()["error"] == "cleanup_unavailable"

    def test_ttl_sweep_removes_stale_wav(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        sid = _insert_source(conn, project)
        seg = _insert_seg(conn, project, sid)

        stale_dir = db.project_dir(project) / "tuning_previews"
        stale_dir.mkdir(parents=True, exist_ok=True)
        stale = stale_dir / "old.wav"
        stale.write_bytes(b"x")
        old_time = time.time() - 25 * 3600
        os.utime(stale, (old_time, old_time))

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)), \
             patch("service_client.submit_job", new=AsyncMock(return_value={"job_id": "x"})), \
             patch("service_client.poll_until_complete", new=AsyncMock(return_value={"status": "complete", "results": []})):
            resp = client.post(f"/projects/{project}/tuning-preview", json=_valid_body(seg))
        assert resp.status_code == 202
        assert not stale.exists()

    def test_get_status_then_unknown_404(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        sid = _insert_source(conn, project)
        seg = _insert_seg(conn, project, sid)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)), \
             patch("service_client.submit_job", new=AsyncMock(return_value={"job_id": "x"})), \
             patch("service_client.poll_until_complete", new=AsyncMock(return_value={"status": "complete", "results": []})):
            resp = client.post(f"/projects/{project}/tuning-preview", json=_valid_body(seg))
        job_id = resp.json()["enqueued_job"]["id"]

        status_resp = client.get(f"/projects/{project}/tuning-preview/{job_id}")
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["id"] == job_id
        assert "status" in body

        unknown_resp = client.get(f"/projects/{project}/tuning-preview/{uuid.uuid4()}")
        assert unknown_resp.status_code == 404


class TestTuningPreviewAudioRouter:
    def test_audio_200_when_complete_and_file_exists(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        job_id = _insert_job(conn, project, "tuning_preview", status="complete")

        d = db.project_dir(project) / "tuning_previews"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{job_id}.wav").write_bytes(b"RIFF....WAVEfmt ")

        resp = client.get(f"/projects/{project}/tuning-preview/{job_id}/audio")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"

    def test_audio_404_when_file_missing(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        job_id = _insert_job(conn, project, "tuning_preview", status="complete")

        resp = client.get(f"/projects/{project}/tuning-preview/{job_id}/audio")
        assert resp.status_code == 404
        assert resp.json()["error"] == "preview_not_ready"

    def test_audio_404_when_job_queued(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        job_id = _insert_job(conn, project, "tuning_preview", status="queued")

        resp = client.get(f"/projects/{project}/tuning-preview/{job_id}/audio")
        assert resp.status_code == 404
        assert resp.json()["error"] == "preview_not_ready"


class TestTuningPreviewHandler:
    def test_payload_capture_draft_overrides_config(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_config(conn, project, target_lufs=-18.0, highpass_hz=120,
                    silence_threshold_db=-45.0, silence_min_duration_secs=0.25)
        sid = _insert_source(conn, project)
        seg = _insert_seg(conn, project, sid)
        captured, mock_submit = _capture()

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete",
                    "results": [{"id": seg, "output_path": "/x.wav", "error": None, "auto_rejected": False}]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "tuning_preview",
                                       params={"stage": "cleanup", "segment_id": seg, "params": VALID_PARAMS})

        assert captured["service"] == "cleanup"
        params = captured["payload"]["params"]
        # Draft values win over saved project config.
        assert params["target_lufs"] == VALID_PARAMS["target_lufs"]
        assert params["highpass_hz"] == VALID_PARAMS["highpass_hz"]
        assert params["silence_threshold_db"] == VALID_PARAMS["silence_threshold_db"]
        assert params["silence_min_duration_secs"] == VALID_PARAMS["silence_min_duration_secs"]
        # Fixed keys still present from _cleanup_params.
        assert params["true_peak_dbtp"] == -2.0
        assert params["output_sample_rate"] == 22050
        assert captured["payload"]["segments"][0]["output_path"].endswith(f"tuning_previews/{job_id}.wav")

        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "complete"

    def test_poll_failed_fails_job(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        sid = _insert_source(conn, project)
        seg = _insert_seg(conn, project, sid)

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "failed", "error": "boom"}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "tuning_preview",
                                       params={"stage": "cleanup", "segment_id": seg, "params": VALID_PARAMS})

        row = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "failed"
        assert row["error"] == "boom"

    def test_segment_result_error_fails_job_with_prefix(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        sid = _insert_source(conn, project)
        seg = _insert_seg(conn, project, sid)

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete",
                    "results": [{"id": seg, "error": "ffmpeg exploded"}]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "tuning_preview",
                                       params={"stage": "cleanup", "segment_id": seg, "params": VALID_PARAMS})

        row = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "failed"
        assert row["error"].startswith("cleanup_error:")

    def test_segment_deleted_before_run_fails_job(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        sid = _insert_source(conn, project)
        seg = _insert_seg(conn, project, sid)
        conn.execute("DELETE FROM segments WHERE id=?", (seg,))
        conn.commit()

        with patch("service_client.submit_job", new=AsyncMock()), \
             patch("service_client.poll_until_complete", new=AsyncMock()):
            job_id = _enqueue_and_run(project, "tuning_preview",
                                       params={"stage": "cleanup", "segment_id": seg, "params": VALID_PARAMS})

        row = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "failed"
        assert "segment_not_found" in row["error"]

    def test_status_exemption_does_not_flip_project_to_processing(self, client, project, isolated_data_dir):
        import db
        from status import recompute_project_status
        conn = db.get_conn(project)
        _insert_source(conn, project, status="complete")
        _insert_job(conn, project, "tuning_preview", status="queued")

        recompute_project_status(project)
        row = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert row["status"] != "processing"
