"""Regression tests for the 2026-07-12 orchestrator review fixes (O1–O18).

External service HTTP calls are mocked via monkeypatch on service_client.
"""

import asyncio
import json
import tarfile
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _insert_source(conn, project_id, status="complete", vocals_path=None):
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO sources (id, project_id, filename, file_path, vocals_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (source_id, project_id, "ep.wav", "source/ep.wav", vocals_path, status, now, now),
    )
    conn.commit()
    return source_id


def _insert_segment(conn, project_id, source_id, status="pending", confidence=0.9,
                    start=0.0, end=5.0, transcript=None, transcript_edited=None,
                    clipping=0, flags=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs,
           speaker_label, match_confidence, status, transcript, transcript_edited,
           clipping_warning, flags, created_at, updated_at)
           VALUES (?,?,?,?,?,?,'S0',?,?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, f"segments/raw/{seg_id}.wav", start, end,
         confidence, status, transcript, transcript_edited, clipping,
         json.dumps(flags) if flags is not None else None, now, now),
    )
    conn.commit()
    return seg_id


def _run_job(project_id, job_id):
    loop = asyncio.new_event_loop()
    loop.run_until_complete(__import__("jobs")._execute_job(project_id, job_id))
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
# O1 — sort=duration
# ========================================================================

class TestSortDuration:
    def test_sort_by_duration_accepted(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        short = _insert_segment(conn, project, src, start=0, end=2)   # 2s
        long = _insert_segment(conn, project, src, start=0, end=9)    # 9s

        resp = client.get(f"/projects/{project}/segments?sort=duration&order=asc")
        assert resp.status_code == 200
        ids = [s["id"] for s in resp.json()["segments"]]
        assert ids == [short, long]

    def test_sort_by_duration_secs_alias_accepted(self, client, project):
        resp = client.get(f"/projects/{project}/segments?sort=duration_secs")
        assert resp.status_code == 200

    def test_invalid_sort_rejected(self, client, project):
        resp = client.get(f"/projects/{project}/segments?sort=nonsense")
        assert resp.status_code == 422
        assert resp.json()["error"] == "invalid_sort"


# ========================================================================
# O2 — filterless bulk defaults to pending+maybe (approved untouched)
# ========================================================================

class TestBulkDefaultStatus:
    def test_filterless_reject_leaves_approved_untouched(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        approved = _insert_segment(conn, project, src, status="approved")
        pending = _insert_segment(conn, project, src, status="pending")

        resp = client.post(f"/projects/{project}/segments/bulk", json={"action": "reject"})
        assert resp.status_code == 200
        assert resp.json()["affected_count"] == 1  # only the pending one

        assert conn.execute("SELECT status FROM segments WHERE id=?", (approved,)).fetchone()["status"] == "approved"
        assert conn.execute("SELECT status FROM segments WHERE id=?", (pending,)).fetchone()["status"] == "rejected"

    def test_explicit_full_status_can_reach_approved(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        approved = _insert_segment(conn, project, src, status="approved")

        resp = client.post(
            f"/projects/{project}/segments/bulk",
            json={"action": "reject", "filter": {"status": "pending,maybe,approved,clipping_warning"}},
        )
        assert resp.status_code == 200
        assert resp.json()["affected_count"] == 1
        assert conn.execute("SELECT status FROM segments WHERE id=?", (approved,)).fetchone()["status"] == "rejected"


# ========================================================================
# O3 — transcript_edited: absent != null
# ========================================================================

class TestTranscriptEditedNull:
    def test_explicit_null_clears_edit(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        seg = _insert_segment(conn, project, src, transcript="orig", transcript_edited="edited")

        resp = client.patch(f"/projects/{project}/segments/{seg}", json={"transcript_edited": None})
        assert resp.status_code == 200
        assert resp.json()["transcript_edited"] is None
        assert conn.execute("SELECT transcript_edited FROM segments WHERE id=?", (seg,)).fetchone()["transcript_edited"] is None

    def test_absent_field_leaves_edit_unchanged(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        seg = _insert_segment(conn, project, src, status="pending",
                              transcript="orig", transcript_edited="edited")

        resp = client.patch(f"/projects/{project}/segments/{seg}", json={"status": "approved"})
        assert resp.status_code == 200
        assert conn.execute("SELECT transcript_edited FROM segments WHERE id=?", (seg,)).fetchone()["transcript_edited"] == "edited"


# ========================================================================
# O4 — flags serialised as JSON array
# ========================================================================

class TestFlagsSerialisation:
    def test_flags_empty_array_when_null(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_segment(conn, project, src, status="pending")

        seg = client.get(f"/projects/{project}/segments").json()["segments"][0]
        assert seg["flags"] == []

    def test_flags_parsed_when_present(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_segment(conn, project, src, status="pending", flags=["short_transcript"])

        seg = client.get(f"/projects/{project}/segments").json()["segments"][0]
        assert seg["flags"] == ["short_transcript"]


# ========================================================================
# O5 — post-export invalidation
# ========================================================================

class TestPostExportInvalidation:
    def _mark_exported(self, conn, project, pdir):
        (pdir / "export.tar.gz").write_bytes(b"\x00" * 10)
        conn.execute("UPDATE projects SET status='exported', exported_at=? WHERE id=?", (_now(), project))
        conn.commit()

    def test_approval_change_returns_to_review(self, client, project):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        src = _insert_source(conn, project, "complete")
        seg = _insert_segment(conn, project, src, status="approved")
        self._mark_exported(conn, project, pdir)

        resp = client.patch(f"/projects/{project}/segments/{seg}", json={"status": "rejected"})
        assert resp.status_code == 200

        status = conn.execute("SELECT status, exported_at FROM projects WHERE id=?", (project,)).fetchone()
        assert status["status"] == "review"
        assert status["exported_at"] is None

    def test_new_source_invalidates_export(self, client, project):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        _insert_source(conn, project, "complete")
        _insert_segment(conn, project, _insert_source(conn, project, "complete"), status="approved")
        self._mark_exported(conn, project, pdir)

        # Upload a new source (mock enqueue so no extraction job actually runs).
        with patch("routers.sources.enqueue", return_value="jid"):
            resp = client.post(
                f"/projects/{project}/sources",
                files={"file": ("ep2.mkv", b"video-bytes", "video/x-matroska")},
            )
        assert resp.status_code == 202

        status = conn.execute("SELECT status, exported_at FROM projects WHERE id=?", (project,)).fetchone()
        assert status["status"] != "exported"
        assert status["exported_at"] is None

    def test_download_refuses_after_invalidation(self, client, project):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        src = _insert_source(conn, project, "complete")
        seg = _insert_segment(conn, project, src, status="approved")
        self._mark_exported(conn, project, pdir)

        client.patch(f"/projects/{project}/segments/{seg}", json={"status": "maybe"})
        resp = client.get(f"/projects/{project}/export/download")
        assert resp.status_code == 404


# ========================================================================
# O6 — re-export regenerates export/; no orphan WAVs; clipping in manifest
# ========================================================================

def _export_with_cleanup(project, conn, pdir, seg_statuses):
    """Run an export where cleanup writes a WAV per approved segment and reports
    the given per-segment status ('success' or 'clipping')."""
    import jobs

    async def mock_submit(service, payload):
        if service == "cleanup":
            for s in payload["segments"]:
                out = pdir / "export" / f"{s['id']}.wav"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"\x00" * 20)
        return {"job_id": payload["job_id"]}

    async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
        results = []
        for seg_id, kind in seg_statuses.items():
            results.append({
                "id": seg_id,
                "output_path": f"/data/export/{seg_id}.wav",
                "clipping_warning": kind == "clipping",
                "auto_rejected": False,
                "error": None,
            })
        return {"job_id": job_id, "status": "complete", "results": results}

    conn.execute("UPDATE projects SET status='exporting', updated_at=? WHERE id=?", (_now(), project))
    conn.commit()
    with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
         patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
        _enqueue_and_run(project, "export", params={"segment_count": len(seg_statuses)})


class TestReExport:
    def test_reject_then_reexport_has_no_orphan_wavs(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        src = _insert_source(conn, project, "complete")
        seg_a = _insert_segment(conn, project, src, status="approved", transcript="A")
        seg_b = _insert_segment(conn, project, src, status="approved", transcript="B")

        # First export: both approved.
        _export_with_cleanup(project, conn, pdir, {seg_a: "success", seg_b: "success"})
        with tarfile.open(pdir / "export.tar.gz", "r:gz") as tar:
            names = set(tar.getnames())
        assert names == {"manifest.json", f"{seg_a}.wav", f"{seg_b}.wav"}

        # Reject B, re-export: only A must remain in the archive.
        conn.execute("UPDATE segments SET status='rejected' WHERE id=?", (seg_b,))
        conn.commit()
        _export_with_cleanup(project, conn, pdir, {seg_a: "success"})
        with tarfile.open(pdir / "export.tar.gz", "r:gz") as tar:
            names = set(tar.getnames())
        assert names == {"manifest.json", f"{seg_a}.wav"}
        assert f"{seg_b}.wav" not in names

        manifest = json.loads((pdir / "export" / "manifest.json").read_text())
        assert {s["id"] for s in manifest["segments"]} == {seg_a}

    def test_clipping_segment_exported_with_flag(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        src = _insert_source(conn, project, "complete")
        seg = _insert_segment(conn, project, src, status="approved", transcript="hi")

        _export_with_cleanup(project, conn, pdir, {seg: "clipping"})

        row = conn.execute("SELECT status, clipping_warning, export_path FROM segments WHERE id=?", (seg,)).fetchone()
        assert row["status"] == "clipping_warning"
        assert row["clipping_warning"] == 1
        assert row["export_path"] == f"export/{seg}.wav"

        manifest = json.loads((pdir / "export" / "manifest.json").read_text())
        entry = next(s for s in manifest["segments"] if s["id"] == seg)
        assert entry["clipping_warning"] is True
        with tarfile.open(pdir / "export.tar.gz", "r:gz") as tar:
            assert f"{seg}.wav" in tar.getnames()


# ========================================================================
# O7 — reference replacement is atomic
# ========================================================================

class TestReferenceReplacement:
    def test_failed_replacement_preserves_original(self, client, project):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)

        # Existing valid reference on disk + in DB.
        original = pdir / "reference.wav"
        original.write_bytes(b"ORIGINAL-REFERENCE-DATA")
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project,))
        conn.commit()

        # Upload a too-short clip (duration 0 via un-parseable bytes) — must 422
        # and leave the original untouched.
        resp = client.post(
            f"/projects/{project}/reference",
            files={"file": ("bad.wav", b"not-a-real-wav", "audio/wav")},
        )
        assert resp.status_code == 422
        assert resp.json()["error"] == "reference_too_short"

        assert original.read_bytes() == b"ORIGINAL-REFERENCE-DATA"
        assert conn.execute("SELECT reference_path FROM projects WHERE id=?", (project,)).fetchone()["reference_path"] == "reference.wav"
        # No leftover temp files.
        assert not list(pdir.glob(".reference.*.tmp"))


# ========================================================================
# O8 — unreachable service is retried until healthy
# ========================================================================

class TestSubmitRetry:
    def test_unreachable_then_healthy_succeeds(self, client, project, isolated_data_dir, monkeypatch):
        import db
        import jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project, "step1_pending")

        monkeypatch.setattr(jobs, "_SUBMIT_RETRY_BASE_SECS", 0.01)
        monkeypatch.setattr(jobs, "_SUBMIT_RETRY_MAX_SECS", 0.02)
        monkeypatch.setattr(jobs, "_SUBMIT_RETRY_TIMEOUT_SECS", 5.0)

        attempts = {"n": 0}

        async def flaky_submit(service, payload):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("connection refused")
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "progress": 100}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=flaky_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "vocal_separation", source_id=src)

        assert attempts["n"] == 3
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "complete"
        # Source must NOT have been marked failed.
        assert conn.execute("SELECT status FROM sources WHERE id=?", (src,)).fetchone()["status"] != "step1_failed"

    def test_http_status_error_is_not_retried(self, client, project, isolated_data_dir, monkeypatch):
        import db
        import jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project, "step1_pending")
        monkeypatch.setattr(jobs, "_SUBMIT_RETRY_BASE_SECS", 0.01)

        attempts = {"n": 0}

        async def erroring_submit(service, payload):
            attempts["n"] += 1
            raise httpx.HTTPStatusError(
                "bad request",
                request=httpx.Request("POST", "http://svc/jobs"),
                response=httpx.Response(400),
            )

        with patch("service_client.submit_job", new=AsyncMock(side_effect=erroring_submit)):
            _enqueue_and_run(project, "vocal_separation", source_id=src)

        assert attempts["n"] == 1  # reachable service error → no retry
        assert conn.execute("SELECT status FROM sources WHERE id=?", (src,)).fetchone()["status"] == "step1_failed"


# ========================================================================
# O10 — empty ?status= must not 500
# ========================================================================

class TestEmptyStatusFilter:
    def test_empty_status_returns_empty(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_segment(conn, project, src, status="pending")

        resp = client.get(f"/projects/{project}/segments?status=")
        assert resp.status_code == 200
        assert resp.json()["segments"] == []
        assert resp.json()["pagination"]["total"] == 0

    def test_empty_status_count_only(self, client, project):
        resp = client.get(f"/projects/{project}/segments?status=&count_only=true")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# ========================================================================
# O12 — crash recovery uses a fresh job id
# ========================================================================

class TestRecovery:
    def test_recovery_supersedes_with_fresh_job_id(self, isolated_data_dir, monkeypatch):
        import db
        import jobs
        # Don't let recovery actually start executing the requeued job — we only
        # want to inspect the rows it writes.
        monkeypatch.setattr(jobs, "_ensure_runner", lambda pid: None)
        project_id = str(uuid.uuid4())
        db.create_project_db(project_id)
        conn = db.get_conn(project_id)
        now = _now()
        conn.execute(
            "INSERT INTO projects (id, name, created_at, updated_at, status) VALUES (?,?,?,?,'processing')",
            (project_id, "P", now, now),
        )
        source_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) VALUES (?,?,?,?,'step2_running',?,?)",
            (source_id, project_id, "ep.wav", "source/ep.wav", now, now),
        )
        old_job = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO jobs (id, project_id, source_id, type, status, params, created_at) VALUES (?,?,?,'diarisation','running','{}',?)",
            (old_job, project_id, source_id, now),
        )
        conn.commit()

        loop = asyncio.new_event_loop()
        loop.run_until_complete(jobs.recover_jobs())
        # Drain the queued item so the (cancelled-on-close) runner can't run it.
        try:
            new_from_queue = jobs._queues[project_id].get_nowait()
        except Exception:
            new_from_queue = None
        loop.close()

        old = conn.execute("SELECT status FROM jobs WHERE id=?", (old_job,)).fetchone()
        assert old["status"] == "cancelled"

        fresh = conn.execute(
            "SELECT id, type, source_id, status FROM jobs WHERE status='queued'"
        ).fetchall()
        assert len(fresh) == 1
        assert fresh[0]["id"] != old_job
        assert fresh[0]["type"] == "diarisation"
        assert fresh[0]["source_id"] == source_id
        assert new_from_queue == fresh[0]["id"]


