"""Multi-engine (GPT-SoVITS alongside XTTS-v2) orchestrator plumbing tests.

This is orchestrator-only plumbing: the gpt-sovits processing service does not
exist yet, so all service calls are mocked exactly like the existing xtts
suite (tests/test_wave_xtts.py). Covers: migration 013, engine routing in
_handle_finetune/_handle_preview, phase-aware progress percent, per-engine
bundle download, create-model validation, and the capabilities engines array.
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
# DB helpers (mirrors tests/test_wave_xtts.py)
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
                cleaned_path=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments
           (id, project_id, source_id, raw_path, export_path, cleaned_path, start_secs,
            end_secs, speaker_label, match_confidence, status, transcript,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, f"segments/raw/{seg_id}.wav", export_path,
         cleaned_path, start, end, "SPEAKER_00", confidence, status, transcript,
         now, now),
    )
    conn.commit()
    return seg_id


def _insert_model(conn, project_id, status="pending", mode="approved",
                  min_confidence=None, manifest="models/m/dataset.json",
                  checkpoint_dir=None, engine=None):
    mid = str(uuid.uuid4())
    now = _now()
    cols = "id, project_id, status, dataset_mode, min_confidence, dataset_manifest_path, checkpoint_dir, params, created_at, updated_at"
    vals = [mid, project_id, status, mode, min_confidence, manifest, checkpoint_dir,
            json.dumps({"epochs": 10}), now, now]
    if engine is not None:
        cols += ", engine"
        vals.append(engine)
    placeholders = ",".join("?" * len(vals))
    conn.execute(f"INSERT INTO models ({cols}) VALUES ({placeholders})", vals)
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
# Migration 013
# ========================================================================

class TestMigration013:
    def test_engine_column_exists_and_defaults_xtts(self, client, project):
        import db
        conn = db.get_conn(project)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(models)").fetchall()}
        assert "engine" in cols

        src = _insert_source(conn, project)
        model_id = _insert_model(conn, project)  # no engine passed
        row = conn.execute("SELECT engine FROM models WHERE id=?", (model_id,)).fetchone()
        assert row["engine"] == "xtts"


# ========================================================================
# Service client
# ========================================================================

class TestServiceClient:
    def test_gpt_sovits_default_url(self):
        import service_client
        assert service_client.SERVICE_URLS["gpt_sovits"] == "http://gpt-sovits:8006"
        assert service_client.get_service_url("gpt_sovits") == "http://gpt-sovits:8006"


# ========================================================================
# Phase-aware progress percent (spec §6)
# ========================================================================

class TestPhasePercent:
    def test_xtts_training_mapping_unchanged(self):
        """The pre-multi-engine formula: int(frac * 100), clamped to 99."""
        import jobs
        detail = {"phase": "training", "epoch": 3, "total_epochs": 10,
                   "step": 100, "total_steps": 200}
        assert jobs._phase_percent(detail) == 25

    def test_training_phase_clamped_at_99(self):
        import jobs
        detail = {"phase": "training", "epoch": 10, "total_epochs": 10,
                   "step": 200, "total_steps": 200}
        assert jobs._phase_percent(detail) == 99

    def test_missing_phase_defaults_to_training_band(self):
        import jobs
        detail = {"epoch": 3, "total_epochs": 10, "step": 100, "total_steps": 200}
        assert jobs._phase_percent(detail) == 25

    def test_preparing_band_0_to_5(self):
        import jobs
        assert jobs._phase_percent({"phase": "preparing"}) == 0
        detail = {"phase": "preparing", "epoch": 1, "total_epochs": 2, "step": 1, "total_steps": 2}
        assert jobs._phase_percent(detail) == 1  # 0 + 0.25*5 = 1.25 -> 1

    def test_training_sovits_band_5_to_50(self):
        import jobs
        detail = {"phase": "training_sovits", "epoch": 3, "total_epochs": 10,
                   "step": 0, "total_steps": 1}
        # frac = (2 + 0)/10 = 0.2 -> 5 + 0.2*45 = 14
        assert jobs._phase_percent(detail) == 14

    def test_training_gpt_band_50_to_95(self):
        import jobs
        detail = {"phase": "training_gpt", "epoch": 6, "total_epochs": 10,
                   "step": 50, "total_steps": 100}
        # frac = (5 + 0.5)/10 = 0.55 -> 50 + 0.55*45 = 74.75
        assert jobs._phase_percent(detail) == 74

    def test_packaging_band_95_to_99(self):
        import jobs
        detail = {"phase": "packaging", "epoch": 1, "total_epochs": 1,
                   "step": 1, "total_steps": 1}
        # frac = 1.0 -> 95 + 1.0*4 = 99
        assert jobs._phase_percent(detail) == 99


# ========================================================================
# Fine-tune handler — engine routing
# ========================================================================

class TestFinetuneEngineRouting:
    def test_xtts_model_submits_to_xtts_service(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="pending", engine="xtts")

        submitted = []

        async def mock_submit(service, payload):
            submitted.append(service)
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "result": {"final_eval_loss": 1.0}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "finetune", params={"model_id": model_id, "params": {}})

        assert submitted == ["xtts"]

    def test_gpt_sovits_model_submits_to_gpt_sovits_service(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="pending", engine="gpt_sovits")

        submitted = []
        captured_payload = {}

        async def mock_submit(service, payload):
            submitted.append(service)
            captured_payload.update(payload)
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "result": {"final_eval_loss": 1.0}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "finetune",
                             params={"model_id": model_id, "params": {"sovits_epochs": 8}})

        assert submitted == ["gpt_sovits"]
        m = conn.execute("SELECT status FROM models WHERE id=?", (model_id,)).fetchone()
        assert m["status"] == "ready"
        # Only the explicit overrides are forwarded — no XTTS project-column
        # merge, no injected language/eval_split (service supplies its own defaults).
        assert captured_payload["params"] == {"sovits_epochs": 8}

    def test_gpt_sovits_omitted_params_are_not_backfilled_from_xtts_columns(
        self, client, project, isolated_data_dir
    ):
        import db
        conn = db.get_conn(project)
        conn.execute("UPDATE projects SET xtts_epochs=42 WHERE id=?", (project,))
        conn.commit()
        model_id = _insert_model(conn, project, status="pending", engine="gpt_sovits")

        captured_payload = {}

        async def mock_submit(service, payload):
            captured_payload.update(payload)
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "result": {}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "finetune", params={"model_id": model_id, "params": {}})

        assert "epochs" not in captured_payload["params"]
        assert captured_payload["params"] == {}

    def test_gpt_sovits_finetune_progress_uses_phase_bands(self, client, project, isolated_data_dir):
        """_complete_job always sets progress=100 at the end, so the
        phase-scaled value must be observed during the callback, not after.
        (mock_poll's job_id is the *service's* job id, not the orchestrator's
        jobs-table row id — the orchestrator job_id is captured separately.)"""
        import db
        import jobs
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="pending", engine="gpt_sovits")
        orchestrator_job_id = jobs.enqueue(
            project, "finetune", params={"model_id": model_id, "params": {}}
        )
        try:
            jobs._queues[project].get_nowait()
        except Exception:
            pass
        observed = {}

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            if on_progress:
                on_progress({"status": "running", "progress": {
                    "phase": "training_gpt", "epoch": 6, "total_epochs": 10,
                    "step": 50, "total_steps": 100}})
                row = conn.execute(
                    "SELECT progress, progress_detail FROM jobs WHERE id=?", (orchestrator_job_id,)
                ).fetchone()
                observed["progress"] = row["progress"]
                observed["phase"] = json.loads(row["progress_detail"])["phase"]
            return {"status": "complete", "result": {}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _run_job(project, orchestrator_job_id)

        assert observed["progress"] == 74
        assert observed["phase"] == "training_gpt"


# ========================================================================
# Preview handler — engine routing
# ========================================================================

class TestPreviewEngineRouting:
    def test_base_preview_routes_to_xtts(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))

        submitted = []

        async def mock_submit(service, payload):
            submitted.append(service)
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "result": {}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "preview",
                             params={"text": "hi", "model_id": None,
                                     "conditioning": {"source": None, "segment_count": 5}})

        assert submitted == ["xtts"]

    def test_gpt_sovits_model_preview_routes_to_gpt_sovits(self, client, project, isolated_data_dir):
        """A ready GPT-SoVITS model previews without any reference/segment audio
        present on the project — the bundle supplies its own stored reference,
        so the orchestrator must not require conditioning for this engine."""
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready", engine="gpt_sovits",
                                 checkpoint_dir="models/m")

        submitted = []
        captured_payload = {}

        async def mock_submit(service, payload):
            submitted.append(service)
            captured_payload.update(payload)
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "result": {}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "preview",
                                      params={"text": "hi", "model_id": model_id,
                                              "conditioning": {"source": None, "segment_count": 5}})

        assert submitted == ["gpt_sovits"]
        assert captured_payload["checkpoint_dir"].endswith("models/m")
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "complete"

    def test_xtts_model_preview_routes_to_xtts(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))
        model_id = _insert_model(conn, project, status="ready", engine="xtts",
                                 checkpoint_dir="models/m")

        submitted = []

        async def mock_submit(service, payload):
            submitted.append(service)
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "result": {}}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "preview",
                             params={"text": "hi", "model_id": model_id,
                                     "conditioning": {"source": "reference_clip", "segment_count": 5}})

        assert submitted == ["xtts"]


# ========================================================================
# Previews router — conditioning gate must not be engine-blind
# ========================================================================

class TestPreviewsRouterConditioningGate:
    """The router validates conditioning availability *before* enqueueing the
    preview job (jobs.py::_handle_preview has its own, identical gate for the
    async path — this covers the synchronous HTTP-level pre-check, which
    TestPreviewEngineRouting bypasses via _enqueue_and_run)."""

    def test_gpt_sovits_model_preview_accepted_without_conditioning_audio(
        self, client, project, isolated_data_dir
    ):
        """No reference_path and no qualifying segments — GPT-SoVITS doesn't
        need FlipSync-side conditioning audio (the service loads its own
        stored reference.wav/.txt from the bundle), so the router must not
        409 conditioning_unavailable for this engine."""
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready", engine="gpt_sovits",
                                 checkpoint_dir="models/m")

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"text": "hi", "model_id": model_id})
        assert resp.status_code == 202
        assert resp.json()["enqueued_job"]["type"] == "preview"

    def test_xtts_model_preview_still_409s_without_conditioning_audio(
        self, client, project, isolated_data_dir
    ):
        """Bit-identical existing behaviour: an XTTS preview still requires
        conditioning audio when none is available."""
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready", engine="xtts",
                                 checkpoint_dir="models/m")

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"text": "hi", "model_id": model_id})
        assert resp.status_code == 409
        assert resp.json()["error"] == "conditioning_unavailable"


# ========================================================================
# Preview sampling — per-engine defaults
# ========================================================================

_SAMPLING_KEYS = ("temperature", "speed", "repetition_penalty", "top_k", "top_p")

_XTTS_SAMPLING_DEFAULTS = {
    "temperature": 0.65,
    "speed": 1.0,
    "repetition_penalty": 10.0,
    "top_k": 50,
    "top_p": 0.85,
}


class TestPreviewSamplingPerEngine:
    """XTTS sampling defaults must never leak into GPT-SoVITS previews: the
    previews router persists only the knobs the caller actually sent, and the
    handler applies XTTS defaults only when the resolved engine is xtts.
    Upstream GPT-SoVITS caps repetition_penalty at 2.0 — forwarding xtts's
    10.0 would garble every real preview while CI stays green."""

    def _service_mocks(self, captured):
        async def mock_submit(service, payload):
            captured["payload"] = payload
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "result": {}}

        return (
            patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)),
            patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)),
        )

    # --- Router: job params carry only explicitly-sent knobs ---------------

    def test_router_persists_no_sampling_knobs_when_none_sent(self, client, project):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready", engine="gpt_sovits",
                                 checkpoint_dir="models/m")

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"text": "hi", "model_id": model_id})
        assert resp.status_code == 202
        job_id = resp.json()["enqueued_job"]["id"]
        row = conn.execute("SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()
        params = json.loads(row["params"])
        for key in _SAMPLING_KEYS:
            assert key not in params, key

    def test_router_persists_only_explicitly_sent_knobs(self, client, project):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready", engine="gpt_sovits",
                                 checkpoint_dir="models/m")

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"text": "hi", "model_id": model_id,
                                     "temperature": 0.9})
        assert resp.status_code == 202
        job_id = resp.json()["enqueued_job"]["id"]
        params = json.loads(conn.execute(
            "SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()["params"])
        assert params["temperature"] == 0.9
        for key in ("speed", "repetition_penalty", "top_k", "top_p"):
            assert key not in params, key

    # --- Handler: per-engine defaults applied at submit time ---------------

    def test_gpt_sovits_preview_payload_carries_no_xtts_sampling(
        self, client, project, isolated_data_dir
    ):
        """No knobs sent → empty params dict, so the gpt-sovits service's own
        SynthParams defaults (rep_penalty 1.35, temp 1.0, …) apply."""
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready", engine="gpt_sovits",
                                 checkpoint_dir="models/m")

        captured = {}
        submit_patch, poll_patch = self._service_mocks(captured)
        with submit_patch, poll_patch:
            _enqueue_and_run(project, "preview",
                             params={"text": "hi", "model_id": model_id,
                                     "conditioning": {"source": None, "segment_count": 5}})

        assert captured["payload"]["params"] == {}

    def test_gpt_sovits_preview_forwards_only_sent_knobs(
        self, client, project, isolated_data_dir
    ):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready", engine="gpt_sovits",
                                 checkpoint_dir="models/m")

        captured = {}
        submit_patch, poll_patch = self._service_mocks(captured)
        with submit_patch, poll_patch:
            _enqueue_and_run(project, "preview",
                             params={"text": "hi", "model_id": model_id,
                                     "temperature": 0.8, "speed": 1.2,
                                     "conditioning": {"source": None, "segment_count": 5}})

        assert captured["payload"]["params"] == {"temperature": 0.8, "speed": 1.2}

    def test_xtts_preview_payload_bit_identical_defaults(
        self, client, project, isolated_data_dir
    ):
        """XTTS previews must keep exactly today's effective values when the
        caller sends no knobs — the defaults just moved from the router into
        the handler."""
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))

        captured = {}
        submit_patch, poll_patch = self._service_mocks(captured)
        with submit_patch, poll_patch:
            _enqueue_and_run(project, "preview",
                             params={"text": "hi", "model_id": None,
                                     "conditioning": {"source": None, "segment_count": 5}})

        assert captured["payload"]["params"] == _XTTS_SAMPLING_DEFAULTS

    def test_xtts_preview_explicit_knobs_override_defaults(
        self, client, project, isolated_data_dir
    ):
        import db
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))

        captured = {}
        submit_patch, poll_patch = self._service_mocks(captured)
        with submit_patch, poll_patch:
            _enqueue_and_run(project, "preview",
                             params={"text": "hi", "model_id": None,
                                     "speed": 1.3, "top_k": 25,
                                     "conditioning": {"source": None, "segment_count": 5}})

        assert captured["payload"]["params"] == {**_XTTS_SAMPLING_DEFAULTS,
                                                 "speed": 1.3, "top_k": 25}


# ========================================================================
# Create-model — engine validation
# ========================================================================

class TestCreateModelEngineValidation:
    def _seed_dataset(self, conn, project_id):
        src = _insert_source(conn, project_id)
        for i in range(31):
            _insert_seg(conn, project_id, src, start=i * 20.0, end=i * 20.0 + 10.0,
                        cleaned_path=f"cleaned/x{i}.wav")

    def test_defaults_to_xtts_when_omitted(self, client, project):
        import db
        conn = db.get_conn(project)
        self._seed_dataset(conn, project)
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/models", json={})
        assert resp.status_code == 202
        row = conn.execute("SELECT engine FROM models WHERE project_id=?", (project,)).fetchone()
        assert row["engine"] == "xtts"

    def test_gpt_sovits_persisted_and_echoed(self, client, project):
        import db
        conn = db.get_conn(project)
        self._seed_dataset(conn, project)
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/models", json={"engine": "gpt_sovits"})
        assert resp.status_code == 202
        row = conn.execute("SELECT engine FROM models WHERE project_id=?", (project,)).fetchone()
        assert row["engine"] == "gpt_sovits"

        listing = client.get(f"/projects/{project}/models").json()["models"]
        assert listing[0]["engine"] == "gpt_sovits"

    def test_gpt_sovits_unavailable_503(self, client, project):
        import db
        conn = db.get_conn(project)
        self._seed_dataset(conn, project)

        async def health(name):
            return name == "xtts"  # xtts healthy, gpt_sovits not

        with patch("service_client.is_healthy", new=AsyncMock(side_effect=health)):
            resp = client.post(f"/projects/{project}/models", json={"engine": "gpt_sovits"})
        assert resp.status_code == 503
        assert resp.json()["error"] == "engine_unavailable"

    def test_xtts_unavailable_still_uses_legacy_error_code(self, client, project):
        """Existing behaviour must stay bit-identical: xtts's own 503 keeps its
        original error code, not the new generic one."""
        import db
        conn = db.get_conn(project)
        self._seed_dataset(conn, project)
        with patch("service_client.is_healthy", new=AsyncMock(return_value=False)):
            resp = client.post(f"/projects/{project}/models", json={})
        assert resp.status_code == 503
        assert resp.json()["error"] == "xtts_unavailable"

    def test_invalid_engine_value_422(self, client, project):
        resp = client.post(f"/projects/{project}/models", json={"engine": "bogus"})
        assert resp.status_code == 422

    def test_gpt_sovits_specific_param_keys_are_forwarded_not_dropped(self, client, project):
        """`params` is a permissive bag: GPT-SoVITS-shaped keys (not in the XTTS
        TrainParams field list) must survive to the persisted overrides so the
        service can use them — the engine ignores keys it doesn't recognise,
        not the orchestrator's request model."""
        import db
        conn = db.get_conn(project)
        self._seed_dataset(conn, project)
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(
                f"/projects/{project}/models",
                json={"engine": "gpt_sovits",
                      "params": {"sovits_epochs": 8, "gpt_epochs": 15}},
            )
        assert resp.status_code == 202
        row = conn.execute("SELECT params FROM models WHERE project_id=?", (project,)).fetchone()
        stored = json.loads(row["params"])
        assert stored == {"sovits_epochs": 8, "gpt_epochs": 15}


