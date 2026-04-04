# Wave 3 — Pipeline Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the FlipSync orchestrator to call real processing services (vocal separation, diarisation, transcription, cleanup/export) by replacing stub job handlers with full implementations.

**Architecture:** New `service_client.py` provides async `submit_job`/`poll_job` primitives. Real job handlers in `jobs.py` use these to drive the pipeline, writing results back to SQLite. Export handler also runs manifest + tar.gz packaging. All deferred Wave 1 bugs are fixed here.

**Tech Stack:** Python 3.11, FastAPI, httpx (async HTTP), SQLite, asyncio, tarfile, json, unittest.mock (tests)

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `services/orchestrator/service_client.py` | **Create** | `submit_job()`, `poll_job()` — HTTP primitives for processing services |
| `services/orchestrator/jobs.py` | **Modify** | Replace 5 stub handlers with real implementations; add `_get_service_url()` |
| `services/orchestrator/status.py` | **Modify** | Fix export_complete circular logic (deferred bug #1) |
| `services/orchestrator/routers/projects.py` | **Modify** | Fix low_coverage_warning bug (deferred bug #2) |
| `services/orchestrator/tests/test_wave3_pipeline.py` | **Create** | Full pipeline tests — all handlers, OOM retry, export edge cases |

---

## Task 1: service_client.py — HTTP client module

**Files:**
- Create: `services/orchestrator/service_client.py`

- [ ] **Step 1: Write the test**

Create `services/orchestrator/tests/test_wave3_pipeline.py` with just the service_client tests first:

```python
"""Wave 3 pipeline integration tests.

All external service calls are mocked via unittest.mock.patch on
service_client.submit_job and service_client.poll_job.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _make_project(tmp_path, match_threshold=0.75):
    """Bootstrap a project DB directly without going through the HTTP layer."""
    import os
    os.environ["DATA_DIR"] = str(tmp_path)
    import db
    db._connections.clear()
    project_id = str(uuid.uuid4())
    db.create_project_db(project_id)
    conn = db.get_conn(project_id)
    now = _now()
    conn.execute(
        """INSERT INTO projects (id, name, created_at, updated_at, status, match_threshold, whisper_model)
           VALUES (?,?,?,?,'ready',?,'large-v2')""",
        (project_id, "Test", now, now, match_threshold),
    )
    conn.commit()
    return project_id


def _insert_source(conn, project_id, status="step1_pending", audio_path=None, vocals_path=None):
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO sources (id, project_id, filename, file_path, audio_path, vocals_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (source_id, project_id, "ep.mkv", "source/ep.mkv",
         audio_path or f"audio/raw/{source_id}.wav",
         vocals_path,
         status, now, now),
    )
    conn.commit()
    return source_id


def _insert_segment(conn, project_id, source_id, status="pending", confidence=0.9,
                    transcript=None, transcript_edited=None, raw_path=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs,
           speaker_label, match_confidence, status, transcript, transcript_edited, created_at, updated_at)
           VALUES (?,?,?,?,0,5,'S0',?,?,?,?,?,?)""",
        (seg_id, project_id, source_id,
         raw_path or f"segments/raw/{seg_id}.wav",
         confidence, status, transcript, transcript_edited, now, now),
    )
    conn.commit()
    return seg_id


class TestServiceClient:
    async def test_submit_job_posts_to_service(self):
        import httpx
        import respx
        # We test service_client as a unit using httpx mock transport
        from service_client import submit_job

        with respx.mock:
            respx.post("http://svc:8001/jobs").mock(
                return_value=httpx.Response(202, json={"job_id": "abc"})
            )
            result = await submit_job("http://svc:8001", {"job_id": "abc", "model": "htdemucs"})
        assert result["job_id"] == "abc"

    async def test_poll_job_returns_on_complete(self):
        import httpx
        import respx
        from service_client import poll_job

        with respx.mock:
            respx.get("http://svc:8001/jobs/abc").mock(
                return_value=httpx.Response(200, json={"job_id": "abc", "status": "complete", "output_path": "/data/out.wav"})
            )
            result = await poll_job("http://svc:8001", "abc", poll_interval=0)
        assert result["status"] == "complete"

    async def test_poll_job_calls_on_progress_for_running(self):
        import httpx
        import respx
        from service_client import poll_job

        progress_calls = []

        async def capture(r):
            progress_calls.append(r["progress"])

        responses = [
            httpx.Response(200, json={"job_id": "abc", "status": "running", "progress": 50}),
            httpx.Response(200, json={"job_id": "abc", "status": "complete", "progress": 100, "output_path": "/out.wav"}),
        ]
        with respx.mock:
            respx.get("http://svc:8001/jobs/abc").mock(side_effect=responses)
            result = await poll_job("http://svc:8001", "abc", poll_interval=0, on_progress=capture)

        assert result["status"] == "complete"
        assert progress_calls == [50]
```

- [ ] **Step 2: Run test — expect import failure**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestServiceClient -v 2>&1 | tail -20
```

Expected: `ModuleNotFoundError: No module named 'service_client'`

- [ ] **Step 3: Create service_client.py**

```python
"""HTTP client primitives for FlipSync processing services.

Services use an async job pattern:
  POST /jobs → 202 with {job_id}
  GET /jobs/{job_id} → poll until status is 'complete' or 'failed'

The orchestrator polls every poll_interval seconds (default 2s per spec).
on_progress is an optional async callable invoked on each non-terminal poll.
"""

import asyncio

import httpx


async def submit_job(service_url: str, payload: dict) -> dict:
    """POST /jobs to a processing service. Returns the 202 response body."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{service_url}/jobs", json=payload)
        resp.raise_for_status()
        return resp.json()


async def poll_job(
    service_url: str,
    job_id: str,
    poll_interval: float = 2.0,
    on_progress=None,
) -> dict:
    """Poll GET /jobs/{job_id} until status is 'complete' or 'failed'.

    Calls on_progress(result) on each non-terminal poll — must be async if provided.
    Returns the final result dict.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(f"{service_url}/jobs/{job_id}")
            resp.raise_for_status()
            result = resp.json()
            if result.get("status") in ("complete", "failed"):
                return result
            if on_progress is not None:
                await on_progress(result)
            await asyncio.sleep(poll_interval)
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestServiceClient -v 2>&1 | tail -20
```

Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
cd services/orchestrator && git add service_client.py tests/test_wave3_pipeline.py
git commit -m "feat: add service_client HTTP primitives with tests

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Fix deferred bugs (status.py + projects.py)

**Files:**
- Modify: `services/orchestrator/status.py`
- Modify: `services/orchestrator/routers/projects.py`

These are known bugs from Wave 1 code review that must be fixed before the real handlers can work correctly.

- [ ] **Step 1: Write failing tests for both bugs**

Add to `tests/test_wave3_pipeline.py`:

```python
class TestDeferredBugFixes:
    def test_export_complete_sets_exported_status(self, isolated_data_dir):
        """Bug #1: recompute_project_status must reach 'exported' after export job completes."""
        from status import recompute_project_status
        import db

        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        now = _now()

        # Simulate: export job just completed — status is still 'exporting'
        conn.execute("UPDATE projects SET status='exporting', updated_at=? WHERE id=?", (now, project_id))
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at, completed_at) VALUES (?,?,'export','complete',?,?)",
            (str(uuid.uuid4()), project_id, now, now),
        )
        conn.commit()

        # After job completes, no active jobs, exporting → should become 'exported'
        recompute_project_status(project_id)
        status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"]
        assert status == "exported"

    def test_low_coverage_warning_at_zero(self, client, isolated_data_dir):
        """Bug #2: coverage_ratio=0.0 should trigger low_coverage_warning."""
        project_id = _make_project(isolated_data_dir)
        import db
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        conn.execute("UPDATE sources SET coverage_ratio=0.0 WHERE id=?", (source_id,))
        conn.commit()

        from routers.projects import _project_stats
        stats = _project_stats(project_id)
        cov = next(s for s in stats["source_coverage"] if s["source_id"] == source_id)
        assert cov["low_coverage_warning"] is True

    def test_low_coverage_warning_not_triggered_when_null(self, client, isolated_data_dir):
        """coverage_ratio IS NULL (not yet diarised) should NOT warn."""
        project_id = _make_project(isolated_data_dir)
        import db
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending")
        # coverage_ratio is NULL by default

        from routers.projects import _project_stats
        stats = _project_stats(project_id)
        cov = next(s for s in stats["source_coverage"] if s["source_id"] == source_id)
        assert cov["low_coverage_warning"] is False
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestDeferredBugFixes -v 2>&1 | tail -20
```

Expected: `FAILED` for `test_export_complete_sets_exported_status` and `test_low_coverage_warning_at_zero`

- [ ] **Step 3: Fix status.py — export_complete circular logic**

Replace the `export_complete` derivation in `recompute_project_status`:

In `services/orchestrator/status.py`, replace:
```python
export_complete = project["status"] == "exported"
```
with:
```python
# Check for a completed export job rather than current status to break the
# circular dependency (exporting → exported can never happen via recompute
# if export_complete is derived from current status).
completed_export = conn.execute(
    "SELECT COUNT(*) FROM jobs WHERE project_id=? AND type='export' AND status='complete'",
    (project_id,),
).fetchone()[0]
export_complete = completed_export > 0
```

- [ ] **Step 4: Fix projects.py — low_coverage_warning at zero**

In `services/orchestrator/routers/projects.py`, in `_project_stats`, replace:
```python
"low_coverage_warning": ratio > 0 and ratio < 0.15,
```
with:
```python
"low_coverage_warning": s["coverage_ratio"] is not None and s["coverage_ratio"] < 0.15,
```

- [ ] **Step 5: Run tests — expect pass**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestDeferredBugFixes -v 2>&1 | tail -20
```

