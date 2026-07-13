"""Wave 3 tests: full pipeline flow, OOM retry, reprocess, threshold re-evaluation,
export flow, and project status recomputation.

All external service HTTP calls are mocked via monkeypatch on service_client functions.
"""

import asyncio
import json
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _insert_source(conn, project_id, status="separation_pending", audio_path=None, vocals_path=None):
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO sources
           (id, project_id, filename, file_path, audio_path, vocals_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (source_id, project_id, "ep01.mkv", "source/ep01.mkv", audio_path, vocals_path, status, now, now),
    )
    conn.commit()
    return source_id


def _insert_segment(conn, project_id, source_id, status="pending", confidence=0.9,
                     transcript=None, transcript_edited=None, raw_path=None,
                     transcript_confidence=None, clipping=0, flags=None,
                     start=10.0, end=15.0):
    seg_id = str(uuid.uuid4())
    now = _now()
    rp = raw_path or f"segments/raw/{seg_id}.wav"
    conn.execute(
        """INSERT INTO segments
           (id, project_id, source_id, raw_path, start_secs, end_secs, speaker_label,
            match_confidence, status, transcript, transcript_edited,
            transcript_confidence, clipping_warning, flags, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, rp, start, end, "SPEAKER_00",
         confidence, status, transcript, transcript_edited,
         transcript_confidence, clipping, flags, now, now),
    )
    conn.commit()
    return seg_id


def _set_reference(conn, project_id, pdir):
    """Create a reference.wav placeholder and update the project."""
    ref = pdir / "reference.wav"
    ref.write_bytes(b"\x00" * 100)
    conn.execute(
        "UPDATE projects SET reference_path='reference.wav' WHERE id=?",
        (project_id,),
    )
    conn.commit()


def _create_segment_wav(pdir, seg_id):
    """Create a placeholder WAV for a segment."""
    wav = pdir / "segments" / "raw" / f"{seg_id}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"\x00" * 100)


def _run_job(project_id, job_id):
    """Execute a single job synchronously by creating a temporary event loop.

    A handler's success path can auto-enqueue a follow-up job (vocal
    separation -> diarisation, diarisation -> transcription), and
    jobs.enqueue() starts a background runner task via _ensure_runner()
    whenever it's called from a running loop — which this is. These tests
    drive jobs one at a time themselves (see _enqueue_and_run, which drains
    the queue immediately after enqueuing), so that runner is never wanted
    here; left alone, it would still be pending when this throwaway loop
    closes below, and later touching that task (e.g. jobs.shutdown_runners()
    on a *different*, still-open loop, or Python garbage-collecting it)
    raises "RuntimeError: Event loop is closed". Suppress runner creation for
    the duration of this call, matching the TestRecovery idiom in
    test_review_fixes.py.
    """
    import jobs
    loop = asyncio.new_event_loop()
    orig_ensure_runner = jobs._ensure_runner
    jobs._ensure_runner = lambda pid: None
    try:
        loop.run_until_complete(jobs._execute_job(project_id, job_id))
    finally:
        jobs._ensure_runner = orig_ensure_runner
        loop.close()


def _enqueue_and_run(project_id, job_type, source_id=None, params=None):
    """Enqueue a job and execute it immediately. Returns job_id."""
    import jobs
    job_id = jobs.enqueue(project_id, job_type, source_id=source_id, params=params)
    # Drain the queue item that enqueue added (so it doesn't linger)
    try:
        jobs._queues[project_id].get_nowait()
    except Exception:
        pass
    _run_job(project_id, job_id)
    return job_id


# ========================================================================
# Full pipeline flow
# ========================================================================

class TestFullPipelineFlow:
    """Test the complete pipeline: vocal_sep → diarisation → auto-transcription."""

    def test_pipeline_vocal_sep_to_diarisation(self, client, project, isolated_data_dir):
        """Pipeline start triggers vocal separation, which auto-enqueues diarisation."""
        import db
        import jobs

        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "separation_pending",
                                    audio_path=f"audio/raw/test.wav")
        _set_reference(conn, project_id=project, pdir=pdir)

        seg1_id = str(uuid.uuid4())
        seg2_id = str(uuid.uuid4())
        diar_segments = [
            {"id": seg1_id, "start_secs": 10.0, "end_secs": 15.0,
             "speaker_label": "SPEAKER_00", "match_confidence": 0.92,
             "wav_path": f"segments/raw/{seg1_id}.wav"},
            {"id": seg2_id, "start_secs": 20.0, "end_secs": 23.0,
             "speaker_label": "SPEAKER_01", "match_confidence": 0.60,
             "wav_path": f"segments/raw/{seg2_id}.wav"},
        ]

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            if service == "vocal_separation":
                return {"job_id": job_id, "status": "complete", "progress": 100,
                        "output_path": f"/data/projects/{project}/audio/vocals/{source_id}.wav"}
            elif service == "diarisation":
                return {"job_id": job_id, "status": "complete",
                        "segments": diar_segments, "coverage_ratio": 0.25}
            elif service == "transcription":
                return {"job_id": job_id, "status": "complete", "progress": 100,
                        "completed_segments": [
                            {"id": seg1_id, "transcript": "Hello world", "transcript_confidence": 0.95},
                        ]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):

            # Start vocal separation
            job_id = _enqueue_and_run(project, "vocal_separation", source_id=source_id)

            # Vocal sep should have auto-enqueued diarisation — run it
            diar_job_id = None
            try:
                diar_job_id = jobs._queues[project].get_nowait()
            except Exception:
                pass

            if diar_job_id:
                _run_job(project, diar_job_id)

            # Diarisation should have auto-enqueued transcription — run it
            trans_job_id = None
            try:
                trans_job_id = jobs._queues[project].get_nowait()
            except Exception:
                pass

            if trans_job_id:
                _run_job(project, trans_job_id)

        # Verify: source should be complete after diarisation
        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "complete"
        assert source["vocals_path"] == f"audio/vocals/{source_id}.wav"
        assert source["coverage_ratio"] == 0.25

        # Verify: segments were written
        segs = conn.execute(
            "SELECT * FROM segments WHERE project_id=? ORDER BY match_confidence DESC",
            (project,),
        ).fetchall()
        assert len(segs) >= 2

        # High confidence segment: transcription landed with 0.95 confidence
        # on a 0.92 match — clears both auto-approve bars → auto_approved.
        high_conf = [s for s in segs if s["match_confidence"] >= 0.75]
        assert all(s["status"] == "auto_approved" for s in high_conf)

        # Low confidence segment should be below_threshold
        low_conf = [s for s in segs if s["match_confidence"] < 0.75]
        assert all(s["status"] == "below_threshold" for s in low_conf)

    def test_pipeline_with_no_reference_fails_diarisation(self, client, project):
        """Diarisation fails if no reference clip is uploaded."""
        import db
        import jobs

        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending",
                                    vocals_path=f"audio/vocals/test.wav")

        job_id = _enqueue_and_run(project, "diarisation", source_id=source_id)

        # Job should have failed
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "no_reference_clip" in job["error"]

        # Source should be diarisation_failed
        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "diarisation_failed"


# ========================================================================
# OOM retry
# ========================================================================

class TestOOMRetry:
    def test_oom_retry_succeeds_on_second_attempt(self, client, project, isolated_data_dir):
        """Vocal separation OOMs, then succeeds with chunking."""
        import db
        import jobs

        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "separation_pending",
                                    audio_path="audio/raw/test.wav")
        _set_reference(conn, project_id=project, pdir=pdir)

        attempt = {"count": 0}

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            if service == "vocal_separation":
                attempt["count"] += 1
                if attempt["count"] == 1:
                    return {"job_id": job_id, "status": "failed",
                            "error": "cuda_oom", "retry_with_chunk_secs": 60}
                else:
                    return {"job_id": job_id, "status": "complete", "progress": 100,
                            "output_path": f"/data/projects/{project}/audio/vocals/{source_id}.wav"}
            elif service == "diarisation":
                return {"job_id": job_id, "status": "complete",
                        "segments": [], "coverage_ratio": 0.0}
            return {"job_id": job_id, "status": "complete"}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):

            job_id = _enqueue_and_run(project, "vocal_separation", source_id=source_id)

        # Source should have succeeded (diarisation_pending after vocal sep, or complete after auto-diarisation)
        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] in ("diarisation_pending", "complete")
        assert attempt["count"] == 2

    def test_oom_retry_fails_on_second_attempt(self, client, project, isolated_data_dir):
        """Vocal separation OOMs twice — source goes to separation_failed."""
        import db
        import jobs

        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "separation_pending")
        _set_reference(conn, project_id=project, pdir=pdir)

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "failed",
                    "error": "cuda_oom", "retry_with_chunk_secs": 60}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):

            job_id = _enqueue_and_run(project, "vocal_separation", source_id=source_id)

        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "separation_failed"
        assert source["separation_error"] is not None


# ========================================================================
# Reprocess
# ========================================================================

class TestReprocessFlow:
    def test_separation_reprocess_clears_vocals_path(self, client, project, monkeypatch):
        import db
        import jobs
        # The reprocess endpoint enqueues a job, which starts a background
        # runner on the TestClient's event loop; that runner can flip the
        # source to separation_running before this test reads it back. Keep the
        # job queued (don't start the runner) so the status assertion below
        # is deterministic — same idiom as TestRecovery in test_review_fixes.py.
        monkeypatch.setattr(jobs, "_ensure_runner", lambda pid: None)

        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete",
                                    vocals_path="audio/vocals/test.wav")
        _insert_segment(conn, project, source_id, status="pending")

        resp = client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["separation"]},
        )
        assert resp.status_code == 202

        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["vocals_path"] is None
        assert source["status"] == "separation_pending"

        count = conn.execute(
            "SELECT COUNT(*) FROM segments WHERE source_id=?", (source_id,)
        ).fetchone()[0]
        assert count == 0

    def test_diarisation_reprocess_deletes_segments_preserves_vocals(self, client, project, monkeypatch):
        import db
        import jobs
        # See test_separation_reprocess_clears_vocals_path: prevent the runner from
        # racing the status assertion below.
        monkeypatch.setattr(jobs, "_ensure_runner", lambda pid: None)

        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete",
                                    vocals_path="audio/vocals/test.wav")
        _insert_segment(conn, project, source_id, status="pending")

        resp = client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["diarisation"]},
        )
        assert resp.status_code == 202

        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["vocals_path"] == "audio/vocals/test.wav"
        assert source["status"] == "diarisation_pending"

        count = conn.execute(
            "SELECT COUNT(*) FROM segments WHERE source_id=?", (source_id,)
        ).fetchone()[0]
        assert count == 0

    def test_reprocess_with_approved_segments_no_confirm_409(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        _insert_segment(conn, project, source_id, status="approved")
        _insert_segment(conn, project, source_id, status="approved")

        resp = client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["diarisation"]},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["approved_count"] == 2

    def test_reprocess_with_approved_segments_confirm_proceeds(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        _insert_segment(conn, project, source_id, status="approved")

        resp = client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["diarisation"], "confirm": True},
        )
        assert resp.status_code == 202

        count = conn.execute(
            "SELECT COUNT(*) FROM segments WHERE source_id=?", (source_id,)
        ).fetchone()[0]
        assert count == 0


# ========================================================================
# Threshold re-evaluation
# ========================================================================

class TestThresholdReEvaluation:
    def test_lower_threshold_promotes_below_threshold_to_pending(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="below_threshold", confidence=0.70)

        resp = client.patch(f"/projects/{project}", json={"match_threshold": 0.65})
        assert resp.status_code == 200

        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "pending"

    def test_raise_threshold_demotes_pending_to_below_threshold(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="pending", confidence=0.80)

        resp = client.patch(f"/projects/{project}", json={"match_threshold": 0.85})
        assert resp.status_code == 200

        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "below_threshold"

    def test_threshold_change_does_not_affect_approved(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="approved", confidence=0.80)

        resp = client.patch(f"/projects/{project}", json={"match_threshold": 0.90})
        assert resp.status_code == 200

        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "approved"

    def test_threshold_change_does_not_affect_rejected(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="rejected", confidence=0.80)

        resp = client.patch(f"/projects/{project}", json={"match_threshold": 0.50})
        assert resp.status_code == 200

        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "rejected"

    def test_threshold_change_does_not_affect_maybe(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="maybe", confidence=0.80)

        resp = client.patch(f"/projects/{project}", json={"match_threshold": 0.90})
        assert resp.status_code == 200

        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "maybe"

    def test_bidirectional_threshold_change(self, client, project):
        """Lower threshold promotes, then raising demotes."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_low = _insert_segment(conn, project, source_id, status="below_threshold", confidence=0.70)
        seg_high = _insert_segment(conn, project, source_id, status="pending", confidence=0.80)

        # Lower threshold to 0.65
        client.patch(f"/projects/{project}", json={"match_threshold": 0.65})
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg_low,)).fetchone()["status"] == "pending"
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg_high,)).fetchone()["status"] == "pending"

        # Raise threshold to 0.85
        client.patch(f"/projects/{project}", json={"match_threshold": 0.85})
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg_low,)).fetchone()["status"] == "below_threshold"
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg_high,)).fetchone()["status"] == "below_threshold"