# ========================================================================
# Per-engine bundle download
# ========================================================================

class TestPerEngineBundleDownload:
    def _write_bundle(self, pdir, model_id, files):
        bundle = pdir / "models" / model_id
        bundle.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (bundle / name).write_bytes(data)
        return bundle

    def test_gpt_sovits_bundle_streams_five_mandatory_files(self, client, project, isolated_data_dir):
        import io
        import tarfile
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready", engine="gpt_sovits",
                                 checkpoint_dir=f"models/m")
        conn.execute("UPDATE models SET checkpoint_dir=? WHERE id=?", (f"models/{model_id}", model_id))
        conn.commit()
        files = {
            "gpt.ckpt": b"GPT",
            "sovits.pth": b"SOVITS",
            "config.json": b'{"x":1}',
            "reference.wav": b"WAV",
            "reference.txt": b"hello there",
        }
        self._write_bundle(isolated_data_dir / "projects" / project, model_id, files)

        resp = client.get(f"/projects/{project}/models/{model_id}/download")
        assert resp.status_code == 200
        tf = tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:")
        members = {m.name: tf.extractfile(m).read() for m in tf.getmembers()}
        assert members == files

    def test_gpt_sovits_missing_mandatory_file_404(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready", engine="gpt_sovits",
                                 checkpoint_dir=f"models/m")
        conn.execute("UPDATE models SET checkpoint_dir=? WHERE id=?", (f"models/{model_id}", model_id))
        conn.commit()
        # reference.txt missing
        self._write_bundle(isolated_data_dir / "projects" / project, model_id, {
            "gpt.ckpt": b"G", "sovits.pth": b"S", "config.json": b"{}", "reference.wav": b"W",
        })
        resp = client.get(f"/projects/{project}/models/{model_id}/download")
        assert resp.status_code == 404
        assert resp.json()["error"] == "model_bundle_not_found"

    def test_xtts_bundle_unaffected(self, client, project, isolated_data_dir):
        """Existing xtts bundle behaviour is bit-identical after the per-engine change."""
        import io
        import tarfile
        import db
        conn = db.get_conn(project)
        model_id = _insert_model(conn, project, status="ready", engine="xtts")
        conn.execute("UPDATE models SET checkpoint_dir=? WHERE id=?", (f"models/{model_id}", model_id))
        conn.commit()
        files = {"model.pth": b"W", "config.json": b"{}", "vocab.json": b"V"}
        self._write_bundle(isolated_data_dir / "projects" / project, model_id, files)

        resp = client.get(f"/projects/{project}/models/{model_id}/download")
        assert resp.status_code == 200
        tf = tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:")
        assert {m.name for m in tf.getmembers()} == set(files)


