"""Shared pytest fixtures for orchestrator tests."""

import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Put the orchestrator source on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Each test gets its own DATA_DIR so databases don't leak between tests."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Clear the connection cache so old connections aren't reused
    import db
    db._connections.clear()
    # Also clear the job queues and runners
    import jobs
    jobs._queues.clear()
    jobs._runners.clear()
    jobs._project_locks.clear()
    yield tmp_path


@pytest.fixture
def client():
    """TestClient with the full FastAPI app, using lifespan=False so startup
    tasks (health polling) don't run during unit tests."""
    from main import app
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture
def test_wav():
    return FIXTURES_DIR / "test_audio.wav"


@pytest.fixture
def test_wav_short():
    return FIXTURES_DIR / "test_audio_short.wav"


@pytest.fixture
def project(client):
    """Create a project and return its ID."""
    resp = client.post("/projects", json={"name": "Test Project"})
    assert resp.status_code == 201
    return resp.json()["id"]
