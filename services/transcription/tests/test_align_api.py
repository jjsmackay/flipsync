"""Task C4: POST /jobs accepts an `align` flag (default False)."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_jobs():
    """Clear in-memory job store between tests."""
    import main as svc
    svc._jobs.clear()
    svc._job_locks.clear()
    yield
    svc._jobs.clear()
    svc._job_locks.clear()


@pytest.fixture()
def client():
    from main import app
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_jobs_accepts_align_flag(client):
    body = {
        "job_id": "j-align",
        "segments": [{"id": "s1", "wav_path": "/nonexistent.wav"}],
        "model": "large-v2",
        "align": True,
    }
    r = client.post("/jobs", json=body)
    assert r.status_code == 202


def test_jobrequest_align_defaults_false():
    from main import JobRequest

    req = JobRequest(job_id="j", segments=[])
    assert req.align is False

    req2 = JobRequest(job_id="j2", segments=[], align=True)
    assert req2.align is True
