"""In-memory FIFO job queue backed by the jobs SQLite table.

Jobs execute one at a time per project; GPU-bound jobs (see GPU_JOB_TYPES)
additionally serialise host-wide via a global lock. Each job type has a handler
registered in HANDLERS. Wave 3 implements real handlers for external service jobs.
"""

import asyncio
import json
import logging
import os
import shutil
import socket
import tarfile
import uuid
import weakref
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import httpx

from db import get_conn, project_dir, utc_now as _now
import service_client
from status import auto_approve_promote, recompute_project_status as _recompute_project_status

logger = logging.getLogger(__name__)

# One asyncio.Lock per project to enforce one-at-a-time execution.
_project_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# GPU-bound job types additionally serialise host-wide: at most one GPU job
# runs across ALL projects at any time (all projects share the same GPU;
# concurrent GPU jobs contend for VRAM and OOM). CPU jobs (extract_audio,
# export — FFmpeg subprocess / CPU-only cleanup service) are not gated.
GPU_JOB_TYPES = frozenset(
    {"vocal_separation", "diarisation", "scout_speakers", "transcription_bulk", "transcription_segment"}
)

# Which processing service each GPU job type talks to. Must mirror the
# service names used by each handler's submit path (_submit_with_retry).
GPU_JOB_SERVICES: dict[str, str] = {
    "vocal_separation": "vocal_separation",
    "diarisation": "diarisation",
    "scout_speakers": "diarisation",
    "transcription_bulk": "transcription",
    "transcription_segment": "transcription",
}

# The global GPU lock is created lazily per event loop: an asyncio.Lock binds
# to the loop it is first awaited on, and tests run each case in a fresh loop
# via asyncio.run() — a module-level Lock bound to a dead loop would raise.
_gpu_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _gpu_lock() -> asyncio.Lock:
    """Return the host-wide GPU lock for the running event loop."""
    loop = asyncio.get_running_loop()
    lock = _gpu_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _gpu_locks[loop] = lock
    return lock

# In-memory queue: project_id -> list of job_id strings (FIFO)
_queues: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)

# Background runner tasks: project_id -> Task
_runners: dict[str, asyncio.Task] = {}

# Seconds an idle project runner waits for new work before exiting.
_IDLE_TIMEOUT_SECS = 60

# Bounded backoff for submitting a job to a processing service that is not yet
# reachable (e.g. still downloading models on first boot). Overridable in tests.
_SUBMIT_RETRY_BASE_SECS = 1.0
_SUBMIT_RETRY_MAX_SECS = 30.0
_SUBMIT_RETRY_TIMEOUT_SECS = 300.0

# GPU jobs wait for their target service to report healthy BEFORE taking the
# host-wide GPU lock: first-boot model downloads can take far longer than the
# submit retry window, and waiting inside the lock would stall every other
# project's GPU work. Timeout is env-configurable (read at call time); the
# poll interval is a module constant so tests can shrink it.
_SERVICE_READY_POLL_SECS = 5.0
_SERVICE_READY_TIMEOUT_DEFAULT_SECS = 1800.0


def _service_ready_timeout_secs() -> float:
    raw = os.environ.get("SERVICE_READY_TIMEOUT_SECS", "")
    try:
        return float(raw) if raw else _SERVICE_READY_TIMEOUT_DEFAULT_SECS
    except ValueError:
        return _SERVICE_READY_TIMEOUT_DEFAULT_SECS


def _is_dns_failure(exc: BaseException) -> bool:
    """True if the exception chain bottoms out in a DNS resolution failure."""
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, socket.gaierror):
            return True
        e = e.__cause__ or e.__context__
    return False


