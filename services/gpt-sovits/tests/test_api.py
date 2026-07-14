"""API contract tests for the GPT-SoVITS service.

The engine (torch / vendored GPT-SoVITS) is patched at the ``engine.finetune``
/ ``engine.synthesise`` / ``engine.vram_available_gb`` module-attribute seam,
so these tests run without torch or the vendored repo installed and never
touch a GPU. Mirrors ``services/xtts/tests/test_api.py``'s cases; the two
deliberate contract differences from xtts are covered explicitly:
  - no CPML-style acceptance gate (GPT-SoVITS weights are MIT and public)
  - ``reference_wavs`` has no minimum length — a fine-tuned GPT-SoVITS model
    supplies its own stored reference.wav/.txt, so the orchestrator submits
    synthesise jobs with an empty list for this engine.
"""

from __future__ import annotations

import threading
import time
import uuid
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Bodies + polling helpers
# ---------------------------------------------------------------------------


def _finetune_body(**over):
    body = {
        "job_id": str(uuid.uuid4()),
        "type": "finetune",
        "manifest_path": "/data/projects/p/models/m/dataset.json",
        "output_dir": "/data/projects/p/models/m",
        "params": {"sovits_epochs": 8, "gpt_epochs": 15, "batch_size": 4},
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
        "checkpoint_dir": "/data/projects/p/models/m",
        "output_path": output_path,
        "params": {"temperature": 1.0},
    }
    body.update(over)
    return body


_RESULT = {
    "checkpoint_dir": "/data/projects/p/models/m",
    "gpt_path": "/data/projects/p/models/m/gpt.ckpt",
    "sovits_path": "/data/projects/p/models/m/sovits.pth",
    "config_path": "/data/projects/p/models/m/config.json",
    "reference_wav_path": "/data/projects/p/models/m/reference.wav",
    "reference_text_path": "/data/projects/p/models/m/reference.txt",
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
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


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
            "phase": "training_sovits",
            "epoch": 3,
            "total_epochs": 8,
            "step": 45,
            "total_steps": 100,
            "train_loss": 4.02,
            "eta_secs": 1200,
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
                and d["progress"].get("phase") == "training_sovits",
            )
            assert mid["status"] == "running"
            assert mid["progress"]["epoch"] == 3
            assert mid["progress"]["total_steps"] == 100
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
        with patch("engine.vram_available_gb", return_value=2.0), patch(
            "engine.finetune"
        ) as mock_ft:
            body = _finetune_body()
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "failed"
        assert data["error"].startswith("insufficient_vram")
        mock_ft.assert_not_called()

    def test_oom_batch_size_4_reports_retry_with_halved_batch(self, client):
        with patch("engine.vram_available_gb", return_value=24.0), patch(
            "engine.finetune", side_effect=_OOM()
        ):
            body = _finetune_body()  # batch_size 4
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "failed"
        assert data["error"] == "cuda_oom"
        assert data["retry_with"] == {"batch_size": 2}

    def test_oom_batch_size_1_is_terminal(self, client):
        with patch("engine.vram_available_gb", return_value=24.0), patch(
            "engine.finetune", side_effect=_OOM()
        ):
            params = {"sovits_epochs": 8, "gpt_epochs": 15, "batch_size": 1}
            body = _finetune_body(params=params)
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "failed"
        assert data["error"] == "cuda_oom"
        assert data["retry_with"] is None

    def test_finetune_failure_reports_flat_error(self, client):
        with patch("engine.vram_available_gb", return_value=24.0), patch(
            "engine.finetune", side_effect=RuntimeError("prep stage 1 exited 1")
        ):
            body = _finetune_body()
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "failed"
        assert data["error"] == "prep stage 1 exited 1"

    def test_finetune_params_forwarded_to_engine(self, client):
        with patch("engine.vram_available_gb", return_value=24.0), patch(
            "engine.finetune", return_value=_RESULT
        ) as mock_ft:
            body = _finetune_body()
            client.post("/jobs", json=body)
            _poll_to_terminal(client, body["job_id"])

        assert mock_ft.call_args.kwargs["params"] == {
            "sovits_epochs": 8,
            "gpt_epochs": 15,
            "batch_size": 4,
        }

    def test_finetune_params_defaulted_when_omitted(self, client):
        with patch("engine.vram_available_gb", return_value=24.0), patch(
            "engine.finetune", return_value=_RESULT
        ) as mock_ft:
            body = _finetune_body(params={})
            client.post("/jobs", json=body)
            _poll_to_terminal(client, body["job_id"])

        params = mock_ft.call_args.kwargs["params"]
        assert set(params) == {"sovits_epochs", "gpt_epochs", "batch_size"}

    def test_unknown_param_keys_are_ignored_not_rejected(self, client):
        with patch("engine.vram_available_gb", return_value=24.0), patch(
            "engine.finetune", return_value=_RESULT
        ):
            body = _finetune_body(params={"sovits_epochs": 8, "some_future_knob": 99})
            resp = client.post("/jobs", json=body)
            assert resp.status_code == 202
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "complete"


