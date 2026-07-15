"""API contract tests for the XTTS service.

The engine (torch / Coqui-TTS) is patched at the ``engine.finetune`` /
``engine.synthesise`` / ``engine.vram_available_gb`` module-attribute seam, so
these tests run without torch or TTS installed and never touch a GPU.
"""

from __future__ import annotations

import threading
import time
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Bodies + polling helpers
# ---------------------------------------------------------------------------


def _finetune_body(**over):
    body = {
        "job_id": str(uuid.uuid4()),
        "type": "finetune",
        "manifest_path": "/data/projects/p/models/m/dataset.json",
        "output_dir": "/data/projects/p/models/m",
        "params": {
            "epochs": 10,
            "batch_size": 3,
            "grad_accum": 1,
            "learning_rate": 5e-6,
            "language": "en",
            "eval_split": 0.1,
        },
    }
    body.update(over)
    return body


def _synth_body(reference_wavs, output_path, **over):
    body = {
        "job_id": str(uuid.uuid4()),
        "type": "synthesise",
        "text": "This is what the cloned voice sounds like.",
        "language": "en",
        "reference_wavs": reference_wavs,
        "checkpoint_dir": None,
        "output_path": output_path,
        "params": {"temperature": 0.65},
    }
    body.update(over)
    return body


_RESULT = {
    "checkpoint_dir": "/data/projects/p/models/m",
    "model_path": "/data/projects/p/models/m/model.pth",
    "config_path": "/data/projects/p/models/m/config.json",
    "vocab_path": "/data/projects/p/models/m/vocab.json",
    "speaker_latents_path": "/data/projects/p/models/m/speaker_latents.pt",
    "final_eval_loss": 2.71,
}


