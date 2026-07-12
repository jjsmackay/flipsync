"""Review-fix wave 2 — Worker B regression tests.

Covers:
- B1: export staging — a failed re-export preserves the previous export
  (WAVs, manifest.json, archive, export_path rows, exported_at).
- B2: clipping_warning segments are included in the re-export cleanup payload.
- B3: _submit_with_retry treats a 409 job_exists response as already-submitted.
- B4: GPU jobs wait for service readiness before taking the GPU lock; readiness
  timeout fails the job with service_unavailable; CPU jobs are not gated.

All external service HTTP calls are mocked via patch on service_client.
"""

import asyncio
import json
import socket
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _insert_source(conn, project_id, status="complete"):
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO sources
           (id, project_id, filename, file_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (source_id, project_id, "ep01.mkv", "source/ep01.mkv", status, now, now),
    )
    conn.commit()
    return source_id


def _insert_segment(conn, project_id, source_id, status="approved", confidence=0.9,
                    transcript="Hello world", clipping=0):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments
           (id, project_id, source_id, raw_path, start_secs, end_secs, speaker_label,
            match_confidence, status, transcript, clipping_warning, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, f"segments/raw/{seg_id}.wav", 10.0, 15.0,
         "SPEAKER_00", confidence, status, transcript, clipping, now, now),
    )
    conn.commit()
    return seg_id


def _create_segment_wav(pdir, seg_id):
    wav = pdir / "segments" / "raw" / f"{seg_id}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"\x00" * 100)


def _run_job(project_id, job_id):
    """Execute a single job synchronously on a throwaway event loop, with the
    background runner suppressed (matches the test_wave3_pipeline idiom)."""
    import jobs
    loop = asyncio.new_event_loop()
    orig_ensure_runner = jobs._ensure_runner
    jobs._ensure_runner = lambda pid: None
    try:
        loop.run_until_complete(jobs._execute_job(project_id, job_id))
    finally:
        jobs._ensure_runner = orig_ensure_runner
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


def _cleanup_ok_result(seg_id, project_id):
    return {
        "id": seg_id,
        "output_path": f"/data/projects/{project_id}/export_tmp/{seg_id}.wav",
        "clipping_warning": False,
        "auto_rejected": False,
        "error": None,
    }


