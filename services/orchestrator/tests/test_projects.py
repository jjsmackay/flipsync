"""Tests for Project CRUD endpoints."""

import pytest


class TestListProjects:
    def test_empty_returns_empty_list(self, client):
        resp = client.get("/projects")
        assert resp.status_code == 200
        assert resp.json() == {"projects": []}

    def test_created_project_appears(self, client):
        client.post("/projects", json={"name": "My Project"})
        resp = client.get("/projects")
        assert resp.status_code == 200
        projects = resp.json()["projects"]
        assert len(projects) == 1
        assert projects[0]["name"] == "My Project"
        assert projects[0]["status"] == "new"

    def test_multiple_projects(self, client):
        client.post("/projects", json={"name": "A"})
        client.post("/projects", json={"name": "B"})
        resp = client.get("/projects")
        names = {p["name"] for p in resp.json()["projects"]}
        assert names == {"A", "B"}


class TestCreateProject:
    def test_minimal_create(self, client):
        resp = client.post("/projects", json={"name": "Test"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Test"
        assert body["status"] == "new"
        assert "id" in body

    def test_with_all_fields(self, client):
        resp = client.post("/projects", json={
            "name": "Full",
            "whisper_model": "small",
            "language": "en",
            "match_threshold": 0.80,
            "target_duration_secs": 3600,
        })
        assert resp.status_code == 201

    def test_missing_name_returns_422(self, client):
        resp = client.post("/projects", json={})
        assert resp.status_code == 422

    def test_invalid_threshold_returns_422(self, client):
        resp = client.post("/projects", json={"name": "Bad", "match_threshold": 1.5})
        assert resp.status_code == 422

    def test_negative_threshold_returns_422(self, client):
        resp = client.post("/projects", json={"name": "Bad", "match_threshold": -0.1})
        assert resp.status_code == 422


class TestGetProject:
    def test_get_existing(self, client, project):
        resp = client.get(f"/projects/{project}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == project
        assert body["status"] == "new"
        assert "config" in body
        assert "stats" in body
        assert "active_jobs" in body
        assert "recent_failed_jobs" in body

    def test_get_nonexistent_returns_404(self, client):
        resp = client.get("/projects/nonexistent-id")
        assert resp.status_code == 404

    def test_config_fields(self, client, project):
        resp = client.get(f"/projects/{project}")
        config = resp.json()["config"]
        assert "whisper_model" in config
        assert "match_threshold" in config
        assert "language" in config
        assert "target_duration_secs" in config

    def test_stats_shape(self, client, project):
        resp = client.get(f"/projects/{project}")
        stats = resp.json()["stats"]
        assert stats["total_segments"] == 0
        assert stats["approved_count"] == 0
        assert "source_coverage" in stats


class TestPatchProject:
    def test_patch_name(self, client, project):
        resp = client.patch(f"/projects/{project}", json={"name": "Renamed"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"

    def test_patch_threshold(self, client, project):
        resp = client.patch(f"/projects/{project}", json={"match_threshold": 0.60})
        assert resp.status_code == 200
        assert resp.json()["config"]["match_threshold"] == 0.60

    def test_patch_nonexistent_returns_404(self, client):
        resp = client.patch("/projects/bad-id", json={"name": "X"})
        assert resp.status_code == 404

    def test_threshold_triggers_segment_reclassification(self, client, project):
        """Lowering threshold moves below_threshold → pending.
        Raising threshold moves pending → below_threshold."""
        import db
        conn = db.get_conn(project)
        import uuid
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

        # Insert a source first (required by FK)
        source_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,'uploaded',?,?)",
            (source_id, project, "f.mp4", "source/f.mp4", now, now),
        )

        # seg1: confidence 0.60 → below_threshold (project threshold is 0.75)
        seg1 = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs, speaker_label, match_confidence, status, created_at, updated_at) VALUES (?,?,?,'seg/1.wav',0,5,'S0',0.60,'below_threshold',?,?)",
            (seg1, project, source_id, now, now),
        )
        # seg2: confidence 0.80 → pending
        seg2 = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs, speaker_label, match_confidence, status, created_at, updated_at) VALUES (?,?,?,'seg/2.wav',5,10,'S0',0.80,'pending',?,?)",
            (seg2, project, source_id, now, now),
        )
        conn.commit()

        # Lower threshold to 0.55: seg1 should move to pending
        resp = client.patch(f"/projects/{project}", json={"match_threshold": 0.55})
        assert resp.status_code == 200
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg1,)).fetchone()[0] == "pending"
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg2,)).fetchone()[0] == "pending"

        # Raise threshold to 0.90: seg1 and seg2 should move to below_threshold
        resp = client.patch(f"/projects/{project}", json={"match_threshold": 0.90})
        assert resp.status_code == 200
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg1,)).fetchone()[0] == "below_threshold"
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg2,)).fetchone()[0] == "below_threshold"

    def test_approved_segments_unaffected_by_threshold(self, client, project):
        """Approved/rejected/maybe segments must not be reclassified on threshold change."""
        import db, uuid
        from datetime import datetime, timezone

        conn = db.get_conn(project)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        source_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,'uploaded',?,?)",
            (source_id, project, "f.mp4", "source/f.mp4", now, now),
        )
        seg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs, speaker_label, match_confidence, status, created_at, updated_at) VALUES (?,?,?,'s/1.wav',0,5,'S0',0.50,'approved',?,?)",
            (seg_id, project, source_id, now, now),
        )
        conn.commit()

        # Raise threshold way above 0.50 — approved should stay approved
        resp = client.patch(f"/projects/{project}", json={"match_threshold": 0.95})
        assert resp.status_code == 200
        status = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()[0]
        assert status == "approved"


