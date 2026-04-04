"""Job queue lifecycle tests — enqueue, run, complete, fail.

These tests use asyncio to exercise the actual asyncio job runner,
not just the database state.
"""

import asyncio
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

ffmpeg_available = shutil.which("ffmpeg") is not None
requires_ffmpeg = pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg not installed")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _make_project(tmp_path):
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
        "INSERT INTO projects (id, name, created_at, updated_at, status) VALUES (?,?,?,?,'new')",
        (project_id, "Test", now, now),
    )
    conn.commit()
    return project_id


class TestEnqueue:
    def test_enqueue_creates_job_row(self, isolated_data_dir):
        project_id = _make_project(isolated_data_dir)
        import jobs, db
        jobs._queues.clear()
        jobs._runners.clear()
        jobs._project_locks.clear()

        job_id = jobs.enqueue(project_id, "extract_audio")

        conn = db.get_conn(project_id)
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row is not None
        assert row["type"] == "extract_audio"
        assert row["status"] == "queued"
        assert row["project_id"] == project_id

    def test_enqueue_with_source_id(self, isolated_data_dir):
        project_id = _make_project(isolated_data_dir)
        import jobs, db
        jobs._queues.clear()
        jobs._runners.clear()
        jobs._project_locks.clear()

        source_id = str(uuid.uuid4())
        job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id)

        conn = db.get_conn(project_id)
        row = conn.execute("SELECT source_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["source_id"] == source_id

    def test_enqueue_with_params(self, isolated_data_dir):
        import json
        project_id = _make_project(isolated_data_dir)
        import jobs, db
        jobs._queues.clear()
        jobs._runners.clear()
        jobs._project_locks.clear()

        job_id = jobs.enqueue(project_id, "transcription_bulk", params={"model": "small"})

        conn = db.get_conn(project_id)
        row = conn.execute("SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert json.loads(row["params"])["model"] == "small"

    def test_enqueue_adds_to_queue(self, isolated_data_dir):
        project_id = _make_project(isolated_data_dir)
        import jobs
        jobs._queues.clear()
        jobs._runners.clear()
        jobs._project_locks.clear()

        jobs.enqueue(project_id, "extract_audio")
        assert not jobs._queues[project_id].empty()


class TestExtractAudioJob:
    @requires_ffmpeg
    def test_extract_audio_success(self, isolated_data_dir, test_wav):
        """Full integration: upload WAV via enqueue, verify status transitions."""
        project_id = _make_project(isolated_data_dir)
        import jobs, db

        # Create a source row pointing at a copy of the test WAV
        conn = db.get_conn(project_id)
        source_id = str(uuid.uuid4())
        now = _now()

        # Copy the test WAV to where the source should be
        from db import project_dir
        src_dir = project_dir(project_id) / "source"
        src_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        dest_wav = src_dir / f"{source_id}.wav"
        shutil.copy(test_wav, dest_wav)

        conn.execute(
            "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,'uploaded',?,?)",
            (source_id, project_id, "test.wav", f"source/{source_id}.wav", now, now),
        )
        conn.commit()

        # Run extraction in-process (synchronously via asyncio)
        async def run():
            jobs._queues.clear()
            jobs._runners.clear()
            jobs._project_locks.clear()
            job_id = jobs.enqueue(project_id, "extract_audio", source_id=source_id)
            # Give the background task time to complete
            await asyncio.sleep(5)
            return job_id

        job_id = asyncio.run(run())

        row = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        source_row = conn.execute("SELECT status, audio_path FROM sources WHERE id=?", (source_id,)).fetchone()

        assert row["status"] == "complete", f"Job failed: {row['error']}"
        assert source_row["status"] == "step1_pending"
        assert source_row["audio_path"] is not None

        # Check the extracted WAV exists
        audio_file = project_dir(project_id) / source_row["audio_path"]
        assert audio_file.exists()

    @requires_ffmpeg
    def test_extract_audio_missing_file_fails(self, isolated_data_dir):
        project_id = _make_project(isolated_data_dir)
        import jobs, db

        conn = db.get_conn(project_id)
        source_id = str(uuid.uuid4())
        now = _now()
        conn.execute(
            "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,'uploaded',?,?)",
            (source_id, project_id, "missing.wav", "source/missing.wav", now, now),
        )
        conn.commit()

        async def run():
            jobs._queues.clear()
            jobs._runners.clear()
            jobs._project_locks.clear()
            job_id = jobs.enqueue(project_id, "extract_audio", source_id=source_id)
            await asyncio.sleep(3)
            return job_id

        job_id = asyncio.run(run())

        row = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "failed"
        assert row["error"] is not None

        source_row = conn.execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source_row["status"] == "extraction_failed"


class TestJobLifecycle:
    @requires_ffmpeg
    def test_job_status_transitions(self, isolated_data_dir, test_wav):
        """Verify queued → running → complete/failed in DB."""
        project_id = _make_project(isolated_data_dir)
        import jobs, db

        conn = db.get_conn(project_id)
        source_id = str(uuid.uuid4())
        now = _now()

        # Copy test WAV
        from db import project_dir
        src_dir = project_dir(project_id) / "source"
        src_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy(test_wav, src_dir / f"{source_id}.wav")

        conn.execute(
            "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,'uploaded',?,?)",
            (source_id, project_id, "test.wav", f"source/{source_id}.wav", now, now),
        )
        conn.commit()

        async def run():
            jobs._queues.clear()
            jobs._runners.clear()
            jobs._project_locks.clear()
            job_id = jobs.enqueue(project_id, "extract_audio", source_id=source_id)
            # Initially queued
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert row["status"] == "queued"
            await asyncio.sleep(5)
            return job_id

        job_id = asyncio.run(run())
        row = conn.execute("SELECT status, started_at, completed_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "complete"
        assert row["started_at"] is not None
        assert row["completed_at"] is not None

    def test_service_job_fails_gracefully_without_audio_path(self, isolated_data_dir):
        """Vocal separation handler marks job as failed when source has no audio_path."""
        project_id = _make_project(isolated_data_dir)
        import jobs, db
        jobs._queues.clear()
        jobs._runners.clear()
        jobs._project_locks.clear()

        conn = db.get_conn(project_id)
        source_id = str(uuid.uuid4())
        now = _now()
        conn.execute(
            "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,'step1_pending',?,?)",
            (source_id, project_id, "ep.wav", "source/ep.wav", now, now),
        )
        conn.commit()

        async def run():
            job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id)
            await asyncio.sleep(2)
            return job_id

        job_id = asyncio.run(run())
        row = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "failed"
        assert "audio_path_missing" in row["error"]

    def test_cancel_running_jobs(self, isolated_data_dir):
        project_id = _make_project(isolated_data_dir)
        import jobs, db
        jobs._queues.clear()
        jobs._runners.clear()
        jobs._project_locks.clear()

        conn = db.get_conn(project_id)
        now = _now()
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) VALUES (?,?,'vocal_separation','queued',?)",
            (str(uuid.uuid4()), project_id, now),
        )
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) VALUES (?,?,'diarisation','running',?)",
            (str(uuid.uuid4()), project_id, now),
        )
        conn.commit()

        jobs.cancel_running_jobs(project_id)

        statuses = {row[0] for row in conn.execute("SELECT status FROM jobs WHERE project_id=?",(project_id,)).fetchall()}
        assert statuses == {"cancelled"}

    @requires_ffmpeg
    def test_one_job_at_a_time_per_project(self, isolated_data_dir, test_wav):
        """Two jobs enqueued for the same project should run sequentially."""
        project_id = _make_project(isolated_data_dir)
        import jobs, db

        conn = db.get_conn(project_id)
        from db import project_dir
        src_dir = project_dir(project_id) / "source"
        src_dir.mkdir(parents=True, exist_ok=True)

        import shutil
        source_ids = []
        for i in range(2):
            sid = str(uuid.uuid4())
            source_ids.append(sid)
            shutil.copy(test_wav, src_dir / f"{sid}.wav")
            now = _now()
            conn.execute(
                "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,'uploaded',?,?)",
                (sid, project_id, f"ep{i}.wav", f"source/{sid}.wav", now, now),
            )
        conn.commit()

        async def run():
            jobs._queues.clear()
            jobs._runners.clear()
            jobs._project_locks.clear()
            for sid in source_ids:
                jobs.enqueue(project_id, "extract_audio", source_id=sid)
            await asyncio.sleep(12)

        asyncio.run(run())

        for sid in source_ids:
            row = conn.execute("SELECT status FROM jobs WHERE source_id=? AND type='extract_audio'", (sid,)).fetchone()
            assert row["status"] == "complete", f"Job for source {sid} did not complete"