# ========================================================================
# O13 — export concurrency guard
# ========================================================================

class TestExportConcurrency:
    def test_concurrent_export_rejected(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project, "complete")
        _insert_segment(conn, project, src, status="approved")
        # An export job is already in flight.
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) VALUES (?,?,'export','running',?)",
            (str(uuid.uuid4()), project, _now()),
        )
        conn.commit()

        resp = client.post(f"/projects/{project}/export")
        assert resp.status_code == 409
        assert resp.json()["error"] == "export_in_progress"


# ========================================================================
# O14 — reprocess deletes orphaned WAVs + confirmation copy matches steps
# ========================================================================

class TestReprocessCleanup:
    def test_reprocess_deletes_orphan_wavs(self, client, project):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        src = _insert_source(conn, project, "complete", vocals_path="audio/vocals/x.wav")
        seg = _insert_segment(conn, project, src, status="pending")

        raw = pdir / "segments" / "raw" / f"{seg}.wav"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"\x00" * 10)
        export = pdir / "export" / f"{seg}.wav"
        export.parent.mkdir(parents=True, exist_ok=True)
        export.write_bytes(b"\x00" * 10)
        conn.execute("UPDATE segments SET export_path=? WHERE id=?", (f"export/{seg}.wav", seg))
        conn.commit()

        resp = client.post(f"/projects/{project}/sources/{src}/reprocess", json={"steps": ["step2"]})
        assert resp.status_code == 202

        assert not raw.exists()
        assert not export.exists()
        assert conn.execute("SELECT COUNT(*) FROM segments WHERE source_id=?", (src,)).fetchone()[0] == 0

    def test_confirmation_copy_names_step1(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project, "complete")
        _insert_segment(conn, project, src, status="approved")

        resp = client.post(f"/projects/{project}/sources/{src}/reprocess", json={"steps": ["step1"]})
        assert resp.status_code == 409
        assert "step 1" in resp.json()["message"]

    def test_confirmation_copy_names_both_steps(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project, "complete")
        _insert_segment(conn, project, src, status="approved")

        resp = client.post(f"/projects/{project}/sources/{src}/reprocess", json={"steps": ["step1", "step2"]})
        assert resp.status_code == 409
        assert "steps 1 and 2" in resp.json()["message"]


