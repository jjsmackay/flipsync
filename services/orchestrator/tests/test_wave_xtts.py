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
                flags=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments
           (id, project_id, source_id, raw_path, export_path, start_secs, end_secs,
            speaker_label, match_confidence, status, transcript, flags, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, f"segments/raw/{seg_id}.wav", export_path,
         start, end, "SPEAKER_00", confidence, status, transcript,
         json.dumps(flags) if flags else None, now, now),
    )
    conn.commit()
    return seg_id


def _insert_many(conn, project_id, source_id, count, dur=10.0, status="approved",
                 confidence=0.9, cleaned=False):
    ids = []
    for i in range(count):
        ep = f"export/x{i}.wav" if cleaned else None
        ids.append(_insert_seg(conn, project_id, source_id, status=status,
                               confidence=confidence, start=i * 20.0, end=i * 20.0 + dur,
                               export_path=ep))
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

        # Only the 15 uncleaned segments were sent to cleanup.
        assert len(captured["payload"]["segments"]) == 15
        assert {s["id"] for s in captured["payload"]["segments"]} == set(missing)

        manifest = json.loads((pdir / "models" / model_id / "dataset.json").read_text())
        assert manifest["speaker"] == "target"
        assert manifest["selection"]["mode"] == "approved"
        assert manifest["selection"]["dropped"] == {"too_short": 0, "too_long": 0, "flagged": 0}
        assert len(manifest["segments"]) == 35
        # audio_file paths are absolute (DATA_DIR-rooted).
        assert all(seg["audio_file"].startswith(str(isolated_data_dir)) for seg in manifest["segments"])
        m = conn.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["segment_count"] == 35
        assert m["dataset_duration_secs"] == pytest.approx(350.0)

    def test_dropped_counts_recorded_in_manifest(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        src = _insert_source(conn, project)
        _insert_many(conn, project, src, 31, dur=10.0, cleaned=True)  # 310s valid, cleaned
        _insert_seg(conn, project, src, status="approved", start=0, end=0.5, export_path="export/s.wav")
        _insert_seg(conn, project, src, status="approved", start=0, end=15, export_path="export/l.wav")
        _insert_seg(conn, project, src, status="approved", start=0, end=5,
                    export_path="export/f.wav", flags=["cleanup_error: x"])
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

    def test_segments_cleaned_requires_export_path(self, client, project):
        import db, jobs
        conn = db.get_conn(project)
        s = _insert_source(conn, project)
        _insert_seg(conn, project, s, status="approved", confidence=0.9, start=0, end=5)  # no export
        _insert_seg(conn, project, s, status="approved", confidence=0.8, start=0, end=5,
                    export_path="export/c.wav")
        src, paths = jobs._resolve_conditioning(conn, self._prow(conn, project), project, "segments_cleaned", 5)
        assert src == "segments_cleaned"
        assert len(paths) == 1 and paths[0].endswith("export/c.wav")

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
