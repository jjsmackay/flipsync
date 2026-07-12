"""Regression tests for review-fix wave 2, Worker C (C1–C8).

C1  POST /pipeline/continue idempotence (+ _auto_enqueue_diarisation guard)
C2  _handle_diarisation unlinks old WAVs on re-run; no-ops on unexpected source status
C3  upload_reference recomputes project status (awaiting_reference -> next state)
C4  ffprobe process killed on timeout
C5  GET /reference/scout after a failed re-scan still returns prior candidates
C6  latest-scout-job tiebreaker at second resolution (rowid)
C7  superseded montage directories removed on re-scan
C8  speaker_match_confidence persisted (migration 006) and serialised

External service HTTP calls are mocked via patch on service_client functions,
following the pattern in test_reference_from_video.py.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _insert_source(conn, project_id, status="separation_pending", vocals_path=None):
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO sources
           (id, project_id, filename, file_path, vocals_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (source_id, project_id, "ep01.mkv", "source/ep01.mkv", vocals_path, status, now, now),
    )
    conn.commit()
    return source_id


def _insert_segment(conn, project_id, source_id, raw_path, status="pending",
                    match_confidence=0.9, export_path=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments
           (id, project_id, source_id, raw_path, export_path, start_secs, end_secs,
            speaker_label, match_confidence, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, raw_path, export_path, 0.0, 2.0,
         "SPEAKER_00", match_confidence, status, now, now),
    )
    conn.commit()
    return seg_id


def _insert_scout_job(conn, project_id, source_id, status="complete", created_at=None,
                      error=None):
    job_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO jobs (id, project_id, source_id, type, status, error, created_at) VALUES (?,?,?,?,?,?,?)",
        (job_id, project_id, source_id, "scout_speakers", status, error, created_at or _now()),
    )
    conn.commit()
    return job_id


def _insert_candidate(conn, project_id, source_id, label, montage_path, scout_job_id,
                      total_secs=30.0, segment_count=10):
    conn.execute(
        """INSERT INTO speaker_candidates
           (id, project_id, scout_job_id, source_id, speaker_label, montage_path,
            total_secs, segment_count, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), project_id, scout_job_id, source_id,
         label, montage_path, total_secs, segment_count, _now()),
    )
    conn.commit()


def _run_job(project_id, job_id):
    import jobs
    loop = asyncio.new_event_loop()
    loop.run_until_complete(jobs._execute_job(project_id, job_id))
    loop.close()


def _enqueue_and_run(project_id, job_type, source_id=None, params=None):
    import jobs
    job_id = jobs.enqueue(project_id, job_type, source_id=source_id, params=params)
    try:
        jobs._queues[project_id].get_nowait()
    except Exception:
        pass
    _run_job(project_id, job_id)
    return job_id


# ========================================================================
# C1 — POST /pipeline/continue idempotence
# ========================================================================

class TestContinueIdempotent:
    def test_double_continue_enqueues_one_job(self, client, project):
        import db
        conn = db.get_conn(project)
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project,))
        conn.commit()
        _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/a.wav")

        async def blocked_submit(service, payload):
            # Never completes — keeps the enqueued job queued/running for the
            # duration of the test so the second POST exercises the guard.
            await asyncio.Event().wait()

        with patch("service_client.submit_job", new=AsyncMock(side_effect=blocked_submit)):
            first = client.post(f"/projects/{project}/pipeline/continue")
            assert first.status_code == 202
            assert len(first.json()["enqueued_jobs"]) == 1

            second = client.post(f"/projects/{project}/pipeline/continue")
            assert second.status_code == 200
            assert second.json()["enqueued_jobs"] == []

            count = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE project_id=? AND type='diarisation'",
                (project,),
            ).fetchone()[0]
            assert count == 1

    def test_continue_no_pending_and_no_active_still_409(self, client, project):
        import db
        conn = db.get_conn(project)
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project,))
        conn.commit()
        _insert_source(conn, project, "complete")

        resp = client.post(f"/projects/{project}/pipeline/continue")
        assert resp.status_code == 409
        assert resp.json()["error"] == "no_pending_sources"

    def test_auto_enqueue_diarisation_guarded(self, client, project):
        import db
        import jobs
        conn = db.get_conn(project)
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project,))
        conn.commit()
        source_id = _insert_source(conn, project, "diarisation_pending",
                                   vocals_path="audio/vocals/a.wav")

        jobs._auto_enqueue_diarisation(project, source_id)
        jobs._auto_enqueue_diarisation(project, source_id)

        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE source_id=? AND type='diarisation'",
            (source_id,),
        ).fetchone()[0]
        assert count == 1


