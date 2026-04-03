"""Tests for segment listing, audio streaming, review actions, and bulk operations."""

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
                    start=0, end=5, transcript=None, clipping=0):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs,
           speaker_label, match_confidence, status, transcript, clipping_warning, created_at, updated_at)
           VALUES (?,?,?,?,?,?,'S0',?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, f"segments/raw/{seg_id}.wav",
         start, end, confidence, status, transcript, clipping, now, now),
    )
    conn.commit()
    return seg_id


class TestListSegments:
    def test_empty_list(self, client, project):
        resp = client.get(f"/projects/{project}/segments")
        assert resp.status_code == 200
        body = resp.json()
        assert body["segments"] == []
        assert body["pagination"]["total"] == 0

    def test_default_filter_is_pending_and_maybe(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        pending_id = _insert_segment(conn, project, source_id, status="pending")
        maybe_id = _insert_segment(conn, project, source_id, status="maybe")
        _insert_segment(conn, project, source_id, status="approved")
        _insert_segment(conn, project, source_id, status="rejected")

        resp = client.get(f"/projects/{project}/segments")
        assert resp.status_code == 200
        ids = {s["id"] for s in resp.json()["segments"]}
        assert pending_id in ids
        assert maybe_id in ids
        assert len(ids) == 2

    def test_filter_by_status(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        _insert_segment(conn, project, source_id, status="approved")
        _insert_segment(conn, project, source_id, status="pending")

        resp = client.get(f"/projects/{project}/segments?status=approved")
        assert resp.status_code == 200
        segs = resp.json()["segments"]
        assert len(segs) == 1
        assert segs[0]["status"] == "approved"

    def test_filter_by_confidence(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        _insert_segment(conn, project, source_id, confidence=0.95)
        _insert_segment(conn, project, source_id, confidence=0.60)

        resp = client.get(f"/projects/{project}/segments?status=pending&min_confidence=0.90")
        assert resp.status_code == 200
        segs = resp.json()["segments"]
        assert len(segs) == 1
        assert segs[0]["match_confidence"] >= 0.90

    def test_filter_by_source(self, client, project):
        import db
        conn = db.get_conn(project)
        source1 = _insert_source(conn, project)
        source2 = _insert_source(conn, project)
        _insert_segment(conn, project, source1)
        _insert_segment(conn, project, source2)

        resp = client.get(f"/projects/{project}/segments?status=pending&source_id={source1}")
        assert resp.status_code == 200
        assert len(resp.json()["segments"]) == 1
        assert resp.json()["segments"][0]["source_id"] == source1

    def test_count_only(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        for _ in range(5):
            _insert_segment(conn, project, source_id)

        resp = client.get(f"/projects/{project}/segments?status=pending&count_only=true")
        assert resp.status_code == 200
        assert resp.json() == {"total": 5}

    def test_pagination(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        for i in range(10):
            _insert_segment(conn, project, source_id, start=i, end=i + 1)

        resp = client.get(f"/projects/{project}/segments?status=pending&page=1&per_page=3")
        body = resp.json()
        assert len(body["segments"]) == 3
        assert body["pagination"]["total"] == 10
        assert body["pagination"]["pages"] == 4

    def test_invalid_sort_returns_422(self, client, project):
        resp = client.get(f"/projects/{project}/segments?sort=invalid_field")
        assert resp.status_code == 422

    def test_invalid_order_returns_422(self, client, project):
        resp = client.get(f"/projects/{project}/segments?order=sideways")
        assert resp.status_code == 422

    def test_nonexistent_project_returns_404(self, client):
        resp = client.get("/projects/bad-id/segments")
        assert resp.status_code == 404

    def test_response_shape(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        _insert_segment(conn, project, source_id)

        resp = client.get(f"/projects/{project}/segments?status=pending")
        seg = resp.json()["segments"][0]
        assert "id" in seg
        assert "source_id" in seg
        assert "source_filename" in seg
        assert "start_secs" in seg
        assert "end_secs" in seg
        assert "duration_secs" in seg
        assert "match_confidence" in seg
        assert "transcript" in seg
        assert "transcript_edited" in seg
        assert "status" in seg
        assert "clipping_warning" in seg
        assert "audio_url" in seg


class TestSegmentAudio:
    def test_audio_not_found_returns_404(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg_id = _insert_segment(conn, project, source_id)
        # WAV doesn't exist on disk yet
        resp = client.get(f"/projects/{project}/segments/{seg_id}/audio")
        assert resp.status_code == 404

    def test_audio_segment_not_found_returns_404(self, client, project):
        resp = client.get(f"/projects/{project}/segments/bad-id/audio")
        assert resp.status_code == 404

    def test_audio_served_when_file_exists(self, client, project, isolated_data_dir, test_wav):
        import db, shutil
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg_id = _insert_segment(conn, project, source_id)

        # Place a WAV file where the segment expects it
        wav_dir = isolated_data_dir / "projects" / project / "segments" / "raw"
        wav_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(test_wav, wav_dir / f"{seg_id}.wav")

        resp = client.get(f"/projects/{project}/segments/{seg_id}/audio")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"


class TestPatchSegment:
    def test_approve_pending(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg_id = _insert_segment(conn, project, source_id, status="pending")

        resp = client.patch(f"/projects/{project}/segments/{seg_id}", json={"status": "approved"})
        assert resp.status_code == 200

    def test_invalid_transition_returns_409(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg_id = _insert_segment(conn, project, source_id, status="rejected")

        resp = client.patch(f"/projects/{project}/segments/{seg_id}", json={"status": "approved"})
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "invalid_transition"

    def test_edit_transcript(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg_id = _insert_segment(conn, project, source_id, transcript="Original")

        resp = client.patch(f"/projects/{project}/segments/{seg_id}", json={"transcript_edited": "Edited"})
        assert resp.status_code == 200
        updated = conn.execute("SELECT transcript_edited FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert updated["transcript_edited"] == "Edited"

    def test_nonexistent_segment_returns_404(self, client, project):
        resp = client.patch(f"/projects/{project}/segments/bad-id", json={"status": "approved"})
        assert resp.status_code == 404


class TestBulkAction:
    def test_bulk_approve(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        for _ in range(5):
            _insert_segment(conn, project, source_id, status="pending", confidence=0.95)

        resp = client.post(f"/projects/{project}/segments/bulk", json={
            "action": "approve",
            "filter": {"status": "pending", "min_confidence": 0.9},
        })
        assert resp.status_code == 200
        assert resp.json()["affected_count"] == 5

    def test_bulk_reject(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        for _ in range(3):
            _insert_segment(conn, project, source_id, status="pending")

        resp = client.post(f"/projects/{project}/segments/bulk", json={"action": "reject"})
        assert resp.status_code == 200
        assert resp.json()["affected_count"] == 3

    def test_bulk_only_affects_allowed_statuses(self, client, project):
        """Bulk 'pending' only moves 'maybe' → 'pending', not 'approved' etc."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        maybe_id = _insert_segment(conn, project, source_id, status="maybe")
        approved_id = _insert_segment(conn, project, source_id, status="approved")

        resp = client.post(f"/projects/{project}/segments/bulk", json={"action": "pending"})
        assert resp.status_code == 200
        assert resp.json()["affected_count"] == 1  # only the maybe one

        assert conn.execute("SELECT status FROM segments WHERE id=?", (approved_id,)).fetchone()[0] == "approved"
        assert conn.execute("SELECT status FROM segments WHERE id=?", (maybe_id,)).fetchone()[0] == "pending"

    def test_bulk_invalid_action_returns_422(self, client, project):
        resp = client.post(f"/projects/{project}/segments/bulk", json={"action": "vanish"})
        assert resp.status_code == 422

    def test_bulk_nonexistent_project_returns_404(self, client):
        resp = client.post("/projects/bad-id/segments/bulk", json={"action": "approve"})
        assert resp.status_code == 404
