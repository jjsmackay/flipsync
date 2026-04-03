"""Tests for Source upload and management endpoints."""

import pytest


class TestSourceUpload:
    def test_upload_source_returns_202(self, client, project, test_wav):
        with open(test_wav, "rb") as f:
            resp = client.post(
                f"/projects/{project}/sources",
                files={"file": ("episode.wav", f, "audio/wav")},
            )
        assert resp.status_code == 202
        body = resp.json()
        assert "id" in body
        assert body["filename"] == "episode.wav"
        assert body["status"] == "extracting"

    def test_upload_sets_project_to_ready(self, client, project, test_wav):
        # Project starts as 'new'
        assert client.get(f"/projects/{project}").json()["status"] == "new"

        with open(test_wav, "rb") as f:
            client.post(
                f"/projects/{project}/sources",
                files={"file": ("ep.wav", f, "audio/wav")},
            )
        # After first upload the project should be 'ready' (or 'processing' if extraction started)
        status = client.get(f"/projects/{project}").json()["status"]
        assert status in ("ready", "processing")

    def test_upload_to_nonexistent_project_returns_404(self, client, test_wav):
        with open(test_wav, "rb") as f:
            resp = client.post(
                "/projects/bad-id/sources",
                files={"file": ("ep.wav", f, "audio/wav")},
            )
        assert resp.status_code == 404

    def test_upload_enqueues_extract_job(self, client, project, test_wav):
        with open(test_wav, "rb") as f:
            client.post(
                f"/projects/{project}/sources",
                files={"file": ("ep.wav", f, "audio/wav")},
            )
        import db
        conn = db.get_conn(project)
        jobs = conn.execute(
            "SELECT type, status FROM jobs WHERE project_id=? AND type='extract_audio'",
            (project,),
        ).fetchall()
        assert len(jobs) == 1

    def test_source_file_written_to_disk(self, client, project, test_wav, isolated_data_dir):
        with open(test_wav, "rb") as f:
            resp = client.post(
                f"/projects/{project}/sources",
                files={"file": ("ep.wav", f, "audio/wav")},
            )
        source_id = resp.json()["id"]
        source_file = isolated_data_dir / "projects" / project / "source" / f"{source_id}.wav"
        assert source_file.exists()
        assert source_file.stat().st_size > 0


class TestSourceDelete:
    def test_delete_source_without_confirm(self, client, project, test_wav):
        with open(test_wav, "rb") as f:
            resp = client.post(
                f"/projects/{project}/sources",
                files={"file": ("ep.wav", f, "audio/wav")},
            )
        source_id = resp.json()["id"]
        resp = client.request("DELETE", f"/projects/{project}/sources/{source_id}", json={"confirm": False})
        # No approved segments, so it should succeed even without confirm
        assert resp.status_code == 200
        assert resp.json()["deleted_segment_count"] == 0

    def test_delete_nonexistent_source_returns_404(self, client, project):
        resp = client.request("DELETE", f"/projects/{project}/sources/bad-id", json={"confirm": True})
        assert resp.status_code == 404

    def test_delete_source_with_approved_segments_requires_confirm(self, client, project):
        """If the source has approved segments, confirm=false returns 409."""
        import db, uuid
        from datetime import datetime, timezone

        conn = db.get_conn(project)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        source_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,'complete',?,?)",
            (source_id, project, "ep.wav", "source/ep.wav", now, now),
        )
        seg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs, speaker_label, match_confidence, status, created_at, updated_at) VALUES (?,?,?,'seg/1.wav',0,5,'S0',0.9,'approved',?,?)",
            (seg_id, project, source_id, now, now),
        )
        conn.commit()

        resp = client.request("DELETE", f"/projects/{project}/sources/{source_id}", json={"confirm": False})
        assert resp.status_code == 409
        assert resp.json()["error"] == "has_approved_segments"

    def test_delete_source_with_approved_segments_with_confirm(self, client, project):
        import db, uuid
        from datetime import datetime, timezone

        conn = db.get_conn(project)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        source_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,'complete',?,?)",
            (source_id, project, "ep.wav", "source/ep.wav", now, now),
        )
        seg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs, speaker_label, match_confidence, status, created_at, updated_at) VALUES (?,?,?,'seg/1.wav',0,5,'S0',0.9,'approved',?,?)",
            (seg_id, project, source_id, now, now),
        )
        conn.commit()

        resp = client.request("DELETE", f"/projects/{project}/sources/{source_id}", json={"confirm": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_segment_count"] == 1
        assert body["deleted_approved_count"] == 1
