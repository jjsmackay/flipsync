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


def _insert_source(conn, project_id, status="step1_pending", audio_path=None, vocals_path=None):
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
                     transcript=None, transcript_edited=None, raw_path=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    rp = raw_path or f"segments/raw/{seg_id}.wav"
    conn.execute(
        """INSERT INTO segments
           (id, project_id, source_id, raw_path, start_secs, end_secs, speaker_label,
            match_confidence, status, transcript, transcript_edited, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, rp, 10.0, 15.0, "SPEAKER_00",
         confidence, status, transcript, transcript_edited, now, now),
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
    """Execute a single job synchronously by creating a temporary event loop."""
    import jobs
    loop = asyncio.new_event_loop()
    loop.run_until_complete(jobs._execute_job(project_id, job_id))
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
        source_id = _insert_source(conn, project, "step1_pending",
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

        # High confidence segment should be pending
        high_conf = [s for s in segs if s["match_confidence"] >= 0.75]
        assert all(s["status"] == "pending" for s in high_conf)

        # Low confidence segment should be below_threshold
        low_conf = [s for s in segs if s["match_confidence"] < 0.75]
        assert all(s["status"] == "below_threshold" for s in low_conf)

    def test_pipeline_with_no_reference_fails_diarisation(self, client, project):
        """Diarisation fails if no reference clip is uploaded."""
        import db
        import jobs

        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "step2_pending",
                                    vocals_path=f"audio/vocals/test.wav")

        job_id = _enqueue_and_run(project, "diarisation", source_id=source_id)

        # Job should have failed
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "no_reference_clip" in job["error"]

        # Source should be step2_failed
        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "step2_failed"


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
        source_id = _insert_source(conn, project, "step1_pending",
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

        # Source should have succeeded (step2_pending after vocal sep, or complete after auto-diarisation)
        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] in ("step2_pending", "complete")
        assert attempt["count"] == 2

    def test_oom_retry_fails_on_second_attempt(self, client, project, isolated_data_dir):
        """Vocal separation OOMs twice — source goes to step1_failed."""
        import db
        import jobs

        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "step1_pending")
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
        assert source["status"] == "step1_failed"
        assert source["step1_error"] is not None


# ========================================================================
# Reprocess
# ========================================================================

class TestReprocessFlow:
    def test_step1_reprocess_clears_vocals_path(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete",
                                    vocals_path="audio/vocals/test.wav")
        _insert_segment(conn, project, source_id, status="pending")

        resp = client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["step1"]},
        )
        assert resp.status_code == 202

        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["vocals_path"] is None
        assert source["status"] == "step1_pending"

        count = conn.execute(
            "SELECT COUNT(*) FROM segments WHERE source_id=?", (source_id,)
        ).fetchone()[0]
        assert count == 0

    def test_step2_reprocess_deletes_segments_preserves_vocals(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete",
                                    vocals_path="audio/vocals/test.wav")
        _insert_segment(conn, project, source_id, status="pending")

        resp = client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["step2"]},
        )
        assert resp.status_code == 202

        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["vocals_path"] == "audio/vocals/test.wav"
        assert source["status"] == "step2_pending"

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
            json={"steps": ["step2"]},
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
            json={"steps": ["step2"], "confirm": True},
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
        _insert_source(conn, project, "step1_pending")

        from status import recompute_project_status
        recompute_project_status(project)

        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "ready"

    def test_project_with_active_jobs_is_processing(self, client, project):
        import db
        conn = db.get_conn(project)
        _insert_source(conn, project, "step1_running")
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

        from status import recompute_project_status
        recompute_project_status(project)

        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "exported"

    def test_exported_project_goes_to_processing_on_reprocess(self, client, project, isolated_data_dir):
        """An exported project should transition to processing when a job is active."""
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        _insert_source(conn, project, "step1_running")

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
