"""Unit tests for the engine module's torch-free surface.

These assert the public seam exists and the module imports without torch
installed — the regression being guarded is the lazy-import discipline: the
API tests patch these functions as module attributes, so a stray top-level
ML import here would break the whole CI suite. The real GPU paths are
exercised by the manual smoke script (``scripts/gpu_smoke.py``), and the
training driver by ``tests/test_finetune_driver.py`` against a fake repo.
"""

from __future__ import annotations

import os

import engine


def _expected_min_vram() -> float:
    """Mirror engine's import-time derivation so the assertion is
    env-independent (the constant is read from FINETUNE_MIN_VRAM_GB, and a
    host that sets it must not fail this suite)."""
    raw = os.environ.get("FINETUNE_MIN_VRAM_GB")
    try:
        return float(raw) if raw else 8.0
    except ValueError:
        return 8.0


def test_import_engine_requires_no_torch():
    # If this module needed torch at import time the suite would already have
    # failed to collect; assert the public seam is present regardless.
    assert engine.FINETUNE_MIN_VRAM_GB == _expected_min_vram()
    assert callable(engine.finetune)
    assert callable(engine.synthesise)
    assert callable(engine.vram_available_gb)
    assert callable(engine.release_cached_model)


def test_vram_available_gb_is_zero_without_cuda():
    # No torch installed in this test environment ⇒ falls back to 0.0 rather
    # than raising.
    assert engine.vram_available_gb() == 0.0


def test_release_cached_model_is_a_safe_no_op():
    # Nothing cached yet ⇒ must not raise even without torch installed.
    engine.release_cached_model()
    assert engine.is_model_loaded() is False