# ========================================================================
# Export flow
# ========================================================================

class TestExportFlow:
    def _run_export(self, project, conn, pdir, cleanup_results, mock_submit_side_effect=None):
        """Helper: enqueue and execute an export job with mocked cleanup service."""
        import jobs

        async def default_submit(service, payload):
            return {"job_id": payload["job_id"]}

        submit_fn = mock_submit_side_effect or default_submit

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "results": cleanup_results}

        # Set project to exporting status (normally done by the endpoint)
        conn.execute(
            "UPDATE projects SET status='exporting', updated_at=? WHERE id=?",
            (_now(), project),
        )
        conn.commit()

        with patch("service_client.submit_job", new=AsyncMock(side_effect=submit_fn)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "export", params={"segment_count": 1})

        return job_id

    def test_export_with_cleanup_success(self, client, project, isolated_data_dir):
        """Full export: cleanup succeeds, manifest written, archive created."""
        import db

        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="approved",
                                  transcript="Hello world", confidence=0.95)
        _create_segment_wav(pdir, seg_id)

        cleanup_results = [{
            "id": seg_id,
            "output_path": f"/data/projects/{project}/export/{seg_id}.wav",
            "clipping_warning": False,
            "auto_rejected": False,
            "error": None,
        }]

        async def mock_submit(service, payload):
            export_wav = pdir / "export" / f"{seg_id}.wav"
            export_wav.parent.mkdir(parents=True, exist_ok=True)
            export_wav.write_bytes(b"\x00" * 50)
            return {"job_id": payload["job_id"]}

        self._run_export(project, conn, pdir, cleanup_results, mock_submit)

        # Verify manifest exists
        manifest_path = pdir / "export" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["version"] == "1"
        assert manifest["project_id"] == project
        assert len(manifest["segments"]) == 1
        assert manifest["segments"][0]["text"] == "Hello world"
        assert manifest["segments"][0]["source_id"] == source_id

        # Verify archive exists
        archive = pdir / "export.tar.gz"
        assert archive.exists()
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
            assert "manifest.json" in names

        # Verify project status
        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "exported"

    def test_export_with_clipping_warning(self, client, project, isolated_data_dir):
        """Segments with clipping get clipping_warning status and column set."""
        import db

        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="approved",
                                  transcript="Hello", confidence=0.9)
        _create_segment_wav(pdir, seg_id)

        cleanup_results = [{
            "id": seg_id,
            "output_path": None,
            "clipping_warning": True,
            "auto_rejected": False,
            "error": None,
        }]

        self._run_export(project, conn, pdir, cleanup_results)

        seg = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "clipping_warning"
        assert seg["clipping_warning"] == 1

    def test_export_with_auto_rejected(self, client, project, isolated_data_dir):
        """Silent-after-trim segments get auto_rejected status."""
        import db

        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="approved",
                                  transcript="Hello", confidence=0.9)
        _create_segment_wav(pdir, seg_id)

        cleanup_results = [{
            "id": seg_id,
            "output_path": None,
            "clipping_warning": False,
            "auto_rejected": True,
            "error": None,
        }]

        self._run_export(project, conn, pdir, cleanup_results)

        seg = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "auto_rejected"

    def test_export_with_ffmpeg_error(self, client, project, isolated_data_dir):
        """FFmpeg error → auto_rejected with cleanup_error flag."""
        import db

        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="approved",
                                  transcript="Hello", confidence=0.9)
        _create_segment_wav(pdir, seg_id)

        cleanup_results = [{
            "id": seg_id,
            "output_path": None,
            "clipping_warning": False,
            "auto_rejected": False,
            "error": "ffmpeg_error: exit code 1",
        }]

        self._run_export(project, conn, pdir, cleanup_results)

        seg = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "auto_rejected"
        flags = json.loads(seg["flags"])
        assert any("cleanup_error" in f for f in flags)

    def test_export_manifest_uses_transcript_edited(self, client, project, isolated_data_dir):
        """Manifest text uses COALESCE(transcript_edited, transcript)."""
        import db

        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="approved",
                                  transcript="Original", transcript_edited="Edited",
                                  confidence=0.9)
        _create_segment_wav(pdir, seg_id)

        cleanup_results = [{
            "id": seg_id,
            "output_path": f"/data/projects/{project}/export/{seg_id}.wav",
            "clipping_warning": False, "auto_rejected": False, "error": None,
        }]

        async def mock_submit(service, payload):
            export_wav = pdir / "export" / f"{seg_id}.wav"
            export_wav.parent.mkdir(parents=True, exist_ok=True)
            export_wav.write_bytes(b"\x00" * 50)
            return {"job_id": payload["job_id"]}

        self._run_export(project, conn, pdir, cleanup_results, mock_submit)

        manifest = json.loads((pdir / "export" / "manifest.json").read_text())
        assert manifest["segments"][0]["text"] == "Edited"

    def test_export_no_approved_segments_returns_409(self, client, project):
        resp = client.post(f"/projects/{project}/export")
        assert resp.status_code == 409
        assert resp.json()["error"] == "no_approved_segments"

    def test_export_download_before_export_returns_404(self, client, project):
        resp = client.get(f"/projects/{project}/export/download")
        assert resp.status_code == 404