Expected: `3 passed`

- [ ] **Step 6: Run full test suite to verify no regressions**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all previously passing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/status.py services/orchestrator/routers/projects.py services/orchestrator/tests/test_wave3_pipeline.py
git commit -m "fix: resolve deferred Wave 1 bugs (export status circular logic, low coverage warning at zero)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Vocal Separation Handler

**Files:**
- Modify: `services/orchestrator/jobs.py`
- Modify: `services/orchestrator/tests/test_wave3_pipeline.py`

- [ ] **Step 1: Write the tests**

Add to `tests/test_wave3_pipeline.py`:

```python
class TestVocalSeparationHandler:
    async def test_success_updates_source_to_step2_pending(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending",
                                   audio_path=f"audio/raw/{source_id_placeholder}.wav")
        # Recreate with correct audio_path
        conn.execute("UPDATE sources SET audio_path=? WHERE id=?",
                     (f"audio/raw/{source_id}.wav", source_id))
        conn.commit()

        svc_result = {
            "job_id": "svc-job-1",
            "status": "complete",
            "progress": 100,
            "output_path": f"/data/projects/{project_id}/audio/vocals/{source_id}.wav",
            "error": None,
            "retry_with_chunk_secs": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock) as mock_submit, \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=svc_result):
            mock_submit.return_value = {"job_id": "svc-job-1"}
            job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id)
            await jobs._handle_vocal_separation(project_id, job_id, source_id, {})

        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "step2_pending"
        assert source["vocals_path"] == f"audio/vocals/{source_id}.wav"
        assert source["step1_model"] == "htdemucs"

        # A diarisation job should have been enqueued
        diar_job = conn.execute(
            "SELECT * FROM jobs WHERE project_id=? AND type='diarisation'", (project_id,)
        ).fetchone()
        assert diar_job is not None
        assert diar_job["source_id"] == source_id

    async def test_success_with_custom_model(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending")
        conn.execute("UPDATE sources SET audio_path=? WHERE id=?",
                     (f"audio/raw/{source_id}.wav", source_id))
        conn.commit()

        svc_result = {
            "job_id": "svc-1", "status": "complete", "progress": 100,
            "output_path": f"/data/projects/{project_id}/audio/vocals/{source_id}.wav",
            "error": None, "retry_with_chunk_secs": None,
        }
        with patch("jobs.submit_job", new_callable=AsyncMock) as mock_submit, \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=svc_result):
            mock_submit.return_value = {"job_id": "svc-1"}
            job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id,
                                  params={"demucs_model": "mdx_extra"})
            await jobs._handle_vocal_separation(project_id, job_id, source_id,
                                                {"demucs_model": "mdx_extra"})

        source = conn.execute("SELECT step1_model FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["step1_model"] == "mdx_extra"

    async def test_oom_retry_succeeds_on_second_attempt(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending")
        conn.execute("UPDATE sources SET audio_path=? WHERE id=?",
                     (f"audio/raw/{source_id}.wav", source_id))
        conn.commit()

        first_fail = {
            "job_id": "svc-1", "status": "failed", "error": "cuda_oom",
            "retry_with_chunk_secs": 60,
        }
        retry_success = {
            "job_id": "svc-2", "status": "complete", "progress": 100,
            "output_path": f"/data/projects/{project_id}/audio/vocals/{source_id}.wav",
            "error": None, "retry_with_chunk_secs": None,
        }

        poll_results = [first_fail, retry_success]
        with patch("jobs.submit_job", new_callable=AsyncMock) as mock_submit, \
             patch("jobs.poll_job", new_callable=AsyncMock, side_effect=poll_results):
            mock_submit.return_value = {"job_id": "svc-1"}
            job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id)
            await jobs._handle_vocal_separation(project_id, job_id, source_id, {})

        # Second submit call should have chunk_secs set
        assert mock_submit.call_count == 2
        second_call_payload = mock_submit.call_args_list[1][0][1]  # (service_url, payload)
        assert second_call_payload["chunk_secs"] == 60

        source = conn.execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "step2_pending"

    async def test_oom_retry_fails_on_second_attempt_marks_step1_failed(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending")
        conn.execute("UPDATE sources SET audio_path=? WHERE id=?",
                     (f"audio/raw/{source_id}.wav", source_id))
        conn.commit()

        fail1 = {"job_id": "svc-1", "status": "failed", "error": "cuda_oom", "retry_with_chunk_secs": 60}
        fail2 = {"job_id": "svc-2", "status": "failed", "error": "cuda_oom", "retry_with_chunk_secs": None}

        with patch("jobs.submit_job", new_callable=AsyncMock) as mock_submit, \
             patch("jobs.poll_job", new_callable=AsyncMock, side_effect=[fail1, fail2]):
            mock_submit.return_value = {"job_id": "svc-1"}
            job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id)
            await jobs._handle_vocal_separation(project_id, job_id, source_id, {})

        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "step1_failed"
        assert source["step1_error"] is not None

        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"

    async def test_non_oom_failure_marks_step1_failed(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending")
        conn.execute("UPDATE sources SET audio_path=? WHERE id=?",
                     (f"audio/raw/{source_id}.wav", source_id))
        conn.commit()

        fail = {"job_id": "svc-1", "status": "failed", "error": "model_error", "retry_with_chunk_secs": None}

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=fail):
            job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id)
            await jobs._handle_vocal_separation(project_id, job_id, source_id, {})

        source = conn.execute("SELECT status, step1_error FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "step1_failed"
        assert "model_error" in source["step1_error"]
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestVocalSeparationHandler -v 2>&1 | tail -20
```

Expected: all FAILED (stub handler calls `_fail_job` immediately)

- [ ] **Step 3: Implement vocal separation handler in jobs.py**

Add `_get_service_url` helper and replace `_handle_stub_service_job` for `vocal_separation`. Add these imports at the top of `jobs.py`:

```python
import os
import uuid as _uuid_module
from service_client import submit_job, poll_job
```

Add `_get_service_url` near the top of `jobs.py` (after imports):

```python
def _get_service_url(service_name: str) -> str:
    urls = {
        "vocal_separation": os.environ.get("VOCAL_SEPARATION_URL", "http://vocal-separation:8001"),
        "diarisation": os.environ.get("DIARISATION_URL", "http://diarisation:8002"),
        "transcription": os.environ.get("TRANSCRIPTION_URL", "http://transcription:8003"),
        "cleanup": os.environ.get("CLEANUP_URL", "http://cleanup:8004"),
    }
    return urls[service_name]
```

Add the real handler:

