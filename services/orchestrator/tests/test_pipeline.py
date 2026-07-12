"""Tests for pipeline control, transcription triggers, jobs list, and export endpoints."""

import uuid
from datetime import datetime, timezone

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _insert_source(conn, project_id, status="step1_pending"):
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (source_id, project_id, "ep.wav", "source/ep.wav", status, now, now),
    )
    conn.commit()
    return source_id


def _insert_segment(conn, project_id, source_id, status="pending", confidence=0.9, transcript=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs, speaker_label, match_confidence, status, transcript, created_at, updated_at) VALUES (?,?,?,?,0,5,'S0',?,?,?,?,?)",
        (seg_id, project_id, source_id, f"seg/{seg_id}.wav", confidence, status, transcript, now, now),
    )
    conn.commit()
    return seg_id


class TestPipelineStart:
    def test_start_with_no_pending_sources_returns_409(self, client, project):
        resp = client.post(f"/projects/{project}/pipeline/start")
        assert resp.status_code == 409
        assert resp.json()["error"] == "no_pending_sources"

    def test_start_enqueues_vocal_separation_jobs(self, client, project):
        import db
        conn = db.get_conn(project)
        _insert_source(conn, project, "step1_pending")
        _insert_source(conn, project, "step1_pending")
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project,))
        conn.commit()

        resp = client.post(f"/projects/{project}/pipeline/start")
        assert resp.status_code == 202
        jobs = resp.json()["enqueued_jobs"]
        assert len(jobs) == 2
        assert all(j["type"] == "vocal_separation" for j in jobs)

    def test_start_without_reference_enqueues_step1_only(self, client, project):
        """Start now always runs step 1; the reference gate happens after step 1
        drains (project → awaiting_reference), not at start."""
        import db
        conn = db.get_conn(project)
        _insert_source(conn, project, "step1_pending")
        _insert_source(conn, project, "step1_pending")

        resp = client.post(f"/projects/{project}/pipeline/start")
        assert resp.status_code == 202
        jobs = resp.json()["enqueued_jobs"]
        assert len(jobs) == 2
        assert all(j["type"] == "vocal_separation" for j in jobs)

    def test_start_nonexistent_project_returns_404(self, client):
        resp = client.post("/projects/bad-id/pipeline/start")
        assert resp.status_code == 404


