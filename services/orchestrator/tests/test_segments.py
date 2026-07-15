"""Tests for segment listing, audio streaming, review actions, and bulk operations."""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
        seg_id = _insert_segment(conn, project, source_id, status="pending", transcript="hello")

        resp = client.patch(f"/projects/{project}/segments/{seg_id}", json={"status": "approved"})
        assert resp.status_code == 200

    def test_invalid_transition_returns_409(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg_id = _insert_segment(conn, project, source_id, status="rejected")

        resp = client.patch(f"/projects/{project}/segments/{seg_id}", json={"status": "approved"})
        assert resp.status_code == 409
        assert resp.json()["error"] == "invalid_transition"

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

    def test_rejected_to_pending_returns_200(self, client, project):
        """Misclick recovery: a rejected segment can be un-rejected back to pending."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg_id = _insert_segment(conn, project, source_id, status="rejected")

        resp = client.patch(f"/projects/{project}/segments/{seg_id}", json={"status": "pending"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"
        updated = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert updated["status"] == "pending"

    def test_auto_rejected_to_pending_returns_409(self, client, project):
        """auto_rejected stays terminal — it's a fact about the audio, not undoable."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg_id = _insert_segment(conn, project, source_id, status="auto_rejected")

        resp = client.patch(f"/projects/{project}/segments/{seg_id}", json={"status": "pending"})
        assert resp.status_code == 409
        assert resp.json()["error"] == "invalid_transition"


class TestBulkAction:
    def test_bulk_approve(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        for _ in range(5):
            _insert_segment(conn, project, source_id, status="pending", confidence=0.95,
                            transcript="words")

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

    def test_bulk_pending_action_affects_rejected_when_filtered(self, client, project):
        """Bulk un-reject: 'pending' action reaches 'rejected' segments when explicitly filtered."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        rejected_id = _insert_segment(conn, project, source_id, status="rejected")
        approved_id = _insert_segment(conn, project, source_id, status="approved")

        resp = client.post(f"/projects/{project}/segments/bulk", json={
            "action": "pending",
            "filter": {"status": "rejected"},
        })
        assert resp.status_code == 200
        assert resp.json()["affected_count"] == 1

        assert conn.execute("SELECT status FROM segments WHERE id=?", (rejected_id,)).fetchone()[0] == "pending"
        assert conn.execute("SELECT status FROM segments WHERE id=?", (approved_id,)).fetchone()[0] == "approved"

    def test_bulk_invalid_action_returns_422(self, client, project):
        resp = client.post(f"/projects/{project}/segments/bulk", json={"action": "vanish"})
        assert resp.status_code == 422

    def test_bulk_nonexistent_project_returns_404(self, client):
        resp = client.post("/projects/bad-id/segments/bulk", json={"action": "approve"})
        assert resp.status_code == 404


class TestAutoApprovedStatus:
    def test_patch_to_auto_approved_returns_409(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        for status in ("pending", "maybe", "approved", "auto_approved"):
            seg = _insert_segment(conn, project, source_id, status=status)
            resp = client.patch(f"/projects/{project}/segments/{seg}",
                                json={"status": "auto_approved"})
            assert resp.status_code == 409, f"from {status}"
            assert resp.json()["error"] == "invalid_transition"

    @pytest.mark.parametrize("target", ["approved", "rejected", "maybe", "pending"])
    def test_user_can_leave_auto_approved(self, client, project, target):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        # auto_approved always carries a transcript; include one so the
        # approve target isn't blocked by the no-transcript guard.
        seg = _insert_segment(conn, project, source_id, status="auto_approved",
                              transcript="auto text")

        resp = client.patch(f"/projects/{project}/segments/{seg}", json={"status": target})
        assert resp.status_code == 200
        assert resp.json()["status"] == target

    def test_status_filter_accepts_auto_approved(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg = _insert_segment(conn, project, source_id, status="auto_approved")
        _insert_segment(conn, project, source_id, status="pending")

        resp = client.get(f"/projects/{project}/segments", params={"status": "auto_approved"})
        assert resp.status_code == 200
        body = resp.json()
        assert [s["id"] for s in body["segments"]] == [seg]


class TestUncertaintySort:
    def test_uncertainty_sort_defaults_to_most_borderline_first(self, client, project):
        """Default order for uncertainty is asc: smallest |confidence - threshold| first."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        # Project match_threshold defaults to 0.75
        seg_far = _insert_segment(conn, project, source_id, confidence=0.99)      # |0.24|
        seg_close = _insert_segment(conn, project, source_id, confidence=0.76)    # |0.01|
        seg_mid = _insert_segment(conn, project, source_id, confidence=0.85)      # |0.10|

        resp = client.get(f"/projects/{project}/segments", params={"sort": "uncertainty"})
        assert resp.status_code == 200
        ids = [s["id"] for s in resp.json()["segments"]]
        assert ids == [seg_close, seg_mid, seg_far]

    def test_uncertainty_sort_desc(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg_far = _insert_segment(conn, project, source_id, confidence=0.99)
        seg_close = _insert_segment(conn, project, source_id, confidence=0.76)

        resp = client.get(f"/projects/{project}/segments",
                          params={"sort": "uncertainty", "order": "desc"})
        ids = [s["id"] for s in resp.json()["segments"]]
        assert ids == [seg_far, seg_close]

    def test_uncertainty_uses_project_threshold(self, client, project):
        """Raising the threshold changes which segment is most borderline."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg_a = _insert_segment(conn, project, source_id, confidence=0.76)
        seg_b = _insert_segment(conn, project, source_id, confidence=0.94)

        client.patch(f"/projects/{project}", json={"match_threshold": 0.93})
        resp = client.get(f"/projects/{project}/segments", params={"sort": "uncertainty"})
        ids = [s["id"] for s in resp.json()["segments"]]
        # seg_a fell to below_threshold (excluded by default filter); seg_b first anyway
        assert ids[0] == seg_b

    def test_other_sorts_still_default_desc(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg_low = _insert_segment(conn, project, source_id, confidence=0.80)
        seg_high = _insert_segment(conn, project, source_id, confidence=0.95)

        resp = client.get(f"/projects/{project}/segments")
        ids = [s["id"] for s in resp.json()["segments"]]
        assert ids == [seg_high, seg_low]


class TestBulkAutoApproved:
    def test_bulk_approve_confirms_auto_approved(self, client, project):
        """Bulk approve with status filter auto_approved is 'confirm all auto-approved'."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg = _insert_segment(conn, project, source_id, status="auto_approved",
                              transcript="auto text")

        resp = client.post(f"/projects/{project}/segments/bulk",
                           json={"action": "approve", "filter": {"status": "auto_approved"}})
        assert resp.status_code == 200
        assert resp.json()["affected_count"] == 1
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"] == "approved"

    def test_bulk_pending_resets_auto_approved(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg = _insert_segment(conn, project, source_id, status="auto_approved")

        resp = client.post(f"/projects/{project}/segments/bulk",
                           json={"action": "pending", "filter": {"status": "auto_approved,maybe"}})
        assert resp.json()["affected_count"] == 1
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()["status"] == "pending"

    def test_default_bulk_filter_does_not_touch_auto_approved(self, client, project):
        """A filterless bulk reject only hits the default pending+maybe set."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project)
        seg_auto = _insert_segment(conn, project, source_id, status="auto_approved")
        seg_pending = _insert_segment(conn, project, source_id, status="pending")

        resp = client.post(f"/projects/{project}/segments/bulk", json={"action": "reject"})
        assert resp.json()["affected_count"] == 1
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg_auto,)).fetchone()["status"] == "auto_approved"
        assert conn.execute("SELECT status FROM segments WHERE id=?", (seg_pending,)).fetchone()["status"] == "rejected"


class TestAdjustBoundaries:
    """POST /segments/{id}/boundaries re-cuts the raw WAV from the source's
    retained vocals file and invalidates the stale cleaned cache."""

    def _setup(self, conn, project, *, cleaned=True, vocals=True):
        import db
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project)
        vocals_rel = None
        if vocals:
            vocals_rel = f"audio/vocals/{source_id}.wav"
            (pdir / "audio" / "vocals").mkdir(parents=True, exist_ok=True)
            (pdir / vocals_rel).write_bytes(b"RIFFvocals")
            conn.execute("UPDATE sources SET vocals_path=? WHERE id=?", (vocals_rel, source_id))
        seg_id = _insert_segment(conn, project, source_id, start=2.0, end=5.0, transcript="hi")
        (pdir / "segments" / "raw").mkdir(parents=True, exist_ok=True)
        (pdir / f"segments/raw/{seg_id}.wav").write_bytes(b"RIFFraw")
        cleaned_rel = None
        if cleaned:
            cleaned_rel = f"cleaned/{seg_id}.wav"
            (pdir / "cleaned").mkdir(parents=True, exist_ok=True)
            (pdir / cleaned_rel).write_bytes(b"RIFFclean")
            conn.execute("UPDATE segments SET cleaned_path=? WHERE id=?", (cleaned_rel, seg_id))
        conn.commit()
        return source_id, seg_id, pdir, cleaned_rel

    def test_extend_reslices_and_clears_cleaned(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id, seg_id, pdir, cleaned_rel = self._setup(conn, project)

        async def fake_slice(src, dst, start, end):
            Path(dst).write_bytes(b"RIFFrecut")
            return True

        with patch("routers.segments.slice_wav", new=AsyncMock(side_effect=fake_slice)), \
             patch("routers.segments.get_duration", new=AsyncMock(return_value=30.0)):
            resp = client.post(f"/projects/{project}/segments/{seg_id}/boundaries",
                               json={"start_secs": 1.5, "end_secs": 6.0})

        assert resp.status_code == 200
        body = resp.json()
        assert body["start_secs"] == 1.5
        assert body["end_secs"] == 6.0
        assert body["duration_secs"] == 4.5
        assert "boundary_edited" in body["flags"]
        row = conn.execute("SELECT cleaned_path FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert row["cleaned_path"] is None
        assert not (pdir / cleaned_rel).exists()
        # A prior export is now stale.
        assert conn.execute("SELECT exported_at FROM projects WHERE id=?", (project,)).fetchone()["exported_at"] is None

    def test_omitted_edge_keeps_current_value(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id, seg_id, pdir, _ = self._setup(conn, project, cleaned=False)
        captured = {}

        async def fake_slice(src, dst, start, end):
            captured["start"], captured["end"] = start, end
            Path(dst).write_bytes(b"x")
            return True

        with patch("routers.segments.slice_wav", new=AsyncMock(side_effect=fake_slice)), \
             patch("routers.segments.get_duration", new=AsyncMock(return_value=30.0)):
            resp = client.post(f"/projects/{project}/segments/{seg_id}/boundaries",
                               json={"end_secs": 7.0})

        assert resp.status_code == 200
        assert captured["start"] == 2.0  # unchanged
        assert captured["end"] == 7.0
        assert resp.json()["start_secs"] == 2.0

    def test_clamps_end_to_vocals_duration(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id, seg_id, pdir, _ = self._setup(conn, project, cleaned=False)
        captured = {}

        async def fake_slice(src, dst, start, end):
            captured["end"] = end
            Path(dst).write_bytes(b"x")
            return True

        with patch("routers.segments.slice_wav", new=AsyncMock(side_effect=fake_slice)), \
             patch("routers.segments.get_duration", new=AsyncMock(return_value=5.5)):
            resp = client.post(f"/projects/{project}/segments/{seg_id}/boundaries",
                               json={"end_secs": 99.0})

        assert resp.status_code == 200
        assert captured["end"] == 5.5
        assert resp.json()["end_secs"] == 5.5

    def test_no_change_422(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id, seg_id, *_ = self._setup(conn, project)
        resp = client.post(f"/projects/{project}/segments/{seg_id}/boundaries", json={})
        assert resp.status_code == 422
        assert resp.json()["error"] == "no_change"

    def test_too_short_422(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id, seg_id, *_ = self._setup(conn, project)
        with patch("routers.segments.get_duration", new=AsyncMock(return_value=30.0)):
            resp = client.post(f"/projects/{project}/segments/{seg_id}/boundaries",
                               json={"start_secs": 3.0, "end_secs": 3.02})
        assert resp.status_code == 422
        assert resp.json()["error"] == "invalid_boundaries"

    def test_no_vocals_409(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id, seg_id, *_ = self._setup(conn, project, vocals=False)
        resp = client.post(f"/projects/{project}/segments/{seg_id}/boundaries",
                           json={"start_secs": 1.0})
        assert resp.status_code == 409
        assert resp.json()["error"] == "vocals_unavailable"

    def test_segment_not_found_404(self, client, project):
        resp = client.post(f"/projects/{project}/segments/{uuid.uuid4()}/boundaries",
                           json={"start_secs": 1.0})
        assert resp.status_code == 404