# ========================================================================
# Capabilities — engines array
# ========================================================================

class TestCapabilitiesEngines:
    def test_engines_array_shape(self, client):
        async def health(name):
            return name == "xtts"

        with patch("main.is_healthy", new=AsyncMock(side_effect=health)):
            resp = client.get("/capabilities")
        assert resp.status_code == 200
        engines = {e["id"]: e for e in resp.json()["engines"]}
        assert engines["xtts"]["healthy"] is True
        assert engines["xtts"]["name"] == "XTTS-v2"
        assert len(engines["xtts"]["languages"]) == 17
        assert "en" in engines["xtts"]["languages"]

        assert engines["gpt_sovits"]["healthy"] is False
        assert engines["gpt_sovits"]["name"] == "GPT-SoVITS"
        assert engines["gpt_sovits"]["languages"] == ["en", "zh", "ja", "ko", "yue"]

    @pytest.mark.parametrize("xtts_ok,gpt_ok,expected", [
        (True, False, True),
        (False, True, True),
        (True, True, True),
        (False, False, False),
    ])
    def test_voice_training_is_any_engine_healthy(self, client, xtts_ok, gpt_ok, expected):
        async def health(name):
            return xtts_ok if name == "xtts" else gpt_ok

        with patch("main.is_healthy", new=AsyncMock(side_effect=health)):
            resp = client.get("/capabilities")
        assert resp.json()["voice_training"] is expected

    def test_backward_compat_xtts_bool_retained(self, client):
        async def health(name):
            return name == "xtts"

        with patch("main.is_healthy", new=AsyncMock(side_effect=health)):
            resp = client.get("/capabilities")
        assert resp.json()["xtts"] is True

        async def health2(name):
            return False

        with patch("main.is_healthy", new=AsyncMock(side_effect=health2)):
            resp = client.get("/capabilities")
        assert resp.json()["xtts"] is False