```python
async def _handle_vocal_separation(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Submit to vocal-separation service; handle OOM retry with chunk_secs."""
    conn = get_conn(project_id)
    source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if source is None:
        _fail_job(project_id, job_id, "source_not_found")
        return

    pdir = project_dir(project_id)
    input_path = str(pdir / source["audio_path"])
    output_path = str(pdir / "audio" / "vocals" / f"{source_id}.wav")

    model = params.get("demucs_model", "htdemucs")
    chunk_secs = params.get("chunk_secs", None)

    conn.execute(
        "UPDATE sources SET status='step1_running', updated_at=? WHERE id=?",
        (_now(), source_id),
    )
    conn.commit()

    svc_url = _get_service_url("vocal_separation")
    service_job_id = job_id  # use orchestrator job_id for first attempt

    payload = {
        "job_id": service_job_id,
        "input_path": input_path,
        "output_path": output_path,
        "model": model,
        "chunk_secs": chunk_secs,
    }

    await submit_job(svc_url, payload)

    async def _update_vs_progress(r):
        _update_progress(project_id, job_id, r.get("progress", 0))

    result = await poll_job(svc_url, service_job_id, on_progress=_update_vs_progress)

    if result["status"] == "failed":
        retry_secs = result.get("retry_with_chunk_secs")
        if retry_secs and chunk_secs is None:
            # OOM retry: submit a new service job with chunk_secs
            retry_service_job_id = str(_uuid_module.uuid4())
            retry_payload = {**payload, "job_id": retry_service_job_id, "chunk_secs": retry_secs}
            await submit_job(svc_url, retry_payload)
            result = await poll_job(svc_url, retry_service_job_id, on_progress=_update_vs_progress)

    if result["status"] == "failed":
        error = result.get("error", "vocal_separation_failed")
        conn.execute(
            "UPDATE sources SET status='step1_failed', step1_error=?, updated_at=? WHERE id=?",
            (error, _now(), source_id),
        )
        conn.commit()
        _fail_job(project_id, job_id, error)
        _recompute_project_status(project_id)
        return

    # Success
    vocals_path = f"audio/vocals/{source_id}.wav"
    conn.execute(
        "UPDATE sources SET status='step2_pending', vocals_path=?, step1_model=?, updated_at=? WHERE id=?",
        (vocals_path, model, _now(), source_id),
    )
    conn.commit()
    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)

    # Enqueue diarisation for this source
    enqueue(project_id, "diarisation", source_id=source_id)
```

Update `HANDLERS` registry to replace the stub for `vocal_separation`:

```python
HANDLERS: dict[str, Callable] = {
    "extract_audio": _handle_extract_audio,
    "vocal_separation": _handle_vocal_separation,
    "diarisation": _handle_stub_service_job,
    "transcription_bulk": _handle_stub_service_job,
    "transcription_segment": _handle_stub_service_job,
    "export": _handle_stub_service_job,
}
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestVocalSeparationHandler -v 2>&1 | tail -20
```

Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/jobs.py services/orchestrator/tests/test_wave3_pipeline.py
git commit -m "feat: implement vocal separation job handler with OOM retry

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Diarisation Handler

**Files:**
- Modify: `services/orchestrator/jobs.py`
- Modify: `services/orchestrator/tests/test_wave3_pipeline.py`

- [ ] **Step 1: Write the tests**

Add to `tests/test_wave3_pipeline.py`:

```python
class TestDiarisationHandler:
    def _make_diar_result(self, project_id, source_id, segments_data):
        """Build a mock diarisation poll result."""
        segments = []
        for d in segments_data:
            seg_id = str(uuid.uuid4())
            segments.append({
                "id": seg_id,
                "start_secs": d["start"],
                "end_secs": d["end"],
                "speaker_label": d.get("speaker", "SPEAKER_00"),
                "match_confidence": d["confidence"],
                "wav_path": f"/data/projects/{project_id}/segments/raw/{seg_id}.wav",
            })
        return {
            "job_id": "svc-diar-1",
            "status": "complete",
            "segments": segments,
            "coverage_ratio": 0.25,
            "error": None,
        }

    async def test_writes_segments_to_db(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir, match_threshold=0.75)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step2_pending",
                                   vocals_path=f"audio/vocals/{source_id_placeholder}.wav")
        conn.execute("UPDATE sources SET vocals_path=? WHERE id=?",
                     (f"audio/vocals/{source_id}.wav", source_id))
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
        conn.commit()

        diar_result = self._make_diar_result(project_id, source_id, [
            {"start": 0.0, "end": 5.0, "confidence": 0.9},   # above threshold → pending
            {"start": 6.0, "end": 10.0, "confidence": 0.6},  # below threshold → below_threshold
            {"start": 11.0, "end": 15.0, "confidence": 0.8}, # above threshold → pending
        ])

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-diar-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=diar_result):
            job_id = jobs.enqueue(project_id, "diarisation", source_id=source_id)
            await jobs._handle_diarisation(project_id, job_id, source_id, {})

        segments = conn.execute("SELECT * FROM segments WHERE source_id=?", (source_id,)).fetchall()
        assert len(segments) == 3

        pending = [s for s in segments if s["status"] == "pending"]
        below = [s for s in segments if s["status"] == "below_threshold"]
        assert len(pending) == 2
        assert len(below) == 1

        # Source should be complete
        src = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert src["status"] == "complete"
        assert src["coverage_ratio"] == pytest.approx(0.25)

    async def test_segments_use_relative_raw_path(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step2_pending")
        conn.execute("UPDATE sources SET vocals_path=? WHERE id=?",
                     (f"audio/vocals/{source_id}.wav", source_id))
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
        conn.commit()

        seg_id = str(uuid.uuid4())
        diar_result = {
            "job_id": "svc-1", "status": "complete",
            "segments": [{
                "id": seg_id, "start_secs": 0.0, "end_secs": 5.0,
                "speaker_label": "SPEAKER_00", "match_confidence": 0.9,
                "wav_path": f"/data/projects/{project_id}/segments/raw/{seg_id}.wav",
            }],
            "coverage_ratio": 0.2, "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=diar_result):
            job_id = jobs.enqueue(project_id, "diarisation", source_id=source_id)
            await jobs._handle_diarisation(project_id, job_id, source_id, {})

        seg = conn.execute("SELECT raw_path FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["raw_path"] == f"segments/raw/{seg_id}.wav"

    async def test_auto_triggers_transcription_when_all_sources_complete(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)

        # Two sources: one already complete, one about to complete via diarisation
        existing_source = _insert_source(conn, project_id, "complete")
        active_source = _insert_source(conn, project_id, "step2_pending")
        conn.execute("UPDATE sources SET vocals_path=? WHERE id=?",
                     (f"audio/vocals/{active_source}.wav", active_source))
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
        conn.commit()

        seg_id = str(uuid.uuid4())
        diar_result = {
            "job_id": "svc-1", "status": "complete",
            "segments": [{
                "id": seg_id, "start_secs": 0.0, "end_secs": 5.0,
                "speaker_label": "SPEAKER_00", "match_confidence": 0.9,
                "wav_path": f"/data/projects/{project_id}/segments/raw/{seg_id}.wav",
            }],
            "coverage_ratio": 0.2, "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=diar_result):
            job_id = jobs.enqueue(project_id, "diarisation", source_id=active_source)
            await jobs._handle_diarisation(project_id, job_id, active_source, {})

        # A transcription_bulk job should have been auto-enqueued
        tx_job = conn.execute(
            "SELECT * FROM jobs WHERE project_id=? AND type='transcription_bulk'", (project_id,)
        ).fetchone()
        assert tx_job is not None

    async def test_does_not_trigger_transcription_while_other_sources_pending(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)

        # Two sources: one just completed diarisation, one still step1_running
        active_source = _insert_source(conn, project_id, "step2_pending")
        still_running = _insert_source(conn, project_id, "step1_running")  # not complete yet
        conn.execute("UPDATE sources SET vocals_path=? WHERE id=?",
                     (f"audio/vocals/{active_source}.wav", active_source))
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
        conn.commit()

        seg_id = str(uuid.uuid4())
        diar_result = {
            "job_id": "svc-1", "status": "complete",
            "segments": [{
                "id": seg_id, "start_secs": 0.0, "end_secs": 5.0,
                "speaker_label": "SPEAKER_00", "match_confidence": 0.9,
                "wav_path": f"/data/projects/{project_id}/segments/raw/{seg_id}.wav",
            }],
            "coverage_ratio": 0.2, "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=diar_result):
            job_id = jobs.enqueue(project_id, "diarisation", source_id=active_source)
            await jobs._handle_diarisation(project_id, job_id, active_source, {})

        tx_job = conn.execute(
            "SELECT * FROM jobs WHERE project_id=? AND type='transcription_bulk'", (project_id,)
        ).fetchone()
        assert tx_job is None  # should NOT have been triggered

    async def test_failure_marks_source_step2_failed(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step2_pending")
        conn.execute("UPDATE sources SET vocals_path=? WHERE id=?",
                     (f"audio/vocals/{source_id}.wav", source_id))
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
        conn.commit()

        fail = {"job_id": "svc-1", "status": "failed", "error": "diarisation_failed", "segments": []}

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=fail):
            job_id = jobs.enqueue(project_id, "diarisation", source_id=source_id)
            await jobs._handle_diarisation(project_id, job_id, source_id, {})

        src = conn.execute("SELECT status, step2_error FROM sources WHERE id=?", (source_id,)).fetchone()
        assert src["status"] == "step2_failed"
        assert "diarisation_failed" in src["step2_error"]

    async def test_no_reference_clip_marks_step2_failed(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step2_pending")
        conn.execute("UPDATE sources SET vocals_path=? WHERE id=?",
                     (f"audio/vocals/{source_id}.wav", source_id))
        # reference_path is NULL by default
        conn.commit()

        job_id = jobs.enqueue(project_id, "diarisation", source_id=source_id)
        await jobs._handle_diarisation(project_id, job_id, source_id, {})

        src = conn.execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
        assert src["status"] == "step2_failed"
        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "reference" in job["error"]
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestDiarisationHandler -v 2>&1 | tail -20
```