# ========================================================================
# C2 — _handle_diarisation: unlink old WAVs on re-run, no-op gate
# ========================================================================

class TestDiarisationRerun:
    def _mock_result(self, segments):
        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "progress": 100,
                    "segments": segments, "coverage_ratio": 0.5}

        return mock_submit, mock_poll

    def test_rerun_unlinks_old_wavs(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project,))
        conn.commit()
        source_id = _insert_source(conn, project, "diarisation_pending",
                                   vocals_path=f"audio/vocals/x.wav")

        pdir = db.project_dir(project)
        old_raw = "segments/raw/old-segment.wav"
        old_export = "export/old-segment.wav"
        for rel in (old_raw, old_export):
            f = pdir / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"RIFF")
        _insert_segment(conn, project, source_id, old_raw, export_path=old_export)

        new_id = str(uuid.uuid4())
        mock_submit, mock_poll = self._mock_result([
            {"id": new_id, "start_secs": 1.0, "end_secs": 3.0,
             "speaker_label": "SPEAKER_00", "match_confidence": 0.10},
        ])
        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "diarisation", source_id=source_id)

        # Old rows AND their WAVs are gone; only the new segment remains.
        assert not (pdir / old_raw).exists()
        assert not (pdir / old_export).exists()
        rows = conn.execute("SELECT id FROM segments WHERE source_id=?", (source_id,)).fetchall()
        assert [r["id"] for r in rows] == [new_id]

    def test_noop_on_unexpected_source_status(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project,))
        conn.commit()
        source_id = _insert_source(conn, project, "complete",
                                   vocals_path="audio/vocals/x.wav")

        pdir = db.project_dir(project)
        raw = "segments/raw/keep-me.wav"
        f = pdir / raw
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"RIFF")
        seg_id = _insert_segment(conn, project, source_id, raw, status="approved")

        submit = AsyncMock()
        with patch("service_client.submit_job", new=submit):
            job_id = _enqueue_and_run(project, "diarisation", source_id=source_id)

        # No-op: job completes without submitting, segments and source untouched.
        submit.assert_not_awaited()
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "complete"
        assert (pdir / raw).exists()
        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "approved"
        src = conn.execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
        assert src["status"] == "complete"


# ========================================================================
# C3 — upload_reference recomputes project status
# ========================================================================

class TestUploadRecomputesStatus:
    def test_upload_moves_project_out_of_awaiting_reference(self, client, project, test_wav):
        import db
        from status import recompute_project_status
        conn = db.get_conn(project)
        _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/a.wav")
        recompute_project_status(project)
        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "awaiting_reference"

        with open(test_wav, "rb") as f:
            resp = client.post(f"/projects/{project}/reference",
                               files={"file": ("ref.wav", f, "audio/wav")})
        assert resp.status_code == 200

        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "ready"


# ========================================================================
# C4 — ffprobe killed on timeout
# ========================================================================