# ========================================================================
# Project status recomputation
# ========================================================================

class TestProjectStatusRecomputation:
    def test_new_project_has_new_status(self, client):
        resp = client.post("/projects", json={"name": "Test"})
        assert resp.json()["status"] == "new"

    def test_project_with_sources_is_ready(self, client, project):
        import db
        conn = db.get_conn(project)
        _insert_source(conn, project, "separation_pending")

        from status import recompute_project_status
        recompute_project_status(project)

        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "ready"

    def test_project_with_active_jobs_is_processing(self, client, project):
        import db
        conn = db.get_conn(project)
        _insert_source(conn, project, "separation_running")
        now = _now()
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), project, "vocal_separation", "running", now),
        )
        conn.commit()

        from status import recompute_project_status
        recompute_project_status(project)

        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "processing"

    def test_project_all_sources_complete_is_review(self, client, project):
        import db
        conn = db.get_conn(project)
        _insert_source(conn, project, "complete")

        from status import recompute_project_status
        recompute_project_status(project)

        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "review"

    def test_project_with_export_archive_is_exported(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        _insert_source(conn, project, "complete")

        (pdir / "export.tar.gz").write_bytes(b"\x00" * 100)
        # A recorded exported_at (set on export completion) is required — a bare
        # archive on disk no longer forces 'exported'.
        conn.execute("UPDATE projects SET exported_at=? WHERE id=?", (_now(), project))
        conn.commit()

        from status import recompute_project_status
        recompute_project_status(project)

        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "exported"

    def test_archive_without_exported_at_is_not_exported(self, client, project, isolated_data_dir):
        """A leftover archive with no exported_at does not re-derive 'exported'."""
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        _insert_source(conn, project, "complete")

        (pdir / "export.tar.gz").write_bytes(b"\x00" * 100)

        from status import recompute_project_status
        recompute_project_status(project)

        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "review"

    def test_exported_project_goes_to_processing_on_reprocess(self, client, project, isolated_data_dir):
        """An exported project should transition to processing when a job is active."""
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        _insert_source(conn, project, "separation_running")

        (pdir / "export.tar.gz").write_bytes(b"\x00" * 100)

        now = _now()
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), project, "vocal_separation", "running", now),
        )
        conn.commit()

        from status import recompute_project_status
        recompute_project_status(project)

        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "processing"


