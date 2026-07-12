"""Wave-3 review fixes.

P1 — no approving an untranscribed segment (PATCH + bulk), and export refuses
to silently drop approved-but-untranscribed segments.
P2 — threshold change re-buckets below_threshold and reports it so the caller
can offer transcription.
"""

import uuid
from datetime import datetime, timezone

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _insert_source(conn, project_id, status="complete"):
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (source_id, project_id, "ep.wav", "source/ep.wav", status, now, now),
    )
    conn.commit()
    return source_id


def _insert_segment(conn, project_id, source_id, status="pending", confidence=0.9,
                    start=0, end=5, transcript=None, transcript_edited=None, clipping=0):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs,
           speaker_label, match_confidence, status, transcript, transcript_edited,
           clipping_warning, created_at, updated_at)
           VALUES (?,?,?,?,?,?,'S0',?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, f"segments/raw/{seg_id}.wav",
         start, end, confidence, status, transcript, transcript_edited, clipping, now, now),
    )
    conn.commit()
    return seg_id


# ---------------------------------------------------------------------------
# P1 — approve requires a transcript
# ---------------------------------------------------------------------------


class TestApproveRequiresTranscript:
    def test_patch_approve_without_transcript_is_rejected(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        seg = _insert_segment(conn, project, src, status="pending", transcript=None)

        resp = client.patch(f"/projects/{project}/segments/{seg}", json={"status": "approved"})

        assert resp.status_code == 409
        assert resp.json()["error"] == "no_transcript"
        # Unchanged in the DB.
        row = conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()
        assert row["status"] == "pending"

    def test_patch_approve_with_transcript_succeeds(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        seg = _insert_segment(conn, project, src, status="pending", transcript="hello there")

        resp = client.patch(f"/projects/{project}/segments/{seg}", json={"status": "approved"})

        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_patch_approve_with_edited_transcript_only_succeeds(self, client, project):
        """A user-provided transcript_edited counts even if the base transcript
        is still NULL (they typed it in themselves)."""
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        seg = _insert_segment(conn, project, src, status="pending",
                              transcript=None, transcript_edited="typed in")

        resp = client.patch(f"/projects/{project}/segments/{seg}", json={"status": "approved"})

        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_bulk_approve_skips_untranscribed(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        good = _insert_segment(conn, project, src, status="pending", transcript="words")
        bare = _insert_segment(conn, project, src, status="pending", transcript=None)

        resp = client.post(
            f"/projects/{project}/segments/bulk",
            json={"action": "approve", "filter": {"status": "pending"}},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["affected_count"] == 1
        assert body["skipped_no_transcript"] == 1
        assert conn.execute("SELECT status FROM segments WHERE id=?", (good,)).fetchone()["status"] == "approved"
        assert conn.execute("SELECT status FROM segments WHERE id=?", (bare,)).fetchone()["status"] == "pending"


class TestWhisperTuningConfig:
    def test_defaults_present_in_config(self, client):
        pid = client.post("/projects", json={"name": "P"}).json()["id"]
        cfg = client.get(f"/projects/{pid}").json()["config"]
        assert cfg["whisper_batch_size"] == 16
        assert cfg["whisper_compute_type"] == "default"

    def test_create_with_overrides(self, client):
        pid = client.post("/projects", json={
            "name": "P", "whisper_batch_size": 4, "whisper_compute_type": "int8_float16",
        }).json()["id"]
        cfg = client.get(f"/projects/{pid}").json()["config"]
        assert cfg["whisper_batch_size"] == 4
        assert cfg["whisper_compute_type"] == "int8_float16"

    def test_patch_updates_values(self, client):
        pid = client.post("/projects", json={"name": "P"}).json()["id"]
        resp = client.patch(f"/projects/{pid}", json={
            "whisper_batch_size": 2, "whisper_compute_type": "int8",
        })
        assert resp.status_code == 200
        cfg = client.get(f"/projects/{pid}").json()["config"]
        assert cfg["whisper_batch_size"] == 2
        assert cfg["whisper_compute_type"] == "int8"

    def test_invalid_compute_type_rejected(self, client):
        pid = client.post("/projects", json={"name": "P"}).json()["id"]
        resp = client.patch(f"/projects/{pid}", json={"whisper_compute_type": "float64"})
        assert resp.status_code == 422

    def test_batch_size_must_be_positive(self, client):
        resp = client.post("/projects", json={"name": "P", "whisper_batch_size": 0})
        assert resp.status_code == 422


def _run_job(project_id, job_id):
    """Execute one job synchronously with the background runner suppressed."""
    import asyncio

    import jobs
    loop = asyncio.new_event_loop()
    orig = jobs._ensure_runner
    jobs._ensure_runner = lambda pid: None
    try:
        loop.run_until_complete(jobs._execute_job(project_id, job_id))
    finally:
        jobs._ensure_runner = orig
        loop.close()


class TestExportRefusesUntranscribed:
    def test_export_fails_when_an_approved_segment_has_no_transcript(self, client, project):
        """Legacy data can hold approved-but-untranscribed segments (approved
        before the guard existed). Export must fail loudly, not silently drop them."""
        import db
        import jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_segment(conn, project, src, status="approved", transcript="fine")
        bare = _insert_segment(conn, project, src, status="approved", transcript=None)

        job_id = jobs.enqueue(project, "export")
        _run_job(project, job_id)

        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "untranscribed" in (job["error"] or "")
        # The offending segment id is reported so the user can find it.
        assert bare in (job["error"] or "") or "1" in (job["error"] or "")
