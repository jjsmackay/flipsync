"""Live wiring tests: the lifespan-started idle watcher actually releases the
resident pyannote models against the running app, and the env switch disables it.

Only model loading is mocked — no GPU/torch needed.
"""

import os
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def idle_env():
    saved = os.environ.get("IDLE_UNLOAD_SECS")

    def _set(value: str):
        os.environ["IDLE_UNLOAD_SECS"] = value

    yield _set

    if saved is None:
        os.environ.pop("IDLE_UNLOAD_SECS", None)
    else:
        os.environ["IDLE_UNLOAD_SECS"] = saved


def test_watcher_disabled_when_idle_secs_zero(idle_env):
    import main

    idle_env("0")
    main._jobs.clear()
    with patch("main._load_models"):
        with TestClient(main.app):
            assert main._unloader is None
            assert main._idle_watch_task is None


def test_watcher_releases_resident_models_when_idle(idle_env):
    import main

    idle_env("1")
    main._jobs.clear()
    with patch("main._load_models"):
        with TestClient(main.app):
            assert main._unloader is not None
            # Simulate pyannote models sitting resident in VRAM.
            main._pipeline = object()
            main._embedding_model = object()

            deadline = time.time() + 6.0
            while time.time() < deadline and main._is_loaded():
                time.sleep(0.2)

            assert main._is_loaded() is False
    main._pipeline = None
    main._embedding_model = None