# ========================================================================
# Transcription handlers
# ========================================================================

class TestTranscriptionHandlers:
    def test_bulk_transcription_writes_results(self, client, project, isolated_data_dir):
        """Bulk transcription handler writes transcripts and deduplicates."""
        import db
        import jobs

        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        seg1 = _insert_segment(conn, project, source_id, status="pending", transcript=None)
        seg2 = _insert_segment(conn, project, source_id, status="pending", transcript=None)

        _create_segment_wav(pdir, seg1)
        _create_segment_wav(pdir, seg2)

        completed_segs = [
            {"id": seg1, "transcript": "Hello world", "transcript_confidence": 0.95},
            {"id": seg2, "transcript": "Goodbye", "transcript_confidence": 0.88},
        ]

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            result = {
                "job_id": job_id, "status": "complete", "progress": 100,
                "completed_segments": completed_segs,
            }
            if on_progress:
                on_progress({"status": "running", "progress": 50,
                            "completed_segments": [completed_segs[0]]})
            return result

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):

            params = {"segment_ids": [seg1, seg2], "model": "large-v2", "language": None}
            _enqueue_and_run(project, "transcription_bulk", params=params)

        s1 = conn.execute("SELECT transcript, transcript_confidence FROM segments WHERE id=?", (seg1,)).fetchone()
        assert s1["transcript"] == "Hello world"
        assert s1["transcript_confidence"] == 0.95

        s2 = conn.execute("SELECT transcript FROM segments WHERE id=?", (seg2,)).fetchone()
        assert s2["transcript"] == "Goodbye"

    def test_segment_retranscription_preserves_edited(self, client, project, isolated_data_dir):
        """Re-transcribing a segment overwrites transcript but preserves transcript_edited."""
        import db
        import jobs

        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="pending",
                                  transcript="Old text", transcript_edited="User edit")
        _create_segment_wav(pdir, seg_id)

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {
                "job_id": job_id, "status": "complete", "progress": 100,
                "completed_segments": [
                    {"id": seg_id, "transcript": "New text", "transcript_confidence": 0.99},
                ],
            }

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):

            params = {"segment_ids": [seg_id]}
            _enqueue_and_run(project, "transcription_segment", params=params)

        seg = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["transcript"] == "New text"
        assert seg["transcript_edited"] == "User edit"
        assert seg["transcript_confidence"] == 0.99