# ========================================================================
# O9 — flat error format for validation + unhandled errors
# ========================================================================

class TestErrorFormat:
    def test_validation_error_is_flat(self, client, project):
        # target_duration_secs must be > 0 — send an invalid body.
        resp = client.post("/projects", json={"name": "x", "match_threshold": 5.0})
        assert resp.status_code == 422
        body = resp.json()
        assert set(body) == {"error", "message", "detail"}
        assert body["error"] == "validation_error"

    def test_unhandled_error_is_flat(self, project):
        from fastapi.testclient import TestClient
        from main import app
        with TestClient(app, raise_server_exceptions=False) as c:
            with patch("routers.projects._project_detail", side_effect=RuntimeError("boom")):
                resp = c.get(f"/projects/{project}")
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "internal_error"
        assert set(body) == {"error", "message", "detail"}


# ========================================================================
# O18 — CORS origins configurable via env, with current defaults
# ========================================================================

class TestCorsOrigins:
    def test_default_origins(self, monkeypatch):
        import main
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        assert main.cors_origins() == ["http://localhost:3000", "http://127.0.0.1:3000"]

    def test_env_override(self, monkeypatch):
        import main
        monkeypatch.setenv("CORS_ORIGINS", "http://host-a:3000, http://host-b:8080 ")
        assert main.cors_origins() == ["http://host-a:3000", "http://host-b:8080"]


