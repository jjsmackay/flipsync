"""Review-fix wave 2 (B3): duplicate POST /jobs must return 409 job_exists.

A duplicate submit (e.g. an orchestrator retry after a timed-out but delivered
POST) must not restart or overwrite the original job's in-memory state.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    with patch("separator.separate", return_value=None), \
         patch("separator.preload_models", return_value=None):
        import main as svc
        svc._jobs.clear()
        with TestClient(svc.app) as c:
            yield c
        svc._jobs.clear()


def _body(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "input_path": "/data/projects/p1/audio/raw/s1.wav",
        "output_path": "/data/projects/p1/audio/vocals/s1.wav",
        "model": "htdemucs",
        "chunk_secs": None,
    }


def test_duplicate_job_id_returns_409_job_exists(client):
    import main as svc

    job_id = str(uuid.uuid4())
    first = client.post("/jobs", json=_body(job_id))
    assert first.status_code == 202

    second = client.post("/jobs", json=_body(job_id))
    assert second.status_code == 409
    body = second.json()
    assert body["error"] == "job_exists"
    assert body["message"]
    assert body["detail"] == {}
    assert job_id in svc._jobs


def test_duplicate_does_not_overwrite_existing_job_state(client):
    import main as svc

    job_id = str(uuid.uuid4())
    sentinel = {"job_id": job_id, "status": "running", "progress": 42, "sentinel": True}
    svc._jobs[job_id] = sentinel

    resp = client.post("/jobs", json=_body(job_id))
    assert resp.status_code == 409
    assert resp.json()["error"] == "job_exists"
    # The original job dict is untouched — not replaced, not restarted.
    assert svc._jobs[job_id] is sentinel
    assert svc._jobs[job_id]["progress"] == 42


def test_fresh_job_id_still_accepted(client):
    resp = client.post("/jobs", json=_body(str(uuid.uuid4())))
    assert resp.status_code == 202