async def wait_for_service_ready(service_name: str) -> bool:
    """Poll the service's GET /health until it reports healthy.

    Returns True once healthy, False if the readiness window
    (SERVICE_READY_TIMEOUT_SECS, default 1800 s) expires. A DNS resolution
    failure means the service hostname does not exist in this environment
    (unit tests, partial deployments) — gating is skipped (returns True) and
    the submit path's own bounded retry surfaces any real connectivity error.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _service_ready_timeout_secs()
    while True:
        try:
            if await service_client.probe_health(service_name):
                return True
        except Exception as exc:
            if _is_dns_failure(exc):
                return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(min(_SERVICE_READY_POLL_SECS, max(deadline - loop.time(), 0.01)))


async def _submit_with_retry(service_name: str, payload: dict) -> dict:
    """Submit a job, retrying with bounded exponential backoff while the service
    is unreachable (connection/timeout errors). A reachable service returning an
    HTTP error status propagates immediately — only transport failures retry.

    Exception: a 409 ``job_exists`` means the service already accepted this
    job_id (e.g. a retry after a submit that timed out client-side but was
    delivered) — that is treated as a successful submit so the caller proceeds
    to poll the original job instead of failing it.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _SUBMIT_RETRY_TIMEOUT_SECS
    delay = _SUBMIT_RETRY_BASE_SECS
    while True:
        try:
            return await service_client.submit_job(service_name, payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                try:
                    body = exc.response.json()
                except Exception:
                    body = {}
                if body.get("error") == "job_exists":
                    logger.info(
                        "Service %s already has job %s; treating as submitted",
                        service_name, payload.get("job_id"),
                    )
                    return body
            raise
        except (httpx.TransportError, ConnectionError, OSError) as exc:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise
            logger.warning(
                "Service %s unreachable (%s); retrying in %.0fs", service_name, exc, min(delay, remaining)
            )
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, _SUBMIT_RETRY_MAX_SECS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enqueue(
    project_id: str,
    job_type: str,
    source_id: str | None = None,
    params: dict | None = None,
) -> str:
    """Create a job row and add it to the in-memory queue. Returns job_id."""
    job_id = str(uuid.uuid4())
    now = _now()
    conn = get_conn(project_id)
    conn.execute(
        """
        INSERT INTO jobs (id, project_id, source_id, type, status, params, created_at)
        VALUES (?, ?, ?, ?, 'queued', ?, ?)
        """,
        (job_id, project_id, source_id, job_type, json.dumps(params or {}), now),
    )
    conn.commit()

    _queues[project_id].put_nowait(job_id)
    _ensure_runner(project_id)
    return job_id


def cancel_running_jobs(project_id: str) -> None:
    """Mark all queued jobs for a project as cancelled (used before delete)."""
    conn = get_conn(project_id)
    conn.execute(
        "UPDATE jobs SET status='cancelled', completed_at=? WHERE project_id=? AND status IN ('queued','running')",
        (_now(), project_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Internal runner
# ---------------------------------------------------------------------------


def _ensure_runner(project_id: str) -> None:
    task = _runners.get(project_id)
    if task is None or task.done():
        try:
            loop = asyncio.get_running_loop()
            _runners[project_id] = loop.create_task(_run_project_queue(project_id))
        except RuntimeError:
            # No running event loop — runner will be started when loop runs
            pass


async def _run_project_queue(project_id: str) -> None:
    """Drain the queue for a single project, running one job at a time."""
    while True:
        try:
            job_id = await asyncio.wait_for(_queues[project_id].get(), timeout=_IDLE_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            # A job may have been enqueued in the window where wait_for was
            # cancelling its get(); that item sits in the queue while enqueue's
            # _ensure_runner saw this (not-yet-done) task and skipped starting a
            # new runner. Re-check before exiting so it can't be stranded.
            if not _queues[project_id].empty():
                continue
            break

        async with _project_locks[project_id]:
            await _execute_job(project_id, job_id)


async def shutdown_runners() -> None:
    """Cancel and await every per-project runner task on the current loop.

    Called from the app's lifespan shutdown so no runner is left pending when
    its event loop closes. An idle runner spends most of its life awaiting
    ``asyncio.wait_for(queue.get(), ...)``; if the loop closes while that's
    still pending, the Task is destroyed without ever being cancelled, and a
    later GC of it raises "RuntimeError: Event loop is closed" (surfaced by
    pytest as a PytestUnraisableExceptionWarning/ResourceWarning) whenever that
    happens to run — which can be during a completely unrelated later test.

    Only tasks bound to *this* running loop are touched: ``_runners`` is
    process-global, and calling ``.cancel()``/awaiting a task that belongs to
    a different (already-closed) loop raises "Event loop is closed" itself.
    In the real app there is only ever one loop, so this is only a guard —
    it matters for tests, which drive jobs on ad hoc throwaway loops.
    """
    current_loop = asyncio.get_running_loop()
    own = [
        (pid, t) for pid, t in _runners.items()
        if not t.done() and t.get_loop() is current_loop
    ]
    for _, t in own:
        t.cancel()
    for pid, t in own:
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Runner task raised during shutdown")
        _runners.pop(pid, None)


async def _execute_job(project_id: str, job_id: str) -> None:
    conn = get_conn(project_id)
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None or row["status"] in ("cancelled", "complete", "failed"):
        return

    # Mark running
    conn.execute(
        "UPDATE jobs SET status='running', started_at=? WHERE id=?",
        (_now(), job_id),
    )
    conn.commit()

    job_type = row["type"]
    params = json.loads(row["params"] or "{}")

    handler = HANDLERS.get(job_type)
    if handler is None:
        _fail_job(project_id, job_id, f"No handler registered for job type '{job_type}'")
        return

    try:
        if job_type in GPU_JOB_TYPES:
            # Wait for the target service to be healthy BEFORE taking the
            # host-wide GPU lock, so a service that is still booting (first-run
            # model downloads can take tens of minutes) never stalls other
            # projects' GPU work behind the lock.
            service_name = GPU_JOB_SERVICES[job_type]
            if not await wait_for_service_ready(service_name):
                _fail_job(
                    project_id, job_id,
                    f"service_unavailable: {service_name} did not become healthy "
                    f"within {int(_service_ready_timeout_secs())}s",
                )
                return
            # Host-wide GPU gate: held for the full duration of the handler so
            # no two GPU jobs (across any projects) ever run concurrently.
            async with _gpu_lock():
                await handler(project_id, job_id, row["source_id"], params)
        else:
            await handler(project_id, job_id, row["source_id"], params)
    except Exception as exc:
        logger.exception("Job %s (%s) raised an exception", job_id, job_type)
        _fail_job(project_id, job_id, str(exc))
        _recompute_project_status(project_id)


def _complete_job(project_id: str, job_id: str) -> None:
    conn = get_conn(project_id)
    conn.execute(
        "UPDATE jobs SET status='complete', progress=100, completed_at=? WHERE id=?",
        (_now(), job_id),
    )
    conn.commit()


def _fail_job(project_id: str, job_id: str, error: str) -> None:
    conn = get_conn(project_id)
    conn.execute(
        "UPDATE jobs SET status='failed', error=?, completed_at=? WHERE id=?",
        (error, _now(), job_id),
    )
    conn.commit()
    # Every failure path recomputes project status so a failed job can't leave
    # the project stuck in 'processing'.
    _recompute_project_status(project_id)


def _update_progress(project_id: str, job_id: str, progress: int) -> None:
    conn = get_conn(project_id)
    conn.execute("UPDATE jobs SET progress=? WHERE id=?", (progress, job_id))
    conn.commit()


def _update_progress_detail(project_id: str, job_id: str, detail: dict) -> None:
    """Persist the full rich-progress object (JSON) for long-running jobs.

    ``progress`` stays the 0-100 integer; ``progress_detail`` carries the
    fine-tune epoch/step/loss/ETA object so the dashboard survives refreshes."""
    conn = get_conn(project_id)
    conn.execute(
        "UPDATE jobs SET progress_detail=? WHERE id=?",
        (json.dumps(detail), job_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------


async def _handle_extract_audio(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Extract audio from a video file using FFmpeg subprocess."""
    conn = get_conn(project_id)
    source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if source is None:
        _fail_job(project_id, job_id, "source_not_found")
        return

    pdir = project_dir(project_id)
    input_path = pdir / source["file_path"]
    output_path = pdir / "audio" / "raw" / f"{source_id}.wav"

    conn.execute(
        "UPDATE sources SET status='extracting', updated_at=? WHERE id=?",
        (_now(), source_id),
    )
    conn.commit()

    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "44100",
        str(output_path),
    ]

    _update_progress(project_id, job_id, 10)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        error_msg = stderr.decode(errors="replace")[-500:]
        conn.execute(
            "UPDATE sources SET status='extraction_failed', updated_at=? WHERE id=?",
            (_now(), source_id),
        )
        conn.commit()
        _fail_job(project_id, job_id, f"ffmpeg_error: {error_msg}")
        _recompute_project_status(project_id)
        return

    duration_secs = await _get_audio_duration(str(output_path))

    conn.execute(
        """
        UPDATE sources
        SET status='separation_pending', audio_path=?, duration_secs=?, updated_at=?
        WHERE id=?
        """,
        (f"audio/raw/{source_id}.wav", duration_secs, _now(), source_id),
    )
    conn.commit()
    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)


async def _get_audio_duration(wav_path: str) -> float | None:
    """Return duration in seconds using ffprobe, or None on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", wav_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return float(stdout.decode().strip())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Vocal Separation handler (calls external service)
# ---------------------------------------------------------------------------


async def _handle_vocal_separation(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Submit vocal separation to external service and poll until complete.

    On OOM failure with retry_with_chunk_secs: resubmit with chunk_secs.
    On second failure: mark source separation_failed.
    """


    conn = get_conn(project_id)
    source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if source is None:
        _fail_job(project_id, job_id, "source_not_found")
        _recompute_project_status(project_id)
        return

    # Transition source to separation_running
    conn.execute(
        "UPDATE sources SET status='separation_running', separation_error=NULL, updated_at=? WHERE id=?",
        (_now(), source_id),
    )
    conn.commit()

    pdir = project_dir(project_id)
    data_prefix = _data_prefix()
    model = params.get("demucs_model", "htdemucs")
    chunk_secs = params.get("chunk_secs", None)

    payload = {
        "job_id": job_id,
        "input_path": f"{data_prefix}/projects/{project_id}/audio/raw/{source_id}.wav",
        "output_path": f"{data_prefix}/projects/{project_id}/audio/vocals/{source_id}.wav",
        "model": model,
        "chunk_secs": chunk_secs,
    }

    try:
        await _submit_with_retry("vocal_separation", payload)
    except Exception as exc:
        conn.execute(
            "UPDATE sources SET status='separation_failed', separation_error=?, updated_at=? WHERE id=?",
            (str(exc), _now(), source_id),
        )
        conn.commit()
        _fail_job(project_id, job_id, f"submit_failed: {exc}")
        _recompute_project_status(project_id)
        return

    def on_progress(result):
        progress = result.get("progress", 0)
        if progress:
            _update_progress(project_id, job_id, progress)

    result = await service_client.poll_until_complete("vocal_separation", job_id, on_progress=on_progress)

    if result["status"] == "failed":
        retry_chunk = result.get("retry_with_chunk_secs")
        if retry_chunk and chunk_secs is None:
            # OOM retry: resubmit with chunking
            logger.info("Vocal separation OOM for source %s, retrying with chunk_secs=%s", source_id, retry_chunk)
            retry_job_id = str(uuid.uuid4())
            retry_payload = {
                "job_id": retry_job_id,
                "input_path": payload["input_path"],
                "output_path": payload["output_path"],
                "model": model,
                "chunk_secs": retry_chunk,
            }
            try:
                await _submit_with_retry("vocal_separation", retry_payload)
                result = await service_client.poll_until_complete("vocal_separation", retry_job_id, on_progress=on_progress)
            except Exception as exc:
                result = {"status": "failed", "error": str(exc)}

        if result["status"] == "failed":
            error = result.get("error", "unknown_error")
            conn.execute(
                "UPDATE sources SET status='separation_failed', separation_error=?, updated_at=? WHERE id=?",
                (error, _now(), source_id),
            )
            conn.commit()
            _fail_job(project_id, job_id, error)
            _recompute_project_status(project_id)
            return

    conn.execute(
        """
        UPDATE sources
        SET status='diarisation_pending', vocals_path=?, separation_model=?, separation_error=NULL, updated_at=?
        WHERE id=?
        """,
        (f"audio/vocals/{source_id}.wav", model, _now(), source_id),
    )
    conn.commit()
    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)

    # Auto-enqueue diarisation
    _auto_enqueue_diarisation(project_id, source_id)


def has_active_diarisation_job(conn, source_id: str) -> bool:
    """True if a queued/running diarisation job already exists for this source.

    Guards enqueue sites (pipeline/continue, the post-separation chain) against
    double-enqueueing diarisation for the same source.
    """
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE source_id=? AND type='diarisation' AND status IN ('queued','running') LIMIT 1",
        (source_id,),
    ).fetchone()
    return row is not None


def _auto_enqueue_diarisation(project_id: str, source_id: str) -> None:
    """After vocal separation succeeds, auto-enqueue diarisation for this source.

    This is the reference gate. With a reference set, diarisation chains straight
    through. Without one, the source rests at diarisation_pending (untouched) and the
    project settles into awaiting_reference; POST /pipeline/continue picks up
    from here once a reference is set.
    """
    conn = get_conn(project_id)
    project = conn.execute("SELECT reference_path FROM projects WHERE id=?", (project_id,)).fetchone()
    if project and project["reference_path"]:
        if not has_active_diarisation_job(conn, source_id):
            enqueue(project_id, "diarisation", source_id=source_id)
    else:
        _recompute_project_status(project_id)


# ---------------------------------------------------------------------------
# Diarisation handler (calls external service)
# ---------------------------------------------------------------------------


def delete_source_segments(conn, project_id: str, source_id: str) -> None:
    """Delete a source's segment rows and their on-disk WAVs (raw + export).

    Does NOT commit — the caller owns the transaction. Shared by the reprocess
    endpoint and the diarisation handler so segment deletion never orphans WAVs.
    """
    pdir = project_dir(project_id)
    rows = conn.execute(
        "SELECT raw_path, export_path FROM segments WHERE source_id=?", (source_id,)
    ).fetchall()
    for row in rows:
        for rel in (row["raw_path"], row["export_path"]):
            if rel:
                f = pdir / rel
                if f.exists():
                    f.unlink()
    conn.execute("DELETE FROM segments WHERE source_id=?", (source_id,))


# Source statuses from which running diarisation is legitimate: freshly queued
# (diarisation_pending) or a crash-recovery re-run (diarisation_running).
_DIARISATION_SOURCE_STATUSES = ("diarisation_pending", "diarisation_running")


async def _handle_diarisation(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Submit diarisation to external service, write segments to DB on completion."""


    conn = get_conn(project_id)
    source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if source is None:
        _fail_job(project_id, job_id, "source_not_found")
        _recompute_project_status(project_id)
        return

    if source["status"] not in _DIARISATION_SOURCE_STATUSES:
        # Duplicate or stale job (double-enqueue, recovery after the source
        # moved on): complete as a no-op without touching segments.
        logger.info(
            "Diarisation job %s is a no-op: source %s is in status '%s'",
            job_id, source_id, source["status"],
        )
        _complete_job(project_id, job_id)
        _recompute_project_status(project_id)
        return

    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project["reference_path"]:
        conn.execute(
            "UPDATE sources SET status='diarisation_failed', diarisation_error='no_reference_clip', updated_at=? WHERE id=?",
            (_now(), source_id),
        )
        conn.commit()
        _fail_job(project_id, job_id, "no_reference_clip")
        return

    # Transition source to diarisation_running
    conn.execute(
        "UPDATE sources SET status='diarisation_running', diarisation_error=NULL, updated_at=? WHERE id=?",
        (_now(), source_id),
    )
    conn.commit()

    data_prefix = _data_prefix()
    payload = {
        "job_id": job_id,
        "input_path": f"{data_prefix}/projects/{project_id}/audio/vocals/{source_id}.wav",
        "reference_path": f"{data_prefix}/projects/{project_id}/{project['reference_path']}",
        "output_dir": f"{data_prefix}/projects/{project_id}/segments/raw/",
        "params": {
            "min_segment_duration": 1.0,
            "min_speakers": 1,
            "max_speakers": 10,
        },
    }

    try:
        await _submit_with_retry("diarisation", payload)
    except Exception as exc:
        conn.execute(
            "UPDATE sources SET status='diarisation_failed', diarisation_error=?, updated_at=? WHERE id=?",
            (str(exc), _now(), source_id),
        )
        conn.commit()
        _fail_job(project_id, job_id, f"submit_failed: {exc}")
        _recompute_project_status(project_id)
        return

    def on_progress(result):
        progress = result.get("progress", 0)
        if progress:
            _update_progress(project_id, job_id, progress)

    result = await service_client.poll_until_complete("diarisation", job_id, on_progress=on_progress)

    if result["status"] == "failed":
        error = result.get("error", "unknown_error")
        conn.execute(
            "UPDATE sources SET status='diarisation_failed', diarisation_error=?, updated_at=? WHERE id=?",
            (error, _now(), source_id),
        )
        conn.commit()
        _fail_job(project_id, job_id, error)
        _recompute_project_status(project_id)
        return

    # Write segments to DB. Clear any pre-existing segments for this source
    # first (rows AND their WAVs) so a re-run (e.g. crash recovery) is
    # idempotent, cannot hit a primary-key conflict, and never orphans the
    # previous run's audio files on disk.
    match_threshold = project["match_threshold"]
    now = _now()
    delete_source_segments(conn, project_id, source_id)
    segments = result.get("segments", [])
    for seg in segments:
        seg_status = "pending" if seg["match_confidence"] >= match_threshold else "below_threshold"
        raw_path = f"segments/raw/{seg['id']}.wav"
        conn.execute(
            """
            INSERT INTO segments
                (id, project_id, source_id, raw_path, start_secs, end_secs,
                 speaker_label, match_confidence, speaker_match_confidence,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seg["id"], project_id, source_id, raw_path,
                seg["start_secs"], seg["end_secs"],
                seg["speaker_label"], seg["match_confidence"],
                seg.get("speaker_match_confidence"),
                seg_status, now, now,
            ),
        )

    # Update source coverage_ratio and status
    coverage = result.get("coverage_ratio", 0.0)
    conn.execute(
        """
        UPDATE sources
        SET status='complete', coverage_ratio=?, diarisation_error=NULL, updated_at=?
        WHERE id=?
        """,
        (coverage, now, source_id),
    )
    conn.commit()
    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)

    # Check if all sources are complete — auto-trigger transcription
    _maybe_auto_transcribe(project_id)


def _maybe_auto_transcribe(project_id: str) -> None:
    """After all sources finish diarisation, auto-trigger transcription for
    pending segments with no transcript.  Triggers when no sources are still
    in-progress (failed sources do NOT block transcription)."""
    conn = get_conn(project_id)
    in_progress = conn.execute(
        """SELECT COUNT(*) FROM sources
           WHERE project_id=? AND status IN ('separation_pending','separation_running','diarisation_pending','diarisation_running')""",
        (project_id,),
    ).fetchone()[0]
    if in_progress > 0:
        return

    # All sources complete — check for untranscribed segments
    segments = conn.execute(
        """
        SELECT id, raw_path FROM segments
        WHERE project_id=? AND status IN ('pending','maybe') AND transcript IS NULL
        """,
        (project_id,),
    ).fetchall()

    if segments:
        project = conn.execute("SELECT whisper_model, language FROM projects WHERE id=?", (project_id,)).fetchone()
        params = {
            "segment_ids": [s["id"] for s in segments],
            "model": project["whisper_model"],
            "language": project["language"],
        }
        enqueue(project_id, "transcription_bulk", params=params)


# ---------------------------------------------------------------------------
# Scout handler (reference-less diarisation pass — calls external service)
# ---------------------------------------------------------------------------


async def _handle_scout_speakers(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Reference-less diarisation pass over one source, yielding speaker
    candidates. Never touches the source's status — a scout only reads its
    vocals stem. On success, replaces the project's speaker_candidates rows."""
    conn = get_conn(project_id)
    source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if source is None:
        _fail_job(project_id, job_id, "source_not_found")
        _recompute_project_status(project_id)
        return

    if not source["vocals_path"]:
        _fail_job(project_id, job_id, "vocals_not_ready")
        _recompute_project_status(project_id)
        return

    # An optional expected speaker count (from the scout request) forces
    # pyannote to that exact count; absent, the default 1–10 range applies.
    expected = params.get("expected_speaker_count")
    scout_params: dict = {
        "min_segment_duration": 1.0,
        "min_speakers": 1,
        "max_speakers": 10,
    }
    if isinstance(expected, int) and expected > 0:
        scout_params["num_speakers"] = expected

    data_prefix = _data_prefix()
    payload = {
        "job_id": job_id,
        "input_path": f"{data_prefix}/projects/{project_id}/{source['vocals_path']}",
        "reference_path": None,
        "output_dir": f"{data_prefix}/projects/{project_id}/reference_candidates/{job_id}/",
        "params": scout_params,
    }

    try:
        await _submit_with_retry("diarisation", payload)
    except Exception as exc:
        _fail_job(project_id, job_id, f"submit_failed: {exc}")
        _recompute_project_status(project_id)
        return

    def on_progress(result):
        progress = result.get("progress", 0)
        if progress:
            _update_progress(project_id, job_id, progress)

    result = await service_client.poll_until_complete("diarisation", job_id, on_progress=on_progress)

    if result["status"] == "failed":
        _fail_job(project_id, job_id, result.get("error", "unknown_error"))
        _recompute_project_status(project_id)
        return

    # Replace the project's candidate set with this scout's speakers. montage_path
    # is stored relative to the project dir. Collect the superseded scouts'
    # job ids first so their montage directories can be removed after commit.
    now = _now()
    old_scout_ids = [
        r["scout_job_id"]
        for r in conn.execute(
            "SELECT DISTINCT scout_job_id FROM speaker_candidates WHERE project_id=? AND scout_job_id != ?",
            (project_id, job_id),
        ).fetchall()
    ]
    conn.execute("DELETE FROM speaker_candidates WHERE project_id=?", (project_id,))
    for sp in result.get("speakers", []):
        # Store the pool turns (index/start/end/duration). Slice paths are
        # derived from scout_job_id + speaker_label + index, not persisted.
        conn.execute(
            """
            INSERT INTO speaker_candidates
                (id, project_id, scout_job_id, source_id, speaker_label,
                 pool_json, total_secs, segment_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), project_id, job_id, source_id, sp["speaker_label"],
                json.dumps(sp.get("pool", [])),
                sp["total_secs"], sp["segment_count"], now,
            ),
        )
    conn.commit()

    # Best-effort removal of superseded montage directories (kept only for the
    # candidate set they backed; the current scan's montages remain until the
    # next scan or project deletion so the user can re-pick).
    pdir = project_dir(project_id)
    for old_id in old_scout_ids:
        shutil.rmtree(pdir / "reference_candidates" / old_id, ignore_errors=True)

    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)


# ---------------------------------------------------------------------------
# Transcription handlers (calls external service)
# ---------------------------------------------------------------------------


async def _handle_transcription_bulk(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Submit bulk transcription to external service. Writes results incrementally,
    deduplicating cumulative completed_segments."""


    conn = get_conn(project_id)
    segment_ids = params.get("segment_ids", [])
    if not segment_ids:
        _fail_job(project_id, job_id, "no_segment_ids")
        _recompute_project_status(project_id)
        return

    project = conn.execute(
        "SELECT whisper_model, language, whisper_batch_size, whisper_compute_type "
        "FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()

    # Build segment list with wav_paths (bulk query). Untranscribed
    # pending/below_threshold segments are eligible for sentence-aligned
    # re-segmentation (spec/pipeline.md §Sentence-aligned re-segmentation);
    # user-touched statuses (maybe, ...) are transcribed without splitting.
    data_prefix = _data_prefix()
    placeholders = ",".join("?" * len(segment_ids))
    rows = conn.execute(
        f"SELECT id, raw_path, start_secs, status, transcript, duration_secs FROM segments WHERE id IN ({placeholders})",
        segment_ids,
    ).fetchall()
    # Preload durations for the short_transcript check — avoids an N+1
    # SELECT per segment inside the poll loop's incremental write path.
    durations = {r["id"]: r["duration_secs"] for r in rows}
    seg_list = [
        {
            "id": r["id"],
            "wav_path": f"{data_prefix}/projects/{project_id}/{r['raw_path']}",
            "start_secs": r["start_secs"],
            "resegment": r["status"] in ("pending", "below_threshold") and r["transcript"] is None,
        }
        for r in rows
    ]

    if not seg_list:
        _fail_job(project_id, job_id, "no_valid_segments")
        _recompute_project_status(project_id)
        return

    model = params.get("model") or project["whisper_model"]
    language = params.get("language", project["language"])
    # batch_size falls back to project config; a params override lets an OOM
    # retry drop it further without changing the project setting.
    batch_size = params.get("batch_size") or project["whisper_batch_size"]

    payload = {
        "job_id": job_id,
        "segments": seg_list,
        "model": model,
        "language": language,
        "batch_size": batch_size,
        "compute_type": project["whisper_compute_type"],
    }

    try:
        await _submit_with_retry("transcription", payload)
    except Exception as exc:
        _fail_job(project_id, job_id, f"submit_failed: {exc}")
        _recompute_project_status(project_id)
        return

    # Track already-written parent segment IDs to deduplicate cumulative
    # results (split entries repeat under the parent id after the parent row
    # has been replaced by children).
    written_ids: set[str] = set()

    def on_progress(result):
        progress = result.get("progress", 0)
        if progress:
            _update_progress(project_id, job_id, progress)

        # Write completed segments incrementally
        _apply_transcription_results(
            project_id, conn, result.get("completed_segments", []), written_ids,
            durations=durations,
        )

    result = await service_client.poll_until_complete("transcription", job_id, on_progress=on_progress)

    if result["status"] == "failed":
        _fail_job(project_id, job_id, result.get("error", "unknown_error"))
        _recompute_project_status(project_id)
        return

    # Final pass: write any remaining completed segments from the final poll
    on_progress(result)

    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)


async def _handle_transcription_segment(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Re-transcribe a single segment. Overwrites transcript + confidence, preserves transcript_edited."""
    conn = get_conn(project_id)
    segment_ids = params.get("segment_ids", [])
    if not segment_ids:
        _fail_job(project_id, job_id, "no_segment_ids")
        _recompute_project_status(project_id)
        return

    seg_id = segment_ids[0]
    seg = conn.execute("SELECT id, raw_path FROM segments WHERE id=? AND project_id=?", (seg_id, project_id)).fetchone()
    if seg is None:
        _fail_job(project_id, job_id, "segment_not_found")
        _recompute_project_status(project_id)
        return

    project = conn.execute(
        "SELECT whisper_model, language, whisper_compute_type FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    data_prefix = _data_prefix()

    payload = {
        "job_id": job_id,
        "segments": [{
            "id": seg["id"],
            "wav_path": f"{data_prefix}/projects/{project_id}/{seg['raw_path']}",
        }],
        "model": project["whisper_model"],
        "language": project["language"],
        "batch_size": 1,
        "compute_type": project["whisper_compute_type"],
    }

    try:
        await _submit_with_retry("transcription", payload)
    except Exception as exc:
        _fail_job(project_id, job_id, f"submit_failed: {exc}")
        _recompute_project_status(project_id)
        return

    result = await service_client.poll_until_complete("transcription", job_id)

    if result["status"] == "failed":
        _fail_job(project_id, job_id, result.get("error", "unknown_error"))
        _recompute_project_status(project_id)
        return

    # transcription_segment jobs never set resegment, so results are always
    # unsplit; the shared writer handles the transcript/flag/auto-approve path.
    completed = [cs for cs in result.get("completed_segments", []) if cs["id"] == seg_id]
    _apply_transcription_results(project_id, conn, completed, set())

    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)


def _apply_transcription_results(
    project_id: str,
    conn,
    completed: list[dict],
    written_ids: set[str],
    durations: dict[str, float] | None = None,
) -> None:
    """Shared write path for transcription results (bulk and single-segment).

    - Deduplicates cumulative ``completed_segments`` keyed on the PARENT
      segment id via the caller-owned ``written_ids`` set.
    - Entries carrying a per-segment ``error`` leave the transcript NULL
      (so future bulk/auto runs naturally retry) and record a
      ``transcription_error: <msg>`` flag; a later successful transcript
      clears the flag.
    - Unsplit entries update transcript/confidence in place (transcript_edited
      is never touched).
    - Split entries (``children``) re-check the parent's eligibility AT WRITE
      TIME (status still pending/below_threshold, transcript and
      transcript_edited still NULL — the snapshot taken at submit can be
      minutes stale). Still eligible: insert one row per child and delete the
      parent row in the same transaction; the parent WAV is deleted
      best-effort after commit. No longer eligible (reviewed/edited mid-job),
      or no child with a positive duration: keep the parent row and WAV and
      write the joined child transcripts to the parent instead.
    - Adds a ``short_transcript`` flag for effective durations < 2.0 s,
      using the caller-preloaded ``durations`` map ({segment_id:
      duration_secs}) when available to avoid an N+1 SELECT in the poll
      loop; ids not in the map fall back to a single lookup.
    - Applies auto-approval to the segments written (spec/pipeline.md
      §Auto-approval).

    Commits at the end (single transaction per call).
    """
    if not completed:
        return

    now = _now()
    pdir = project_dir(project_id)
    parent_wavs: list[Path] = []
    touched_ids: list[str] = []

    def _write_transcript(seg_id: str, transcript, confidence) -> None:
        """Plain in-place transcript write + flag/auto-approve bookkeeping."""
        conn.execute(
            """
            UPDATE segments
            SET transcript=?, transcript_confidence=?, updated_at=?
            WHERE id=? AND project_id=?
            """,
            (transcript, confidence, now, seg_id, project_id),
        )
        _clear_transcription_error_flag(conn, seg_id)
        duration = durations.get(seg_id) if durations else None
        if duration is None:
            row = conn.execute(
                "SELECT duration_secs FROM segments WHERE id=?", (seg_id,)
            ).fetchone()
            duration = row["duration_secs"] if row is not None else None
        if duration is not None and duration < 2.0:
            _add_flag(conn, seg_id, "short_transcript")
        touched_ids.append(seg_id)

    for cs in completed:
        seg_id = cs["id"]
        if seg_id in written_ids:
            continue
        written_ids.add(seg_id)

        if cs.get("error"):
            # Per-segment service failure: leave transcript NULL so every
            # retranscription selector (transcript IS NULL) still picks the
            # segment up, and surface the failure as a flag.
            _set_transcription_error_flag(conn, seg_id, cs["error"])
            continue

        children = cs.get("children")
        if children:
            parent = conn.execute(
                """SELECT source_id, speaker_label, match_confidence, status,
                          raw_path, transcript, transcript_edited
                   FROM segments WHERE id=? AND project_id=?""",
                (seg_id, project_id),
            ).fetchone()
            if parent is None:
                logger.warning("Transcription returned children for unknown segment %s", seg_id)
                continue
            # Defensive: a child whose clamped bounds inverted or collapsed
            # would be a negative/zero-duration row with a zero-frame WAV.
            valid_children = [
                ch for ch in children if ch["end_secs"] > ch["start_secs"]
            ]
            # Eligibility re-check at write time (mirrors the submit-time
            # snapshot in _handle_transcription_bulk).
            still_eligible = (
                parent["status"] in ("pending", "below_threshold")
                and parent["transcript"] is None
                and parent["transcript_edited"] is None
            )
            if not still_eligible or not valid_children:
                # Do not split, do not delete anything: fold the children
                # back into a plain parent write.
                texts = [
                    t for t in ((ch.get("transcript") or "").strip() for ch in children) if t
                ]
                confs = [
                    ch["transcript_confidence"]
                    for ch in children
                    if ch.get("transcript_confidence") is not None
                ]
                _write_transcript(
                    seg_id, " ".join(texts), min(confs) if confs else None
                )
                continue
            for ch in valid_children:
                # duration_secs is a GENERATED column — never inserted.
                # Children inherit attribution + status from the parent; the
                # service supplies id, WAV, absolute timestamps and transcript.
                child_raw = f"segments/raw/{Path(ch['wav_path']).name}"
                conn.execute(
                    """
                    INSERT INTO segments
                        (id, project_id, source_id, raw_path, start_secs, end_secs,
                         speaker_label, match_confidence, transcript,
                         transcript_confidence, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ch["id"], project_id, parent["source_id"], child_raw,
                        ch["start_secs"], ch["end_secs"],
                        parent["speaker_label"], parent["match_confidence"],
                        ch["transcript"], ch.get("transcript_confidence"),
                        parent["status"], now, now,
                    ),
                )
                if (ch["end_secs"] - ch["start_secs"]) < 2.0:
                    _add_flag(conn, ch["id"], "short_transcript")
                touched_ids.append(ch["id"])
            conn.execute("DELETE FROM segments WHERE id=? AND project_id=?", (seg_id, project_id))
            if parent["raw_path"]:
                parent_wavs.append(pdir / parent["raw_path"])
        else:
            _write_transcript(seg_id, cs["transcript"], cs.get("transcript_confidence"))

    if touched_ids:
        auto_approve_promote(conn, project_id, now, segment_ids=touched_ids)

    conn.commit()

    # Best-effort parent WAV removal, only after the replacement committed.
    for wav in parent_wavs:
        try:
            if wav.exists():
                wav.unlink()
        except OSError as exc:
            logger.warning("Could not delete replaced parent WAV %s: %s", wav, exc)


_TRANSCRIPTION_ERROR_PREFIX = "transcription_error: "


def _set_transcription_error_flag(conn, segment_id: str, message: str) -> None:
    """Record a per-segment transcription failure in the flags array.

    Any existing ``transcription_error:`` flag is replaced (dedupe on
    re-add), mirroring the ``cleanup_error: <msg>`` pattern.
    """
    row = conn.execute("SELECT flags FROM segments WHERE id=?", (segment_id,)).fetchone()
    if row is None:
        logger.warning("Transcription returned an error for unknown segment %s", segment_id)
        return
    flags = [
        f for f in (json.loads(row["flags"]) if row["flags"] else [])
        if not f.startswith(_TRANSCRIPTION_ERROR_PREFIX)
    ]
    flags.append(f"{_TRANSCRIPTION_ERROR_PREFIX}{message}")
    conn.execute("UPDATE segments SET flags=? WHERE id=?", (json.dumps(flags), segment_id))


def _clear_transcription_error_flag(conn, segment_id: str) -> None:
    """Drop any stale ``transcription_error:`` flag after a successful write."""
    row = conn.execute("SELECT flags FROM segments WHERE id=?", (segment_id,)).fetchone()
    if row is None or not row["flags"]:
        return
    flags = json.loads(row["flags"])
    kept = [f for f in flags if not f.startswith(_TRANSCRIPTION_ERROR_PREFIX)]
    if len(kept) != len(flags):
        conn.execute("UPDATE segments SET flags=? WHERE id=?", (json.dumps(kept), segment_id))


# ---------------------------------------------------------------------------
# Shared cleanup step (used by export and dataset_build)
# ---------------------------------------------------------------------------


class CleanupError(Exception):
    """Raised by _run_cleanup on a submit failure or a job-level cleanup failure.

    The message is the error string to surface on the failing job (and, for
    dataset builds, the model row)."""


async def _run_cleanup(
    project_id: str,
    project_row: Any,
    segments: list,
    job_id: str,
    on_progress: Callable | None = None,
) -> list[dict]:
    """Submit a cleanup job for the given segments and poll until complete.

    Each segment row must expose ``id`` and ``raw_path``. Cleaned WAVs are
    written to ``export/{id}.wav`` on the shared volume. Returns the service's
    per-segment ``results`` list. Raises CleanupError on submit failure or a
    job-level cleanup failure.
    """
    data_prefix = _data_prefix()
    cleanup_segments = [
        {
            "id": seg["id"],
            "input_path": f"{data_prefix}/projects/{project_id}/{seg['raw_path']}",
            "output_path": f"{data_prefix}/projects/{project_id}/export/{seg['id']}.wav",
        }
        for seg in segments
    ]

    cleanup_payload = {
        "job_id": job_id,
        "segments": cleanup_segments,
        "params": {
            "target_lufs": project_row["target_lufs"],
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

    try:
        await _submit_with_retry("cleanup", cleanup_payload)
    except Exception as exc:
        raise CleanupError(f"cleanup_submit_failed: {exc}")

    result = await service_client.poll_until_complete("cleanup", job_id, on_progress=on_progress)
    if result["status"] == "failed":
        raise CleanupError(result.get("error", "unknown_error"))
    return result.get("results", [])


def _apply_cleanup_results(conn, results: list[dict]) -> None:
    """Apply cleanup per-segment results to the database (commits on completion).

    - FFmpeg error → ``auto_rejected`` status + ``cleanup_error`` flag
    - silent-after-trim → ``auto_rejected`` status
    - clipping → ``clipping_warning`` status + column + ``export_path``
    - success → ``export_path`` recorded
    """
    now = _now()
    for seg_result in results:
        seg_id = seg_result["id"]

        if seg_result.get("error"):
            # FFmpeg error — auto_reject with cleanup_error flag
            conn.execute(
                "UPDATE segments SET status='auto_rejected', updated_at=? WHERE id=?",
                (now, seg_id),
            )
            _add_flag(conn, seg_id, f"cleanup_error: {seg_result['error']}")

        elif seg_result.get("auto_rejected"):
            # Silent after trim
            conn.execute(
                "UPDATE segments SET status='auto_rejected', updated_at=? WHERE id=?",
                (now, seg_id),
            )

        elif seg_result.get("clipping_warning"):
            # Clipping detected — the cleaned WAV is still exported (with the
            # flag recorded in the manifest); set the column, the status, AND
            # export_path so the segment appears in the manifest.
            conn.execute(
                """
                UPDATE segments
                SET status='clipping_warning', clipping_warning=1, export_path=?, updated_at=?
                WHERE id=?
                """,
                (f"export/{seg_id}.wav", now, seg_id),
            )

        else:
            # Success — record export_path
            conn.execute(
                "UPDATE segments SET export_path=?, updated_at=? WHERE id=?",
                (f"export/{seg_id}.wav", now, seg_id),
            )

    conn.commit()


def _select_dataset_segments(
    conn, mode: str, min_confidence: float | None
) -> tuple[list, dict]:
    """Select segments for a dataset build, applying training filters.

    ``approved`` mode selects ``status='approved'``; ``auto`` mode selects
    ``match_confidence >= min_confidence`` (default 0.85) with status not
    ``rejected``/``auto_rejected``. Training filters then drop segments outside
    the [1.0, 11.0] s range and any carrying a ``cleanup_error`` flag.

    Returns ``(kept_rows, dropped)`` where ``dropped`` counts
    ``{too_short, too_long, flagged}``.
    """
    if mode == "auto":
        floor = min_confidence if min_confidence is not None else 0.85
        rows = conn.execute(
            """
            SELECT seg.*, src.filename AS source_filename
            FROM segments seg JOIN sources src ON src.id = seg.source_id
            WHERE seg.match_confidence >= ?
              AND seg.status NOT IN ('rejected', 'auto_rejected')
            """,
            (floor,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT seg.*, src.filename AS source_filename
            FROM segments seg JOIN sources src ON src.id = seg.source_id
            WHERE seg.status = 'approved'
            """,
        ).fetchall()

    kept: list = []
    dropped = {"too_short": 0, "too_long": 0, "flagged": 0}
    for r in rows:
        dur = r["duration_secs"] or 0.0
        if dur < 1.0:
            dropped["too_short"] += 1
            continue
        if dur > 11.0:
            dropped["too_long"] += 1
            continue
        flags = json.loads(r["flags"]) if r["flags"] else []
        if any(str(f).startswith("cleanup_error") for f in flags):
            dropped["flagged"] += 1
            continue
        kept.append(r)
    return kept, dropped



# ---------------------------------------------------------------------------


async def _handle_export(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Export pipeline: submit cleanup for exportable segments, write manifest,
    package archive.

    The new export is staged in export_tmp/ and only replaces the previous
    export/ (WAVs, manifest.json, archive, export_path rows, exported_at)
    once cleanup has succeeded — a failed re-export leaves the previous
    export fully intact and still downloadable.
    """
    conn = get_conn(project_id)
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    # Exportable = approved (auto_approved is treated identically) plus
    # clipping_warning: keep-unless-rejected semantics — clipping segments
    # export with the flag recorded in the manifest. Cleanup re-flags them.
    approved = conn.execute(
        """
        SELECT seg.*, src.filename AS source_filename
        FROM segments seg
        JOIN sources src ON src.id = seg.source_id
        WHERE seg.project_id=? AND seg.status IN ('approved', 'auto_approved', 'clipping_warning')
        """,
        (project_id,),
    ).fetchall()

    if not approved:
        _fail_job(project_id, job_id, "no_approved_segments")
        _recompute_project_status(project_id)
        return

    # Refuse to build an export that would silently omit approved-but-
    # untranscribed segments (the manifest drops NULL-transcript rows). The
    # PATCH/bulk guards prevent new ones; this catches legacy data approved
    # before those guards existed. Fail loudly with the offending ids.
    untranscribed = [
        seg["id"]
        for seg in approved
        if seg["transcript"] is None and seg["transcript_edited"] is None
    ]
    if untranscribed:
        preview = ", ".join(untranscribed[:5])
        more = "" if len(untranscribed) <= 5 else f" (+{len(untranscribed) - 5} more)"
        _fail_job(
            project_id,
            job_id,
            f"untranscribed_approved_segments: {len(untranscribed)} approved "
            f"segment(s) have no transcript: {preview}{more}. Transcribe or reject them.",
        )
        _recompute_project_status(project_id)
        return

    pdir = project_dir(project_id)
    data_prefix = _data_prefix()
    export_dir = pdir / "export"
    staging_dir = pdir / "export_tmp"
    archive_path = pdir / "export.tar.gz"
    archive_tmp = pdir / "export.tar.gz.tmp"

    # Clear any stale staging left by an earlier failed/interrupted export,
    # then stage this run's WAVs in export_tmp/.
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Build cleanup payload — outputs land in the staging directory.
        cleanup_segments = []
        for seg in approved:
            cleanup_segments.append({
                "id": seg["id"],
                "input_path": f"{data_prefix}/projects/{project_id}/{seg['raw_path']}",
                "output_path": f"{data_prefix}/projects/{project_id}/export_tmp/{seg['id']}.wav",
            })

        cleanup_payload = {
            "job_id": job_id,
            "segments": cleanup_segments,
            "params": {
                "target_lufs": project["target_lufs"],
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

        try:
            await _submit_with_retry("cleanup", cleanup_payload)
        except Exception as exc:
            _fail_job(project_id, job_id, f"cleanup_submit_failed: {exc}")
            _recompute_project_status(project_id)
            return

        def on_progress(result):
            progress = result.get("progress", 0)
            if progress:
                _update_progress(project_id, job_id, progress)

        result = await service_client.poll_until_complete("cleanup", job_id, on_progress=on_progress)

        if result["status"] == "failed":
            _fail_job(project_id, job_id, result.get("error", "unknown_error"))
            _recompute_project_status(project_id)
            return

        # Cleanup succeeded — apply per-segment results and promote the staged
        # export. All DB writes commit together at the end; any failure before
        # then rolls back so the previous export's rows survive.
        try:
            now = _now()
            conn.execute(
                "UPDATE segments SET export_path=NULL WHERE project_id=?", (project_id,)
            )
            for seg_result in result.get("results", []):
                seg_id = seg_result["id"]

                if seg_result.get("error"):
                    # FFmpeg error — auto_reject with cleanup_error flag
                    conn.execute(
                        "UPDATE segments SET status='auto_rejected', updated_at=? WHERE id=?",
                        (now, seg_id),
                    )
                    _add_flag(conn, seg_id, f"cleanup_error: {seg_result['error']}")

                elif seg_result.get("auto_rejected"):
                    # Silent after trim
                    conn.execute(
                        "UPDATE segments SET status='auto_rejected', updated_at=? WHERE id=?",
                        (now, seg_id),
                    )

                elif seg_result.get("clipping_warning"):
                    # Clipping detected — the cleaned WAV is still exported (with
                    # the flag recorded in the manifest); set the column, the
                    # status, AND export_path so the segment appears in the
                    # manifest.
                    conn.execute(
                        """
                        UPDATE segments
                        SET status='clipping_warning', clipping_warning=1, export_path=?, updated_at=?
                        WHERE id=?
                        """,
                        (f"export/{seg_id}.wav", now, seg_id),
                    )

                else:
                    # Success — record export_path (final location after promotion)
                    conn.execute(
                        "UPDATE segments SET export_path=?, updated_at=? WHERE id=?",
                        (f"export/{seg_id}.wav", now, seg_id),
                    )

            # Write manifest.json (from the uncommitted DB state on this same
            # connection) into staging, and build the new archive alongside the
            # old one, packaging exactly the manifest's WAVs.
            audio_files = _write_manifest(
                project_id, project, pdir, export_dir=staging_dir
            )
            _package_archive(
                pdir, audio_files, export_dir=staging_dir, archive_path=archive_tmp
            )

            # Promote: replace the old export directory and archive, then
            # commit the matching DB state.
            if export_dir.exists():
                shutil.rmtree(export_dir)
            staging_dir.rename(export_dir)
            os.replace(archive_tmp, archive_path)

            # Mark project as exported and record when, so a later approval or
            # source change can invalidate it (see status.invalidate_export).
            conn.execute(
                "UPDATE projects SET status='exported', exported_at=?, updated_at=? WHERE id=?",
                (now, now, project_id),
            )
            conn.commit()
        except BaseException:
            # Keep the previous export's export_path rows and exported_at.
            conn.rollback()
            raise

        _complete_job(project_id, job_id)
    finally:
        # Success renames staging away; on any failure remove leftovers so the
        # previous export/ and archive remain the only visible state.
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        if archive_tmp.exists():
            archive_tmp.unlink()



def _write_manifest(
    project_id: str, project: Any, pdir: Path, export_dir: Path | None = None
) -> list[str]:
    """Write manifest.json (into export_dir, default export/) from the database.

    Returns the list of audio filenames referenced by the manifest so the
    archive can be built to contain exactly those WAVs plus manifest.json.
    """
    conn = get_conn(project_id)

    # Get segments that were successfully cleaned (have export_path and are still approved)
    rows = conn.execute(
        """
        SELECT seg.id, seg.export_path, COALESCE(seg.transcript_edited, seg.transcript) AS text,
               src.filename AS source_filename, seg.start_secs, seg.end_secs, seg.duration_secs,
               seg.match_confidence, seg.transcript_confidence, seg.clipping_warning
        FROM segments seg
        JOIN sources src ON src.id = seg.source_id
        WHERE seg.project_id=? AND seg.export_path IS NOT NULL
          AND seg.status IN ('approved', 'auto_approved', 'clipping_warning')
        """,
        (project_id,),
    ).fetchall()

    manifest_segments = []
    audio_files: list[str] = []
    total_duration = 0.0
    for r in rows:
        transcript = r["text"]
        if transcript is None:
            # Skip segments without transcript (edge case)
            logger.warning("Segment %s has no transcript, excluding from manifest", r["id"])
            continue
        dur = r["duration_secs"] or 0.0
        total_duration += dur
        manifest_segments.append({
            "id": r["id"],
            "audio_file": f"{r['id']}.wav",
            "text": transcript,
            "source": r["source_filename"],
            "start_secs": r["start_secs"],
            "end_secs": r["end_secs"],
            "duration_secs": dur,
            "match_confidence": r["match_confidence"],
            "transcript_confidence": r["transcript_confidence"],
            "clipping_warning": bool(r["clipping_warning"]),
        })
        audio_files.append(f"{r['id']}.wav")

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

    target_dir = export_dir if export_dir is not None else pdir / "export"
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = target_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return audio_files


def _package_archive(
    pdir: Path,
    audio_files: list[str],
    export_dir: Path | None = None,
    archive_path: Path | None = None,
) -> None:
    """Package manifest.json plus exactly the manifest's WAVs into the archive.

    Building from the manifest (rather than globbing export/) guarantees the
    archive never contains orphan WAVs from a previous export.
    """
    if export_dir is None:
        export_dir = pdir / "export"
    if archive_path is None:
        archive_path = pdir / "export.tar.gz"

    with tarfile.open(archive_path, "w:gz") as tar:
        manifest = export_dir / "manifest.json"
        if manifest.exists():
            tar.add(str(manifest), arcname="manifest.json")
        for name in audio_files:
            f = export_dir / name
            if f.exists():
                tar.add(str(f), arcname=name)


# ---------------------------------------------------------------------------
# Dataset build handler (v1.5) — shared cleanup + manifest for a model
# ---------------------------------------------------------------------------


async def _handle_dataset_build(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Build a fine-tune dataset for a model: select segments, clean any lacking
    cleaned audio, write models/{model_id}/dataset.json (absolute audio paths)."""
    conn = get_conn(project_id)
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    model_id = params.get("model_id")
    mode = params.get("mode", "approved")
    min_confidence = params.get("min_confidence")

    def _fail_model(msg: str) -> None:
        conn.execute(
            "UPDATE models SET status='failed', error=?, updated_at=? WHERE id=?",
            (msg, _now(), model_id),
        )
        conn.commit()

    # Re-select at run time — approvals/thresholds may have moved since enqueue.
    kept, dropped = _select_dataset_segments(conn, mode, min_confidence)
    total_dur = sum((r["duration_secs"] or 0.0) for r in kept)
    if total_dur < 300.0:
        msg = f"insufficient dataset ({total_dur:.1f}s selected, 300s required)"
        _fail_model(msg)
        _fail_job(project_id, job_id, msg)
        _recompute_project_status(project_id)
        return

    # Clean only segments that lack cleaned audio; cleanup occupies 0-80%.
    to_clean = [r for r in kept if r["export_path"] is None]

    def on_progress(result):
        progress = result.get("progress", 0)
        if progress:
            _update_progress(project_id, job_id, int(progress * 0.8))

    if to_clean:
        try:
            results = await _run_cleanup(project_id, project, to_clean, job_id, on_progress)
        except CleanupError as exc:
            _fail_model(str(exc))
            _fail_job(project_id, job_id, str(exc))
            _recompute_project_status(project_id)
            return
        _apply_cleanup_results(conn, results)

    _update_progress(project_id, job_id, 80)

    # Re-read the kept set: some may have been auto_rejected during cleanup;
    # keep only those with cleaned audio available (absolute export path).
    kept_ids = [r["id"] for r in kept]
    placeholders = ",".join("?" * len(kept_ids))
    rows = conn.execute(
        f"""
        SELECT seg.*, src.filename AS source_filename
        FROM segments seg JOIN sources src ON src.id = seg.source_id
        WHERE seg.id IN ({placeholders})
        """,
        kept_ids,
    ).fetchall()
    final = [r for r in rows if r["status"] != "auto_rejected" and r["export_path"] is not None]

    # Write the dataset manifest (export-manifest schema, absolute audio paths).
    data_prefix = _data_prefix()
    models_dir = project_dir(project_id) / "models" / model_id
    models_dir.mkdir(parents=True, exist_ok=True)

    manifest_segments = []
    total_duration = 0.0
    for r in final:
        text = r["transcript_edited"] or r["transcript"]
        if text is None:
            logger.warning("Segment %s has no transcript, excluding from dataset", r["id"])
            continue
        dur = r["duration_secs"] or 0.0
        total_duration += dur
        manifest_segments.append({
            "id": r["id"],
            "audio_file": f"{data_prefix}/projects/{project_id}/{r['export_path']}",
            "text": text,
            "source": r["source_filename"],
            "start_secs": r["start_secs"],
            "end_secs": r["end_secs"],
            "duration_secs": dur,
            "match_confidence": r["match_confidence"],
            "transcript_confidence": r["transcript_confidence"],
        })

    if not manifest_segments:
        msg = "no usable segments after cleanup (missing transcripts or cleaned audio)"
        _fail_model(msg)
        _fail_job(project_id, job_id, msg)
        _recompute_project_status(project_id)
        return

    manifest = {
        "version": "1",
        "project_id": project_id,
        "exported_at": _now(),
        "speaker": "target",
        "segments": manifest_segments,
        "selection": {
            "mode": mode,
            "min_confidence": min_confidence,
            "dropped": dropped,
        },
        "stats": {
            "segment_count": len(manifest_segments),
            "total_duration_secs": total_duration,
        },
    }
    (models_dir / "dataset.json").write_text(json.dumps(manifest, indent=2))

    conn.execute(
        """
        UPDATE models
        SET segment_count=?, dataset_duration_secs=?, dataset_manifest_path=?, updated_at=?
        WHERE id=?
        """,
        (len(manifest_segments), total_duration, f"models/{model_id}/dataset.json", _now(), model_id),
    )
    conn.commit()

    _update_progress(project_id, job_id, 100)
    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)


# ---------------------------------------------------------------------------
# Fine-tune handler (v1.5) — XTTS service
# ---------------------------------------------------------------------------


async def _handle_finetune(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Submit an XTTS-v2 fine-tune and poll (10 s) to completion.

    Guards on the model row (dataset build must have succeeded). Mirrors the
    vocal-separation OOM retry: on failure with retry_with, resubmit once with
    merged params and a fresh job id."""
    conn = get_conn(project_id)
    model_id = params.get("model_id")
    hyper = params.get("params", {}) or {}

    model = conn.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
    if model is None or model["dataset_manifest_path"] is None or model["status"] != "pending":
        _fail_job(project_id, job_id, "dataset build did not complete")
        _recompute_project_status(project_id)
        return

    conn.execute(
        "UPDATE models SET status='training', updated_at=? WHERE id=?",
        (_now(), model_id),
    )
    conn.commit()

    project = conn.execute("SELECT language FROM projects WHERE id=?", (project_id,)).fetchone()
    language = project["language"] or "en"

    data_prefix = _data_prefix()
    manifest_path = f"{data_prefix}/projects/{project_id}/{model['dataset_manifest_path']}"
    output_dir = f"{data_prefix}/projects/{project_id}/models/{model_id}"

    hyperparams = {
        "epochs": hyper.get("epochs", 10),
        "batch_size": hyper.get("batch_size", 3),
        "grad_accum": hyper.get("grad_accum", 1),
        "learning_rate": hyper.get("learning_rate", 5e-6),
    }

    def make_payload(hp: dict) -> dict:
        return {
            "job_id": str(uuid.uuid4()),
            "type": "finetune",
            "manifest_path": manifest_path,
            "output_dir": output_dir,
            "params": {**hp, "language": language, "eval_split": 0.1},
        }

    def on_progress(result):
        detail = result.get("progress")
        if not isinstance(detail, dict):
            return
        epoch = detail.get("epoch") or 0
        total_epochs = detail.get("total_epochs") or 1
        step = detail.get("step") or 0
        total_steps = detail.get("total_steps") or 1
        pct = int(((epoch - 1) + step / max(total_steps, 1)) / max(total_epochs, 1) * 100)
        _update_progress(project_id, job_id, max(0, min(99, pct)))
        _update_progress_detail(project_id, job_id, detail)

    def _fail(msg: str) -> None:
        conn.execute(
            "UPDATE models SET status='failed', error=?, updated_at=? WHERE id=?",
            (msg, _now(), model_id),
        )
        conn.commit()
        _fail_job(project_id, job_id, msg)
        _recompute_project_status(project_id)

    payload = make_payload(hyperparams)
    try:
        await _submit_with_retry("xtts", payload)
    except Exception as exc:
        _fail(f"submit_failed: {exc}")
        return

    result = await service_client.poll_until_complete(
        "xtts", payload["job_id"], interval_secs=10.0, on_progress=on_progress
    )

    if result["status"] == "failed":
        retry_with = result.get("retry_with")
        if retry_with:
            logger.info("Fine-tune OOM for model %s, retrying with %s", model_id, retry_with)
            retry_payload = make_payload({**hyperparams, **retry_with})
            try:
                await _submit_with_retry("xtts", retry_payload)
                result = await service_client.poll_until_complete(
                    "xtts", retry_payload["job_id"], interval_secs=10.0, on_progress=on_progress
                )
            except Exception as exc:
                result = {"status": "failed", "error": str(exc)}

        if result["status"] == "failed":
            _fail(result.get("error", "unknown_error"))
            return

    res = result.get("result") or {}
    conn.execute(
        """
        UPDATE models
        SET status='ready', checkpoint_dir=?, eval_loss=?, updated_at=?
        WHERE id=?
        """,
        (f"models/{model_id}", res.get("final_eval_loss"), _now(), model_id),
    )
    conn.commit()
    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)


# ---------------------------------------------------------------------------
# Preview handler (v1.5) — XTTS synthesise
# ---------------------------------------------------------------------------


def _resolve_conditioning(
    conn, project_row: Any, project_id: str, source: str | None, segment_count: int
) -> tuple[str, list[str]]:
    """Resolve a conditioning source to absolute WAV paths on /data.

    Returns ``(resolved_source, abs_paths)``. Raises LookupError when the
    requested (or, for source=None, any) source has no audio available.

    - ``reference_clip`` → the project reference clip (single element)
    - ``segments_raw`` → top-N by match_confidence, status not rejected/
      auto_rejected, duration 2-12 s → raw_path
    - ``segments_cleaned`` → same query + export_path present → export_path
    - ``None`` → best available: cleaned, then raw, then reference
    """
    data_prefix = _data_prefix()

    def _abs(rel: str) -> str:
        return f"{data_prefix}/projects/{project_id}/{rel}"

    def try_reference():
        ref = project_row["reference_path"]
        if ref:
            return "reference_clip", [_abs(ref)]
        return None

    def try_segments(cleaned: bool):
        col = "export_path" if cleaned else "raw_path"
        extra = "AND seg.export_path IS NOT NULL" if cleaned else ""
        rows = conn.execute(
            f"""
            SELECT seg.{col} AS p FROM segments seg
            WHERE seg.status NOT IN ('rejected', 'auto_rejected')
              AND seg.duration_secs BETWEEN 2 AND 12
              {extra}
            ORDER BY seg.match_confidence DESC
            LIMIT ?
            """,
            (segment_count,),
        ).fetchall()
        if rows:
            return ("segments_cleaned" if cleaned else "segments_raw"), [_abs(r["p"]) for r in rows]
        return None

    if source == "reference_clip":
        res = try_reference()
        if res:
            return res
        raise LookupError("no reference clip available")
    if source == "segments_raw":
        res = try_segments(False)
        if res:
            return res
        raise LookupError("no raw segments available")
    if source == "segments_cleaned":
        res = try_segments(True)
        if res:
            return res
        raise LookupError("no cleaned segments available")

    # source is None: prefer cleaned, then raw, then reference.
    for attempt in (lambda: try_segments(True), lambda: try_segments(False), try_reference):
        res = attempt()
        if res:
            return res
    raise LookupError("no conditioning audio available")


async def _handle_preview(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Synthesise a preview WAV via the XTTS service (zero-shot or fine-tuned)."""
    conn = get_conn(project_id)
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    text = params.get("text", "")
    model_id = params.get("model_id")
    cond = params.get("conditioning") or {}
    source = cond.get("source")
    segment_count = cond.get("segment_count", 5)

    checkpoint_dir = None
    if model_id:
        model = conn.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        if model is None or model["status"] != "ready":
            _fail_job(project_id, job_id, "model_not_ready")
            _recompute_project_status(project_id)
            return
        cp = model["checkpoint_dir"] or f"models/{model_id}"
        checkpoint_dir = f"{_data_prefix()}/projects/{project_id}/{cp}"

    try:
        _resolved, reference_wavs = _resolve_conditioning(
            conn, project, project_id, source, segment_count
        )
    except LookupError as exc:
        _fail_job(project_id, job_id, f"conditioning_unavailable: {exc}")
        _recompute_project_status(project_id)
        return

    previews_dir = project_dir(project_id) / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    output_path = f"{_data_prefix()}/projects/{project_id}/previews/{job_id}.wav"

    payload = {
        "job_id": job_id,
        "type": "synthesise",
        "text": text,
        "language": project["language"] or "en",
        "reference_wavs": reference_wavs,
        "checkpoint_dir": checkpoint_dir,
        "output_path": output_path,
        "params": {"temperature": 0.65},
    }

    try:
        await _submit_with_retry("xtts", payload)
    except Exception as exc:
        _fail_job(project_id, job_id, f"submit_failed: {exc}")
        _recompute_project_status(project_id)
        return

    result = await service_client.poll_until_complete("xtts", job_id)

    if result["status"] == "failed":
        _fail_job(project_id, job_id, result.get("error", "unknown_error"))
        _recompute_project_status(project_id)
        return

    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _add_flag(conn, segment_id: str, flag: str) -> None:
    """Add a flag to a segment's JSON flags array."""
    row = conn.execute("SELECT flags FROM segments WHERE id=?", (segment_id,)).fetchone()
    flags = json.loads(row["flags"]) if row and row["flags"] else []
    if flag not in flags:
        flags.append(flag)
    conn.execute("UPDATE segments SET flags=? WHERE id=?", (json.dumps(flags), segment_id))


def _data_prefix() -> str:
    """Return the data directory prefix used for absolute paths sent to services."""
    return os.environ.get("DATA_DIR", "/data")


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Callable] = {
    "extract_audio": _handle_extract_audio,
    "vocal_separation": _handle_vocal_separation,
    "diarisation": _handle_diarisation,
    "scout_speakers": _handle_scout_speakers,
    "transcription_bulk": _handle_transcription_bulk,
    "transcription_segment": _handle_transcription_segment,
    "export": _handle_export,
    "dataset_build": _handle_dataset_build,
    "finetune": _handle_finetune,
    "preview": _handle_preview,
}


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------


async def recover_jobs() -> None:
    """On startup, re-queue any jobs that were queued or running when the
    process last died.

    Each stuck job is superseded by a fresh job row with a new job_id rather
    than resubmitted under its old id. This avoids the 409 a processing service
    returns for a duplicate job_id, and — combined with diarisation's
    delete-before-insert — avoids segment primary-key conflicts on re-run.
    """
    from db import list_project_ids, get_conn

    for project_id in list_project_ids():
        conn = get_conn(project_id)
        stuck = conn.execute(
            "SELECT id, source_id, type, params FROM jobs WHERE project_id=? AND status IN ('queued','running') ORDER BY created_at",
            (project_id,),
        ).fetchall()

        new_ids: list[str] = []
        for row in stuck:
            new_id = str(uuid.uuid4())
            now = _now()
            conn.execute(
                """
                INSERT INTO jobs (id, project_id, source_id, type, status, params, created_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (new_id, project_id, row["source_id"], row["type"], row["params"], now),
            )
            conn.execute(
                "UPDATE jobs SET status='cancelled', error='superseded_by_recovery', completed_at=? WHERE id=?",
                (now, row["id"]),
            )
            new_ids.append(new_id)
        conn.commit()

        for new_id in new_ids:
            _queues[project_id].put_nowait(new_id)
        if new_ids:
            _ensure_runner(project_id)
            _recompute_project_status(project_id)


