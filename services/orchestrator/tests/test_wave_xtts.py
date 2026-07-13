"""v1.5 XTTS integration tests: migration 003, service-client readiness,
dataset_build / finetune / preview handlers, and the models + previews routers.

External service HTTP calls are mocked via patch on service_client functions,
following the Wave 3 pattern (tests/test_wave3_pipeline.py).
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _insert_source(conn, project_id, status="complete"):
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (source_id, project_id, "ep01.mkv", "source/ep01.mkv", status, now, now),
    )
    conn.commit()
    return source_id


def _insert_seg(conn, project_id, source_id, status="approved", confidence=0.9,
                start=0.0, end=10.0, transcript="hello world", export_path=None,
                cleaned_path=None, flags=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments
           (id, project_id, source_id, raw_path, export_path, cleaned_path, start_secs,
            end_secs, speaker_label, match_confidence, status, transcript, flags,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, f"segments/raw/{seg_id}.wav", export_path,
         cleaned_path, start, end, "SPEAKER_00", confidence, status, transcript,
         json.dumps(flags) if flags else None, now, now),
    )
    conn.commit()
    return seg_id


def _insert_many(conn, project_id, source_id, count, dur=10.0, status="approved",
                 confidence=0.9, cleaned=False):
    ids = []
    for i in range(count):
        # "Cleaned" for dataset purposes means the cleaned/ cache (cleaned_path),
        # not export_path — dataset builds are decoupled from export/.
        cp = f"cleaned/x{i}.wav" if cleaned else None
        ids.append(_insert_seg(conn, project_id, source_id, status=status,
                               confidence=confidence, start=i * 20.0, end=i * 20.0 + dur,
                               cleaned_path=cp))
    return ids


def _insert_model(conn, project_id, status="pending", mode="approved",
                  min_confidence=None, manifest="models/m/dataset.json",
                  checkpoint_dir=None):
    mid = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO models
           (id, project_id, status, dataset_mode, min_confidence, dataset_manifest_path,
            checkpoint_dir, params, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (mid, project_id, status, mode, min_confidence, manifest, checkpoint_dir,
         json.dumps({"epochs": 10}), now, now),
    )
    conn.commit()
    return mid


def _set_reference(conn, project_id, pdir):
    ref = pdir / "reference.wav"
    ref.write_bytes(b"\x00" * 100)
    conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
    conn.commit()


def _run_job(project_id, job_id):
    import jobs
    loop = asyncio.new_event_loop()
    loop.run_until_complete(jobs._execute_job(project_id, job_id))
    loop.close()


def _enqueue_and_run(project_id, job_type, source_id=None, params=None):
    import jobs
    job_id = jobs.enqueue(project_id, job_type, source_id=source_id, params=params)
    try:
        jobs._queues[project_id].get_nowait()
    except Exception:
        pass
    _run_job(project_id, job_id)
    return job_id


# ========================================================================
# Task 5 — migration 003
# ========================================================================

class TestMigration003:
    def test_progress_detail_column_and_models_table(self, client, project):
        import db
        conn = db.get_conn(project)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        assert "progress_detail" in cols
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "models" in tables
        model_cols = {r["name"] for r in conn.execute("PRAGMA table_info(models)").fetchall()}
        assert {"status", "dataset_mode", "min_confidence", "dataset_manifest_path",
                "checkpoint_dir", "eval_loss"} <= model_cols


# ========================================================================
# Task 6 — service client readiness
# ========================================================================

class TestIsHealthy:
    def test_true_on_200(self):
        import service_client

        class Resp:
            status_code = 200

        fake = AsyncMock()
        fake.get = AsyncMock(return_value=Resp())
        with patch("service_client._get_client", return_value=fake):
            ok = asyncio.new_event_loop().run_until_complete(service_client.is_healthy("xtts"))
        assert ok is True

    def test_false_on_connect_error(self):
        import service_client
        import httpx

        fake = AsyncMock()
        fake.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch("service_client._get_client", return_value=fake):
            ok = asyncio.new_event_loop().run_until_complete(service_client.is_healthy("xtts"))
        assert ok is False


# ========================================================================
# Capabilities endpoint — drives the frontend's Train-vs-Export terminal stage
# ========================================================================

class TestCapabilities:
    def test_xtts_true_when_healthy(self, client):
        with patch("main.is_healthy", AsyncMock(return_value=True)):
            resp = client.get("/capabilities")
        assert resp.status_code == 200
        assert resp.json()["xtts"] is True

    def test_xtts_false_when_unhealthy(self, client):
        with patch("main.is_healthy", AsyncMock(return_value=False)):
            resp = client.get("/capabilities")
        assert resp.status_code == 200
        assert resp.json()["xtts"] is False

    def test_serves_bulk_action_sources_table(self, client):
        from state_machines import BULK_ACTION_SOURCES

        with patch("main.is_healthy", AsyncMock(return_value=False)):
            resp = client.get("/capabilities")
        served = resp.json()["bulk_action_sources"]
        assert served == {
            action: sorted(statuses) for action, statuses in BULK_ACTION_SOURCES.items()
        }


# ========================================================================
# Task 7 — dataset selection + dataset_build handler
# ========================================================================

class TestDatasetSelection:
    def test_approved_mode_excludes_pending_and_rejected(self, client, project):
        import db, jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        a = _insert_seg(conn, project, src, status="approved")
        _insert_seg(conn, project, src, status="pending")
        _insert_seg(conn, project, src, status="rejected")

        kept, dropped = jobs._select_dataset_segments(conn, "approved", None)
        assert [r["id"] for r in kept] == [a]
        assert dropped == {"too_short": 0, "too_long": 0, "flagged": 0}

    def test_approved_mode_includes_auto_approved(self, client, project):
        """auto_approved is treated as approved everywhere else (export set,
        approved_duration_secs) — the dataset gate must match."""
        import db, jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        a = _insert_seg(conn, project, src, status="approved")
        aa = _insert_seg(conn, project, src, status="auto_approved", start=20, end=30)

        kept, _ = jobs._select_dataset_segments(conn, "approved", None)
        assert {r["id"] for r in kept} == {a, aa}

    def test_approved_mode_excludes_clipping_warning(self, client, project):
        """Unlike export, clipping_warning-status segments stay out of the
        training set (training quality)."""
        import db, jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        a = _insert_seg(conn, project, src, status="approved")
        _insert_seg(conn, project, src, status="clipping_warning", start=20, end=30)

        kept, _ = jobs._select_dataset_segments(conn, "approved", None)
        assert [r["id"] for r in kept] == [a]

    def test_auto_mode_confidence_floor_and_status(self, client, project):
        import db, jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        hi = _insert_seg(conn, project, src, status="pending", confidence=0.95)
        _insert_seg(conn, project, src, status="pending", confidence=0.50)   # below floor
        _insert_seg(conn, project, src, status="rejected", confidence=0.99)  # rejected
        _insert_seg(conn, project, src, status="auto_rejected", confidence=0.99)

        kept, _ = jobs._select_dataset_segments(conn, "auto", 0.85)
        assert [r["id"] for r in kept] == [hi]

    def test_training_filters_and_dropped_counts(self, client, project):
        import db, jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        good = _insert_seg(conn, project, src, status="approved", start=0, end=5)
        _insert_seg(conn, project, src, status="approved", start=0, end=0.5)   # too short
        _insert_seg(conn, project, src, status="approved", start=0, end=15)    # too long
        _insert_seg(conn, project, src, status="approved", start=0, end=5,
                    flags=["cleanup_error: boom"])                              # flagged

        kept, dropped = jobs._select_dataset_segments(conn, "approved", None)
        assert [r["id"] for r in kept] == [good]
        assert dropped == {"too_short": 1, "too_long": 1, "flagged": 1}


class TestDatasetBuildHandler:
    def test_all_cleaned_makes_no_cleanup_call(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        src = _insert_source(conn, project)
        _insert_many(conn, project, src, 31, dur=10.0, cleaned=True)  # 310s, already cleaned
        model_id = _insert_model(conn, project)

        submit = AsyncMock(side_effect=lambda s, p: {"job_id": p["job_id"]})
        with patch("service_client.submit_job", new=submit), \
             patch("service_client.poll_until_complete", new=AsyncMock()):
            job_id = _enqueue_and_run(project, "dataset_build",
                                      params={"model_id": model_id, "mode": "approved",
                                              "min_confidence": None})

        submit.assert_not_called()
        manifest = json.loads((pdir / "models" / model_id / "dataset.json").read_text())
        assert len(manifest["segments"]) == 31
        m = conn.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["segment_count"] == 31
        assert m["dataset_manifest_path"] == f"models/{model_id}/dataset.json"
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "complete"

    def test_cleans_only_missing_and_writes_manifest(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        src = _insert_source(conn, project)
        _insert_many(conn, project, src, 20, dur=10.0, cleaned=True)      # 200s cleaned
        missing = _insert_many(conn, project, src, 15, dur=10.0, cleaned=False)  # 150s uncleaned

        model_id = _insert_model(conn, project)

        captured = {}

        async def mock_submit(service, payload):
            captured["payload"] = payload
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete",
                    "results": [{"id": sid, "output_path": f"/x/{sid}.wav",
                                 "clipping_warning": False, "auto_rejected": False,
                                 "error": None} for sid in missing]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "dataset_build",
                             params={"model_id": model_id, "mode": "approved",
                                     "min_confidence": None})

        # Only the 15 uncleaned segments were sent to cleanup, into cleaned/
        # (never export/ — dataset builds must not pollute the export set).
        assert len(captured["payload"]["segments"]) == 15
        assert {s["id"] for s in captured["payload"]["segments"]} == set(missing)
        assert all("/cleaned/" in s["output_path"] for s in captured["payload"]["segments"])
        assert all("/export/" not in s["output_path"] for s in captured["payload"]["segments"])

        manifest = json.loads((pdir / "models" / model_id / "dataset.json").read_text())
        assert manifest["speaker"] == "target"
        assert manifest["selection"]["mode"] == "approved"
        assert manifest["selection"]["dropped"] == {"too_short": 0, "too_long": 0, "flagged": 0}
        assert len(manifest["segments"]) == 35
        # audio_file paths are absolute (DATA_DIR-rooted).
        assert all(seg["audio_file"].startswith(str(isolated_data_dir)) for seg in manifest["segments"])
        assert all(seg["source_id"] == src for seg in manifest["segments"])
        m = conn.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["segment_count"] == 35
        assert m["dataset_duration_secs"] == pytest.approx(350.0)

    def test_dropped_counts_recorded_in_manifest(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        src = _insert_source(conn, project)
        _insert_many(conn, project, src, 31, dur=10.0, cleaned=True)  # 310s valid, cleaned
        _insert_seg(conn, project, src, status="approved", start=0, end=0.5, cleaned_path="cleaned/s.wav")
        _insert_seg(conn, project, src, status="approved", start=0, end=15, cleaned_path="cleaned/l.wav")
        _insert_seg(conn, project, src, status="approved", start=0, end=5,
                    cleaned_path="cleaned/f.wav", flags=["cleanup_error: x"])
        model_id = _insert_model(conn, project)

        with patch("service_client.submit_job", new=AsyncMock()), \
             patch("service_client.poll_until_complete", new=AsyncMock()):
            _enqueue_and_run(project, "dataset_build",
                             params={"model_id": model_id, "mode": "approved",
                                     "min_confidence": None})

        manifest = json.loads((pdir / "models" / model_id / "dataset.json").read_text())
        assert manifest["selection"]["dropped"] == {"too_short": 1, "too_long": 1, "flagged": 1}
        assert len(manifest["segments"]) == 31

    def test_under_300s_fails_job_and_model(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_many(conn, project, src, 5, dur=10.0, cleaned=True)  # 50s only
        model_id = _insert_model(conn, project)

        with patch("service_client.submit_job", new=AsyncMock()), \
             patch("service_client.poll_until_complete", new=AsyncMock()):
            job_id = _enqueue_and_run(project, "dataset_build",
                                      params={"model_id": model_id, "mode": "approved",
                                              "min_confidence": None})

        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "insufficient" in job["error"]
        m = conn.execute("SELECT status, error FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "failed"
        assert "insufficient" in m["error"]


# ========================================================================
# Task 8 — finetune handler
# ========================================================================

class TestFinetuneHandler:
    def test_happy_path(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="pending")

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            if on_progress:
                on_progress({"status": "running", "progress": {
                    "phase": "training", "epoch": 3, "total_epochs": 10,
                    "step": 100, "total_steps": 200, "train_loss": 2.8,
                    "eval_loss": 3.0, "eta_secs": 100}})
            return {"job_id": job_id, "status": "complete",
                    "result": {"final_eval_loss": 2.71, "checkpoint_dir": "/abs/models/x"}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "finetune",
                                      params={"model_id": model_id,
                                              "params": {"epochs": 10, "batch_size": 3}})

        m = conn.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "ready"
        assert m["checkpoint_dir"] == f"models/{model_id}"
        assert m["eval_loss"] == 2.71
        job = conn.execute("SELECT status, progress_detail FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "complete"
        detail = json.loads(job["progress_detail"])
        assert detail["epoch"] == 3 and detail["train_loss"] == 2.8

    def test_oom_retry_then_success(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="pending")

        submitted = []

        async def mock_submit(service, payload):
            submitted.append(payload)
            return {"job_id": payload["job_id"]}

        attempt = {"n": 0}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            attempt["n"] += 1
            if attempt["n"] == 1:
                return {"status": "failed", "error": "cuda_oom",
                        "retry_with": {"batch_size": 1, "grad_accum": 3}}
            return {"status": "complete", "result": {"final_eval_loss": 2.5}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "finetune",
                             params={"model_id": model_id, "params": {"batch_size": 3, "grad_accum": 1}})

        assert len(submitted) == 2
        assert submitted[1]["params"]["batch_size"] == 1
        assert submitted[1]["params"]["grad_accum"] == 3
        assert submitted[0]["job_id"] != submitted[1]["job_id"]
        m = conn.execute("SELECT status FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "ready"

    def test_second_oom_is_terminal(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="pending")

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "failed", "error": "cuda_oom",
                    "retry_with": {"batch_size": 1, "grad_accum": 3}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "finetune",
                                      params={"model_id": model_id, "params": {"batch_size": 3}})

        m = conn.execute("SELECT status FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "failed"
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"

    def test_insufficient_vram_no_resubmit(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="pending")

        submitted = []

        async def mock_submit(service, payload):
            submitted.append(payload)
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "failed", "error": "insufficient_vram: 12 GB required, 8.0 GB available"}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "finetune",
                             params={"model_id": model_id, "params": {}})

        assert len(submitted) == 1
        m = conn.execute("SELECT status, error FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "failed"
        assert "insufficient_vram" in m["error"]

    def test_poll_exception_fails_model_not_just_job(self, client, project, isolated_data_dir):
        """The 2026-07-14 incident: xtts went down mid-run (stack redeploy), the
        status poll raised ConnectError, and the catch-all failed the job but left
        the model wedged at 'training' — unrecoverable (POST/DELETE 409, no cancel)."""
        import db
        import httpx
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="pending")

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            raise httpx.ConnectError("All connection attempts failed")

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "finetune",
                                      params={"model_id": model_id, "params": {}})

        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        m = conn.execute("SELECT status, error FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "failed"
        assert "connection" in m["error"].lower()

    def test_preview_failure_leaves_ready_model_untouched(self, client, project, isolated_data_dir, monkeypatch):
        """The failed-job → fail-model invariant is scoped to dataset_build and
        finetune: a preview blowing up must not fail the ready model it used."""
        import db
        import httpx
        import jobs
        # Shrink the submit retry budget so the ConnectError fails fast.
        monkeypatch.setattr(jobs, "_SUBMIT_RETRY_BASE_SECS", 0.01)
        monkeypatch.setattr(jobs, "_SUBMIT_RETRY_MAX_SECS", 0.02)
        monkeypatch.setattr(jobs, "_SUBMIT_RETRY_TIMEOUT_SECS", 0.1)
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready",
                                 checkpoint_dir="models/x")
        _set_reference(conn, project, isolated_data_dir / "projects" / project)

        async def mock_submit(service, payload):
            raise httpx.ConnectError("All connection attempts failed")

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)):
            job_id = _enqueue_and_run(project, "preview",
                                      params={"model_id": model_id, "text": "hi"})

        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        m = conn.execute("SELECT status FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "ready"

    def test_dataset_build_failed_first_fails_fast(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        # Model that never got a manifest (dataset build failed).
        model_id = _insert_model(conn, project, status="failed", manifest=None)
        conn.execute("UPDATE models SET dataset_manifest_path=NULL WHERE id=?", (model_id,))
        conn.commit()

        submit = AsyncMock()
        with patch("service_client.submit_job", new=submit), \
             patch("service_client.poll_until_complete", new=AsyncMock()):
            job_id = _enqueue_and_run(project, "finetune",
                                      params={"model_id": model_id, "params": {}})

        submit.assert_not_called()
        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        m = conn.execute("SELECT status FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "failed"


# ========================================================================
# Task 9 — conditioning resolution + preview handler
# ========================================================================

class TestConditioningResolution:
    def _prow(self, conn, project_id):
        return conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    def test_reference_clip(self, client, project, isolated_data_dir):
        import db, jobs
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))
        src, refs = jobs._resolve_conditioning(conn, self._prow(conn, project), project, "reference_clip", 5)
        assert src == "reference_clip"
        assert len(refs) == 1 and refs[0].endswith("reference.wav")

    def test_reference_clip_missing_raises(self, client, project):
        import db, jobs
        conn = db.get_conn(project)
        with pytest.raises(LookupError):
            jobs._resolve_conditioning(conn, self._prow(conn, project), project, "reference_clip", 5)

    def test_segments_raw_top_n_by_confidence(self, client, project):
        import db, jobs
        conn = db.get_conn(project)
        s = _insert_source(conn, project)
        _insert_seg(conn, project, s, status="pending", confidence=0.9, start=0, end=5)
        _insert_seg(conn, project, s, status="pending", confidence=0.7, start=0, end=5)
        _insert_seg(conn, project, s, status="rejected", confidence=0.99, start=0, end=5)  # excluded
        _insert_seg(conn, project, s, status="pending", confidence=0.99, start=0, end=1)   # too short
        src, paths = jobs._resolve_conditioning(conn, self._prow(conn, project), project, "segments_raw", 5)
        assert src == "segments_raw"
        assert len(paths) == 2  # only the two valid pending segments

    def test_segments_cleaned_falls_back_to_export_path(self, client, project):
        import db, jobs
        conn = db.get_conn(project)
        s = _insert_source(conn, project)
        _insert_seg(conn, project, s, status="approved", confidence=0.9, start=0, end=5)  # no cleaned audio
        _insert_seg(conn, project, s, status="approved", confidence=0.8, start=0, end=5,
                    export_path="export/c.wav")
        src, paths = jobs._resolve_conditioning(conn, self._prow(conn, project), project, "segments_cleaned", 5)
        assert src == "segments_cleaned"
        assert len(paths) == 1 and paths[0].endswith("export/c.wav")

    def test_segments_cleaned_prefers_cleaned_path(self, client, project):
        import db, jobs
        conn = db.get_conn(project)
        s = _insert_source(conn, project)
        # Both paths present → the dataset cache (cleaned_path) wins.
        _insert_seg(conn, project, s, status="approved", confidence=0.9, start=0, end=5,
                    export_path="export/c.wav", cleaned_path="cleaned/c.wav")
        src, paths = jobs._resolve_conditioning(conn, self._prow(conn, project), project, "segments_cleaned", 5)
        assert src == "segments_cleaned"
        assert len(paths) == 1 and paths[0].endswith("cleaned/c.wav")

    def test_fallback_prefers_cleaned_then_raw_then_reference(self, client, project, isolated_data_dir):
        import db, jobs
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))
        s = _insert_source(conn, project)
        _insert_seg(conn, project, s, status="approved", confidence=0.9, start=0, end=5)  # raw only
        # No cleaned segments → falls back to raw (before reference).
        src, _ = jobs._resolve_conditioning(conn, self._prow(conn, project), project, None, 5)
        assert src == "segments_raw"

    def test_fallback_no_audio_raises(self, client, project):
        import db, jobs
        conn = db.get_conn(project)
        with pytest.raises(LookupError):
            jobs._resolve_conditioning(conn, self._prow(conn, project), project, None, 5)


class TestPreviewHandler:
    def test_zero_shot_happy_path(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        _set_reference(conn, project, pdir)

        captured = {}

        async def mock_submit(service, payload):
            captured["payload"] = payload
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete",
                    "result": {"output_path": "x", "duration_secs": 1.0}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "preview",
                                      params={"text": "hi", "model_id": None,
                                              "conditioning": {"source": None, "segment_count": 5}})

        p = captured["payload"]
        assert p["type"] == "synthesise"
        assert p["checkpoint_dir"] is None
        assert p["output_path"].endswith(f"previews/{job_id}.wav")
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "complete"

    def test_temperature_forwarded_from_params(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))

        captured = {}

        async def mock_submit(service, payload):
            captured["payload"] = payload
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete",
                    "result": {"output_path": "x", "duration_secs": 1.0}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "preview",
                             params={"text": "hi", "model_id": None, "temperature": 0.9,
                                     "conditioning": {"source": None, "segment_count": 5}})

        assert captured["payload"]["params"]["temperature"] == 0.9

    def test_temperature_defaults_to_065(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))

        captured = {}

        async def mock_submit(service, payload):
            captured["payload"] = payload
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete",
                    "result": {"output_path": "x", "duration_secs": 1.0}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "preview",
                             params={"text": "hi", "model_id": None,
                                     "conditioning": {"source": None, "segment_count": 5}})

        assert captured["payload"]["params"]["temperature"] == 0.65

    def test_sampling_params_forwarded_with_defaults(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))

        captured = {}

        async def mock_submit(service, payload):
            captured["payload"] = payload
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete",
                    "result": {"output_path": "x", "duration_secs": 1.0}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "preview",
                             params={"text": "hi", "model_id": None,
                                     "speed": 1.3, "top_k": 25,
                                     "conditioning": {"source": None, "segment_count": 5}})

        p = captured["payload"]["params"]
        # Explicit values pass through; the rest fill from the shared defaults.
        assert p == {"temperature": 0.65, "speed": 1.3, "repetition_penalty": 10.0,
                     "top_k": 25, "top_p": 0.85}

    def test_finetuned_uses_checkpoint_dir(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        _set_reference(conn, project, pdir)
        model_id = _insert_model(conn, project, status="ready", checkpoint_dir=f"models/m")

        captured = {}

        async def mock_submit(service, payload):
            captured["payload"] = payload
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "result": {}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "preview",
                             params={"text": "hi", "model_id": model_id,
                                     "conditioning": {"source": "reference_clip", "segment_count": 5}})

        assert captured["payload"]["checkpoint_dir"].endswith("models/m")

    def test_non_ready_model_fails_job(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="training")

        submit = AsyncMock()
        with patch("service_client.submit_job", new=submit), \
             patch("service_client.poll_until_complete", new=AsyncMock()):
            job_id = _enqueue_and_run(project, "preview",
                                      params={"text": "hi", "model_id": model_id,
                                              "conditioning": {"source": None, "segment_count": 5}})

        submit.assert_not_called()
        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "model_not_ready" in job["error"]


# ========================================================================
# Task 10 — models router
# ========================================================================

class TestModelsRouter:
    def test_post_defaults_approved_202(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_many(conn, project, src, 31, dur=10.0, cleaned=True)  # 310s

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/models", json={})
        assert resp.status_code == 202
        body = resp.json()
        assert body["model"]["status"] == "pending"
        assert body["model"]["dataset_mode"] == "approved"
        types = [j["type"] for j in body["enqueued_jobs"]]
        assert types == ["dataset_build", "finetune"]

        row = conn.execute("SELECT * FROM models WHERE project_id=?", (project,)).fetchone()
        assert row["dataset_mode"] == "approved"
        assert row["min_confidence"] is None

    def test_post_auto_mode_persists_min_confidence(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        # auto mode over pending high-confidence segments.
        _insert_many(conn, project, src, 31, dur=10.0, cleaned=True, status="pending", confidence=0.95)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/models",
                               json={"dataset": {"mode": "auto", "min_confidence": 0.9}})
        assert resp.status_code == 202
        row = conn.execute("SELECT * FROM models WHERE project_id=?", (project,)).fetchone()
        assert row["dataset_mode"] == "auto"
        assert row["min_confidence"] == 0.9

    def test_post_insufficient_dataset_409(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_many(conn, project, src, 5, dur=10.0, cleaned=True)  # 50s

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/models", json={})
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "insufficient_dataset"
        assert body["detail"]["required_secs"] == 300
        assert "selected_duration_secs" in body["detail"]

    def test_post_finetune_in_progress_409(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_many(conn, project, src, 31, dur=10.0, cleaned=True)
        _insert_model(conn, project, status="training")

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/models", json={})
        assert resp.status_code == 409
        assert resp.json()["error"] == "finetune_in_progress"

    def test_post_503_when_unhealthy(self, client, project):
        with patch("service_client.is_healthy", new=AsyncMock(return_value=False)):
            resp = client.post(f"/projects/{project}/models", json={})
        assert resp.status_code == 503
        assert resp.json()["error"] == "xtts_unavailable"

    def test_get_list_shape(self, client, project):
        import db
        conn = db.get_conn(project)
        _insert_model(conn, project, status="ready")
        resp = client.get(f"/projects/{project}/models")
        assert resp.status_code == 200
        models = resp.json()["models"]
        assert len(models) == 1
        assert models[0]["status"] == "ready"
        assert isinstance(models[0]["params"], dict)

    def test_delete_happy(self, client, project):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready")
        resp = client.delete(f"/projects/{project}/models/{model_id}")
        assert resp.status_code == 204
        assert conn.execute("SELECT COUNT(*) FROM models WHERE id=?", (model_id,)).fetchone()[0] == 0

    def test_delete_while_training_409(self, client, project):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="training")
        resp = client.delete(f"/projects/{project}/models/{model_id}")
        assert resp.status_code == 409
        assert resp.json()["error"] == "model_training"

    def test_delete_unknown_404(self, client, project):
        resp = client.delete(f"/projects/{project}/models/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json()["error"] == "model_not_found"


class TestModelDownload:
    """GET /projects/{id}/models/{model_id}/download — stream the trained XTTS
    checkpoint bundle as an uncompressed tar for use outside FlipSync."""

    def _write_bundle(self, pdir, model_id, files):
        bundle = pdir / "models" / model_id
        bundle.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (bundle / name).write_bytes(data)
        return bundle

    def test_download_unknown_model_404(self, client, project, isolated_data_dir):
        resp = client.get(f"/projects/{project}/models/{uuid.uuid4()}/download")
        assert resp.status_code == 404
        assert resp.json()["error"] == "model_not_found"

    def test_download_not_ready_409(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="training")
        resp = client.get(f"/projects/{project}/models/{model_id}/download")
        assert resp.status_code == 409
        assert resp.json()["error"] == "model_not_ready"

    def test_download_missing_bundle_404(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready",
                                 checkpoint_dir=None)  # nothing on disk
        resp = client.get(f"/projects/{project}/models/{model_id}/download")
        assert resp.status_code == 404
        assert resp.json()["error"] == "model_bundle_not_found"

    def test_download_streams_tar_bundle(self, client, project, isolated_data_dir):
        import io
        import tarfile
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready")
        conn.execute("UPDATE models SET checkpoint_dir=? WHERE id=?",
                     (f"models/{model_id}", model_id))
        conn.commit()
        files = {
            "model.pth": b"WEIGHTS",
            "config.json": b'{"x":1}',
            "vocab.json": b"VOCAB",
            "speaker_latents.pt": b"LATENTS",
        }
        self._write_bundle(isolated_data_dir / "projects" / project, model_id, files)

        resp = client.get(f"/projects/{project}/models/{model_id}/download")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/x-tar"
        assert "attachment" in resp.headers["content-disposition"]
        assert f"{model_id}.tar" in resp.headers["content-disposition"]

        tf = tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:")
        members = {m.name: tf.extractfile(m).read() for m in tf.getmembers()}
        assert members == files

    def test_download_bundle_without_latents_ok(self, client, project, isolated_data_dir):
        """speaker_latents.pt is optional; the three mandatory files suffice."""
        import io
        import tarfile
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready")
        conn.execute("UPDATE models SET checkpoint_dir=? WHERE id=?",
                     (f"models/{model_id}", model_id))
        conn.commit()
        files = {"model.pth": b"W", "config.json": b"{}", "vocab.json": b"V"}
        self._write_bundle(isolated_data_dir / "projects" / project, model_id, files)

        resp = client.get(f"/projects/{project}/models/{model_id}/download")
        assert resp.status_code == 200
        tf = tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:")
        assert {m.name for m in tf.getmembers()} == set(files)

    def test_download_incomplete_bundle_404(self, client, project, isolated_data_dir):
        """A ready model whose checkpoint is missing a mandatory file is a 404."""
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready")
        conn.execute("UPDATE models SET checkpoint_dir=? WHERE id=?",
                     (f"models/{model_id}", model_id))
        conn.commit()
        # vocab.json missing
        self._write_bundle(isolated_data_dir / "projects" / project, model_id,
                           {"model.pth": b"W", "config.json": b"{}"})
        resp = client.get(f"/projects/{project}/models/{model_id}/download")
        assert resp.status_code == 404
        assert resp.json()["error"] == "model_bundle_not_found"


# ========================================================================
# Task 11 — previews router + progress_detail in job summaries
# ========================================================================

class TestPreviewsRouter:
    def test_post_zero_shot_202(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews", json={"text": "Hello there."})
        assert resp.status_code == 202
        assert resp.json()["enqueued_job"]["type"] == "preview"

    def test_post_conditioning_unavailable_409(self, client, project):
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews", json={"text": "Hello"})
        assert resp.status_code == 409
        assert resp.json()["error"] == "conditioning_unavailable"

    def test_post_model_not_ready_409(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))
        model_id = _insert_model(conn, project, status="training")
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"text": "Hi", "model_id": model_id})
        assert resp.status_code == 409
        assert resp.json()["error"] == "model_not_ready"

    def test_post_503_when_unhealthy(self, client, project):
        with patch("service_client.is_healthy", new=AsyncMock(return_value=False)):
            resp = client.post(f"/projects/{project}/previews", json={"text": "Hi"})
        assert resp.status_code == 503

    def test_post_text_too_long_422(self, client, project):
        resp = client.post(f"/projects/{project}/previews", json={"text": "x" * 501})
        assert resp.status_code == 422

    def test_post_accepts_temperature_and_enqueues_it(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"text": "Hello there.", "temperature": 0.8})
        assert resp.status_code == 202
        job_id = resp.json()["enqueued_job"]["id"]
        row = conn.execute("SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert json.loads(row["params"])["temperature"] == 0.8

    def test_post_temperature_out_of_range_422(self, client, project):
        resp = client.post(f"/projects/{project}/previews",
                           json={"text": "Hi", "temperature": 5.0})
        assert resp.status_code == 422

    def test_post_accepts_sampling_params_and_enqueues_them(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(
                f"/projects/{project}/previews",
                json={"text": "Hello there.", "speed": 1.25,
                      "repetition_penalty": 6.0, "top_k": 40, "top_p": 0.7},
            )
        assert resp.status_code == 202
        job_id = resp.json()["enqueued_job"]["id"]
        p = json.loads(conn.execute(
            "SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()["params"])
        assert p["speed"] == 1.25
        assert p["repetition_penalty"] == 6.0
        assert p["top_k"] == 40
        assert p["top_p"] == 0.7

    @pytest.mark.parametrize("field,value", [
        ("speed", 3.0),
        ("repetition_penalty", 0.5),
        ("top_k", 0),
        ("top_p", 1.5),
    ])
    def test_post_sampling_param_out_of_range_422(self, client, project, field, value):
        resp = client.post(f"/projects/{project}/previews",
                           json={"text": "Hi", field: value})
        assert resp.status_code == 422

    def test_get_list_maps_params(self, client, project):
        import db
        conn = db.get_conn(project)
        now = _now()
        pid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, params, created_at) VALUES (?,?,?,?,?,?)",
            (pid, project, "preview", "complete",
             json.dumps({"text": "hi", "model_id": None,
                         "conditioning": {"source": "segments_cleaned", "segment_count": 5}}), now),
        )
        conn.commit()
        resp = client.get(f"/projects/{project}/previews")
        assert resp.status_code == 200
        pv = resp.json()["previews"]
        assert len(pv) == 1
        assert pv[0]["text"] == "hi"
        assert pv[0]["conditioning"]["source"] == "segments_cleaned"

    def test_audio_404_before_complete_200_after(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        now = _now()
        pid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, params, created_at) VALUES (?,?,?,?,?,?)",
            (pid, project, "preview", "running", json.dumps({"text": "hi"}), now),
        )
        conn.commit()
        assert client.get(f"/projects/{project}/previews/{pid}/audio").status_code == 404

        (pdir / "previews").mkdir(parents=True, exist_ok=True)
        (pdir / "previews" / f"{pid}.wav").write_bytes(b"\x00" * 50)
        conn.execute("UPDATE jobs SET status='complete' WHERE id=?", (pid,))
        conn.commit()
        resp = client.get(f"/projects/{project}/previews/{pid}/audio")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"

    def test_progress_detail_in_project_detail_and_jobs_list(self, client, project):
        import db
        conn = db.get_conn(project)
        now = _now()
        jid = str(uuid.uuid4())
        detail = {"phase": "training", "epoch": 2, "total_epochs": 10, "train_loss": 3.1}
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, progress, progress_detail, created_at) VALUES (?,?,?,?,?,?,?)",
            (jid, project, "finetune", "running", 20, json.dumps(detail), now),
        )
        conn.commit()

        detail_resp = client.get(f"/projects/{project}").json()
        active = [j for j in detail_resp["active_jobs"] if j["id"] == jid][0]
        assert active["progress_detail"]["epoch"] == 2

        jobs_resp = client.get(f"/projects/{project}/jobs").json()
        job = [j for j in jobs_resp["jobs"] if j["id"] == jid][0]
        assert job["progress_detail"]["train_loss"] == 3.1


# ========================================================================
# Review fixes — mode validation + empty-manifest guard
# ========================================================================


class TestReviewFixes:
    def test_invalid_dataset_mode_returns_422(self, client, project):
        resp = client.post(
            f"/projects/{project}/models", json={"dataset": {"mode": "bogus"}}
        )
        assert resp.status_code == 422

    def test_dataset_build_all_missing_transcripts_fails_early(
        self, client, project, isolated_data_dir
    ):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        # 31 cleaned 10 s segments (>300 s floor) but none has a transcript.
        for i in range(31):
            _insert_seg(conn, project, src, transcript=None,
                        start=i * 20.0, end=i * 20.0 + 10.0,
                        cleaned_path=f"cleaned/x{i}.wav")
        model_id = _insert_model(conn, project)

        with patch("service_client.submit_job", new=AsyncMock()) as mock_submit, \
             patch("service_client.poll_until_complete", new=AsyncMock()):
            job_id = _enqueue_and_run(project, "dataset_build",
                                      params={"model_id": model_id, "mode": "approved",
                                              "min_confidence": None})
            mock_submit.assert_not_awaited()  # all cleaned — no cleanup call

        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "no usable segments" in job["error"]
        m = conn.execute("SELECT status, error FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "failed"
        assert "no usable segments" in m["error"]


# ========================================================================
# Integration fixes — migration 009 + cleaned_path lifecycle
# ========================================================================


class TestMigration009:
    def test_cleaned_path_column_exists(self, client, project):
        import db
        conn = db.get_conn(project)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(segments)").fetchall()]
        assert "cleaned_path" in cols


class TestCleanedPathLifecycle:
    def test_dataset_cleanup_sets_cleaned_path_never_status(
        self, client, project, isolated_data_dir
    ):
        """Dataset cleanup records cleaned_path on success AND clipping, and
        leaves rows completely alone on error / silent-after-trim — review
        statuses never change from a training action."""
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_many(conn, project, src, 31, dur=10.0, cleaned=True)  # 310s floor
        ok = _insert_seg(conn, project, src, status="approved", start=1000, end=1010)
        clipped = _insert_seg(conn, project, src, status="approved", start=1020, end=1030)
        errored = _insert_seg(conn, project, src, status="approved", start=1040, end=1050)
        silent = _insert_seg(conn, project, src, status="approved", start=1060, end=1070)
        model_id = _insert_model(conn, project)

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "results": [
                {"id": ok, "output_path": "x", "clipping_warning": False,
                 "auto_rejected": False, "error": None},
                {"id": clipped, "output_path": "x", "clipping_warning": True,
                 "auto_rejected": False, "error": None},
                {"id": errored, "output_path": None, "clipping_warning": False,
                 "auto_rejected": False, "error": "ffmpeg exploded"},
                {"id": silent, "output_path": None, "clipping_warning": False,
                 "auto_rejected": True, "error": None},
            ]}

        with patch("service_client.submit_job",
                   new=AsyncMock(side_effect=lambda s, p: {"job_id": p["job_id"]})), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "dataset_build",
                                      params={"model_id": model_id, "mode": "approved",
                                              "min_confidence": None})

        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, status, cleaned_path, export_path, flags, clipping_warning "
            "FROM segments WHERE id IN (?,?,?,?)", (ok, clipped, errored, silent)
        ).fetchall()}
        # Success and clipping → cleaned_path set (clipped audio IS in datasets).
        assert rows[ok]["cleaned_path"] == f"cleaned/{ok}.wav"
        assert rows[clipped]["cleaned_path"] == f"cleaned/{clipped}.wav"
        # Error / silent → row untouched: no cleaned_path, no flags.
        assert rows[errored]["cleaned_path"] is None
        assert rows[errored]["flags"] is None
        assert rows[silent]["cleaned_path"] is None
        # No status or export-set mutation from a training action.
        for r in rows.values():
            assert r["status"] == "approved"
            assert r["export_path"] is None
            assert r["clipping_warning"] == 0

        # Manifest: 31 pre-cleaned + ok + clipped; failed ones just drop out.
        pdir = db.project_dir(project)
        manifest = json.loads((pdir / "models" / model_id / "dataset.json").read_text())
        ids = {s["id"] for s in manifest["segments"]}
        assert ok in ids and clipped in ids
        assert errored not in ids and silent not in ids
        assert len(manifest["segments"]) == 33
        assert all("/cleaned/" in s["audio_file"] for s in manifest["segments"])
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "complete"

    def test_export_does_not_clear_cleaned_path(self, client, project, isolated_data_dir):
        """Staged export nulls export_path project-wide and replaces export/,
        but the dataset cache (cleaned_path / cleaned/) is untouched."""
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        seg = _insert_seg(conn, project, src, status="approved",
                          cleaned_path="cleaned/keep.wav", export_path="export/old.wav")

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "results": [
                {"id": seg, "output_path": "x", "clipping_warning": False,
                 "auto_rejected": False, "error": None},
            ]}

        with patch("service_client.submit_job",
                   new=AsyncMock(side_effect=lambda s, p: {"job_id": p["job_id"]})), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "export", params={})

        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "complete", job["error"]
        row = conn.execute(
            "SELECT cleaned_path, export_path FROM segments WHERE id=?", (seg,)
        ).fetchone()
        assert row["cleaned_path"] == "cleaned/keep.wav"
        assert row["export_path"] == f"export/{seg}.wav"


# ========================================================================
# Integration fixes — interrupted fine-tune recovery (no wedged model rows)
# ========================================================================


class TestFinetuneRecoveryWedge:
    def test_interrupted_finetune_marks_model_failed(self, client, project, isolated_data_dir):
        """recover_jobs re-queues an interrupted finetune; the model row is
        still 'training' from the dead run. The re-run must fail the model row
        (not just the job) so it isn't wedged forever — POST /models 409s
        while pending/training, DELETE 409s, and there is no cancel."""
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="training")

        submit = AsyncMock()
        with patch("service_client.submit_job", new=submit), \
             patch("service_client.poll_until_complete", new=AsyncMock()):
            job_id = _enqueue_and_run(project, "finetune",
                                      params={"model_id": model_id, "params": {}})

        submit.assert_not_called()
        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "interrupted" in job["error"]
        m = conn.execute("SELECT status, error FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "failed"
        assert "fine-tune interrupted (orchestrator restart)" == m["error"]
        # The model is no longer wedged: it can now be deleted...
        assert client.delete(f"/projects/{project}/models/{model_id}").status_code == 204

    def test_pending_model_without_manifest_marks_model_failed(
        self, client, project, isolated_data_dir
    ):
        """A finetune reaching a 'pending' model with no manifest means the
        dataset build never completed — fail the row too, with the accurate
        message."""
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="pending", manifest=None)

        with patch("service_client.submit_job", new=AsyncMock()), \
             patch("service_client.poll_until_complete", new=AsyncMock()):
            job_id = _enqueue_and_run(project, "finetune",
                                      params={"model_id": model_id, "params": {}})

        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "dataset build did not complete" in job["error"]
        m = conn.execute("SELECT status, error FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "failed"
        assert "dataset build did not complete" in m["error"]

    def test_already_failed_model_left_alone(self, client, project, isolated_data_dir):
        """Guard-trip on a model already 'failed' (dataset build failed and
        recorded its own error) must not overwrite that error."""
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="failed", manifest=None)
        conn.execute("UPDATE models SET error='insufficient dataset' WHERE id=?", (model_id,))
        conn.commit()

        with patch("service_client.submit_job", new=AsyncMock()), \
             patch("service_client.poll_until_complete", new=AsyncMock()):
            _enqueue_and_run(project, "finetune",
                             params={"model_id": model_id, "params": {}})

        m = conn.execute("SELECT status, error FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "failed"
        assert m["error"] == "insufficient dataset"


# ========================================================================
# Integration fixes — voice jobs and project status
# ========================================================================


class TestVoiceJobsProjectStatus:
    def _insert_job(self, conn, project_id, job_type, status="running"):
        jid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, params, created_at) VALUES (?,?,?,?,?,?)",
            (jid, project_id, job_type, status, "{}", _now()),
        )
        conn.commit()
        return jid

    def test_voice_job_types_registry(self):
        import jobs
        assert jobs.VOICE_JOB_TYPES == {"dataset_build", "finetune", "preview"}
        assert jobs.VOICE_JOB_TYPES <= set(jobs.HANDLERS)

    def test_running_voice_jobs_do_not_flip_project_to_processing(self, client, project):
        import db
        from status import recompute_project_status
        conn = db.get_conn(project)
        _insert_source(conn, project, status="complete")

        for jtype in ("dataset_build", "finetune", "preview"):
            self._insert_job(conn, project, jtype)
        recompute_project_status(project)
        row = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert row["status"] == "review"

        # Control: a pipeline job still flips the project to processing.
        self._insert_job(conn, project, "vocal_separation")
        recompute_project_status(project)
        row = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert row["status"] == "processing"

    def test_active_jobs_api_still_includes_voice_jobs(self, client, project):
        import db
        conn = db.get_conn(project)
        jid = self._insert_job(conn, project, "finetune")
        detail = client.get(f"/projects/{project}").json()
        assert jid in [j["id"] for j in detail["active_jobs"]]

    def test_export_can_be_enqueued_while_finetune_runs(self, client, project, isolated_data_dir):
        import db
        from status import recompute_project_status
        conn = db.get_conn(project)
        src = _insert_source(conn, project, status="complete")
        _insert_seg(conn, project, src, status="approved")
        self._insert_job(conn, project, "finetune")
        recompute_project_status(project)

        with patch("service_client.submit_job",
                   new=AsyncMock(side_effect=lambda s, p: {"job_id": p["job_id"]})), \
             patch("service_client.poll_until_complete",
                   new=AsyncMock(return_value={"status": "failed", "error": "x"})):
            resp = client.post(f"/projects/{project}/export")
        assert resp.status_code == 202
        assert resp.json()["enqueued_job"]["type"] == "export"