# ========================================================================
# Auto-approve re-evaluation on PATCH /projects
# ========================================================================

class TestAutoApproveReEvaluation:
    """PATCH /projects 3-step re-eval: demote, promote, threshold swap."""

    def _eligible_segment(self, conn, project, source_id, **overrides):
        kwargs = dict(status="pending", confidence=0.9,
                      transcript="Hello there", transcript_confidence=0.95)
        kwargs.update(overrides)
        return _insert_segment(conn, project, source_id, **kwargs)

    def test_patch_promotes_eligible_pending(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg = self._eligible_segment(conn, project, source_id)

        resp = client.patch(f"/projects/{project}", json={"auto_approve_transcript_threshold": 0.5})
        assert resp.status_code == 200

        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"] == "auto_approved"

    def test_patch_demotes_when_thresholds_raised(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg = self._eligible_segment(conn, project, source_id, status="auto_approved")

        resp = client.patch(f"/projects/{project}", json={"auto_approve_transcript_threshold": 0.99})
        assert resp.status_code == 200

        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"] == "pending"

    def test_disabling_auto_approve_demotes_all(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg = self._eligible_segment(conn, project, source_id, status="auto_approved")

        resp = client.patch(f"/projects/{project}", json={"auto_approve_enabled": False})
        assert resp.status_code == 200
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"] == "pending"

        # Re-enabling promotes it back
        resp = client.patch(f"/projects/{project}", json={"auto_approve_enabled": True})
        assert resp.status_code == 200
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"] == "auto_approved"

    def test_three_step_order_demoted_segment_falls_below_threshold(self, client, project):
        """Raising match_threshold above a segment demotes auto_approved →
        pending (step 1), then pending → below_threshold (step 3)."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg = self._eligible_segment(conn, project, source_id, status="auto_approved", confidence=0.9)

        resp = client.patch(f"/projects/{project}", json={"match_threshold": 0.95})
        assert resp.status_code == 200

        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"] == "below_threshold"

    def test_promotion_uses_max_of_match_and_auto_match_threshold(self, client, project):
        """match_confidence must clear max(match_threshold, auto_approve_match_threshold)."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        # 0.9 clears auto (0.85) but the display threshold is being raised to 0.92
        seg = self._eligible_segment(conn, project, source_id, confidence=0.9)

        resp = client.patch(f"/projects/{project}", json={
            "match_threshold": 0.92, "auto_approve_match_threshold": 0.85,
        })
        assert resp.status_code == 200
        # Not promoted; swept to below_threshold by step 3.
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"] == "below_threshold"

    def test_reeval_preserves_user_statuses(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        kept = {}
        for status in ("approved", "rejected", "maybe", "clipping_warning"):
            kept[status] = self._eligible_segment(conn, project, source_id, status=status)

        resp = client.patch(f"/projects/{project}", json={"auto_approve_match_threshold": 0.5})
        assert resp.status_code == 200

        for status, seg in kept.items():
            assert conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"] == status

    @pytest.mark.parametrize("overrides", [
        {"transcript": None},                                   # no transcript at all
        {"transcript": ""},                                     # empty effective transcript
        {"transcript": "Hi", "transcript_edited": ""},          # edited-to-empty wins COALESCE
        {"confidence": 0.80},                                   # below auto match threshold (0.85)
        {"transcript_confidence": 0.80},                        # below transcript threshold (0.90)
        {"transcript_confidence": None},                        # never transcribed confidence
        {"flags": '["short_transcript"]'},                      # flagged
        {"clipping": 1},                                        # clipping column set
        {"status": "maybe"},                                    # not pending
    ])
    def test_each_failing_condition_blocks_promotion(self, client, project, overrides):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg = self._eligible_segment(conn, project, source_id, **overrides)
        original_status = conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"]

        # Touch an auto field to trigger re-eval without moving the bars.
        resp = client.patch(f"/projects/{project}", json={"auto_approve_transcript_threshold": 0.90001})
        assert resp.status_code == 200

        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"] == original_status

    def test_fully_eligible_segment_is_promoted_by_reeval(self, client, project):
        """Sanity check for the parametrised blockers above: with no blocker
        the same PATCH does promote."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg = self._eligible_segment(conn, project, source_id)

        resp = client.patch(f"/projects/{project}", json={"auto_approve_transcript_threshold": 0.90001})
        assert resp.status_code == 200
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"] == "auto_approved"

    def test_patch_without_reeval_fields_does_not_touch_segments(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg = self._eligible_segment(conn, project, source_id)

        resp = client.patch(f"/projects/{project}", json={"name": "Renamed"})
        assert resp.status_code == 200
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"] == "pending"


# ========================================================================
# Sentence-aligned re-segmentation (children replacement)
# ========================================================================

class TestResegmentation:
    def _run_bulk(self, project, seg_ids, poll_results, submit_capture=None):
        """Run a transcription_bulk job whose poll returns poll_results in
        sequence (last one is the final result)."""
        calls = {"n": 0}

        async def mock_submit(service, payload):
            if submit_capture is not None:
                submit_capture.append(payload)
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            # Feed every non-final result through on_progress, return the last.
            for r in poll_results[:-1]:
                if on_progress:
                    on_progress({"job_id": job_id, "status": "running", **r})
            final = {"job_id": job_id, "status": "complete", "progress": 100, **poll_results[-1]}
            return final

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            params = {"segment_ids": seg_ids, "model": "large-v2", "language": None}
            return _enqueue_and_run(project, "transcription_bulk", params=params)

    def test_bulk_payload_carries_start_secs_and_resegment(self, client, project, isolated_data_dir):
        """Untranscribed pending segments get resegment=true; maybe and
        already-transcribed segments do not."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_pending = _insert_segment(conn, project, source_id, status="pending", start=42.5, end=50.0)
        seg_below = _insert_segment(conn, project, source_id, status="below_threshold", confidence=0.5)
        seg_maybe = _insert_segment(conn, project, source_id, status="maybe")
        seg_done = _insert_segment(conn, project, source_id, status="pending", transcript="Already done")

        captured = []
        self._run_bulk(project, [seg_pending, seg_below, seg_maybe, seg_done],
                       [{"completed_segments": []}], submit_capture=captured)

        assert len(captured) == 1
        by_id = {s["id"]: s for s in captured[0]["segments"]}
        assert by_id[seg_pending]["resegment"] is True
        assert by_id[seg_pending]["start_secs"] == 42.5
        assert by_id[seg_below]["resegment"] is True
        assert by_id[seg_maybe]["resegment"] is False
        assert by_id[seg_done]["resegment"] is False

    def test_segment_rerun_payload_never_sets_resegment(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="pending")

        captured = []

        async def mock_submit(service, payload):
            captured.append(payload)
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "progress": 100,
                    "completed_segments": [{"id": seg_id, "transcript": "Hi there friend",
                                            "transcript_confidence": 0.9}]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "transcription_segment", params={"segment_ids": [seg_id]})

        assert not captured[0]["segments"][0].get("resegment")

    def _children_payload(self, parent_id, project):
        c1, c2 = str(uuid.uuid4()), str(uuid.uuid4())
        return {
            "id": parent_id,
            "children": [
                {"id": c1,
                 "wav_path": f"/data/projects/{project}/segments/raw/{c1}.wav",
                 "start_secs": 10.0, "end_secs": 13.5,
                 "transcript": "I told you not to come back here.",
                 "transcript_confidence": 0.93},
                {"id": c2,
                 "wav_path": f"/data/projects/{project}/segments/raw/{c2}.wav",
                 "start_secs": 13.5, "end_secs": 15.0,
                 "transcript": "And yet.",
                 "transcript_confidence": 0.91},
            ],
        }

    def test_children_replace_parent(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        parent = _insert_segment(conn, project, source_id, status="pending", confidence=0.7)
        _create_segment_wav(pdir, parent)

        entry = self._children_payload(parent, project)
        self._run_bulk(project, [parent], [{"completed_segments": [entry]}])

        # Parent row gone
        assert conn.execute("SELECT COUNT(*) FROM segments WHERE id=?", (parent,)).fetchone()[0] == 0
        # Parent WAV deleted best-effort
        assert not (pdir / "segments" / "raw" / f"{parent}.wav").exists()

        rows = {r["id"]: r for r in conn.execute(
            "SELECT * FROM segments WHERE source_id=?", (source_id,)).fetchall()}
        assert len(rows) == 2
        c1 = entry["children"][0]
        r1 = rows[c1["id"]]
        # Child inherits attribution + status from parent
        assert r1["source_id"] == source_id
        assert r1["speaker_label"] == "SPEAKER_00"
        assert r1["match_confidence"] == 0.7
        assert r1["status"] == "pending"
        # Child takes payload timestamps/transcript; raw_path stored relative
        assert r1["start_secs"] == 10.0 and r1["end_secs"] == 13.5
        assert r1["duration_secs"] == pytest.approx(3.5)
        assert r1["transcript"] == c1["transcript"]
        assert r1["transcript_confidence"] == 0.93
        assert r1["raw_path"] == f"segments/raw/{c1['id']}.wav"
        assert not r1["flags"]

        # Second child is 1.5s -> short_transcript flag
        r2 = rows[entry["children"][1]["id"]]
        assert "short_transcript" in json.loads(r2["flags"])

    def test_children_dedup_across_cumulative_polls(self, client, project, isolated_data_dir):
        """The same children entry arriving in every cumulative poll (and the
        final result) is applied exactly once, keyed on the parent id."""
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        parent = _insert_segment(conn, project, source_id, status="pending")
        _create_segment_wav(pdir, parent)

        entry = self._children_payload(parent, project)
        # Two progress polls + final, all repeating the same cumulative entry
        self._run_bulk(project, [parent], [
            {"progress": 30, "completed_segments": [entry]},
            {"progress": 60, "completed_segments": [entry]},
            {"completed_segments": [entry]},
        ])

        count = conn.execute("SELECT COUNT(*) FROM segments WHERE source_id=?", (source_id,)).fetchone()[0]
        assert count == 2
        assert conn.execute("SELECT COUNT(*) FROM segments WHERE id=?", (parent,)).fetchone()[0] == 0

    def test_unsplit_dedup_across_cumulative_polls(self, client, project, isolated_data_dir):
        """Repeated unsplit entries only write once (updated_at unchanged after first write)."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg = _insert_segment(conn, project, source_id, status="pending")

        entry = {"id": seg, "transcript": "Same words every poll", "transcript_confidence": 0.5}
        self._run_bulk(project, [seg], [
            {"progress": 50, "completed_segments": [entry]},
            {"completed_segments": [entry]},
        ])

        row = conn.execute("SELECT transcript FROM segments WHERE id=?", (seg,)).fetchone()
        assert row["transcript"] == "Same words every poll"

    def test_missing_parent_wav_does_not_fail_job(self, client, project, isolated_data_dir):
        """Parent WAV already gone on disk — replacement still succeeds."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        parent = _insert_segment(conn, project, source_id, status="pending")
        # No _create_segment_wav — file does not exist

        entry = self._children_payload(parent, project)
        job_id = self._run_bulk(project, [parent], [{"completed_segments": [entry]}])

        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "complete"
        assert conn.execute("SELECT COUNT(*) FROM segments WHERE source_id=?", (source_id,)).fetchone()[0] == 2

    def test_children_of_below_threshold_parent_inherit_status(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        parent = _insert_segment(conn, project, source_id, status="below_threshold", confidence=0.5)
        _create_segment_wav(pdir, parent)

        entry = self._children_payload(parent, project)
        self._run_bulk(project, [parent], [{"completed_segments": [entry]}])

        statuses = [r["status"] for r in conn.execute(
            "SELECT status FROM segments WHERE source_id=?", (source_id,)).fetchall()]
        # Inherited below_threshold; never auto_approved (eligibility needs pending)
        assert statuses == ["below_threshold", "below_threshold"]


# ========================================================================
# Auto-approval when transcription results land
# ========================================================================

class TestAutoApproveOnTranscription:
    def test_bulk_unsplit_result_auto_approves_eligible(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_good = _insert_segment(conn, project, source_id, status="pending", confidence=0.9)
        seg_low_match = _insert_segment(conn, project, source_id, status="pending", confidence=0.8)
        seg_low_conf = _insert_segment(conn, project, source_id, status="pending", confidence=0.9)

        completed = [
            {"id": seg_good, "transcript": "A fine long sentence.", "transcript_confidence": 0.95},
            {"id": seg_low_match, "transcript": "A fine long sentence.", "transcript_confidence": 0.95},
            {"id": seg_low_conf, "transcript": "A fine long sentence.", "transcript_confidence": 0.85},
        ]

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "progress": 100,
                    "completed_segments": completed}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            params = {"segment_ids": [seg_good, seg_low_match, seg_low_conf]}
            _enqueue_and_run(project, "transcription_bulk", params=params)

        get = lambda s: conn.execute("SELECT status FROM segments WHERE id=?", (s,)).fetchone()["status"]
        assert get(seg_good) == "auto_approved"
        assert get(seg_low_match) == "pending"   # 0.8 < auto match threshold 0.85
        assert get(seg_low_conf) == "pending"    # 0.85 < transcript threshold 0.90

    def test_children_auto_approve_individually(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        parent = _insert_segment(conn, project, source_id, status="pending", confidence=0.9)
        _create_segment_wav(pdir, parent)

        c1, c2 = str(uuid.uuid4()), str(uuid.uuid4())
        entry = {"id": parent, "children": [
            # 4s child, high confidence -> auto_approved
            {"id": c1, "wav_path": f"/data/projects/{project}/segments/raw/{c1}.wav",
             "start_secs": 10.0, "end_secs": 14.0,
             "transcript": "Plenty long enough to keep.", "transcript_confidence": 0.95},
            # 1s child -> short_transcript flag blocks auto-approval
            {"id": c2, "wav_path": f"/data/projects/{project}/segments/raw/{c2}.wav",
             "start_secs": 14.0, "end_secs": 15.0,
             "transcript": "Short.", "transcript_confidence": 0.95},
        ]}

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "progress": 100,
                    "completed_segments": [entry]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "transcription_bulk", params={"segment_ids": [parent]})

        get = lambda s: conn.execute("SELECT status FROM segments WHERE id=?", (s,)).fetchone()["status"]
        assert get(c1) == "auto_approved"
        assert get(c2) == "pending"

    def test_single_segment_rerun_auto_approves(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="pending", confidence=0.9,
                                  transcript="Old low-confidence text", transcript_confidence=0.5)
        _create_segment_wav(pdir, seg_id)

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "progress": 100,
                    "completed_segments": [{"id": seg_id, "transcript": "Much better this time.",
                                            "transcript_confidence": 0.97}]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "transcription_segment", params={"segment_ids": [seg_id]})

        seg = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "auto_approved"
        assert seg["transcript"] == "Much better this time."

    def test_disabled_project_never_auto_approves(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        conn.execute("UPDATE projects SET auto_approve_enabled=0 WHERE id=?", (project,))
        conn.commit()
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="pending", confidence=0.95)

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "progress": 100,
                    "completed_segments": [{"id": seg_id, "transcript": "Great stuff indeed.",
                                            "transcript_confidence": 0.99}]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "transcription_bulk", params={"segment_ids": [seg_id]})

        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()["status"] == "pending"


# ========================================================================
# Export includes auto_approved
# ========================================================================

class TestExportIncludesAutoApproved:
    def test_export_trigger_accepts_only_auto_approved(self, client, project):
        """Pre-flight count includes auto_approved (not just approved)."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        _insert_segment(conn, project, source_id, status="auto_approved",
                        transcript="Hello", transcript_confidence=0.95)
        conn.execute("UPDATE projects SET status='review' WHERE id=?", (project,))
        conn.commit()

        resp = client.post(f"/projects/{project}/export")
        assert resp.status_code == 202
        assert resp.json()["enqueued_job"]["segment_count"] == 1

    def test_export_job_cleans_and_manifests_auto_approved(self, client, project, isolated_data_dir):
        """Cleanup submission and the manifest both include auto_approved segments."""
        import db

        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        seg_approved = _insert_segment(conn, project, source_id, status="approved",
                                       transcript="Manually approved", confidence=0.95)
        seg_auto = _insert_segment(conn, project, source_id, status="auto_approved",
                                   transcript="Automatically approved", confidence=0.9)
        _create_segment_wav(pdir, seg_approved)
        _create_segment_wav(pdir, seg_auto)

        cleanup_results = [
            {"id": s, "output_path": f"/data/projects/{project}/export/{s}.wav",
             "clipping_warning": False, "auto_rejected": False, "error": None}
            for s in (seg_approved, seg_auto)
        ]

        submitted = {}

        async def mock_submit(service, payload):
            submitted["segments"] = payload["segments"]
            for s in (seg_approved, seg_auto):
                wav = pdir / "export" / f"{s}.wav"
                wav.parent.mkdir(parents=True, exist_ok=True)
                wav.write_bytes(b"\x00" * 50)
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "results": cleanup_results}

        conn.execute("UPDATE projects SET status='exporting' WHERE id=?", (project,))
        conn.commit()

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "export", params={"segment_count": 2})

        # Both segments went to cleanup
        assert {s["id"] for s in submitted["segments"]} == {seg_approved, seg_auto}

        # Both appear in the manifest
        manifest = json.loads((pdir / "export" / "manifest.json").read_text())
        assert {s["id"] for s in manifest["segments"]} == {seg_approved, seg_auto}
        texts = {s["id"]: s["text"] for s in manifest["segments"]}
        assert texts[seg_auto] == "Automatically approved"