Expected: all FAILED

- [ ] **Step 3: Implement diarisation handler in jobs.py**

```python
async def _handle_diarisation(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Submit to diarisation service; write segments to DB; auto-trigger transcription."""
    conn = get_conn(project_id)
    source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if source is None:
        _fail_job(project_id, job_id, "source_not_found")
        return

    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project["reference_path"]:
        conn.execute(
            "UPDATE sources SET status='step2_failed', step2_error='no_reference_clip', updated_at=? WHERE id=?",
            (_now(), source_id),
        )
        conn.commit()
        _fail_job(project_id, job_id, "no_reference_clip: upload a reference audio clip first")
        _recompute_project_status(project_id)
        return

    pdir = project_dir(project_id)
    input_path = str(pdir / source["vocals_path"])
    reference_path = str(pdir / project["reference_path"])
    output_dir = str(pdir / "segments" / "raw")

    conn.execute(
        "UPDATE sources SET status='step2_running', updated_at=? WHERE id=?",
        (_now(), source_id),
    )
    conn.commit()

    svc_url = _get_service_url("diarisation")
    payload = {
        "job_id": job_id,
        "input_path": input_path,
        "reference_path": reference_path,
        "output_dir": output_dir,
        "params": {
            "min_segment_duration": 1.0,
            "min_speakers": 1,
            "max_speakers": 10,
        },
    }

    await submit_job(svc_url, payload)

    async def _update_diar_progress(r):
        _update_progress(project_id, job_id, r.get("progress", 0))

    result = await poll_job(svc_url, job_id, on_progress=_update_diar_progress)

    if result["status"] == "failed":
        error = result.get("error", "diarisation_failed")
        conn.execute(
            "UPDATE sources SET status='step2_failed', step2_error=?, updated_at=? WHERE id=?",
            (error, _now(), source_id),
        )
        conn.commit()
        _fail_job(project_id, job_id, error)
        _recompute_project_status(project_id)
        return

    # Write segments to DB
    threshold = project["match_threshold"]
    now = _now()
    for seg in result.get("segments", []):
        status = "pending" if seg["match_confidence"] >= threshold else "below_threshold"
        raw_path = f"segments/raw/{seg['id']}.wav"
        conn.execute(
            """
            INSERT INTO segments
                (id, project_id, source_id, raw_path, start_secs, end_secs,
                 speaker_label, match_confidence, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (seg["id"], project_id, source_id, raw_path,
             seg["start_secs"], seg["end_secs"], seg["speaker_label"],
             seg["match_confidence"], status, now, now),
        )

    conn.execute(
        "UPDATE sources SET status='complete', coverage_ratio=?, updated_at=? WHERE id=?",
        (result.get("coverage_ratio"), _now(), source_id),
    )
    conn.commit()
    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)

    # Auto-trigger transcription if all sources are now in terminal states
    in_progress = conn.execute(
        """SELECT COUNT(*) FROM sources
           WHERE project_id=? AND status IN ('step1_pending','step1_running','step2_pending','step2_running')""",
        (project_id,),
    ).fetchone()[0]

    if in_progress == 0:
        segments_to_transcribe = conn.execute(
            """SELECT id, raw_path FROM segments
               WHERE project_id=? AND status IN ('pending','maybe') AND transcript IS NULL""",
            (project_id,),
        ).fetchall()
        if segments_to_transcribe:
            tx_params = {"segment_ids": [s["id"] for s in segments_to_transcribe]}
            enqueue(project_id, "transcription_bulk", params=tx_params)
```

Update `HANDLERS` registry:

```python
HANDLERS: dict[str, Callable] = {
    "extract_audio": _handle_extract_audio,
    "vocal_separation": _handle_vocal_separation,
    "diarisation": _handle_diarisation,
    "transcription_bulk": _handle_stub_service_job,
    "transcription_segment": _handle_stub_service_job,
    "export": _handle_stub_service_job,
}
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestDiarisationHandler -v 2>&1 | tail -20
```

Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/jobs.py services/orchestrator/tests/test_wave3_pipeline.py
git commit -m "feat: implement diarisation handler — writes segments, auto-triggers transcription

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Transcription Handlers (bulk + segment)

**Files:**
- Modify: `services/orchestrator/jobs.py`
- Modify: `services/orchestrator/tests/test_wave3_pipeline.py`

- [ ] **Step 1: Write the tests**

Add to `tests/test_wave3_pipeline.py`:

```python
class TestTranscriptionBulkHandler:
    async def test_writes_transcripts_incrementally(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg1 = _insert_segment(conn, project_id, source_id, status="pending")
        seg2 = _insert_segment(conn, project_id, source_id, status="pending")

        final_result = {
            "job_id": "svc-1", "status": "complete", "progress": 100,
            "completed_segments": [
                {"id": seg1, "transcript": "Hello world", "transcript_confidence": 0.95},
                {"id": seg2, "transcript": "Goodbye", "transcript_confidence": 0.88},
            ],
            "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=final_result):
            job_id = jobs.enqueue(project_id, "transcription_bulk",
                                  params={"segment_ids": [seg1, seg2]})
            await jobs._handle_transcription_bulk(project_id, job_id, None,
                                                  {"segment_ids": [seg1, seg2]})

        s1 = conn.execute("SELECT transcript, transcript_confidence FROM segments WHERE id=?", (seg1,)).fetchone()
        s2 = conn.execute("SELECT transcript FROM segments WHERE id=?", (seg2,)).fetchone()
        assert s1["transcript"] == "Hello world"
        assert s1["transcript_confidence"] == pytest.approx(0.95)
        assert s2["transcript"] == "Goodbye"

    async def test_deduplicates_cumulative_results(self, isolated_data_dir):
        """on_progress may write seg1; final result includes seg1 again — no duplicate write error."""
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg1 = _insert_segment(conn, project_id, source_id, status="pending")
        seg2 = _insert_segment(conn, project_id, source_id, status="pending")

        # Simulate incremental: first poll returns seg1, final returns both (cumulative)
        progress_result = {
            "job_id": "svc-1", "status": "running", "progress": 50,
            "completed_segments": [
                {"id": seg1, "transcript": "Hello", "transcript_confidence": 0.9},
            ],
        }
        final_result = {
            "job_id": "svc-1", "status": "complete", "progress": 100,
            "completed_segments": [
                {"id": seg1, "transcript": "Hello", "transcript_confidence": 0.9},
                {"id": seg2, "transcript": "World", "transcript_confidence": 0.85},
            ],
            "error": None,
        }

        # poll_job will call on_progress with progress_result, then return final_result
        # We need to simulate this by patching differently
        poll_calls = []
        async def fake_poll(svc_url, job_id, poll_interval=2.0, on_progress=None):
            if on_progress:
                await on_progress(progress_result)
            return final_result

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", side_effect=fake_poll):
            job_id = jobs.enqueue(project_id, "transcription_bulk",
                                  params={"segment_ids": [seg1, seg2]})
            await jobs._handle_transcription_bulk(project_id, job_id, None,
                                                  {"segment_ids": [seg1, seg2]})

        # Should be exactly 2 transcribed segments, not 3 (seg1 not written twice)
        rows = conn.execute(
            "SELECT id, transcript FROM segments WHERE source_id=? AND transcript IS NOT NULL",
            (source_id,)
        ).fetchall()
        assert len(rows) == 2
        transcripts = {r["id"]: r["transcript"] for r in rows}
        assert transcripts[seg1] == "Hello"
        assert transcripts[seg2] == "World"

    async def test_failure_marks_job_failed(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg1 = _insert_segment(conn, project_id, source_id, status="pending")

        fail = {"job_id": "svc-1", "status": "failed", "error": "model_load_failed",
                "completed_segments": [], "progress": 0}

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=fail):
            job_id = jobs.enqueue(project_id, "transcription_bulk",
                                  params={"segment_ids": [seg1]})
            await jobs._handle_transcription_bulk(project_id, job_id, None, {"segment_ids": [seg1]})

        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "model_load_failed" in job["error"]


class TestTranscriptionSegmentHandler:
    async def test_overwrites_transcript_and_confidence(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg_id = _insert_segment(conn, project_id, source_id, status="pending",
                                 transcript="Old transcript")

        final_result = {
            "job_id": "svc-1", "status": "complete", "progress": 100,
            "completed_segments": [
                {"id": seg_id, "transcript": "New transcript", "transcript_confidence": 0.92},
            ],
            "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=final_result):
            job_id = jobs.enqueue(project_id, "transcription_segment",
                                  params={"segment_ids": [seg_id]})
            await jobs._handle_transcription_segment(project_id, job_id, None,
                                                     {"segment_ids": [seg_id]})

        seg = conn.execute("SELECT transcript, transcript_confidence FROM segments WHERE id=?",
                           (seg_id,)).fetchone()
        assert seg["transcript"] == "New transcript"
        assert seg["transcript_confidence"] == pytest.approx(0.92)

    async def test_preserves_transcript_edited(self, isolated_data_dir):
        """Re-transcribing a segment must not touch transcript_edited."""
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg_id = _insert_segment(conn, project_id, source_id, status="pending",
                                 transcript="Old", transcript_edited="User edit preserved")

        final_result = {
            "job_id": "svc-1", "status": "complete", "progress": 100,
            "completed_segments": [
                {"id": seg_id, "transcript": "Machine new", "transcript_confidence": 0.88},
            ],
            "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=final_result):
            job_id = jobs.enqueue(project_id, "transcription_segment",
                                  params={"segment_ids": [seg_id]})
            await jobs._handle_transcription_segment(project_id, job_id, None,
                                                     {"segment_ids": [seg_id]})

        seg = conn.execute("SELECT transcript, transcript_edited FROM segments WHERE id=?",
                           (seg_id,)).fetchone()
        assert seg["transcript"] == "Machine new"
        assert seg["transcript_edited"] == "User edit preserved"  # untouched
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestTranscriptionBulkHandler tests/test_wave3_pipeline.py::TestTranscriptionSegmentHandler -v 2>&1 | tail -20
```