# ---------------------------------------------------------------------------
# Synthesise flow
# ---------------------------------------------------------------------------


class TestSynthesise:
    def test_synthesise_completes(self, client, tmp_path):
        out = str(tmp_path / "out" / "preview.wav")
        result = {"output_path": out, "duration_secs": 4.2}

        with patch("engine.synthesise", return_value=result) as mock_syn:
            body = _synth_body([], out)
            resp = client.post("/jobs", json=body)
            assert resp.status_code == 202
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "complete"
        assert data["result"] == {"output_path": out, "duration_secs": 4.2}
        mock_syn.assert_called_once()

    def test_empty_reference_wavs_is_accepted(self, client, tmp_path):
        """Deliberate deviation from xtts: a fine-tuned GPT-SoVITS bundle
        supplies its own stored reference.wav/.txt, so the orchestrator sends
        an empty reference_wavs list for this engine — it must not 422."""
        out = str(tmp_path / "out.wav")
        with patch("engine.synthesise", return_value={"output_path": out, "duration_secs": 1.0}):
            body = _synth_body([], out)
            resp = client.post("/jobs", json=body)
            assert resp.status_code == 202

    def test_synthesise_missing_reference_fails(self, client, tmp_path):
        """The reference-existence check is a no-op for the common (empty
        reference_wavs) case, but stays live code for any future
        aux_ref_audio_paths-style use — cover it directly rather than leaving
        it untested."""
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

    def test_sampling_params_forwarded_to_engine(self, client, tmp_path):
        out = str(tmp_path / "out" / "preview.wav")
        result = {"output_path": out, "duration_secs": 4.2}

        with patch("engine.synthesise", return_value=result) as mock_syn:
            body = _synth_body(
                [], out,
                params={
                    "temperature": 0.8, "speed": 1.1, "repetition_penalty": 1.5,
                    "top_k": 20, "top_p": 0.9,
                },
            )
            client.post("/jobs", json=body)
            _poll_to_terminal(client, body["job_id"])

        assert mock_syn.call_args.kwargs["params"] == {
            "temperature": 0.8, "speed": 1.1, "repetition_penalty": 1.5,
            "top_k": 20, "top_p": 0.9,
        }

    def test_sampling_params_default_when_omitted(self, client, tmp_path):
        out = str(tmp_path / "out" / "preview.wav")
        result = {"output_path": out, "duration_secs": 4.2}

        with patch("engine.synthesise", return_value=result) as mock_syn:
            body = _synth_body([], out, params={})
            client.post("/jobs", json=body)
            _poll_to_terminal(client, body["job_id"])

        assert mock_syn.call_args.kwargs["params"] == {
            "temperature": 1.0, "speed": 1.0, "repetition_penalty": 1.35,
            "top_k": 15, "top_p": 1.0,
        }

    def test_synthesise_failure_reports_flat_error(self, client, tmp_path):
        out = str(tmp_path / "out.wav")
        with patch("engine.synthesise", side_effect=RuntimeError("bundle missing reference.wav")):
            body = _synth_body([], out)
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "failed"
        assert data["error"] == "bundle missing reference.wav"


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

    def test_missing_required_field_returns_422(self, client, tmp_path):
        body = _synth_body([], str(tmp_path / "out.wav"))
        del body["text"]
        resp = client.post("/jobs", json=body)
        assert resp.status_code == 422
        assert resp.json()["error"] == "validation_error"
