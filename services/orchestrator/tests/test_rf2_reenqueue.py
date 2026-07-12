"""Reprocess accepts a source already at the target pending status (re-enqueue).

A job that fails before its handler runs — e.g. a service-readiness timeout —
leaves the source at separation_pending/diarisation_pending. Retrying via the
reprocess endpoint must re-enqueue rather than 409 on a self-transition.
"""

import uuid
from datetime import datetime, timezone

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _make_project_with_source(client, status):
    resp = client.post("/projects", json={"name": "reenqueue-test"})
    assert resp.status_code == 201
    project_id = resp.json()["id"]

    import db

    conn = db.get_conn(project_id)
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (source_id, project_id, "a.mp4", "source/a.mp4", status, now, now),
    )
    conn.commit()
    return project_id, source_id


@pytest.mark.parametrize(
    ("status", "steps", "job_type"),
    [
        ("separation_pending", ["separation"], "vocal_separation"),
        ("diarisation_pending", ["diarisation"], "diarisation"),
    ],
)
def test_reprocess_reenqueues_from_target_pending_status(client, status, steps, job_type):
    project_id, source_id = _make_project_with_source(client, status)

    resp = client.post(
        f"/projects/{project_id}/sources/{source_id}/reprocess",
        json={"steps": steps},
    )
    assert resp.status_code == 202, resp.text
    enqueued = resp.json()["enqueued_jobs"]
    assert [j["type"] for j in enqueued] == [job_type]

    import db

    conn = db.get_conn(project_id)
    row = conn.execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
    assert row["status"] == status


def test_reprocess_still_rejects_genuinely_invalid_status(client):
    project_id, source_id = _make_project_with_source(client, "extraction_failed")

    resp = client.post(
        f"/projects/{project_id}/sources/{source_id}/reprocess",
        json={"steps": ["separation"]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "invalid_source_status"