Expected: all FAILED

- [ ] **Step 3: Implement transcription handlers in jobs.py**

```python
async def _handle_transcription_bulk(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Transcribe a batch of segments; write results incrementally with deduplication."""
    conn = get_conn(project_id)
    segment_ids = params.get("segment_ids", [])

    project = conn.execute("SELECT whisper_model, language FROM projects WHERE id=?", (project_id,)).fetchone()
    pdir = project_dir(project_id)

    segments_data = conn.execute(
        f"SELECT id, raw_path FROM segments WHERE id IN ({','.join('?' * len(segment_ids))})",
        segment_ids,
    ).fetchall()

    payload = {
        "job_id": job_id,
        "segments": [
            {"id": s["id"], "wav_path": str(pdir / s["raw_path"])}
            for s in segments_data
        ],
        "model": project["whisper_model"],
        "language": project["language"],
        "batch_size": 16,
    }

    svc_url = _get_service_url("transcription")
    await submit_job(svc_url, payload)

    written_ids: set[str] = set()

    def _write_completed_segments(completed):
        for seg in completed:
            if seg["id"] not in written_ids:
                conn.execute(
                    "UPDATE segments SET transcript=?, transcript_confidence=?, updated_at=? WHERE id=?",
                    (seg["transcript"], seg.get("transcript_confidence"), _now(), seg["id"]),
                )
                written_ids.add(seg["id"])
        conn.commit()

    async def _on_progress(r):
        _write_completed_segments(r.get("completed_segments", []))
        _update_progress(project_id, job_id, r.get("progress", 0))

    result = await poll_job(svc_url, job_id, on_progress=_on_progress)

    if result["status"] == "failed":
        _fail_job(project_id, job_id, result.get("error", "transcription_failed"))
        _recompute_project_status(project_id)
        return

    _write_completed_segments(result.get("completed_segments", []))
    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)


async def _handle_transcription_segment(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Re-transcribe a single segment; overwrites transcript but preserves transcript_edited."""
    conn = get_conn(project_id)
    segment_id = params["segment_ids"][0]
    seg = conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
    if seg is None:
        _fail_job(project_id, job_id, "segment_not_found")
        return

    project = conn.execute("SELECT whisper_model, language FROM projects WHERE id=?", (project_id,)).fetchone()
    pdir = project_dir(project_id)

    payload = {
        "job_id": job_id,
        "segments": [{"id": segment_id, "wav_path": str(pdir / seg["raw_path"])}],
        "model": project["whisper_model"],
        "language": project["language"],
        "batch_size": 16,
    }

    svc_url = _get_service_url("transcription")
    await submit_job(svc_url, payload)
    result = await poll_job(svc_url, job_id)

    if result["status"] == "failed":
        _fail_job(project_id, job_id, result.get("error", "transcription_failed"))
        _recompute_project_status(project_id)
        return

    for seg_result in result.get("completed_segments", []):
        if seg_result["id"] == segment_id:
            # Only update transcript and confidence — NOT transcript_edited
            conn.execute(
                "UPDATE segments SET transcript=?, transcript_confidence=?, updated_at=? WHERE id=?",
                (seg_result["transcript"], seg_result.get("transcript_confidence"), _now(), segment_id),
            )
    conn.commit()
    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)
```

Update `HANDLERS` registry to replace stubs:

```python
HANDLERS: dict[str, Callable] = {
    "extract_audio": _handle_extract_audio,
    "vocal_separation": _handle_vocal_separation,
    "diarisation": _handle_diarisation,
    "transcription_bulk": _handle_transcription_bulk,
    "transcription_segment": _handle_transcription_segment,
    "export": _handle_stub_service_job,
}
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestTranscriptionBulkHandler tests/test_wave3_pipeline.py::TestTranscriptionSegmentHandler -v 2>&1 | tail -20
```

Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/jobs.py services/orchestrator/tests/test_wave3_pipeline.py
git commit -m "feat: implement transcription bulk and segment handlers

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 6: Export Handler

**Files:**
- Modify: `services/orchestrator/jobs.py`
- Modify: `services/orchestrator/tests/test_wave3_pipeline.py`

- [ ] **Step 1: Write the tests**

Add to `tests/test_wave3_pipeline.py`:

