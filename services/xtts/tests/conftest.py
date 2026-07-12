"""Shared fixtures for the XTTS service tests.

The engine module (torch / Coqui-TTS) is never imported here: the API-layer
tests patch ``engine.finetune`` / ``engine.synthesise`` / ``engine.vram_
available_gb`` as module attributes, so the suite runs without any ML deps.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

# Accept the CPML before the app's lifespan runs so /health reports healthy for
# the job-flow tests. The dedicated health tests override this per-test.
os.environ["XTTS_ACCEPT_CPML"] = "1"


@pytest.fixture(scope="module")
def client():
    """Module-scoped TestClient with a clean job store."""
    import main as svc

    svc._jobs.clear()
    svc._startup_error = None
    with TestClient(svc.app) as c:
        yield c
    svc._jobs.clear()


@pytest.fixture
def manifest_file(tmp_path):
    """Factory writing a dataset manifest and returning its path."""

    def _write(segments: list[dict]) -> str:
        path = str(tmp_path / "dataset.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "version": "1",
                    "project_id": "p1",
                    "speaker": "target",
                    "segments": segments,
                },
                fh,
            )
        return path

    return _write
