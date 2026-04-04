"""Wave 3 pipeline integration tests.

All external service calls are mocked via unittest.mock.patch on
service_client.submit_job and service_client.poll_job.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _make_project(tmp_path, match_threshold=0.75):
    """Bootstrap a project DB directly without going through the HTTP layer."""
    import os
    os.environ["DATA_DIR"] = str(tmp_path)
    import db
    db._connections.clear()
    project_id = str(uuid.uuid4())
    db.create_project_db(project_id)
    conn = db.get_conn(project_id)
    now = _now()
    conn.execute(
        """INSERT INTO projects (id, name, created_at, updated_at, status, match_threshold, whisper_model)
           VALUES (?,?,?,?,'ready',?,'large-v2')""",
        (project_id, "Test", now, now, match_threshold),
    )
    conn.commit()
    return project_id


def _insert_source(conn, project_id, status="step1_pending", audio_path=None, vocals_path=None):
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO sources (id, project_id, filename, file_path, audio_path, vocals_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (source_id, project_id, "ep.mkv", "source/ep.mkv",
         audio_path or f"audio/raw/{source_id}.wav",
         vocals_path,
         status, now, now),
    )
    conn.commit()
    return source_id


def _insert_segment(conn, project_id, source_id, status="pending", confidence=0.9,
                    transcript=None, transcript_edited=None, raw_path=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs,
           speaker_label, match_confidence, status, transcript, transcript_edited, created_at, updated_at)
           VALUES (?,?,?,?,0,5,'S0',?,?,?,?,?,?)""",
        (seg_id, project_id, source_id,
         raw_path or f"segments/raw/{seg_id}.wav",
         confidence, status, transcript, transcript_edited, now, now),
    )
    conn.commit()
    return seg_id


class TestServiceClient:
    async def test_submit_job_posts_to_service(self):
        import httpx
        import respx
        from service_client import submit_job

        with respx.mock:
            respx.post("http://svc:8001/jobs").mock(
                return_value=httpx.Response(202, json={"job_id": "abc"})
            )
            result = await submit_job("http://svc:8001", {"job_id": "abc", "model": "htdemucs"})
        assert result["job_id"] == "abc"

    async def test_poll_job_returns_on_complete(self):
        import httpx
        import respx
        from service_client import poll_job

        with respx.mock:
            respx.get("http://svc:8001/jobs/abc").mock(
                return_value=httpx.Response(200, json={"job_id": "abc", "status": "complete", "output_path": "/data/out.wav"})
            )
            result = await poll_job("http://svc:8001", "abc", poll_interval=0)
        assert result["status"] == "complete"

    async def test_poll_job_calls_on_progress_for_running(self):
        import httpx
        import respx
        from service_client import poll_job

        progress_calls = []

        async def capture(r):
            progress_calls.append(r["progress"])

        responses = [
            httpx.Response(200, json={"job_id": "abc", "status": "running", "progress": 50}),
            httpx.Response(200, json={"job_id": "abc", "status": "complete", "progress": 100, "output_path": "/out.wav"}),
        ]
        with respx.mock:
            respx.get("http://svc:8001/jobs/abc").mock(side_effect=responses)
            result = await poll_job("http://svc:8001", "abc", poll_interval=0, on_progress=capture)

        assert result["status"] == "complete"
        assert progress_calls == [50]

    async def test_poll_job_returns_on_failed(self):
        import httpx
        import respx
        from service_client import poll_job

        with respx.mock:
            respx.get("http://svc:8001/jobs/abc").mock(
                return_value=httpx.Response(200, json={
                    "job_id": "abc", "status": "failed",
                    "error": "cuda_oom", "retry_with_chunk_secs": 60
                })
            )
            result = await poll_job("http://svc:8001", "abc", poll_interval=0)
        assert result["status"] == "failed"
        assert result["error"] == "cuda_oom"
        assert result["retry_with_chunk_secs"] == 60


class TestDeferredBugFixes:
    def test_export_complete_sets_exported_status(self, isolated_data_dir):
        """Bug #1: recompute_project_status must reach 'exported' after export job completes."""
        from status import recompute_project_status
        import db

        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        now = _now()

        # Simulate: export job just completed — status is still 'exporting'
        conn.execute("UPDATE projects SET status='exporting', updated_at=? WHERE id=?", (now, project_id))
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at, completed_at) VALUES (?,?,'export','complete',?,?)",
            (str(uuid.uuid4()), project_id, now, now),
        )
        conn.commit()

        # After job completes, no active jobs, exporting → should become 'exported'
        recompute_project_status(project_id)
        status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"]
        assert status == "exported"

    def test_low_coverage_warning_at_zero(self, isolated_data_dir):
        """Bug #2: coverage_ratio=0.0 should trigger low_coverage_warning."""
        project_id = _make_project(isolated_data_dir)
        import db
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        conn.execute("UPDATE sources SET coverage_ratio=0.0 WHERE id=?", (source_id,))
        conn.commit()

        from routers.projects import _project_stats
        stats = _project_stats(project_id)
        cov = next(s for s in stats["source_coverage"] if s["source_id"] == source_id)
        assert cov["low_coverage_warning"] is True

    def test_low_coverage_warning_not_triggered_when_null(self, isolated_data_dir):
        """coverage_ratio IS NULL (not yet diarised) should NOT warn."""
        project_id = _make_project(isolated_data_dir)
        import db
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending")
        # coverage_ratio is NULL by default

        from routers.projects import _project_stats
        stats = _project_stats(project_id)
        cov = next(s for s in stats["source_coverage"] if s["source_id"] == source_id)
        assert cov["low_coverage_warning"] is False