```python
class TestExportHandler:
    def _approved_segment_result(self, seg_id):
        """A cleanup result entry for a successfully processed segment."""
        return {
            "id": seg_id,
            "output_path": f"/data/export/{seg_id}.wav",
            "clipping_warning": False,
            "auto_rejected": False,
            "error": None,
        }

    async def test_success_creates_archive_and_manifest(self, isolated_data_dir):
        import db, jobs, tarfile, json
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg_id = _insert_segment(conn, project_id, source_id, status="approved",
                                 transcript="Hello world")

        # Create the raw WAV file so the export can find it
        pdir = Path(str(isolated_data_dir)) / "projects" / project_id
        raw_wav = pdir / "segments" / "raw" / f"{seg_id}.wav"
        raw_wav.parent.mkdir(parents=True, exist_ok=True)
        raw_wav.write_bytes(b"RIFF" + b"\x00" * 36)  # minimal fake WAV

        cleanup_result = {
            "job_id": "svc-1", "status": "complete",
            "results": [self._approved_segment_result(seg_id)],
            "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=cleanup_result):
            conn.execute("UPDATE projects SET status='exporting' WHERE id=?", (project_id,))
            conn.commit()
            job_id = jobs.enqueue(project_id, "export", params={"segment_count": 1})
            await jobs._handle_export(project_id, job_id, None, {"segment_count": 1})

        # Archive should exist
        archive = pdir / "export.tar.gz"
        assert archive.exists()

        # manifest.json should be inside the archive
        with tarfile.open(str(archive), "r:gz") as tar:
            names = tar.getnames()
        assert "manifest.json" in names

        # Project status should be exported
        status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"]
        assert status == "exported"

    async def test_manifest_uses_coalesce_transcript_edited(self, isolated_data_dir):
        import db, jobs, tarfile, json
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg_id = _insert_segment(conn, project_id, source_id, status="approved",
                                 transcript="Original", transcript_edited="User edit")

        pdir = Path(str(isolated_data_dir)) / "projects" / project_id
        raw_wav = pdir / "segments" / "raw" / f"{seg_id}.wav"
        raw_wav.parent.mkdir(parents=True, exist_ok=True)
        raw_wav.write_bytes(b"RIFF" + b"\x00" * 36)

        cleanup_result = {
            "job_id": "svc-1", "status": "complete",
            "results": [self._approved_segment_result(seg_id)],
            "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=cleanup_result):
            conn.execute("UPDATE projects SET status='exporting' WHERE id=?", (project_id,))
            conn.commit()
            job_id = jobs.enqueue(project_id, "export", params={"segment_count": 1})
            await jobs._handle_export(project_id, job_id, None, {"segment_count": 1})

        archive = pdir / "export.tar.gz"
        with tarfile.open(str(archive), "r:gz") as tar:
            manifest_data = json.loads(tar.extractfile("manifest.json").read())

        assert manifest_data["segments"][0]["text"] == "User edit"

    async def test_clipping_warning_segment_sets_column_and_status(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg_id = _insert_segment(conn, project_id, source_id, status="approved",
                                 transcript="Clipping")

        pdir = Path(str(isolated_data_dir)) / "projects" / project_id
        raw_wav = pdir / "segments" / "raw" / f"{seg_id}.wav"
        raw_wav.parent.mkdir(parents=True, exist_ok=True)
        raw_wav.write_bytes(b"RIFF" + b"\x00" * 36)

        cleanup_result = {
            "job_id": "svc-1", "status": "complete",
            "results": [{
                "id": seg_id, "output_path": None,
                "clipping_warning": True, "auto_rejected": False, "error": None,
            }],
            "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=cleanup_result):
            conn.execute("UPDATE projects SET status='exporting' WHERE id=?", (project_id,))
            conn.commit()
            job_id = jobs.enqueue(project_id, "export", params={"segment_count": 1})
            await jobs._handle_export(project_id, job_id, None, {"segment_count": 1})

        seg = conn.execute("SELECT status, clipping_warning FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "clipping_warning"
        assert seg["clipping_warning"] == 1  # column also set

    async def test_auto_rejected_segment_sets_status(self, isolated_data_dir):
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg_id = _insert_segment(conn, project_id, source_id, status="approved",
                                 transcript="Silent")

        pdir = Path(str(isolated_data_dir)) / "projects" / project_id
        (pdir / "segments" / "raw").mkdir(parents=True, exist_ok=True)
        (pdir / "segments" / "raw" / f"{seg_id}.wav").write_bytes(b"RIFF" + b"\x00" * 36)

        cleanup_result = {
            "job_id": "svc-1", "status": "complete",
            "results": [{
                "id": seg_id, "output_path": None,
                "clipping_warning": False, "auto_rejected": True, "error": None,
            }],
            "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=cleanup_result):
            conn.execute("UPDATE projects SET status='exporting' WHERE id=?", (project_id,))
            conn.commit()
            job_id = jobs.enqueue(project_id, "export", params={"segment_count": 1})
            await jobs._handle_export(project_id, job_id, None, {"segment_count": 1})

        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "auto_rejected"

    async def test_ffmpeg_error_marks_auto_rejected_with_cleanup_error_flag(self, isolated_data_dir):
        import db, jobs, json
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg_id = _insert_segment(conn, project_id, source_id, status="approved",
                                 transcript="Error seg")

        pdir = Path(str(isolated_data_dir)) / "projects" / project_id
        (pdir / "segments" / "raw").mkdir(parents=True, exist_ok=True)
        (pdir / "segments" / "raw" / f"{seg_id}.wav").write_bytes(b"RIFF" + b"\x00" * 36)

        cleanup_result = {
            "job_id": "svc-1", "status": "complete",
            "results": [{
                "id": seg_id, "output_path": None,
                "clipping_warning": False, "auto_rejected": False,
                "error": "ffmpeg_error: exit code 1",
            }],
            "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=cleanup_result):
            conn.execute("UPDATE projects SET status='exporting' WHERE id=?", (project_id,))
            conn.commit()
            job_id = jobs.enqueue(project_id, "export", params={"segment_count": 1})
            await jobs._handle_export(project_id, job_id, None, {"segment_count": 1})

        seg = conn.execute("SELECT status, flags FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "auto_rejected"
        flags = json.loads(seg["flags"])
        assert any("cleanup_error" in f for f in flags)

    async def test_export_job_status_becomes_exported(self, isolated_data_dir):
        """After export completes, project status should become exported."""
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")
        seg_id = _insert_segment(conn, project_id, source_id, status="approved",
                                 transcript="OK")

        pdir = Path(str(isolated_data_dir)) / "projects" / project_id
        (pdir / "segments" / "raw").mkdir(parents=True, exist_ok=True)
        (pdir / "segments" / "raw" / f"{seg_id}.wav").write_bytes(b"RIFF" + b"\x00" * 36)

        cleanup_result = {
            "job_id": "svc-1", "status": "complete",
            "results": [self._approved_segment_result(seg_id)],
            "error": None,
        }

        with patch("jobs.submit_job", new_callable=AsyncMock, return_value={"job_id": "svc-1"}), \
             patch("jobs.poll_job", new_callable=AsyncMock, return_value=cleanup_result):
            conn.execute("UPDATE projects SET status='exporting' WHERE id=?", (project_id,))
            conn.commit()
            job_id = jobs.enqueue(project_id, "export", params={"segment_count": 1})
            await jobs._handle_export(project_id, job_id, None, {"segment_count": 1})

        project = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
        assert project["status"] == "exported"
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestExportHandler -v 2>&1 | tail -20
```

Expected: all FAILED

- [ ] **Step 3: Implement export handler in jobs.py**

Add `import json`, `import tarfile` to top-level imports in jobs.py. Then add the handler:

```python
async def _handle_export(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Run cleanup on all approved segments, write manifest.json, create tar.gz archive."""
    import json
    import tarfile as _tarfile

    conn = get_conn(project_id)
    pdir = project_dir(project_id)

    approved = conn.execute(
        """SELECT s.id, s.raw_path, src.filename as source_filename
           FROM segments s JOIN sources src ON s.source_id = src.id
           WHERE s.project_id=? AND s.status='approved'""",
        (project_id,),
    ).fetchall()

    if not approved:
        _fail_job(project_id, job_id, "no_approved_segments")
        return

    export_dir = pdir / "export"
    export_dir.mkdir(exist_ok=True)

    project = conn.execute("SELECT target_lufs FROM projects WHERE id=?", (project_id,)).fetchone()
    target_lufs = project["target_lufs"] if project["target_lufs"] else -23.0

    cleanup_segments = [
        {
            "id": s["id"],
            "input_path": str(pdir / s["raw_path"]),
            "output_path": str(export_dir / f"{s['id']}.wav"),
        }
        for s in approved
    ]

    payload = {
        "job_id": job_id,
        "segments": cleanup_segments,
        "params": {
            "target_lufs": target_lufs,
            "true_peak_dbtp": -2.0,
            "lra": 7.0,
            "highpass_hz": 80,
            "silence_threshold_db": -50.0,
            "silence_min_duration_secs": 0.1,
            "clipping_threshold_db": -0.1,
            "clipping_min_consecutive_samples": 3,
            "output_sample_rate": 22050,
            "output_channels": 1,
        },
    }

    svc_url = _get_service_url("cleanup")
    await submit_job(svc_url, payload)

    async def _update_export_progress(r):
        _update_progress(project_id, job_id, r.get("progress", 0))

    result = await poll_job(svc_url, job_id, on_progress=_update_export_progress)

    if result["status"] == "failed":
        _fail_job(project_id, job_id, result.get("error", "cleanup_failed"))
        _recompute_project_status(project_id)
        return

    now = _now()
    for seg_result in result.get("results", []):
        seg_id = seg_result["id"]
        if seg_result.get("error"):
            flags_raw = conn.execute("SELECT flags FROM segments WHERE id=?", (seg_id,)).fetchone()
            flags = json.loads(flags_raw["flags"] or "[]")
            flags.append(f"cleanup_error: {seg_result['error']}")
            conn.execute(
                "UPDATE segments SET status='auto_rejected', flags=?, updated_at=? WHERE id=?",
                (json.dumps(flags), now, seg_id),
            )
        elif seg_result.get("auto_rejected"):
            conn.execute(
                "UPDATE segments SET status='auto_rejected', updated_at=? WHERE id=?",
                (now, seg_id),
            )
        elif seg_result.get("clipping_warning"):
            conn.execute(
                "UPDATE segments SET status='clipping_warning', clipping_warning=1, updated_at=? WHERE id=?",
                (now, seg_id),
            )
        else:
            conn.execute(
                "UPDATE segments SET export_path=?, updated_at=? WHERE id=?",
                (f"export/{seg_id}.wav", now, seg_id),
            )
    conn.commit()

    # Write manifest.json from DB using COALESCE(transcript_edited, transcript)
    exported_segs = conn.execute(
        """SELECT s.id, s.export_path, COALESCE(s.transcript_edited, s.transcript) AS text,
                  src.filename AS source, s.start_secs, s.end_secs, s.duration_secs,
                  s.match_confidence, s.transcript_confidence
           FROM segments s JOIN sources src ON s.source_id = src.id
           WHERE s.project_id=? AND s.export_path IS NOT NULL
           ORDER BY src.filename, s.start_secs""",
        (project_id,),
    ).fetchall()

    manifest_segments = []
    for s in exported_segs:
        if not s["text"]:
            logger.warning("Segment %s has no transcript, excluding from manifest", s["id"])
            continue
        manifest_segments.append({
            "id": s["id"],
            "audio_file": f"{s['id']}.wav",
            "text": s["text"],
            "source": s["source"],
            "start_secs": s["start_secs"],
            "end_secs": s["end_secs"],
            "duration_secs": s["duration_secs"],
            "match_confidence": s["match_confidence"],
            "transcript_confidence": s["transcript_confidence"],
        })

    total_duration = sum(s["duration_secs"] for s in manifest_segments)
    manifest = {
        "version": "1",
        "project_id": project_id,
        "exported_at": _now(),
        "speaker": "target",
        "segments": manifest_segments,
        "stats": {
            "segment_count": len(manifest_segments),
            "total_duration_secs": total_duration,
        },
    }
    (export_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Package tar.gz (WAVs + manifest only)
    archive_path = pdir / "export.tar.gz"
    with _tarfile.open(str(archive_path), "w:gz") as tar:
        tar.add(str(export_dir / "manifest.json"), arcname="manifest.json")
        for seg in manifest_segments:
            wav_file = export_dir / seg["audio_file"]
            if wav_file.exists():
                tar.add(str(wav_file), arcname=seg["audio_file"])

    # Set project status to exported directly (recompute can't derive this)
    conn.execute(
        "UPDATE projects SET status='exported', updated_at=? WHERE id=?",
        (_now(), project_id),
    )
    conn.commit()
    _complete_job(project_id, job_id)
```

