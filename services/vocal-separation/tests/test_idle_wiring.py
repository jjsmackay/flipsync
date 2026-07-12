"""Live wiring tests: the lifespan-started idle watcher actually releases a
resident model against the running app, and the env switch disables it.

These drive the real FastAPI lifespan (via TestClient) and the real single-worker
executor — only Demucs preload is mocked so no GPU/torch is needed.
"""

import os
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def idle_env():
    """Set IDLE_UNLOAD_SECS for the test and restore it afterwards."""
    saved = os.environ.get("IDLE_UNLOAD_SECS")

    def _set(value: str):
        os.environ["IDLE_UNLOAD_SECS"] = value

    yield _set

    if saved is None:
        os.environ.pop("IDLE_UNLOAD_SECS", None)
    else:
        os.environ["IDLE_UNLOAD_SECS"] = saved


def test_watcher_disabled_when_idle_secs_zero(idle_env):
    import main as svc

    idle_env("0")
    svc._jobs.clear()
    with patch("separator.preload_models"):
        with TestClient(svc.app):
            assert svc._unloader is None
            assert svc._idle_watch_task is None


def test_watcher_releases_resident_model_when_idle(idle_env):
    import main as svc
    import separator as sep

    idle_env("1")
    svc._jobs.clear()
    with patch("separator.preload_models"):
        with TestClient(svc.app):
            assert svc._unloader is not None
            # Simulate a model sitting resident in VRAM.
            sep._model_cache["htdemucs"] = object()

            # The watcher (1s idle window, ~1s poll) should free it.
            deadline = time.time() + 6.0
            while time.time() < deadline and sep.is_model_loaded():
                time.sleep(0.2)

            assert sep.is_model_loaded() is False
    sep._model_cache.clear()
