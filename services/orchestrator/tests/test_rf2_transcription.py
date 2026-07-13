"""Review-fix wave 2 — transcription write-path regressions (Worker A).

A1: resegmentation results re-check parent eligibility at write time, so a
    review or transcript edit that lands mid-job is never destroyed.
A2: per-segment transcription errors leave the transcript NULL and record a
    ``transcription_error: <msg>`` flag; a later success clears it and the
    segment stays selectable for retranscription.
A3: bulk transcription preloads segment durations so the incremental write
    path does not issue a per-segment duration SELECT in the poll loop.
A4: children with inverted/zero-length bounds are skipped defensively; if
    none survive, the result folds back into a plain parent write.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_wave3_pipeline import (
    _create_segment_wav,
    _enqueue_and_run,
    _insert_segment,
    _insert_source,
)


def _run_bulk(project, seg_ids, poll_results, submit_capture=None, before_final=None):
    """Run a transcription_bulk job with mocked service calls.

    ``poll_results[:-1]`` are fed through on_progress as running polls;
    ``poll_results[-1]`` is the final (complete) result. ``before_final``
    runs just before the final result is returned — i.e. after the
    submit-time eligibility snapshot but before results are written — to
    simulate user actions landing mid-job.
    """

    async def mock_submit(service, payload):
        if submit_capture is not None:
            submit_capture.append(payload)
        return {"job_id": payload["job_id"]}

    async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
        for r in poll_results[:-1]:
            if on_progress:
                on_progress({"job_id": job_id, "status": "running", **r})
        if before_final is not None:
            before_final()
        return {"job_id": job_id, "status": "complete", "progress": 100, **poll_results[-1]}

    with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
         patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
        params = {"segment_ids": seg_ids, "model": "large-v2", "language": None}
        return _enqueue_and_run(project, "transcription_bulk", params=params)


def _children_entry(parent_id, project, bounds=None):
    """A cumulative-results entry that splits parent_id into two children."""
    if bounds is None:
        bounds = [(10.0, 13.5), (13.5, 15.0)]
    children = []
    for (start, end), (text, conf) in zip(
        bounds,
        [("I told you not to come back here.", 0.93), ("And yet.", 0.91)],
    ):
        cid = str(uuid.uuid4())
        children.append({
            "id": cid,
            "wav_path": f"/data/projects/{project}/segments/raw/{cid}.wav",
            "start_secs": start, "end_secs": end,
            "transcript": text, "transcript_confidence": conf,
        })
    return {"id": parent_id, "children": children}


JOINED = "I told you not to come back here. And yet."


class TestWhisperTuningPayload:
    def test_bulk_payload_carries_config_batch_size_and_compute_type(
        self, client, project, isolated_data_dir
    ):
        import db
        conn = db.get_conn(project)
        client.patch(f"/projects/{project}", json={
            "whisper_batch_size": 4, "whisper_compute_type": "int8_float16",
        })
        source_id = _insert_source(conn, project, "complete")
        seg = _insert_segment(conn, project, source_id, status="pending", confidence=0.7)
        _create_segment_wav(db.project_dir(project), seg)

        capture = []
        _run_bulk(
            project, [seg],
            [{"completed_segments": [
                {"id": seg, "transcript": "x", "transcript_confidence": 0.9}
            ]}],
            submit_capture=capture,
        )

        assert capture[0]["batch_size"] == 4
        assert capture[0]["compute_type"] == "int8_float16"

    def test_bulk_payload_carries_align_words_config(
        self, client, project, isolated_data_dir
    ):
        import db
        conn = db.get_conn(project)
        client.patch(f"/projects/{project}", json={"align_words": True})
        source_id = _insert_source(conn, project, "complete")
        seg = _insert_segment(conn, project, source_id, status="pending", confidence=0.7)
        _create_segment_wav(db.project_dir(project), seg)

        capture = []
        _run_bulk(
            project, [seg],
            [{"completed_segments": [
                {"id": seg, "transcript": "x", "transcript_confidence": 0.9}
            ]}],
            submit_capture=capture,
        )

        assert capture[0]["align"] is True


# ========================================================================
# A1 — write-time eligibility re-check for children results
# ========================================================================

class TestResegmentationRaceRecheck:
    def _setup_parent(self, conn, project, pdir, **kwargs):
        source_id = _insert_source(conn, project, "complete")
        parent = _insert_segment(conn, project, source_id, status="pending",
                                 confidence=0.7, **kwargs)
        _create_segment_wav(pdir, parent)
        return source_id, parent

    def _assert_not_split(self, conn, pdir, source_id, parent):
        """Parent row and WAV survive; no child rows; joined transcript."""
        row = conn.execute("SELECT * FROM segments WHERE id=?", (parent,)).fetchone()
        assert row is not None
        assert row["transcript"] == JOINED
        assert row["transcript_confidence"] == pytest.approx(0.91)  # min of children
        assert (pdir / "segments" / "raw" / f"{parent}.wav").exists()
        count = conn.execute(
            "SELECT COUNT(*) FROM segments WHERE source_id=?", (source_id,)
        ).fetchone()[0]
        assert count == 1  # only the parent — no children inserted
        return row

    def test_parent_reviewed_mid_job_is_not_split(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id, parent = self._setup_parent(conn, project, pdir)

        def approve_mid_job():
            conn.execute("UPDATE segments SET status='approved' WHERE id=?", (parent,))
            conn.commit()

        entry = _children_entry(parent, project)
        _run_bulk(project, [parent], [{"completed_segments": [entry]}],
                  before_final=approve_mid_job)

        row = self._assert_not_split(conn, pdir, source_id, parent)
        assert row["status"] == "approved"  # the review survives

    def test_parent_edited_mid_job_is_not_split(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id, parent = self._setup_parent(conn, project, pdir)

        def edit_mid_job():
            conn.execute(
                "UPDATE segments SET transcript_edited='My careful edit.' WHERE id=?",
                (parent,),
            )
            conn.commit()

        entry = _children_entry(parent, project)
        _run_bulk(project, [parent], [{"completed_segments": [entry]}],
                  before_final=edit_mid_job)

        row = self._assert_not_split(conn, pdir, source_id, parent)
        assert row["transcript_edited"] == "My careful edit."

    def test_parent_transcribed_mid_job_is_not_split(self, client, project, isolated_data_dir):
        """A transcript that landed mid-job (e.g. a single-segment rerun)
        also blocks the split."""
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id, parent = self._setup_parent(conn, project, pdir)

        def transcribe_mid_job():
            conn.execute("UPDATE segments SET transcript='Earlier rerun.' WHERE id=?", (parent,))
            conn.commit()

        entry = _children_entry(parent, project)
        _run_bulk(project, [parent], [{"completed_segments": [entry]}],
                  before_final=transcribe_mid_job)

        self._assert_not_split(conn, pdir, source_id, parent)

    def test_untouched_parent_still_splits(self, client, project, isolated_data_dir):
        """Control: the re-check does not break the normal replacement path."""
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id, parent = self._setup_parent(conn, project, pdir)

        entry = _children_entry(parent, project)
        _run_bulk(project, [parent], [{"completed_segments": [entry]}])

        assert conn.execute("SELECT COUNT(*) FROM segments WHERE id=?", (parent,)).fetchone()[0] == 0
        assert not (pdir / "segments" / "raw" / f"{parent}.wav").exists()
        rows = conn.execute("SELECT id FROM segments WHERE source_id=?", (source_id,)).fetchall()
        assert {r["id"] for r in rows} == {ch["id"] for ch in entry["children"]}


# ========================================================================
# A2 — per-segment transcription errors
# ========================================================================

class TestTranscriptionErrorFlag:
    def _error_entry(self, seg_id, msg="CUDA out of memory"):
        return {"id": seg_id, "transcript": "", "transcript_confidence": 0.0, "error": msg}

    def test_error_leaves_transcript_null_and_flags(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="pending")

        _run_bulk(project, [seg_id],
                  [{"completed_segments": [self._error_entry(seg_id)]}])

        row = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert row["transcript"] is None
        assert row["transcript_confidence"] is None
        assert row["status"] == "pending"  # never auto-approved
        assert json.loads(row["flags"]) == ["transcription_error: CUDA out of memory"]

    def test_error_flag_replaced_not_duplicated(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="pending",
                                 flags='["cleanup_error: boom"]')

        _run_bulk(project, [seg_id], [{"completed_segments": [self._error_entry(seg_id, "first")]}])
        _run_bulk(project, [seg_id], [{"completed_segments": [self._error_entry(seg_id, "second")]}])

        flags = json.loads(conn.execute(
            "SELECT flags FROM segments WHERE id=?", (seg_id,)).fetchone()["flags"])
        # Unrelated flags survive; exactly one transcription_error, the latest.
        assert flags == ["cleanup_error: boom", "transcription_error: second"]

    def test_errored_segment_remains_selectable_for_retranscription(
        self, client, project, isolated_data_dir
    ):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="pending")

        _run_bulk(project, [seg_id],
                  [{"completed_segments": [self._error_entry(seg_id)]}])

        # transcript stayed NULL, so the run selector still picks it up.
        resp = client.post(f"/projects/{project}/transcription/run")
        assert resp.status_code == 202
        assert resp.json()["enqueued_job"]["segment_count"] == 1

    def test_later_success_clears_error_flag(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="pending",
                                 flags='["cleanup_error: boom"]')

        _run_bulk(project, [seg_id],
                  [{"completed_segments": [self._error_entry(seg_id)]}])
        _run_bulk(project, [seg_id],
                  [{"completed_segments": [{"id": seg_id, "transcript": "All good now.",
                                            "transcript_confidence": 0.95}]}])

        row = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert row["transcript"] == "All good now."
        assert row["transcript_confidence"] == 0.95
        assert json.loads(row["flags"]) == ["cleanup_error: boom"]

    def test_single_segment_rerun_clears_error_flag(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="pending",
                                 flags='["transcription_error: old failure"]')

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "progress": 100,
                    "completed_segments": [{"id": seg_id, "transcript": "Recovered.",
                                            "transcript_confidence": 0.9}]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "transcription_segment", params={"segment_ids": [seg_id]})

        row = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert row["transcript"] == "Recovered."
        assert json.loads(row["flags"] or "[]") == []


# ========================================================================
# A3 — duration preload (no N+1 SELECT in the poll loop)
# ========================================================================

_FALLBACK_DURATION_SQL = "SELECT duration_secs FROM segments WHERE id=?"


class _CountingConn:
    """Delegating wrapper that counts the per-segment duration fallback SELECT."""

    def __init__(self, conn):
        self._conn = conn
        self.duration_selects = 0

    def execute(self, sql, *args):
        if sql.strip() == _FALLBACK_DURATION_SQL:
            self.duration_selects += 1
        return self._conn.execute(sql, *args)

    def commit(self):
        self._conn.commit()


class TestDurationPreload:
    def test_preloaded_durations_skip_per_segment_selects(self, client, project, isolated_data_dir):
        import db
        import jobs
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_short = _insert_segment(conn, project, source_id, status="pending", start=0.0, end=1.5)
        seg_long = _insert_segment(conn, project, source_id, status="pending", start=0.0, end=6.0)

        completed = [
            {"id": seg_short, "transcript": "Hi.", "transcript_confidence": 0.9},
            {"id": seg_long, "transcript": "A much longer line of dialogue.",
             "transcript_confidence": 0.9},
        ]
        counting = _CountingConn(conn)
        jobs._apply_transcription_results(
            project, counting, completed, set(),
            durations={seg_short: 1.5, seg_long: 6.0},
        )

        assert counting.duration_selects == 0
        flags = json.loads(conn.execute(
            "SELECT flags FROM segments WHERE id=?", (seg_short,)).fetchone()["flags"])
        assert "short_transcript" in flags
        long_flags = conn.execute(
            "SELECT flags FROM segments WHERE id=?", (seg_long,)).fetchone()["flags"]
        assert not long_flags or "short_transcript" not in json.loads(long_flags)

    def test_missing_id_falls_back_to_single_lookup(self, client, project, isolated_data_dir):
        import db
        import jobs
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="pending", start=0.0, end=1.5)

        counting = _CountingConn(conn)
        jobs._apply_transcription_results(
            project, counting,
            [{"id": seg_id, "transcript": "Hi.", "transcript_confidence": 0.9}],
            set(), durations={"some-other-segment": 5.0},
        )

        assert counting.duration_selects == 1
        flags = json.loads(conn.execute(
            "SELECT flags FROM segments WHERE id=?", (seg_id,)).fetchone()["flags"])
        assert "short_transcript" in flags

    def test_bulk_handler_plumbs_durations_through(self, client, project, isolated_data_dir):
        """End-to-end: a short segment written via the bulk poll loop still
        gets the short_transcript flag (the preloaded map is actually used)."""
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "complete")
        seg_id = _insert_segment(conn, project, source_id, status="maybe", start=0.0, end=1.5)

        _run_bulk(project, [seg_id],
                  [{"completed_segments": [{"id": seg_id, "transcript": "Hi.",
                                            "transcript_confidence": 0.9}]}])

        flags = json.loads(conn.execute(
            "SELECT flags FROM segments WHERE id=?", (seg_id,)).fetchone()["flags"])
        assert "short_transcript" in flags


# ========================================================================
# A4 — defensive skip of inverted/zero-length children
# ========================================================================

class TestChildBoundsDefence:
    def _child(self, project, start, end, text="Text.", conf=0.9):
        cid = str(uuid.uuid4())
        return {"id": cid,
                "wav_path": f"/data/projects/{project}/segments/raw/{cid}.wav",
                "start_secs": start, "end_secs": end,
                "transcript": text, "transcript_confidence": conf}

    def test_inverted_child_is_skipped(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        parent = _insert_segment(conn, project, source_id, status="pending")
        _create_segment_wav(pdir, parent)

        good = self._child(project, 10.0, 13.5, "Kept child.")
        inverted = self._child(project, 15.0, 14.0, "Inverted child.")
        entry = {"id": parent, "children": [good, inverted]}

        _run_bulk(project, [parent], [{"completed_segments": [entry]}])

        rows = conn.execute("SELECT id FROM segments WHERE source_id=?", (source_id,)).fetchall()
        assert {r["id"] for r in rows} == {good["id"]}  # parent replaced, inverted skipped

    def test_all_children_invalid_falls_back_to_plain_write(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project, "complete")
        parent = _insert_segment(conn, project, source_id, status="pending")
        _create_segment_wav(pdir, parent)

        entry = {"id": parent, "children": [
            self._child(project, 15.0, 14.0, "One.", 0.8),
            self._child(project, 14.0, 14.0, "Two.", 0.7),  # zero-length also invalid
        ]}

        _run_bulk(project, [parent], [{"completed_segments": [entry]}])

        row = conn.execute("SELECT * FROM segments WHERE id=?", (parent,)).fetchone()
        assert row is not None  # parent kept
        assert row["transcript"] == "One. Two."
        assert row["transcript_confidence"] == pytest.approx(0.7)
        assert (pdir / "segments" / "raw" / f"{parent}.wav").exists()
        assert conn.execute(
            "SELECT COUNT(*) FROM segments WHERE source_id=?", (source_id,)
        ).fetchone()[0] == 1