def _poll_to_terminal(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    data = client.get(f"/jobs/{job_id}").json()
    while data["status"] == "running" and time.time() < deadline:
        time.sleep(0.02)
        data = client.get(f"/jobs/{job_id}").json()
    return data


def _wait_for(client, job_id, predicate, timeout=5.0):
    deadline = time.time() + timeout
    data = client.get(f"/jobs/{job_id}").json()
    while not predicate(data) and time.time() < deadline:
        time.sleep(0.02)
        data = client.get(f"/jobs/{job_id}").json()
    return data


class _OOM(RuntimeError):
    """A RuntimeError that reads as CUDA OOM (no real torch required)."""

    def __init__(self):
        super().__init__("CUDA out of memory. Tried to allocate 2.00 GiB")


# ---------------------------------------------------------------------------
# Health / CPML gate
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_ok_when_cpml_accepted(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_health_503_when_cpml_unset(self, monkeypatch):
        import main as svc

        monkeypatch.delenv("XTTS_ACCEPT_CPML", raising=False)
        try:
            with TestClient(svc.app) as c:  # re-runs lifespan with env unset
                resp = c.get("/health")
                assert resp.status_code == 503
                data = resp.json()
                assert data["error"] == "cpml_not_accepted"
                assert "message" in data and "detail" in data
        finally:
            svc._startup_error = None


# ---------------------------------------------------------------------------
# Fine-tune flow
# ---------------------------------------------------------------------------


class TestFinetune:
    def test_finetune_completes_with_result_passthrough(self, client):
        with patch("engine.vram_available_gb", return_value=24.0), patch(
            "engine.finetune", return_value=_RESULT
        ):
            body = _finetune_body()
            resp = client.post("/jobs", json=body)
            assert resp.status_code == 202
            assert resp.json() == {"job_id": body["job_id"]}
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "complete"
        assert data["result"] == _RESULT
        assert data["error"] is None
        assert data["progress"]["phase"] == "packaging"

    def test_finetune_progress_visible_mid_run(self, client):
        release = threading.Event()
        progress = {
            "phase": "training",
            "epoch": 3,
            "total_epochs": 10,
            "step": 412,
            "total_steps": 1380,
            "train_loss": 2.84,
            "eval_loss": 3.01,
            "eta_secs": 5400,
        }

        def fake_finetune(*, manifest_path, output_dir, params, progress_cb):
            progress_cb(progress)
            release.wait(timeout=5)
            return _RESULT

        with patch("engine.vram_available_gb", return_value=24.0), patch(
            "engine.finetune", side_effect=fake_finetune
        ):
            body = _finetune_body()
            client.post("/jobs", json=body)
            mid = _wait_for(
                client,
                body["job_id"],
                lambda d: isinstance(d["progress"], dict)
                and d["progress"].get("phase") == "training",
            )
            assert mid["status"] == "running"
            assert mid["progress"]["epoch"] == 3
            assert mid["progress"]["total_steps"] == 1380
            release.set()
            final = _poll_to_terminal(client, body["job_id"])

        assert final["status"] == "complete"
        assert final["result"]["final_eval_loss"] == 2.71

    def test_duplicate_job_id_returns_409(self, client):
        with patch("engine.vram_available_gb", return_value=24.0), patch(
            "engine.finetune", return_value=_RESULT
        ):
            body = _finetune_body()
            first = client.post("/jobs", json=body)
            assert first.status_code == 202
            second = client.post("/jobs", json=body)

        assert second.status_code == 409
        assert second.json()["error"] == "job_exists"

    def test_vram_preflight_failure(self, client):
        with patch("engine.vram_available_gb", return_value=8.0), patch(
            "engine.finetune"
        ) as mock_ft:
            body = _finetune_body()
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "failed"
        assert data["error"].startswith("insufficient_vram")
        mock_ft.assert_not_called()

    def test_oom_fails_loud_no_retry_with(self, client):
        """Fail loud: an OOM fails the job with a cuda_oom message and does NOT
        advertise a retry_with (no silent auto-downscale)."""
        with patch("engine.vram_available_gb", return_value=24.0), patch(
            "engine.finetune", side_effect=_OOM()
        ):
            body = _finetune_body()  # batch_size 3, grad_accum 1
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "failed"
        assert data["error"].startswith("cuda_oom")
        assert "batch_size=3" in data["error"]
        assert data["retry_with"] is None

    def test_oom_batch_size_1_fails_loud(self, client):
        with patch("engine.vram_available_gb", return_value=24.0), patch(
            "engine.finetune", side_effect=_OOM()
        ):
            params = {
                "epochs": 10,
                "batch_size": 1,
                "grad_accum": 3,
                "learning_rate": 5e-6,
                "language": "en",
                "eval_split": 0.1,
            }
            body = _finetune_body(params=params)
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "failed"
        assert data["error"].startswith("cuda_oom")
        assert data["retry_with"] is None

    def test_oom_via_systemexit_survives_and_fails_loud(self):
        """Regression: coqui's Trainer.fit() catches the OOM and calls
        sys.exit(1). That SystemExit must be caught (so the service survives),
        the OOM detected through its context chain, and the job failed loudly —
        NOT propagated (which would kill the process → lost job → orchestrator
        404)."""
        import main as svc

        def _coqui_style_exit():
            # Mirror coqui: OOM is caught, then sys.exit(1) is raised while
            # handling it, so the OOM is the SystemExit's __context__.
            try:
                raise _OOM()
            except _OOM:
                raise SystemExit(1)

        job = {"job_id": "sysexit-1", "status": "running", "retry_with": None}
        req = svc.FinetuneJob(
            job_id="sysexit-1", type="finetune",
            manifest_path="/x/dataset.json", output_dir="/x/out",
            params=svc.FinetuneParams(language="en", batch_size=2, grad_accum=1),
        )
        with patch("engine.release_cached_model"), \
             patch("engine.vram_available_gb", return_value=24.0), \
             patch("engine.finetune", side_effect=lambda *a, **k: _coqui_style_exit()):
            # Must return normally (not raise SystemExit).
            svc._run_finetune(job, req)

        assert job["status"] == "failed"
        assert job["error"].startswith("cuda_oom")
        assert job.get("retry_with") is None


# ---------------------------------------------------------------------------
# Synthesise flow
# ---------------------------------------------------------------------------


class TestSynthesise:
    def test_synthesise_completes(self, client, tmp_path):
        ref = tmp_path / "ref.wav"
        ref.write_bytes(b"RIFFstub")
        out = str(tmp_path / "out" / "preview.wav")
        result = {"output_path": out, "duration_secs": 4.2}

        with patch("engine.synthesise", return_value=result) as mock_syn:
            body = _synth_body([str(ref)], out)
            resp = client.post("/jobs", json=body)
            assert resp.status_code == 202
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "complete"
        assert data["result"] == {"output_path": out, "duration_secs": 4.2}
        mock_syn.assert_called_once()

    def test_sampling_params_forwarded_to_engine(self, client, tmp_path):
        ref = tmp_path / "ref.wav"
        ref.write_bytes(b"RIFFstub")
        out = str(tmp_path / "out" / "preview.wav")
        result = {"output_path": out, "duration_secs": 4.2}

        with patch("engine.synthesise", return_value=result) as mock_syn:
            body = _synth_body(
                [str(ref)], out,
                params={
                    "temperature": 0.9, "speed": 1.2, "repetition_penalty": 5.0,
                    "top_k": 30, "top_p": 0.7,
                },
            )
            client.post("/jobs", json=body)
            _poll_to_terminal(client, body["job_id"])

        assert mock_syn.call_args.kwargs["params"] == {
            "temperature": 0.9, "speed": 1.2, "repetition_penalty": 5.0,
            "top_k": 30, "top_p": 0.7,
            "length_penalty": 1.0, "num_beams": 1, "enable_text_splitting": True,
        }

    def test_sampling_params_default_when_omitted(self, client, tmp_path):
        ref = tmp_path / "ref.wav"
        ref.write_bytes(b"RIFFstub")
        out = str(tmp_path / "out" / "preview.wav")
        result = {"output_path": out, "duration_secs": 4.2}

        with patch("engine.synthesise", return_value=result) as mock_syn:
            body = _synth_body([str(ref)], out, params={"temperature": 0.65})
            client.post("/jobs", json=body)
            _poll_to_terminal(client, body["job_id"])

        assert mock_syn.call_args.kwargs["params"] == {
            "temperature": 0.65, "speed": 1.0, "repetition_penalty": 10.0,
            "top_k": 50, "top_p": 0.85,
            "length_penalty": 1.0, "num_beams": 1, "enable_text_splitting": True,
        }

    def test_synthesise_missing_reference_fails(self, client, tmp_path):
        out = str(tmp_path / "out.wav")
        missing = str(tmp_path / "does_not_exist.wav")
        with patch("engine.synthesise") as mock_syn:
            body = _synth_body([missing], out)
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "failed"
        assert data["error"].startswith("reference_not_found")
        assert missing in data["error"]
        mock_syn.assert_not_called()


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}
# ---------------------------------------------------------------------------


class TestGetJob:
    def test_unknown_job_returns_404_flat(self, client):
        jid = str(uuid.uuid4())
        resp = client.get(f"/jobs/{jid}")
        assert resp.status_code == 404
        data = resp.json()
        assert data["error"] == "job_not_found"
        assert jid in data["message"]
        assert "detail" in data

    def test_get_is_idempotent(self, client):
        with patch("engine.vram_available_gb", return_value=24.0), patch(
            "engine.finetune", return_value=_RESULT
        ):
            body = _finetune_body()
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])
            r1 = client.get(f"/jobs/{body['job_id']}").json()
            r2 = client.get(f"/jobs/{body['job_id']}").json()

        assert data["status"] == "complete"
        assert r1 == r2


# ---------------------------------------------------------------------------
# Validation (flat 422)
# ---------------------------------------------------------------------------


class TestValidation:
    def test_bad_type_returns_422_flat(self, client):
        body = _finetune_body(type="bogus")
        resp = client.post("/jobs", json=body)
        assert resp.status_code == 422
        data = resp.json()
        assert data["error"] == "validation_error"
        assert not isinstance(data["detail"], list)

    def test_empty_reference_wavs_returns_422(self, client, tmp_path):
        body = _synth_body([], str(tmp_path / "out.wav"))
        resp = client.post("/jobs", json=body)
        assert resp.status_code == 422
        assert resp.json()["error"] == "validation_error"

    def test_post_jobs_503_when_cpml_unset(self, monkeypatch, tmp_path):
        import main as svc

        monkeypatch.delenv("XTTS_ACCEPT_CPML", raising=False)
        try:
            with TestClient(svc.app) as c:  # re-runs lifespan with env unset
                body = _synth_body(["/data/ref.wav"], str(tmp_path / "out.wav"))
                resp = c.post("/jobs", json=body)
                assert resp.status_code == 503
                assert resp.json()["error"] == "cpml_not_accepted"
        finally:
            svc._startup_error = None