Update `HANDLERS` registry to replace the export stub:

```python
HANDLERS: dict[str, Callable] = {
    "extract_audio": _handle_extract_audio,
    "vocal_separation": _handle_vocal_separation,
    "diarisation": _handle_diarisation,
    "transcription_bulk": _handle_transcription_bulk,
    "transcription_segment": _handle_transcription_segment,
    "export": _handle_export,
}
```

Remove `_handle_stub_service_job` function entirely since all stubs are now replaced.

- [ ] **Step 4: Run tests — expect pass**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestExportHandler -v 2>&1 | tail -20
```

Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/jobs.py services/orchestrator/tests/test_wave3_pipeline.py
git commit -m "feat: implement export handler — cleanup service, manifest.json, tar.gz archive

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 7: Threshold Re-evaluation Tests

**Files:**
- Modify: `services/orchestrator/tests/test_wave3_pipeline.py`

These test the `PATCH /projects/{id}` endpoint's bidirectional threshold logic (already implemented in projects.py but needs explicit Wave 3 test coverage).

- [ ] **Step 1: Write the tests**

Add to `tests/test_wave3_pipeline.py`:

```python
class TestThresholdReEvaluation:
    def test_lowering_threshold_promotes_below_threshold_to_pending(self, client, isolated_data_dir):
        project_id = _make_project(isolated_data_dir, match_threshold=0.75)
        import db
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")

        # Segment with confidence 0.70 — below old threshold (0.75), should be below_threshold
        seg_id = _insert_segment(conn, project_id, source_id, status="below_threshold", confidence=0.70)

        # Lower threshold to 0.65 — segment (0.70 >= 0.65) should become pending
        resp = client.patch(f"/projects/{project_id}", json={"match_threshold": 0.65})
        assert resp.status_code == 200

        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "pending"

    def test_raising_threshold_demotes_pending_to_below_threshold(self, client, isolated_data_dir):
        project_id = _make_project(isolated_data_dir, match_threshold=0.75)
        import db
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")

        # Segment with confidence 0.70 — above old threshold (0.75)? No — insert as pending
        seg_id = _insert_segment(conn, project_id, source_id, status="pending", confidence=0.70)

        # Raise threshold to 0.80 — segment (0.70 < 0.80) should become below_threshold
        resp = client.patch(f"/projects/{project_id}", json={"match_threshold": 0.80})
        assert resp.status_code == 200

        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "below_threshold"

    def test_threshold_change_does_not_affect_approved(self, client, isolated_data_dir):
        project_id = _make_project(isolated_data_dir, match_threshold=0.75)
        import db
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")

        seg_id = _insert_segment(conn, project_id, source_id, status="approved", confidence=0.60)

        # Lower threshold even further — approved should stay approved
        resp = client.patch(f"/projects/{project_id}", json={"match_threshold": 0.50})
        assert resp.status_code == 200

        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "approved"

    def test_threshold_change_does_not_affect_rejected(self, client, isolated_data_dir):
        project_id = _make_project(isolated_data_dir, match_threshold=0.75)
        import db
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")

        seg_id = _insert_segment(conn, project_id, source_id, status="rejected", confidence=0.90)

        resp = client.patch(f"/projects/{project_id}", json={"match_threshold": 0.50})
        assert resp.status_code == 200

        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "rejected"

    def test_threshold_change_does_not_affect_maybe(self, client, isolated_data_dir):
        project_id = _make_project(isolated_data_dir, match_threshold=0.75)
        import db
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")

        seg_id = _insert_segment(conn, project_id, source_id, status="maybe", confidence=0.60)

        resp = client.patch(f"/projects/{project_id}", json={"match_threshold": 0.80})
        assert resp.status_code == 200

        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "maybe"

    def test_threshold_change_does_not_affect_clipping_warning(self, client, isolated_data_dir):
        project_id = _make_project(isolated_data_dir, match_threshold=0.75)
        import db
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "complete")

        seg_id = _insert_segment(conn, project_id, source_id, status="clipping_warning", confidence=0.60)

        resp = client.patch(f"/projects/{project_id}", json={"match_threshold": 0.80})
        assert resp.status_code == 200

        seg = conn.execute("SELECT status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert seg["status"] == "clipping_warning"
```

- [ ] **Step 2: Run tests — expect pass** (logic already implemented in projects.py)

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestThresholdReEvaluation -v 2>&1 | tail -20
```

Expected: `6 passed` (implementation already exists in projects.py from Wave 1)

If any fail, debug the projects.py patch_project function and fix.

- [ ] **Step 3: Commit**

```bash
git add services/orchestrator/tests/test_wave3_pipeline.py
git commit -m "test: add threshold re-evaluation coverage for Wave 3

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 8: Project Status Recomputation Tests

**Files:**
- Modify: `services/orchestrator/tests/test_wave3_pipeline.py`

- [ ] **Step 1: Write the tests**

Add to `tests/test_wave3_pipeline.py`:

```python
class TestProjectStatusRecomputation:
    def test_processing_when_active_jobs(self, isolated_data_dir):
        from status import recompute_project_status
        import db
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_running")
        now = _now()
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) VALUES (?,?,'vocal_separation','running',?)",
            (str(uuid.uuid4()), project_id, now),
        )
        conn.commit()
        recompute_project_status(project_id)
        status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"]
        assert status == "processing"

    def test_review_when_all_sources_complete_no_active_jobs(self, isolated_data_dir):
        from status import recompute_project_status
        import db
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        _insert_source(conn, project_id, "complete")
        _insert_source(conn, project_id, "complete")
        recompute_project_status(project_id)
        status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"]
        assert status == "review"

    def test_ready_when_sources_exist_but_not_all_complete(self, isolated_data_dir):
        from status import recompute_project_status
        import db
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        _insert_source(conn, project_id, "complete")
        _insert_source(conn, project_id, "step1_pending")
        recompute_project_status(project_id)
        status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"]
        assert status == "ready"

    def test_new_when_no_sources(self, isolated_data_dir):
        from status import recompute_project_status
        import db
        project_id = _make_project(isolated_data_dir)
        recompute_project_status(project_id)
        status = conn.execute = db.get_conn(project_id).execute
        status_val = status("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"]
        assert status_val == "new"

    def test_exporting_preserved_when_jobs_active(self, isolated_data_dir):
        from status import recompute_project_status
        import db
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        now = _now()
        conn.execute("UPDATE projects SET status='exporting' WHERE id=?", (project_id,))
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) VALUES (?,?,'export','running',?)",
            (str(uuid.uuid4()), project_id, now),
        )
        conn.commit()
        recompute_project_status(project_id)
        status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"]
        assert status == "exporting"

    def test_exported_after_export_job_completes(self, isolated_data_dir):
        from status import recompute_project_status
        import db
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        now = _now()
        # No active jobs, but there is a completed export job
        conn.execute("UPDATE projects SET status='exporting' WHERE id=?", (project_id,))
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at, completed_at) VALUES (?,?,'export','complete',?,?)",
            (str(uuid.uuid4()), project_id, now, now),
        )
        conn.commit()
        recompute_project_status(project_id)
        status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"]
        assert status == "exported"
