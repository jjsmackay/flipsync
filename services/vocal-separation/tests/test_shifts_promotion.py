"""Increment C: `shifts` (Demucs test-time augmentation) promoted from a
hardcoded 0 to a per-job parameter the orchestrator can set from project config.
"""

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def svc_and_mock():
    with patch("separator.separate") as mock_sep, patch("separator.preload_models"):
        mock_sep.return_value = None
        import main as svc
        svc._jobs.clear()
        yield svc, mock_sep
        svc._jobs.clear()


def test_run_separation_forwards_shifts(svc_and_mock):
    svc, mock_sep = svc_and_mock
    job = {"job_id": "j1", "input_path": "/in.wav", "dest_path": "/out.wav",
           "model": "htdemucs", "chunk_secs": None, "shifts": 3}
    svc._jobs["j1"] = {"status": "running"}
    svc._run_separation(job)
    assert mock_sep.call_args.kwargs["shifts"] == 3


def test_run_separation_chunked_forwards_shifts(svc_and_mock):
    svc, mock_sep = svc_and_mock
    job = {"job_id": "j2", "input_path": "/in.wav", "dest_path": "/out.wav",
           "model": "htdemucs", "chunk_secs": 60, "shifts": 2}
    svc._jobs["j2"] = {"status": "running"}
    svc._run_separation(job)
    assert mock_sep.call_args.kwargs["shifts"] == 2


def test_submit_accepts_shifts(svc_and_mock):
    svc, _ = svc_and_mock
    with TestClient(svc.app) as c:
        body = {"job_id": str(uuid.uuid4()), "input_path": "/in.wav",
                "output_path": "/out.wav", "model": "htdemucs",
                "chunk_secs": None, "shifts": 4}
        resp = c.post("/jobs", json=body)
        assert resp.status_code == 202


def test_shifts_defaults_to_zero(svc_and_mock):
    from main import JobRequest
    req = JobRequest(job_id="x", input_path="/a", output_path="/b")
    assert req.shifts == 0
