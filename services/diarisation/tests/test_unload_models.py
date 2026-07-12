"""Tests for the diarisation service's model unload (drops pyannote globals)."""

import main


def test_is_loaded_true_when_models_resident():
    main._pipeline = object()
    main._embedding_model = object()
    assert main._is_loaded() is True


def test_unload_models_clears_globals():
    main._pipeline = object()
    main._embedding_model = object()

    main._unload_models()

    assert main._pipeline is None
    assert main._embedding_model is None
    assert main._is_loaded() is False


def test_unload_models_safe_when_already_empty():
    main._pipeline = None
    main._embedding_model = None
    main._unload_models()  # must not raise without CUDA/torch
    assert main._is_loaded() is False


def test_unload_does_not_flip_readiness():
    """Idle-unloading must not make /health report unhealthy — readiness means
    'loaded successfully at least once', not 'resident right now'."""
    main._models_ready = True
    main._pipeline = object()
    main._embedding_model = object()

    main._unload_models()

    assert main._models_ready is True
