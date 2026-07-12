"""Review-fix wave 2 (B3 + B5) — diarisation service.

B5: _evict_finished_jobs must be a no-op while the finished-job count is at or
below the cap. Without the guard, a negative excess slices from the END of the
list (finished[:-1]) and evicts almost every finished job.

B3: duplicate POST /jobs must return 409 job_exists instead of overwriting the
original job and spawning a second GPU task.
"""

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    with patch("main._load_models"):
        from main import app
        with TestClient(app) as c:
            yield c


@pytest.fixture(autouse=True)
def clear_jobs():
    import main
    main._jobs.clear()
    yield
    main._jobs.clear()


def _seed_jobs(n: int, status: str = "complete", prefix: str = "job") -> list[str]:
    import main
    ids = []
    for i in range(n):
        jid = f"{prefix}-{i}"
        main._jobs[jid] = {"job_id": jid, "status": status, "progress": 100}
        ids.append(jid)
    return ids


# ---------------------------------------------------------------------------
# B5 — eviction guard
# ---------------------------------------------------------------------------


def test_eviction_noop_below_cap():
    """len(finished) < cap must evict nothing (regression: negative slice)."""
    import main

    ids = _seed_jobs(main._MAX_FINISHED_JOBS - 1)
    main._evict_finished_jobs()
    assert len(main._jobs) == main._MAX_FINISHED_JOBS - 1
    assert set(main._jobs) == set(ids)


def test_eviction_noop_at_cap():
    import main

    ids = _seed_jobs(main._MAX_FINISHED_JOBS)
    main._evict_finished_jobs()
    assert set(main._jobs) == set(ids)


def test_eviction_drops_only_oldest_beyond_cap():
    import main

    ids = _seed_jobs(main._MAX_FINISHED_JOBS + 5)
    main._evict_finished_jobs()
    # The 5 oldest finished jobs go; the newest cap-many stay.
    assert set(main._jobs) == set(ids[5:])


def test_eviction_retains_running_jobs():
    import main

    running = _seed_jobs(3, status="running", prefix="run")
    finished = _seed_jobs(main._MAX_FINISHED_JOBS + 2, prefix="done")
    main._evict_finished_jobs()
    assert all(jid in main._jobs for jid in running)
    assert set(main._jobs) == set(running) | set(finished[2:])


# ---------------------------------------------------------------------------
# B3 — duplicate POST /jobs guard
# ---------------------------------------------------------------------------


def _job_body(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "input_path": "/data/projects/p1/audio/vocals/s1.wav",
        "reference_path": "/data/projects/p1/reference.wav",
        "output_dir": "/data/projects/p1/segments/raw",
    }


def test_duplicate_job_id_returns_409_job_exists(client):
    import main

    job_id = str(uuid.uuid4())
    with patch("main._run_job"):
        first = client.post("/jobs", json=_job_body(job_id))
        assert first.status_code == 202

        second = client.post("/jobs", json=_job_body(job_id))

    assert second.status_code == 409
    body = second.json()
    assert body["error"] == "job_exists"
    assert body["message"]
    assert body["detail"] == {}
    assert job_id in main._jobs


def test_duplicate_does_not_overwrite_existing_job_state(client):
    import main

    job_id = str(uuid.uuid4())
    sentinel = {"job_id": job_id, "status": "running", "progress": 42, "sentinel": True}
    main._jobs[job_id] = sentinel

    with patch("main._run_job"):
        resp = client.post("/jobs", json=_job_body(job_id))

    assert resp.status_code == 409
    assert main._jobs[job_id] is sentinel


def test_fresh_job_id_still_accepted(client):
    with patch("main._run_job"):
        resp = client.post("/jobs", json=_job_body(str(uuid.uuid4())))
    assert resp.status_code == 202