class TestVocalSeparationHandler:
    async def test_success_updates_source_to_step2_pending(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending")
        conn.execute("UPDATE sources SET audio_path=? WHERE id=?",
                     (f"audio/raw/{source_id}.wav", source_id))
        conn.commit()

        svc_result = {
            "job_id": "svc-job-1", "status": "complete", "progress": 100,
            "output_path": f"/data/projects/{project_id}/audio/vocals/{source_id}.wav",
            "error": None, "retry_with_chunk_secs": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-job-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=svc_result):
            job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id)
            await jobs._handle_vocal_separation(project_id, job_id, source_id, {})

        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "step2_pending"
        assert source["vocals_path"] == f"audio/vocals/{source_id}.wav"
        assert source["step1_model"] == "htdemucs"

        # A diarisation job should have been enqueued
        diar_job = conn.execute(
            "SELECT * FROM jobs WHERE project_id=? AND type='diarisation'", (project_id,)
        ).fetchone()
        assert diar_job is not None
        assert diar_job["source_id"] == source_id

    async def test_success_with_custom_model(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending")
        conn.execute("UPDATE sources SET audio_path=? WHERE id=?",
                     (f"audio/raw/{source_id}.wav", source_id))
        conn.commit()

        svc_result = {
            "job_id": "svc-1", "status": "complete", "progress": 100,
            "output_path": f"/data/projects/{project_id}/audio/vocals/{source_id}.wav",
            "error": None, "retry_with_chunk_secs": None,
        }
        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=svc_result):
            job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id,
                                  params={"demucs_model": "mdx_extra"})
            await jobs._handle_vocal_separation(project_id, job_id, source_id,
                                                {"demucs_model": "mdx_extra"})

        source = conn.execute("SELECT step1_model FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["step1_model"] == "mdx_extra"

    async def test_oom_retry_succeeds_on_second_attempt(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending")
        conn.execute("UPDATE sources SET audio_path=? WHERE id=?",
                     (f"audio/raw/{source_id}.wav", source_id))
        conn.commit()

        first_fail = {
            "job_id": "svc-1", "status": "failed", "error": "cuda_oom",
            "retry_with_chunk_secs": 60,
        }
        retry_success = {
            "job_id": "svc-2", "status": "complete", "progress": 100,
            "output_path": f"/data/projects/{project_id}/audio/vocals/{source_id}.wav",
            "error": None, "retry_with_chunk_secs": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}) as mock_submit, \
             patch("jobs.poll_job", new_callable=AsyncMock, side_effect=[first_fail, retry_success]):
            job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id)
            await jobs._handle_vocal_separation(project_id, job_id, source_id, {})

        # Second submit call should have chunk_secs set
        assert mock_submit.call_count == 2
        second_call_payload = mock_submit.call_args_list[1][0][1]  # positional arg index 1 is payload
        assert second_call_payload["chunk_secs"] == 60

        source = conn.execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "step2_pending"

    async def test_oom_retry_fails_on_second_attempt_marks_step1_failed(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending")
        conn.execute("UPDATE sources SET audio_path=? WHERE id=?",
                     (f"audio/raw/{source_id}.wav", source_id))
        conn.commit()

        fail1 = {"job_id": "svc-1", "status": "failed", "error": "cuda_oom", "retry_with_chunk_secs": 60}
        fail2 = {"job_id": "svc-2", "status": "failed", "error": "cuda_oom", "retry_with_chunk_secs": None}

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, side_effect=[fail1, fail2]):
            job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id)
            await jobs._handle_vocal_separation(project_id, job_id, source_id, {})

        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "step1_failed"
        assert source["step1_error"] is not None

        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"

    async def test_non_oom_failure_marks_step1_failed(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending")
        conn.execute("UPDATE sources SET audio_path=? WHERE id=?",
                     (f"audio/raw/{source_id}.wav", source_id))
        conn.commit()

        fail = {"job_id": "svc-1", "status": "failed", "error": "model_error", "retry_with_chunk_secs": None}

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=fail):
            job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id)
            await jobs._handle_vocal_separation(project_id, job_id, source_id, {})

        source = conn.execute("SELECT status, step1_error FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "step1_failed"
        assert "model_error" in source["step1_error"]
