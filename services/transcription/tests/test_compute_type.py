"""Tests for the whisper compute_type override in load_model.

faster_whisper / ctranslate2 aren't installed in CI, so we inject fake modules
(load_model imports them lazily inside the function).
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

import transcriber


@pytest.fixture
def fake_whisper():
    """Install fake faster_whisper + ctranslate2 modules; yield the WhisperModel
    mock so tests can inspect how it was constructed. cuda count is settable."""
    state = {"cuda": 1}
    model_ctor = MagicMock(return_value=MagicMock())

    fw = types.ModuleType("faster_whisper")
    fw.WhisperModel = model_ctor
    ct = types.ModuleType("ctranslate2")
    ct.get_cuda_device_count = lambda: state["cuda"]

    saved = {k: sys.modules.get(k) for k in ("faster_whisper", "ctranslate2")}
    sys.modules["faster_whisper"] = fw
    sys.modules["ctranslate2"] = ct
    transcriber._model_cache = {"model": None, "model_size": None, "compute_type": None}
    try:
        yield model_ctor, state
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        transcriber._model_cache = {"model": None, "model_size": None, "compute_type": None}


def test_default_derives_float16_on_gpu(fake_whisper):
    model_ctor, _ = fake_whisper
    transcriber.load_model("large-v2", compute_type="default")
    kwargs = model_ctor.call_args.kwargs
    assert kwargs["compute_type"] == "float16"
    assert kwargs["device"] == "cuda"


def test_default_derives_int8_on_cpu(fake_whisper):
    model_ctor, state = fake_whisper
    state["cuda"] = 0
    transcriber.load_model("large-v2", compute_type="default")
    kwargs = model_ctor.call_args.kwargs
    assert kwargs["compute_type"] == "int8"
    assert kwargs["device"] == "cpu"


def test_explicit_compute_type_is_passed_through(fake_whisper):
    model_ctor, _ = fake_whisper
    transcriber.load_model("large-v2", compute_type="int8_float16")
    assert model_ctor.call_args.kwargs["compute_type"] == "int8_float16"


def test_cache_reloads_when_compute_type_changes(fake_whisper):
    model_ctor, _ = fake_whisper
    transcriber.load_model("large-v2", compute_type="float16")
    transcriber.load_model("large-v2", compute_type="float16")  # cache hit
    assert model_ctor.call_count == 1
    transcriber.load_model("large-v2", compute_type="int8_float16")  # changed → reload
    assert model_ctor.call_count == 2


def test_api_rejects_invalid_compute_type():
    from fastapi.testclient import TestClient

    import main
    with TestClient(main.app) as client:
        resp = client.post("/jobs", json={
            "job_id": "j1",
            "segments": [{"id": "s1", "wav_path": "/tmp/x.wav"}],
            "model": "large-v2",
            "compute_type": "float64",
        })
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"
