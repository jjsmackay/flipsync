"""Tests for reference clip upload endpoint."""

import pytest


class TestReferenceUpload:
    def test_upload_valid_reference(self, client, project, test_wav):
        with open(test_wav, "rb") as f:
            resp = client.post(
                f"/projects/{project}/reference",
                files={"file": ("ref.wav", f, "audio/wav")},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["reference_path"] == "reference.wav"
        assert body["duration_secs"] > 5.0

    def test_upload_short_reference_returns_422(self, client, project, test_wav_short):
        with open(test_wav_short, "rb") as f:
            resp = client.post(
                f"/projects/{project}/reference",
                files={"file": ("short.wav", f, "audio/wav")},
            )
        assert resp.status_code == 422
        body = resp.json()["detail"]
        assert body["error"] == "reference_too_short"
        assert "duration_secs" in body["detail"]
        assert body["detail"]["minimum_secs"] == 5.0

    def test_upload_to_nonexistent_project_returns_404(self, client, test_wav):
        with open(test_wav, "rb") as f:
            resp = client.post(
                "/projects/bad-id/reference",
                files={"file": ("ref.wav", f, "audio/wav")},
            )
        assert resp.status_code == 404

    def test_reference_written_to_disk(self, client, project, test_wav, isolated_data_dir):
        with open(test_wav, "rb") as f:
            client.post(
                f"/projects/{project}/reference",
                files={"file": ("ref.wav", f, "audio/wav")},
            )
        ref_file = isolated_data_dir / "projects" / project / "reference.wav"
        assert ref_file.exists()
        assert ref_file.stat().st_size > 0

    def test_reference_path_saved_to_project(self, client, project, test_wav):
        import db
        with open(test_wav, "rb") as f:
            client.post(
                f"/projects/{project}/reference",
                files={"file": ("ref.wav", f, "audio/wav")},
            )
        conn = db.get_conn(project)
        p = conn.execute("SELECT reference_path FROM projects WHERE id=?", (project,)).fetchone()
        assert p["reference_path"] == "reference.wav"

    def test_replace_existing_reference(self, client, project, test_wav):
        """Uploading a second time replaces the first reference."""
        for _ in range(2):
            with open(test_wav, "rb") as f:
                resp = client.post(
                    f"/projects/{project}/reference",
                    files={"file": ("ref.wav", f, "audio/wav")},
                )
            assert resp.status_code == 200
