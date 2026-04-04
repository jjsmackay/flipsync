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
