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


class TestDiarisationHandler:
    def _make_diar_result(self, project_id, source_id, segments_data):
        """Build a mock diarisation poll result."""
        segments = []
        for d in segments_data:
            seg_id = str(uuid.uuid4())
            segments.append({
                "id": seg_id,
                "start_secs": d["start"],
                "end_secs": d["end"],
                "speaker_label": d.get("speaker", "SPEAKER_00"),
                "match_confidence": d["confidence"],
                "wav_path": f"/data/projects/{project_id}/segments/raw/{seg_id}.wav",
            })
        return {
            "job_id": "svc-diar-1",
            "status": "complete",
            "segments": segments,
            "coverage_ratio": 0.25,
            "error": None,
        }

    async def test_writes_segments_to_db(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir, match_threshold=0.75)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step2_pending")
        conn.execute("UPDATE sources SET vocals_path=? WHERE id=?",
                     (f"audio/vocals/{source_id}.wav", source_id))
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
        conn.commit()

        diar_result = self._make_diar_result(project_id, source_id, [
            {"start": 0.0, "end": 5.0, "confidence": 0.9},
            {"start": 6.0, "end": 10.0, "confidence": 0.6},
            {"start": 11.0, "end": 15.0, "confidence": 0.8},
        ])

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-diar-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=diar_result):
            job_id = jobs.enqueue(project_id, "diarisation", source_id=source_id)
            await jobs._handle_diarisation(project_id, job_id, source_id, {})

        segments = conn.execute("SELECT * FROM segments WHERE source_id=?", (source_id,)).fetchall()
        assert len(segments) == 3

        pending = [s for s in segments if s["status"] == "pending"]
        below = [s for s in segments if s["status"] == "below_threshold"]
        assert len(pending) == 2
        assert len(below) == 1

        src = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert src["status"] == "complete"
        assert abs(src["coverage_ratio"] - 0.25) < 0.001

    async def test_segments_use_relative_raw_path(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step2_pending")
        conn.execute("UPDATE sources SET vocals_path=? WHERE id=?",
                     (f"audio/vocals/{source_id}.wav", source_id))
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
        conn.commit()

        seg_id = str(uuid.uuid4())
        diar_result = {
            "job_id": "svc-1", "status": "complete",
            "segments": [{
                "id": seg_id, "start_secs": 0.0, "end_secs": 5.0,
                "speaker_label": "SPEAKER_00", "match_confidence": 0.9,
                "wav_path": f"/data/projects/{project_id}/segments/raw/{seg_id}.wav",
            }],
            "coverage_ratio": 0.2, "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=diar_result):
            job_id = jobs.enqueue(project_id, "diarisation", source_id=source_id)
            await jobs._handle_diarisation(project_id, job_id, source_id, {})

        seg = conn.execute("SELECT raw_path FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["raw_path"] == f"segments/raw/{seg_id}.wav"

    async def test_auto_triggers_transcription_when_all_sources_complete(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)

        existing_source = _insert_source(conn, project_id, "complete")
        active_source = _insert_source(conn, project_id, "step2_pending")
        conn.execute("UPDATE sources SET vocals_path=? WHERE id=?",
                     (f"audio/vocals/{active_source}.wav", active_source))
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
        conn.commit()

        seg_id = str(uuid.uuid4())
        diar_result = {
            "job_id": "svc-1", "status": "complete",
            "segments": [{
                "id": seg_id, "start_secs": 0.0, "end_secs": 5.0,
                "speaker_label": "SPEAKER_00", "match_confidence": 0.9,
                "wav_path": f"/data/projects/{project_id}/segments/raw/{seg_id}.wav",
            }],
            "coverage_ratio": 0.2, "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=diar_result):
            job_id = jobs.enqueue(project_id, "diarisation", source_id=active_source)
            await jobs._handle_diarisation(project_id, job_id, active_source, {})

        tx_job = conn.execute(
            "SELECT * FROM jobs WHERE project_id=? AND type='transcription_bulk'", (project_id,)
        ).fetchone()
        assert tx_job is not None

    async def test_does_not_trigger_transcription_while_other_sources_pending(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)

        active_source = _insert_source(conn, project_id, "step2_pending")
        still_running = _insert_source(conn, project_id, "step1_running")
        conn.execute("UPDATE sources SET vocals_path=? WHERE id=?",
                     (f"audio/vocals/{active_source}.wav", active_source))
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
        conn.commit()

        seg_id = str(uuid.uuid4())
        diar_result = {
            "job_id": "svc-1", "status": "complete",
            "segments": [{
                "id": seg_id, "start_secs": 0.0, "end_secs": 5.0,
                "speaker_label": "SPEAKER_00", "match_confidence": 0.9,
                "wav_path": f"/data/projects/{project_id}/segments/raw/{seg_id}.wav",
            }],
            "coverage_ratio": 0.2, "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=diar_result):
            job_id = jobs.enqueue(project_id, "diarisation", source_id=active_source)
            await jobs._handle_diarisation(project_id, job_id, active_source, {})

        tx_job = conn.execute(
            "SELECT * FROM jobs WHERE project_id=? AND type='transcription_bulk'", (project_id,)
        ).fetchone()
        assert tx_job is None

    async def test_failure_marks_source_step2_failed(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step2_pending")
        conn.execute("UPDATE sources SET vocals_path=? WHERE id=?",
                     (f"audio/vocals/{source_id}.wav", source_id))
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
        conn.commit()

        fail = {"job_id": "svc-1", "status": "failed", "error": "diarisation_failed", "segments": []}

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=fail):
            job_id = jobs.enqueue(project_id, "diarisation", source_id=source_id)
            await jobs._handle_diarisation(project_id, job_id, source_id, {})

        src = conn.execute("SELECT status, step2_error FROM sources WHERE id=?", (source_id,)).fetchone()
        assert src["status"] == "step2_failed"
        assert "diarisation_failed" in src["step2_error"]

    async def test_no_reference_clip_marks_step2_failed(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step2_pending")
        conn.execute("UPDATE sources SET vocals_path=? WHERE id=?",
                     (f"audio/vocals/{source_id}.wav", source_id))
        # reference_path is NULL by default
        conn.commit()

        job_id = jobs.enqueue(project_id, "diarisation", source_id=source_id)
        await jobs._handle_diarisation(project_id, job_id, source_id, {})

        src = conn.execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
        assert src["status"] == "step2_failed"
        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "reference" in job["error"]


class TestTranscriptionBulkHandler:
    async def test_writes_transcripts(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg1 = _insert_segment(conn, project_id, source_id, status="pending")
        seg2 = _insert_segment(conn, project_id, source_id, status="pending")

        final_result = {
            "job_id": "svc-1", "status": "complete", "progress": 100,
            "completed_segments": [
                {"id": seg1, "transcript": "Hello world", "transcript_confidence": 0.95},
                {"id": seg2, "transcript": "Goodbye", "transcript_confidence": 0.88},
            ],
            "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=final_result):
            job_id = jobs.enqueue(project_id, "transcription_bulk",
                                  params={"segment_ids": [seg1, seg2]})
            await jobs._handle_transcription_bulk(project_id, job_id, None,
                                                  {"segment_ids": [seg1, seg2]})

        s1 = conn.execute("SELECT transcript, transcript_confidence FROM segments WHERE id=?", (seg1,)).fetchone()
        s2 = conn.execute("SELECT transcript FROM segments WHERE id=?", (seg2,)).fetchone()
        assert s1["transcript"] == "Hello world"
        assert abs(s1["transcript_confidence"] - 0.95) < 0.001
        assert s2["transcript"] == "Goodbye"

    async def test_deduplicates_cumulative_results(self, isolated_data_dir):
        """completed_segments is cumulative — seg1 may appear in both progress and final result."""
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg1 = _insert_segment(conn, project_id, source_id, status="pending")
        seg2 = _insert_segment(conn, project_id, source_id, status="pending")

        progress_result = {
            "job_id": "svc-1", "status": "running", "progress": 50,
            "completed_segments": [
                {"id": seg1, "transcript": "Hello", "transcript_confidence": 0.9},
            ],
        }
        final_result = {
            "job_id": "svc-1", "status": "complete", "progress": 100,
            "completed_segments": [
                {"id": seg1, "transcript": "Hello", "transcript_confidence": 0.9},
                {"id": seg2, "transcript": "World", "transcript_confidence": 0.85},
            ],
            "error": None,
        }

        async def fake_poll(svc_url, job_id, poll_interval=2.0, on_progress=None):
            if on_progress:
                await on_progress(progress_result)
            return final_result

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", side_effect=fake_poll):
            job_id = jobs.enqueue(project_id, "transcription_bulk",
                                  params={"segment_ids": [seg1, seg2]})
            await jobs._handle_transcription_bulk(project_id, job_id, None,
                                                  {"segment_ids": [seg1, seg2]})

        rows = conn.execute(
            "SELECT id, transcript FROM segments WHERE source_id=? AND transcript IS NOT NULL",
            (source_id,)
        ).fetchall()
        assert len(rows) == 2
        transcripts = {r["id"]: r["transcript"] for r in rows}
        assert transcripts[seg1] == "Hello"
        assert transcripts[seg2] == "World"

    async def test_failure_marks_job_failed(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg1 = _insert_segment(conn, project_id, source_id, status="pending")

        fail = {"job_id": "svc-1", "status": "failed", "error": "model_load_failed",
                "completed_segments": [], "progress": 0}

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=fail):
            job_id = jobs.enqueue(project_id, "transcription_bulk",
                                  params={"segment_ids": [seg1]})
            await jobs._handle_transcription_bulk(project_id, job_id, None, {"segment_ids": [seg1]})

        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "model_load_failed" in job["error"]


class TestTranscriptionSegmentHandler:
    async def test_overwrites_transcript_and_confidence(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg_id = _insert_segment(conn, project_id, source_id, status="pending",
                                 transcript="Old transcript")

        final_result = {
            "job_id": "svc-1", "status": "complete", "progress": 100,
            "completed_segments": [
                {"id": seg_id, "transcript": "New transcript", "transcript_confidence": 0.92},
            ],
            "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=final_result):
            job_id = jobs.enqueue(project_id, "transcription_segment",
                                  params={"segment_ids": [seg_id]})
            await jobs._handle_transcription_segment(project_id, job_id, None,
                                                     {"segment_ids": [seg_id]})

        seg = conn.execute("SELECT transcript, transcript_confidence FROM segments WHERE id=?",
                           (seg_id,)).fetchone()
        assert seg["transcript"] == "New transcript"
        assert abs(seg["transcript_confidence"] - 0.92) < 0.001

    async def test_preserves_transcript_edited(self, isolated_data_dir):
        """Re-transcribing must not touch transcript_edited."""
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg_id = _insert_segment(conn, project_id, source_id, status="pending",
                                 transcript="Old", transcript_edited="User edit preserved")

        final_result = {
            "job_id": "svc-1", "status": "complete", "progress": 100,
            "completed_segments": [
                {"id": seg_id, "transcript": "Machine new", "transcript_confidence": 0.88},
            ],
            "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=final_result):
            job_id = jobs.enqueue(project_id, "transcription_segment",
                                  params={"segment_ids": [seg_id]})
            await jobs._handle_transcription_segment(project_id, job_id, None,
                                                     {"segment_ids": [seg_id]})

        seg = conn.execute("SELECT transcript, transcript_edited FROM segments WHERE id=?",
                           (seg_id,)).fetchone()
        assert seg["transcript"] == "Machine new"
        assert seg["transcript_edited"] == "User edit preserved"
