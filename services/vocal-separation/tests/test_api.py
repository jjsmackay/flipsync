"""API tests for the vocal-separation service.

Demucs is mocked — these tests verify HTTP contracts only.
"""

from __future__ import annotations

import asyncio
import time
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _poll_to_terminal(client, job_id, timeout=5.0):
    """Poll a job until it leaves the 'running' state or the timeout elapses."""
    deadline = time.time() + timeout
    data = client.get(f"/jobs/{job_id}").json()
    while data["status"] == "running" and time.time() < deadline:
        time.sleep(0.02)
        data = client.get(f"/jobs/{job_id}").json()
    return data


def _job_body(**overrides):
    body = {
        "job_id": str(uuid.uuid4()),
        "input_path": "/data/projects/p/audio/raw/s.wav",
        "output_path": "/data/projects/p/audio/vocals/s.wav",
        "model": "htdemucs",
        "chunk_secs": None,
    }
    body.update(overrides)
    return body


class _OOM(RuntimeError):
    """A RuntimeError that reads as a CUDA OOM (no real torch required)."""

    def __init__(self):
        super().__init__("CUDA out of memory. Tried to allocate 2.00 GiB")


# ---------------------------------------------------------------------------
# OOM / chunk-retry state machine
# ---------------------------------------------------------------------------


class TestOOMStateMachine:
    """The whole-file → chunked-retry → fail path, exercised via mocked separate.

    OOM is simulated with a message-matched RuntimeError so these tests do not
    require torch or a GPU (and also cover the generic-RuntimeError case in V3).
    """

    def test_whole_file_oom_then_chunked_success_completes(self, client):
        calls = []

        def side_effect(*, chunk_secs, **kwargs):
            calls.append(chunk_secs)
            if chunk_secs is None:
                raise _OOM()  # whole-file OOMs
            return None  # chunked retry succeeds

        with patch("separator.separate", side_effect=side_effect):
            body = _job_body()
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "complete"
        assert data["error"] is None
        assert data["retry_with_chunk_secs"] is None
        assert calls == [None, 60]  # whole-file, then internal 60s chunk retry

    def test_chunked_retry_oom_returns_null_retry(self, client):
        """After the internal chunked retry also OOMs, retry_with_chunk_secs is
        null — never the chunk size that just failed (V2)."""

        def side_effect(**kwargs):
            raise _OOM()

        with patch("separator.separate", side_effect=side_effect):
            body = _job_body()
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "failed"
        assert data["error"] == "cuda_oom"
        assert data["retry_with_chunk_secs"] is None
        assert data["message"]  # V4: message present in poll response

    def test_explicit_chunk_secs_oom_returns_null_retry(self, client):
        """A job submitted with explicit chunk_secs that OOMs must not ask for a
        retry at the same (failed) chunk size."""

        def side_effect(**kwargs):
            raise _OOM()

        with patch("separator.separate", side_effect=side_effect):
            body = _job_body(chunk_secs=60)
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "failed"
        assert data["error"] == "cuda_oom"
        assert data["retry_with_chunk_secs"] is None

    def test_generic_runtime_error_oom_triggers_chunked_path(self, client):
        """A CUDA OOM surfacing as a plain RuntimeError (not OutOfMemoryError)
        must reach the chunked-retry path, not processing_error (V3)."""
        calls = []

        def side_effect(*, chunk_secs, **kwargs):
            calls.append(chunk_secs)
            if chunk_secs is None:
                raise RuntimeError("CUBLAS_STATUS_ALLOC_FAILED")
            return None

        with patch("separator.separate", side_effect=side_effect):
            body = _job_body()
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert calls == [None, 60]
        assert data["status"] == "complete"
        assert data["error"] is None

    def test_non_oom_error_is_processing_error_with_message(self, client):
        """A non-OOM failure is reported as processing_error with a message and
        no futile retry signal."""

        def side_effect(**kwargs):
            raise ValueError("corrupt input")

        with patch("separator.separate", side_effect=side_effect):
            body = _job_body()
            client.post("/jobs", json=body)
            data = _poll_to_terminal(client, body["job_id"])

        assert data["status"] == "failed"
        assert data["error"] == "processing_error"
        assert "corrupt input" in data["message"]
        assert data["retry_with_chunk_secs"] is None


# ---------------------------------------------------------------------------
# Validation error format (V5)
# ---------------------------------------------------------------------------


class TestValidationErrorFormat:
    def test_422_uses_flat_error_format(self, client):
        resp = client.post("/jobs", json={"input_path": "/x", "output_path": "/y"})
        assert resp.status_code == 422
        data = resp.json()
        assert data["error"] == "validation_error"
        assert "message" in data
        assert "detail" in data
        # Must NOT be FastAPI's default top-level list-in-detail shape.
        assert not isinstance(data["detail"], list)


# ---------------------------------------------------------------------------
# Preload failure surfaces via /health (V1)
# ---------------------------------------------------------------------------


class TestPreloadHealth:
    def test_preload_failure_makes_health_unhealthy(self):
        """If model preload raises at startup, /health reports 503 with the flat
        error format instead of silently reporting ok."""
        import main as svc

        def boom(names):
            raise RuntimeError("torch>=2.6 broke demucs checkpoint load")

        with patch("separator.preload_models", side_effect=boom):
            try:
                with TestClient(svc.app) as c:  # triggers lifespan → preload
                    resp = c.get("/health")
                    assert resp.status_code == 503
                    data = resp.json()
                    assert data["error"] == "model_preload_failed"
                    assert "message" in data and "detail" in data
            finally:
                svc._preload_error = None  # reset module state for other tests

    def test_healthy_when_preload_succeeds(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