def _run_export(project, conn, cleanup_results, submit_fn=None, poll_fn=None):
    """Enqueue and execute an export job with the cleanup service mocked."""
    async def default_submit(service, payload):
        # Emulate the cleanup service writing the staged WAVs.
        for s in payload["segments"]:
            p = Path(s["output_path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x00" * 64)
        return {"job_id": payload["job_id"]}

    async def default_poll(service, job_id, interval_secs=2.0, on_progress=None):
        return {"job_id": job_id, "status": "complete", "results": cleanup_results}

    conn.execute(
        "UPDATE projects SET status='exporting', updated_at=? WHERE id=?",
        (_now(), project),
    )
    conn.commit()

    with patch("service_client.submit_job", new=AsyncMock(side_effect=submit_fn or default_submit)), \
         patch("service_client.poll_until_complete", new=AsyncMock(side_effect=poll_fn or default_poll)):
        return _enqueue_and_run(project, "export", params={"segment_count": len(cleanup_results)})


# ========================================================================
# B1 — export staging
# ========================================================================

class TestExportStaging:
    def _project_with_approved_segment(self, project):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project)
        seg_id = _insert_segment(conn, project, source_id)
        _create_segment_wav(pdir, seg_id)
        return conn, pdir, seg_id

    def test_successful_export_promotes_staging(self, client, project, isolated_data_dir):
        """Cleanup outputs land in export_tmp/ and are promoted to export/."""
        import db
        conn, pdir, seg_id = self._project_with_approved_segment(project)

        job_id = _run_export(project, conn, [_cleanup_ok_result(seg_id, project)])

        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "complete"

        # Staged WAV promoted to the final export location.
        assert (pdir / "export" / f"{seg_id}.wav").exists()
        assert not (pdir / "export_tmp").exists()
        assert not (pdir / "export.tar.gz.tmp").exists()

        manifest = json.loads((pdir / "export" / "manifest.json").read_text())
        assert [s["id"] for s in manifest["segments"]] == [seg_id]

        with tarfile.open(pdir / "export.tar.gz", "r:gz") as tar:
            assert sorted(tar.getnames()) == sorted(["manifest.json", f"{seg_id}.wav"])

        seg = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["export_path"] == f"export/{seg_id}.wav"
        p = conn.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "exported"
        assert p["exported_at"] is not None

    def test_failed_reexport_preserves_previous_export(self, client, project, isolated_data_dir):
        """A re-export whose cleanup fails must leave the previous export —
        WAVs, manifest, archive, export_path rows and exported_at — intact."""
        conn, pdir, seg_id = self._project_with_approved_segment(project)

        _run_export(project, conn, [_cleanup_ok_result(seg_id, project)])

        old_manifest = (pdir / "export" / "manifest.json").read_bytes()
        old_archive = (pdir / "export.tar.gz").read_bytes()
        old_exported_at = conn.execute(
            "SELECT exported_at FROM projects WHERE id=?", (project,)
        ).fetchone()["exported_at"]
        assert old_exported_at is not None

        async def failing_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "failed", "error": "cleanup_boom"}

        job2 = _run_export(project, conn, [], poll_fn=failing_poll)

        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job2,)).fetchone()
        assert job["status"] == "failed"
        assert job["error"] == "cleanup_boom"

        # Previous export fully intact.
        assert (pdir / "export" / f"{seg_id}.wav").exists()
        assert (pdir / "export" / "manifest.json").read_bytes() == old_manifest
        assert (pdir / "export.tar.gz").read_bytes() == old_archive
        seg = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["export_path"] == f"export/{seg_id}.wav"
        assert seg["status"] == "approved"
        p = conn.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
        assert p["exported_at"] == old_exported_at

        # Staging cleaned up.
        assert not (pdir / "export_tmp").exists()
        assert not (pdir / "export.tar.gz.tmp").exists()

    def test_failed_submit_preserves_previous_export(self, client, project, isolated_data_dir):
        """Same guarantee when the cleanup submit itself errors out."""
        conn, pdir, seg_id = self._project_with_approved_segment(project)

        _run_export(project, conn, [_cleanup_ok_result(seg_id, project)])
        old_manifest = (pdir / "export" / "manifest.json").read_bytes()

        async def boom_submit(service, payload):
            raise httpx.HTTPStatusError(
                "500",
                request=httpx.Request("POST", "http://cleanup:8004/jobs"),
                response=httpx.Response(
                    500, request=httpx.Request("POST", "http://cleanup:8004/jobs")
                ),
            )

        job2 = _run_export(project, conn, [], submit_fn=boom_submit)

        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job2,)).fetchone()
        assert job["status"] == "failed"
        assert job["error"].startswith("cleanup_submit_failed")
        assert (pdir / "export" / "manifest.json").read_bytes() == old_manifest
        seg = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["export_path"] == f"export/{seg_id}.wav"
        assert not (pdir / "export_tmp").exists()

    def test_stale_staging_cleared_at_start(self, client, project, isolated_data_dir):
        """Leftover export_tmp/ from an interrupted run never leaks into the
        new export."""
        conn, pdir, seg_id = self._project_with_approved_segment(project)

        stale = pdir / "export_tmp" / "stale.wav"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"\x00" * 10)

        _run_export(project, conn, [_cleanup_ok_result(seg_id, project)])

        assert not (pdir / "export_tmp").exists()
        assert not (pdir / "export" / "stale.wav").exists()
        assert (pdir / "export" / f"{seg_id}.wav").exists()


# ========================================================================
# B2 — clipping_warning included in re-export
# ========================================================================