class TestDeleteProject:
    def test_delete_without_confirm_returns_422(self, client, project):
        resp = client.request("DELETE", f"/projects/{project}", json={"confirm": False})
        assert resp.status_code == 422

    def test_delete_with_confirm(self, client, project):
        resp = client.request("DELETE", f"/projects/{project}", json={"confirm": True})
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        # Project is gone
        assert client.get(f"/projects/{project}").status_code == 404

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.request("DELETE", "/projects/bad-id", json={"confirm": True})
        assert resp.status_code == 404

    def test_delete_with_active_jobs_returns_409(self, client, project):
        import db, uuid
        from datetime import datetime, timezone

        conn = db.get_conn(project)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) VALUES (?,?,'extract_audio','running',?)",
            (str(uuid.uuid4()), project, now),
        )
        conn.commit()

        resp = client.request("DELETE", f"/projects/{project}", json={"confirm": True})
        assert resp.status_code == 409
        assert resp.json()["error"] == "jobs_active"


class TestAutoApproveConfig:
    def test_defaults_on_create(self, client):
        resp = client.post("/projects", json={"name": "Defaults"})
        pid = resp.json()["id"]
        cfg = client.get(f"/projects/{pid}").json()["config"]
        assert cfg["auto_approve_enabled"] is True
        assert cfg["auto_approve_match_threshold"] == 0.85
        assert cfg["auto_approve_transcript_threshold"] == 0.90

    def test_create_with_auto_approve_fields(self, client):
        resp = client.post("/projects", json={
            "name": "Custom",
            "auto_approve_enabled": False,
            "auto_approve_match_threshold": 0.7,
            "auto_approve_transcript_threshold": 0.8,
        })
        assert resp.status_code == 201
        cfg = client.get(f"/projects/{resp.json()['id']}").json()["config"]
        assert cfg["auto_approve_enabled"] is False
        assert cfg["auto_approve_match_threshold"] == 0.7
        assert cfg["auto_approve_transcript_threshold"] == 0.8

    def test_patch_auto_approve_fields(self, client, project):
        resp = client.patch(f"/projects/{project}", json={
            "auto_approve_enabled": False,
            "auto_approve_match_threshold": 0.9,
            "auto_approve_transcript_threshold": 0.95,
        })
        assert resp.status_code == 200
        cfg = resp.json()["config"]
        assert cfg["auto_approve_enabled"] is False
        assert cfg["auto_approve_match_threshold"] == 0.9
        assert cfg["auto_approve_transcript_threshold"] == 0.95

    def test_invalid_auto_approve_threshold_returns_422(self, client, project):
        resp = client.patch(f"/projects/{project}", json={"auto_approve_match_threshold": 1.5})
        assert resp.status_code == 422

    def test_stats_include_auto_approved_count_and_duration(self, client, project):
        import uuid as _uuid
        from datetime import datetime, timezone
        import db
        conn = db.get_conn(project)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        src = str(_uuid.uuid4())
        conn.execute(
            "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (src, project, "ep.wav", "source/ep.wav", "complete", now, now),
        )
        for status, dur in (("approved", 4.0), ("auto_approved", 6.0), ("pending", 3.0)):
            sid = str(_uuid.uuid4())
            conn.execute(
                """INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs,
                   speaker_label, match_confidence, status, created_at, updated_at)
                   VALUES (?,?,?,?,0,?,'S0',0.9,?,?,?)""",
                (sid, project, src, f"segments/raw/{sid}.wav", dur, status, now, now),
            )
        conn.commit()

        stats = client.get(f"/projects/{project}").json()["stats"]
        assert stats["approved_count"] == 1
        assert stats["auto_approved_count"] == 1
        # approved_duration_secs covers approved + auto_approved (export contents)
        assert stats["approved_duration_secs"] == 10.0

        # List view mirrors the same fields
        listed = client.get("/projects").json()["projects"]
        entry = next(p for p in listed if p["id"] == project)
        assert entry["stats"]["auto_approved_count"] == 1
        assert entry["stats"]["approved_duration_secs"] == 10.0