class TestReprocess:
    def test_reprocess_step1_valid(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")

        resp = client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["step1"]},
        )
        assert resp.status_code == 202
        assert resp.json()["enqueued_jobs"][0]["type"] == "vocal_separation"

    def test_reprocess_step2_valid(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")

        resp = client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["step2"]},
        )
        assert resp.status_code == 202
        assert resp.json()["enqueued_jobs"][0]["type"] == "diarisation"

    def test_reprocess_invalid_steps_returns_422(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")

        resp = client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["step3"]},
        )
        assert resp.status_code == 422

    def test_reprocess_with_approved_segments_requires_confirm(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        _insert_segment(conn, project, source_id, status="approved")

        resp = client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["step1"]},
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "would_invalidate_approvals"

    def test_reprocess_with_auto_approved_segments_requires_confirm(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        _insert_segment(conn, project, source_id, status="auto_approved")

        resp = client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["step1"]},
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "would_invalidate_approvals"
        assert resp.json()["detail"]["approved_count"] == 1

    def test_reprocess_with_approved_segments_confirm_proceeds(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        _insert_segment(conn, project, source_id, status="approved")

        resp = client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["step1"], "confirm": True},
        )
        assert resp.status_code == 202

    def test_reprocess_deletes_existing_segments(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        _insert_segment(conn, project, source_id, status="pending")

        client.post(
            f"/projects/{project}/sources/{source_id}/reprocess",
            json={"steps": ["step1"], "confirm": True},
        )
        count = conn.execute("SELECT COUNT(*) FROM segments WHERE source_id=?", (source_id,)).fetchone()[0]
        assert count == 0

    def test_reprocess_nonexistent_source_returns_404(self, client, project):
        resp = client.post(
            f"/projects/{project}/sources/bad-id/reprocess",
            json={"steps": ["step1"]},
        )
        assert resp.status_code == 404


class TestTranscription:
    def test_run_transcription_no_segments_returns_409(self, client, project):
        resp = client.post(f"/projects/{project}/transcription/run")
        assert resp.status_code == 409
        assert resp.json()["error"] == "no_segments_to_transcribe"

    def test_run_transcription_enqueues_bulk_job(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        _insert_segment(conn, project, source_id, status="pending", transcript=None)
        _insert_segment(conn, project, source_id, status="pending", transcript=None)

        resp = client.post(f"/projects/{project}/transcription/run")
        assert resp.status_code == 202
        body = resp.json()["enqueued_job"]
        assert body["type"] == "transcription_bulk"
        assert body["segment_count"] == 2

    def test_run_transcription_skips_already_transcribed(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        # One with transcript, one without
        _insert_segment(conn, project, source_id, status="pending", transcript="Hello")
        _insert_segment(conn, project, source_id, status="pending", transcript=None)

        resp = client.post(f"/projects/{project}/transcription/run")
        assert resp.status_code == 202
        assert resp.json()["enqueued_job"]["segment_count"] == 1

    def test_rerun_segment_transcription(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="pending")

        resp = client.post(f"/projects/{project}/segments/{seg_id}/transcription/rerun")
        assert resp.status_code == 202
        assert resp.json()["enqueued_job"]["type"] == "transcription_segment"

    def test_rerun_segment_transcription_nonexistent_returns_404(self, client, project):
        resp = client.post(f"/projects/{project}/segments/bad-id/transcription/rerun")
        assert resp.status_code == 404


class TestJobsList:
    def test_list_jobs_empty(self, client, project):
        resp = client.get(f"/projects/{project}/jobs")
        assert resp.status_code == 200
        assert resp.json()["jobs"] == []

    def test_list_jobs_after_upload(self, client, project, test_wav):
        with open(test_wav, "rb") as f:
            client.post(
                f"/projects/{project}/sources",
                files={"file": ("ep.wav", f, "audio/wav")},
            )
        resp = client.get(f"/projects/{project}/jobs")
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        assert len(jobs) >= 1
        types = {j["type"] for j in jobs}
        assert "extract_audio" in types

    def test_list_jobs_filter_by_status(self, client, project):
        import db
        conn = db.get_conn(project)
        now = _now()
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) VALUES (?,?,'extract_audio','complete',?)",
            (str(uuid.uuid4()), project, now),
        )
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) VALUES (?,?,'vocal_separation','failed',?)",
            (str(uuid.uuid4()), project, now),
        )
        conn.commit()

        resp = client.get(f"/projects/{project}/jobs?status=complete")
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        assert all(j["status"] == "complete" for j in jobs)


class TestExport:
    def test_export_with_no_approved_segments_returns_409(self, client, project):
        resp = client.post(f"/projects/{project}/export")
        assert resp.status_code == 409
        assert resp.json()["error"] == "no_approved_segments"

    def test_export_enqueues_export_job(self, client, project):
        import db
        from status import recompute_project_status
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        _insert_segment(conn, project, source_id, status="approved")
        recompute_project_status(project)  # complete source → 'review'

        resp = client.post(f"/projects/{project}/export")
        assert resp.status_code == 202
        body = resp.json()["enqueued_job"]
        assert body["type"] == "export"
        assert body["segment_count"] == 1

    def test_export_invalid_project_state_returns_409(self, client, project):
        """Exporting a project that hasn't reached review/exported is rejected."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "step1_running")
        _insert_segment(conn, project, source_id, status="approved")
        # Project left in 'new'/processing-ish state (never recomputed to review).

        resp = client.post(f"/projects/{project}/export")
        assert resp.status_code == 409
        assert resp.json()["error"] == "invalid_project_state"

    def test_download_before_export_returns_404(self, client, project):
        resp = client.get(f"/projects/{project}/export/download")
        assert resp.status_code == 404

    def test_export_nonexistent_project_returns_404(self, client):
        resp = client.post("/projects/bad-id/export")
        assert resp.status_code == 404