class TestClippingWarningReexport:
    def test_clipping_warning_segments_included_in_payload(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        source_id = _insert_source(conn, project)

        seg_ok = _insert_segment(conn, project, source_id, status="approved")
        seg_clip = _insert_segment(conn, project, source_id, status="clipping_warning", clipping=1)
        seg_rej = _insert_segment(conn, project, source_id, status="rejected")
        for s in (seg_ok, seg_clip, seg_rej):
            _create_segment_wav(pdir, s)

        captured = {}

        async def capture_submit(service, payload):
            captured["payload"] = payload
            for s in payload["segments"]:
                p = Path(s["output_path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"\x00" * 64)
            return {"job_id": payload["job_id"]}

        cleanup_results = [
            _cleanup_ok_result(seg_ok, project),
            {**_cleanup_ok_result(seg_clip, project), "clipping_warning": True},
        ]
        _run_export(project, conn, cleanup_results, submit_fn=capture_submit)

        payload_ids = {s["id"] for s in captured["payload"]["segments"]}
        assert payload_ids == {seg_ok, seg_clip}
        assert seg_rej not in payload_ids

        # Cleanup re-flagged the clipping segment; it stays in the manifest.
        seg = conn.execute("SELECT * FROM segments WHERE id=?", (seg_clip,)).fetchone()
        assert seg["status"] == "clipping_warning"
        assert seg["clipping_warning"] == 1
        assert seg["export_path"] == f"export/{seg_clip}.wav"
        manifest = json.loads((pdir / "export" / "manifest.json").read_text())
        assert {s["id"] for s in manifest["segments"]} == {seg_ok, seg_clip}


# ========================================================================
# B3 — idempotent submit retry
# ========================================================================

def _status_error(status_code, body=None):
    req = httpx.Request("POST", "http://transcription:8003/jobs")
    resp = httpx.Response(status_code, request=req, json=body or {})
    return httpx.HTTPStatusError(str(status_code), request=req, response=resp)


class TestSubmitRetryIdempotent:
    def test_409_job_exists_treated_as_submitted(self):
        import jobs

        err = _status_error(409, {"error": "job_exists", "message": "dup", "detail": {}})
        with patch("service_client.submit_job", new=AsyncMock(side_effect=err)):
            result = asyncio.run(jobs._submit_with_retry("transcription", {"job_id": "j1"}))
        assert result["error"] == "job_exists"

    def test_409_other_error_raises(self):
        import jobs

        err = _status_error(409, {"error": "somewhere_else", "message": "no", "detail": {}})
        with patch("service_client.submit_job", new=AsyncMock(side_effect=err)):
            with pytest.raises(httpx.HTTPStatusError):
                asyncio.run(jobs._submit_with_retry("transcription", {"job_id": "j1"}))

    def test_other_http_error_raises(self):
        import jobs

        err = _status_error(500, {"error": "boom", "message": "no", "detail": {}})
        with patch("service_client.submit_job", new=AsyncMock(side_effect=err)):
            with pytest.raises(httpx.HTTPStatusError):
                asyncio.run(jobs._submit_with_retry("transcription", {"job_id": "j1"}))


# ========================================================================
# B4 — service readiness gate for GPU jobs
# ========================================================================

def _make_bare_project():
    """Bootstrap a project DB directly (no HTTP layer)."""
    import db
    project_id = str(uuid.uuid4())
    db.create_project_db(project_id)
    conn = db.get_conn(project_id)
    now = _now()
    conn.execute(
        "INSERT INTO projects (id, name, created_at, updated_at, status) VALUES (?,?,?,?,'new')",
        (project_id, "Test", now, now),
    )
    conn.commit()
    return project_id


@pytest.fixture()
def fast_readiness(monkeypatch):
    import jobs
    monkeypatch.setenv("SERVICE_READY_TIMEOUT_SECS", "0.05")
    monkeypatch.setattr(jobs, "_SERVICE_READY_POLL_SECS", 0.01)


class TestServiceReadyGate:
    def test_gpu_job_service_never_ready_fails_service_unavailable(
        self, isolated_data_dir, fast_readiness, monkeypatch
    ):
        import db
        import jobs

        project_id = _make_bare_project()
        called = []

        async def handler(pid, jid, sid, params):
            called.append(jid)

        monkeypatch.setitem(jobs.HANDLERS, "vocal_separation", handler)

        with patch("service_client.probe_health", new=AsyncMock(return_value=False)):
            job_id = _enqueue_and_run(project_id, "vocal_separation")

        assert called == [], "handler must not run when the service is unavailable"
        job = db.get_conn(project_id).execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        assert job["status"] == "failed"
        assert job["error"].startswith("service_unavailable: vocal_separation")

    def test_gpu_job_runs_when_service_healthy(self, isolated_data_dir, fast_readiness, monkeypatch):
        import db
        import jobs

        project_id = _make_bare_project()

        async def handler(pid, jid, sid, params):
            jobs._complete_job(pid, jid)

        monkeypatch.setitem(jobs.HANDLERS, "transcription_bulk", handler)

        probe = AsyncMock(return_value=True)
        with patch("service_client.probe_health", new=probe):
            job_id = _enqueue_and_run(project_id, "transcription_bulk")

        # Probed the right service (job type → service mapping).
        probe.assert_awaited_with("transcription")
        job = db.get_conn(project_id).execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        assert job["status"] == "complete"

    def test_transient_probe_error_then_healthy(self, isolated_data_dir, fast_readiness, monkeypatch):
        """A non-DNS transport error keeps polling until healthy."""
        import db
        import jobs

        monkeypatch.setenv("SERVICE_READY_TIMEOUT_SECS", "5")
        project_id = _make_bare_project()

        async def handler(pid, jid, sid, params):
            jobs._complete_job(pid, jid)

        monkeypatch.setitem(jobs.HANDLERS, "diarisation", handler)

        refused = httpx.ConnectError("connection refused")
        probe = AsyncMock(side_effect=[refused, False, True])
        with patch("service_client.probe_health", new=probe):
            job_id = _enqueue_and_run(project_id, "diarisation")

        assert probe.await_count == 3
        job = db.get_conn(project_id).execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        assert job["status"] == "complete"

    def test_dns_failure_skips_gate(self, isolated_data_dir, fast_readiness, monkeypatch):
        """Unresolvable service hostname (unit tests, partial deploys) skips
        readiness gating instead of burning the whole readiness window."""
        import db
        import jobs

        project_id = _make_bare_project()

        async def handler(pid, jid, sid, params):
            jobs._complete_job(pid, jid)

        monkeypatch.setitem(jobs.HANDLERS, "scout_speakers", handler)

        dns_error = httpx.ConnectError("Name or service not known")
        dns_error.__cause__ = socket.gaierror(-2, "Name or service not known")
        with patch("service_client.probe_health", new=AsyncMock(side_effect=dns_error)):
            job_id = _enqueue_and_run(project_id, "scout_speakers")

        job = db.get_conn(project_id).execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        assert job["status"] == "complete"

    def test_cpu_job_never_probes_health(self, isolated_data_dir, monkeypatch):
        import db
        import jobs

        project_id = _make_bare_project()

        async def handler(pid, jid, sid, params):
            jobs._complete_job(pid, jid)

        monkeypatch.setitem(jobs.HANDLERS, "export", handler)

        probe = AsyncMock(side_effect=AssertionError("CPU jobs must not probe health"))
        with patch("service_client.probe_health", new=probe):
            job_id = _enqueue_and_run(project_id, "export")

        assert probe.await_count == 0
        job = db.get_conn(project_id).execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        assert job["status"] == "complete"

    def test_wait_for_service_ready_timeout_returns_false(self, fast_readiness):
        import jobs

        with patch("service_client.probe_health", new=AsyncMock(return_value=False)):
            assert asyncio.run(jobs.wait_for_service_ready("diarisation")) is False

    def test_gpu_job_services_covers_all_gpu_types(self):
        import jobs

        assert set(jobs.GPU_JOB_SERVICES) == set(jobs.GPU_JOB_TYPES)