class FakeHangingProc:
    def __init__(self):
        self.killed = False
        self.returncode = None

    async def communicate(self):
        await asyncio.sleep(60)

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class TestFfprobeKill:
    def test_hanging_ffprobe_is_killed(self, monkeypatch):
        from routers import reference

        proc = FakeHangingProc()

        async def fake_exec(*args, **kwargs):
            return proc

        monkeypatch.setattr(reference.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(reference, "_FFPROBE_TIMEOUT_SECS", 0.05)

        duration = asyncio.run(reference._get_duration("/nonexistent/file.wav"))

        assert proc.killed is True
        assert duration == 0.0  # wave fallback on a nonexistent path


# ========================================================================
# C5/C6 — GET /reference/scout: prior candidates on failure, tiebreaker
# ========================================================================

class TestScoutStatusReporting:
    def test_failed_rescan_returns_prior_candidates(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending",
                                   vocals_path="audio/vocals/v.wav")

        ok_job = _insert_scout_job(conn, project, source_id, status="complete",
                                   created_at="2026-01-01T00:00:00Z")
        _insert_candidate(conn, project, source_id, "SPEAKER_00",
                          f"reference_candidates/{ok_job}/SPEAKER_00.wav", ok_job)
        _insert_scout_job(conn, project, source_id, status="failed",
                          created_at="2026-01-02T00:00:00Z", error="gpu_oom")

        resp = client.get(f"/projects/{project}/reference/scout")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert body["error"] == "gpu_oom"
        labels = [s["speaker_label"] for s in body["speakers"]]
        assert labels == ["SPEAKER_00"]
        assert body["speakers"][0]["sample_url"] == \
            f"/projects/{project}/reference/scout/samples/SPEAKER_00"

    def test_same_second_tiebreak_prefers_latest_insert(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending",
                                   vocals_path="audio/vocals/v.wav")
        ts = "2026-01-01T00:00:00Z"

        ok_job = _insert_scout_job(conn, project, source_id, status="complete", created_at=ts)
        _insert_candidate(conn, project, source_id, "SPEAKER_00",
                          f"reference_candidates/{ok_job}/SPEAKER_00.wav", ok_job)
        _insert_scout_job(conn, project, source_id, status="failed", created_at=ts,
                          error="boom")

        # Failed job was inserted later within the same second — it must win.
        body = client.get(f"/projects/{project}/reference/scout").json()
        assert body["status"] == "failed"
        assert [s["speaker_label"] for s in body["speakers"]] == ["SPEAKER_00"]

        # A later complete scout (same timestamp again) takes over.
        _insert_scout_job(conn, project, source_id, status="complete", created_at=ts)
        body = client.get(f"/projects/{project}/reference/scout").json()
        assert body["status"] == "complete"


# ========================================================================
# C7 — superseded montage directories removed on re-scan
# ========================================================================

class TestScoutMontageCleanup:
    def test_rescan_removes_superseded_montage_dir(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending",
                                   vocals_path="audio/vocals/v.wav")

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "mode": "scout",
                    "speakers": [{"speaker_label": "SPEAKER_00",
                                  "montage_path": "/x/a.wav",
                                  "total_secs": 20.0, "segment_count": 5}]}

        pdir = db.project_dir(project)
        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job1 = _enqueue_and_run(project, "scout_speakers", source_id=source_id)

            job1_dir = pdir / "reference_candidates" / job1
            job1_dir.mkdir(parents=True, exist_ok=True)
            (job1_dir / "SPEAKER_00.wav").write_bytes(b"RIFF")

            job2 = _enqueue_and_run(project, "scout_speakers", source_id=source_id)

        # Old scan's montage dir is gone; candidates reference the new scan.
        assert not job1_dir.exists()
        cands = conn.execute(
            "SELECT scout_job_id FROM speaker_candidates WHERE project_id=?", (project,)
        ).fetchall()
        assert [c["scout_job_id"] for c in cands] == [job2]


# ========================================================================
# C8 — speaker_match_confidence persisted and serialised
# ========================================================================

class TestSpeakerMatchConfidence:
    def test_migration_006_applied(self, client, project):
        import db
        conn = db.get_conn(project)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(segments)")}
        assert "speaker_match_confidence" in cols
        applied = {r["filename"] for r in conn.execute("SELECT filename FROM _migrations")}
        assert "006_speaker_match_confidence.sql" in applied

    def test_persisted_and_serialised(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project,))
        conn.commit()
        source_id = _insert_source(conn, project, "diarisation_pending",
                                   vocals_path="audio/vocals/x.wav")

        scored_id = str(uuid.uuid4())
        unscored_id = str(uuid.uuid4())

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "coverage_ratio": 0.4,
                    "segments": [
                        {"id": scored_id, "start_secs": 0.0, "end_secs": 2.0,
                         "speaker_label": "SPEAKER_00", "match_confidence": 0.10,
                         "speaker_match_confidence": 0.42},
                        {"id": unscored_id, "start_secs": 2.0, "end_secs": 4.0,
                         "speaker_label": "SPEAKER_00", "match_confidence": 0.10},
                    ]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "diarisation", source_id=source_id)

        rows = {r["id"]: r["speaker_match_confidence"] for r in conn.execute(
            "SELECT id, speaker_match_confidence FROM segments WHERE source_id=?", (source_id,)
        )}
        assert rows[scored_id] == 0.42
        assert rows[unscored_id] is None

        resp = client.get(f"/projects/{project}/segments", params={"status": "below_threshold"})
        assert resp.status_code == 200
        segs = {s["id"]: s for s in resp.json()["segments"]}
        assert segs[scored_id]["speaker_match_confidence"] == 0.42
        assert segs[unscored_id]["speaker_match_confidence"] is None