```

- [ ] **Step 2: Run tests — expect pass**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestProjectStatusRecomputation -v 2>&1 | tail -20
```

Expected: `6 passed` (the `test_exported_after_export_job_completes` tests the fixed circular logic)

If `test_new_when_no_sources` has a syntax error (bad variable naming), fix it:
```python
def test_new_when_no_sources(self, isolated_data_dir):
    from status import recompute_project_status
    import db
    project_id = _make_project(isolated_data_dir)
    conn = db.get_conn(project_id)
    recompute_project_status(project_id)
    status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"]
    assert status == "new"
```

- [ ] **Step 3: Commit**

```bash
git add services/orchestrator/tests/test_wave3_pipeline.py
git commit -m "test: add project status recomputation tests

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 9: End-to-End Pipeline Integration Test

**Files:**
- Modify: `services/orchestrator/tests/test_wave3_pipeline.py`

This test exercises the full happy path through the job queue, verifying that handlers chain correctly (vocal_sep → diarisation → transcription).

- [ ] **Step 1: Write the test**

Add to `tests/test_wave3_pipeline.py`:

```python
class TestFullPipelineFlow:
    async def test_vocal_sep_to_diarisation_to_transcription_chain(self, isolated_data_dir):
        """Integration test: vocal_separation completes → diarisation job enqueued and runs
        → segments written → transcription_bulk auto-triggered."""
        import db, jobs
        project_id = _make_project(isolated_data_dir)
        conn = db.get_conn(project_id)
        source_id = _insert_source(conn, project_id, "step1_pending")
        conn.execute("UPDATE sources SET audio_path=? WHERE id=?",
                     (f"audio/raw/{source_id}.wav", source_id))
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
        conn.commit()

        seg_id = str(uuid.uuid4())

        vs_result = {
            "job_id": "vs-1", "status": "complete", "progress": 100,
            "output_path": f"/data/projects/{project_id}/audio/vocals/{source_id}.wav",
            "error": None, "retry_with_chunk_secs": None,
        }
        diar_result = {
            "job_id": "diar-1", "status": "complete",
            "segments": [{
                "id": seg_id, "start_secs": 0.0, "end_secs": 5.0,
                "speaker_label": "SPEAKER_00", "match_confidence": 0.9,
                "wav_path": f"/data/projects/{project_id}/segments/raw/{seg_id}.wav",
            }],
            "coverage_ratio": 0.3, "error": None,
        }
        tx_result = {
            "job_id": "tx-1", "status": "complete", "progress": 100,
            "completed_segments": [{"id": seg_id, "transcript": "Hello", "transcript_confidence": 0.9}],
            "error": None,
        }

        submit_results = [{"job_id": "vs-1"}, {"job_id": "diar-1"}, {"job_id": "tx-1"}]
        poll_results = [vs_result, diar_result, tx_result]

        with patch("jobs.submit_job", new_callable=AsyncMock, side_effect=submit_results), \
             patch("jobs.poll_job", new_callable=AsyncMock, side_effect=poll_results):
            # Step 1: vocal separation
            vs_job_id = jobs.enqueue(project_id, "vocal_separation", source_id=source_id)
            await jobs._handle_vocal_separation(project_id, vs_job_id, source_id, {})

            # Step 2: diarisation (enqueued by vocal_sep handler)
            diar_job = conn.execute(
                "SELECT * FROM jobs WHERE type='diarisation' AND source_id=?", (source_id,)
            ).fetchone()
            assert diar_job is not None
            await jobs._handle_diarisation(project_id, diar_job["id"], source_id, {})

            # Step 3: transcription (auto-enqueued by diarisation handler)
            tx_job = conn.execute(
                "SELECT * FROM jobs WHERE type='transcription_bulk'", (project_id,)
            ).fetchone()
            assert tx_job is not None
            import json as _json
            tx_params = _json.loads(tx_job["params"])
            await jobs._handle_transcription_bulk(project_id, tx_job["id"], None, tx_params)

        # Final state
        source = conn.execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "complete"

        segment = conn.execute("SELECT transcript, status FROM segments WHERE id=?", (seg_id,)).fetchone()
        assert segment["transcript"] == "Hello"
        assert segment["status"] == "pending"  # above threshold, not yet reviewed

        # All orchestrator jobs should be complete
        jobs_rows = conn.execute(
            "SELECT type, status FROM jobs WHERE project_id=?", (project_id,)
        ).fetchall()
        for j in jobs_rows:
            assert j["status"] == "complete", f"Job {j['type']} is {j['status']}"
```

- [ ] **Step 2: Run test — expect pass**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/test_wave3_pipeline.py::TestFullPipelineFlow -v 2>&1 | tail -20
```

Expected: `1 passed`

- [ ] **Step 3: Run full test suite — verify all pass**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with respx python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: all previous tests pass + new Wave 3 tests pass. Total should be ≥ 155 tests.

- [ ] **Step 4: Commit**

```bash
git add services/orchestrator/tests/test_wave3_pipeline.py
git commit -m "test: add end-to-end pipeline integration test

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 10: Add respx to requirements.txt

**Files:**
- Modify: `services/orchestrator/requirements.txt`

- [ ] **Step 1: Add respx**

Add `respx==0.21.1` to `services/orchestrator/requirements.txt`:

```
fastapi==0.115.0
uvicorn==0.30.6
python-multipart==0.0.9
aiofiles==23.2.1
httpx==0.27.0
pytest==8.3.3
pytest-asyncio==0.24.0
anyio==4.6.2
respx==0.21.1
```

- [ ] **Step 2: Run all tests with pinned version to verify**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with "respx==0.21.1" python -m pytest tests/ -v 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add services/orchestrator/requirements.txt
git commit -m "chore: add respx to test dependencies

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 11: Final Integration Check + Deferred Bug Memory Update

- [ ] **Step 1: Run the full test suite one final time**

```bash
cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio --with "respx==0.21.1" python -m pytest tests/ -v 2>&1 | tail -30
```

All tests must pass. Note the exact count.

- [ ] **Step 2: Verify all HANDLERS entries are real (no stubs)**

```bash
grep -n "stub_service_job\|not_yet_integrated" services/orchestrator/jobs.py
```

Expected: no output (all stubs replaced)

- [ ] **Step 3: Update memory — mark Wave 1 deferred bugs as resolved**

Update `/home/jmackay/.claude/projects/-home-jmackay-Projects-jjsmackay-flipsync/memory/project_wave1_deferred_bugs.md` to mark bugs 1, 2, and 4 as resolved. Bugs 3 and 5 are Wave 2 concerns.

- [ ] **Step 4: Final commit if any uncommitted changes remain**

```bash
git status
```

If clean, Wave 3 is complete.

---

## Self-Review Checklist

Against the deliverables listed in the task prompt:

| Deliverable | Task(s) | Status |
|-------------|---------|--------|
| Service client module | Task 1 | ✓ |
| Pipeline start (vocal_sep → diar → transcription chain) | Tasks 3, 4, 5 | ✓ |
| OOM retry for vocal separation | Task 3 | ✓ |
| Reprocess endpoint wiring | Pipeline.py already handles enqueue; handlers now execute correctly | ✓ |
| Transcription endpoints | Task 5 | ✓ |
| Threshold re-evaluation (bidirectional) | Task 7 (already in projects.py, tested) | ✓ |
| Project status recomputation | Tasks 2, 8 | ✓ |
| Cleanup + export (POST + GET /download) | Task 6 | ✓ |
| clipping_warning column + status | Task 6 | ✓ |
| auto_rejected + cleanup_error flags | Task 6 | ✓ |
| Tests: full pipeline flow | Task 9 | ✓ |
| Tests: OOM retry | Task 3 | ✓ |
| Tests: reprocess | Tasks 3-4 (handler correctly re-queues on reprocess) | ✓ |
| Tests: threshold re-evaluation | Task 7 | ✓ |
| Tests: export flow (clipping, auto_reject) | Task 6 | ✓ |
| Tests: project status recomputation | Task 8 | ✓ |
| Fix export_complete circular logic | Task 2 | ✓ |
| Fix low_coverage_warning at 0.0 | Task 2 | ✓ |
