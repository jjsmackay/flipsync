"""API tests for the vocal-separation service.

Demucs is mocked — these tests verify HTTP contracts only.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


# We need to patch separator.separate BEFORE importing main so that
# the TestClient never tries to run real Demucs.
@pytest.fixture(scope="module")
def client():
    """Return a TestClient with separator.separate patched to a no-op."""
    with patch("separator.separate") as mock_sep, \
         patch("separator.preload_models") as mock_preload:
        mock_sep.return_value = None
        mock_preload.return_value = None
        # Import main after patching so background tasks use the mock
        import main as svc
        # Clear any leftover jobs between test runs
        svc._jobs.clear()
        with TestClient(svc.app) as c:
            yield c
        svc._jobs.clear()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_body(self, client):
        resp = client.get("/health")
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /jobs
# ---------------------------------------------------------------------------

class TestSubmitJob:
    def _valid_body(self):
        return {
            "job_id": str(uuid.uuid4()),
            "input_path": "/data/projects/p1/audio/raw/s1.wav",
            "output_path": "/data/projects/p1/audio/vocals/s1.wav",
            "model": "htdemucs",
            "chunk_secs": None,
        }

    def test_submit_returns_202(self, client):
        body = self._valid_body()
        resp = client.post("/jobs", json=body)
        assert resp.status_code == 202

    def test_submit_returns_job_id(self, client):
        body = self._valid_body()
        resp = client.post("/jobs", json=body)
        data = resp.json()
        assert "job_id" in data
        assert data["job_id"] == body["job_id"]

    def test_submit_with_chunk_secs(self, client):
        body = self._valid_body()
        body["chunk_secs"] = 60
        resp = client.post("/jobs", json=body)
        assert resp.status_code == 202

    def test_submit_mdx_extra_model(self, client):
        body = self._valid_body()
        body["model"] = "mdx_extra"
        resp = client.post("/jobs", json=body)
        assert resp.status_code == 202

    def test_submit_missing_job_id_returns_422(self, client):
        body = {
            "input_path": "/data/in.wav",
            "output_path": "/data/out.wav",
            "model": "htdemucs",
        }
        resp = client.post("/jobs", json=body)
        assert resp.status_code == 422

    def test_submit_missing_input_path_returns_422(self, client):
        body = {
            "job_id": str(uuid.uuid4()),
            "output_path": "/data/out.wav",
            "model": "htdemucs",
        }
        resp = client.post("/jobs", json=body)
        assert resp.status_code == 422

    def test_submit_missing_output_path_returns_422(self, client):
        body = {
            "job_id": str(uuid.uuid4()),
            "input_path": "/data/in.wav",
            "model": "htdemucs",
        }
        resp = client.post("/jobs", json=body)
        assert resp.status_code == 422

    def test_submit_invalid_model_returns_422(self, client):
        body = self._valid_body()
        body["model"] = "unknown_model"
        resp = client.post("/jobs", json=body)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}
# ---------------------------------------------------------------------------

class TestGetJob:
    def test_unknown_job_returns_404(self, client):
        resp = client.get(f"/jobs/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_unknown_job_error_body(self, client):
        jid = str(uuid.uuid4())
        resp = client.get(f"/jobs/{jid}")
        data = resp.json()
        assert data["error"] == "not_found"
        assert jid in data["message"]
        assert "detail" in data

    def test_known_job_returns_valid_status(self, client):
        body = {
            "job_id": str(uuid.uuid4()),
            "input_path": "/data/projects/p/audio/raw/s.wav",
            "output_path": "/data/projects/p/audio/vocals/s.wav",
            "model": "htdemucs",
            "chunk_secs": None,
        }
        client.post("/jobs", json=body)
        resp = client.get(f"/jobs/{body['job_id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == body["job_id"]
        assert data["status"] in {"running", "complete", "failed"}
        assert "progress" in data
        assert "output_path" in data
        assert "error" in data
        assert "retry_with_chunk_secs" in data

    def test_get_job_is_idempotent(self, client):
        """Calling GET /jobs/{id} twice returns the same state (no side effects)."""
        body = {
            "job_id": str(uuid.uuid4()),
            "input_path": "/data/projects/p/audio/raw/s.wav",
            "output_path": "/data/projects/p/audio/vocals/s.wav",
            "model": "htdemucs",
        }
        client.post("/jobs", json=body)
        r1 = client.get(f"/jobs/{body['job_id']}").json()
        r2 = client.get(f"/jobs/{body['job_id']}").json()
        assert r1["job_id"] == r2["job_id"]
        # status should not change between two reads (no mutation on read)
        assert r1["status"] == r2["status"]
