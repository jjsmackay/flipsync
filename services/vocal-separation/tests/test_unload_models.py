"""Tests for separator.unload_models() — dropping the cached Demucs model."""

import separator as sep


def test_unload_models_clears_cache():
    sep._model_cache["htdemucs"] = object()  # stand-in for a loaded model
    assert sep.is_model_loaded() is True

    sep.unload_models()

    assert sep._model_cache == {}
    assert sep.is_model_loaded() is False


def test_unload_models_is_safe_when_empty():
    sep._model_cache.clear()
    # Must not raise even with nothing loaded and no CUDA available.
    sep.unload_models()
    assert sep.is_model_loaded() is False