# ========================================================================
# O16 — queue-runner idle-timeout race
# ========================================================================

class TestIdleTimeoutRace:
    def test_item_enqueued_during_timeout_is_not_stranded(self, isolated_data_dir, monkeypatch):
        """If a job lands in the queue exactly as the runner's get() times out,
        the runner must pick it up instead of exiting and stranding it."""
        import db
        import jobs
        project_id = str(uuid.uuid4())
        db.create_project_db(project_id)
        conn = db.get_conn(project_id)
        now = _now()
        conn.execute(
            "INSERT INTO projects (id, name, created_at, updated_at, status) VALUES (?,?,?,?,'processing')",
            (project_id, "P", now, now),
        )
        job_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, params, created_at) VALUES (?,?,'dummy','queued','{}',?)",
            (job_id, project_id, now),
        )
        conn.commit()

        ran = {}

        async def dummy_handler(pid, jid, sid, params):
            ran[jid] = True
            jobs._complete_job(pid, jid)

        monkeypatch.setitem(jobs.HANDLERS, "dummy", dummy_handler)
        # After the item is processed, the next (empty) get() should time out
        # quickly so the runner exits cleanly.
        monkeypatch.setattr(jobs, "_IDLE_TIMEOUT_SECS", 0.05)

        real_wait_for = asyncio.wait_for
        calls = {"n": 0}

        async def fake_wait_for(aw, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                # Simulate the race: the item is already in the queue, but this
                # get() times out without consuming it.
                aw.close()
                raise asyncio.TimeoutError
            return await real_wait_for(aw, timeout)

        async def run():
            # Item present before the runner's first (timing-out) get().
            jobs._queues[project_id].put_nowait(job_id)
            monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
            task = asyncio.ensure_future(jobs._run_project_queue(project_id))
            await real_wait_for(asyncio.shield(task), timeout=2)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run())
        finally:
            loop.close()

        assert ran.get(job_id) is True
        assert calls["n"] >= 2  # timed out once, then consumed the item


class TestMigrationsOnOpen:
    """Databases created by an older build gain later migrations on first open."""

    def test_get_conn_applies_pending_migrations(self, isolated_data_dir):
        import sqlite3 as sq

        import db

        # Simulate a pre-exported_at database: apply only the initial schema
        # and record it in the migrations ledger.
        pdir = isolated_data_dir / "projects" / "legacy-project"
        pdir.mkdir(parents=True)
        conn = sq.connect(pdir / "project.db")
        conn.executescript((db._MIGRATIONS_DIR / "001_initial_schema.sql").read_text())
        conn.execute(
            "CREATE TABLE _migrations (filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO _migrations VALUES ('001_initial_schema.sql', datetime('now'))"
        )
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
        assert "exported_at" not in cols  # precondition: legacy schema
        conn.close()

        opened = db.get_conn("legacy-project")
        cols = {r[1] for r in opened.execute("PRAGMA table_info(projects)")}
        assert "exported_at" in cols
        applied = {r[0] for r in opened.execute("SELECT filename FROM _migrations")}
        assert "002_add_exported_at.sql" in applied
