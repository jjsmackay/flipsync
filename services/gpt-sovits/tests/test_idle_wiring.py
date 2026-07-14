"""Live wiring tests: the lifespan-started idle watcher actually releases a
resident synthesis model against the running app, and the env switch disables
it. Drives the real FastAPI lifespan (via TestClient) and the real
single-worker executor — residency is simulated on engine._model_cache so no
GPU/torch is needed.
"""

from __future__ import annotations

import os
import time

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
    with TestClient(svc.app):
        assert svc._unloader is None
        assert svc._idle_watch_task is None


def test_watcher_releases_resident_model_when_idle(idle_env):
    import engine
    import main as svc

    idle_env("1")
    svc._jobs.clear()
    with TestClient(svc.app):
        assert svc._unloader is not None
        # Simulate a synthesis model sitting resident in VRAM.
        engine._model_cache = ("/data/projects/p/models/m", object())

        # The watcher (1s idle window, ~1s poll) should free it.
        deadline = time.time() + 6.0
        while time.time() < deadline and engine.is_model_loaded():
            time.sleep(0.2)

        assert engine.is_model_loaded() is False