class TestTuningConfig:
    """Per-project pipeline tuning knobs (migration 011): demucs, diarisation,
    whisper beam/vad, cleanup, and xtts finetune hyperparameters."""

    def test_tuning_defaults_on_create(self, client):
        pid = client.post("/projects", json={"name": "T"}).json()["id"]
        cfg = client.get(f"/projects/{pid}").json()["config"]
        assert cfg["demucs_model"] == "htdemucs"
        assert cfg["demucs_shifts"] == 0
        assert cfg["diar_min_speakers"] == 1
        assert cfg["diar_max_speakers"] == 10
        assert cfg["diar_min_segment_duration"] == 1.0
        assert cfg["whisper_beam_size"] == 5
        assert cfg["whisper_vad_filter"] is False
        assert cfg["target_lufs"] == -23.0
        assert cfg["highpass_hz"] == 80
        assert cfg["silence_threshold_db"] == -50.0
        assert cfg["silence_min_duration_secs"] == 0.1
        assert cfg["xtts_epochs"] == 10
        assert cfg["xtts_batch_size"] == 3
        assert cfg["xtts_grad_accum"] == 1
        assert cfg["xtts_learning_rate"] == 5e-6

    def test_create_with_tuning_overrides(self, client):
        resp = client.post("/projects", json={
            "name": "Custom",
            "demucs_model": "mdx_extra",
            "demucs_shifts": 2,
            "diar_min_speakers": 2,
            "diar_max_speakers": 4,
            "diar_min_segment_duration": 0.5,
            "whisper_beam_size": 1,
            "whisper_vad_filter": True,
            "target_lufs": -18.0,
            "highpass_hz": 100,
            "silence_threshold_db": -45.0,
            "silence_min_duration_secs": 0.2,
            "xtts_epochs": 20,
            "xtts_batch_size": 4,
            "xtts_grad_accum": 2,
            "xtts_learning_rate": 1e-5,
        })
        assert resp.status_code == 201
        cfg = client.get(f"/projects/{resp.json()['id']}").json()["config"]
        assert cfg["demucs_model"] == "mdx_extra"
        assert cfg["demucs_shifts"] == 2
        assert cfg["diar_max_speakers"] == 4
        assert cfg["whisper_vad_filter"] is True
        assert cfg["target_lufs"] == -18.0
        assert cfg["highpass_hz"] == 100
        assert cfg["xtts_epochs"] == 20
        assert cfg["xtts_learning_rate"] == 1e-5

    def test_patch_tuning_fields(self, client, project):
        resp = client.patch(f"/projects/{project}", json={
            "demucs_model": "mdx_extra",
            "whisper_beam_size": 3,
            "whisper_vad_filter": True,
            "target_lufs": -20.0,
            "xtts_epochs": 15,
        })
        assert resp.status_code == 200
        cfg = resp.json()["config"]
        assert cfg["demucs_model"] == "mdx_extra"
        assert cfg["whisper_beam_size"] == 3
        assert cfg["whisper_vad_filter"] is True
        assert cfg["target_lufs"] == -20.0
        assert cfg["xtts_epochs"] == 15

    def test_invalid_demucs_model_returns_422(self, client):
        resp = client.post("/projects", json={"name": "B", "demucs_model": "spleeter"})
        assert resp.status_code == 422

    def test_invalid_demucs_shifts_returns_422(self, client):
        resp = client.post("/projects", json={"name": "B", "demucs_shifts": -1})
        assert resp.status_code == 422

    def test_invalid_beam_size_returns_422(self, client):
        resp = client.post("/projects", json={"name": "B", "whisper_beam_size": 0})
        assert resp.status_code == 422

    def test_invalid_target_lufs_returns_422(self, client):
        # LUFS is a negative loudness value; a positive target is invalid.
        resp = client.post("/projects", json={"name": "B", "target_lufs": 5.0})
        assert resp.status_code == 422

    def test_invalid_xtts_learning_rate_returns_422(self, client):
        resp = client.post("/projects", json={"name": "B", "xtts_learning_rate": 0})
        assert resp.status_code == 422

    def test_patch_invalid_epochs_returns_422(self, client, project):
        resp = client.patch(f"/projects/{project}", json={"xtts_epochs": 0})
        assert resp.status_code == 422
