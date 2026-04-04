"""In-memory FIFO job queue backed by the jobs SQLite table.

Jobs execute one at a time per project. Each job type has a handler registered
in HANDLERS. Wave 3 will add handlers for external service jobs; Wave 1 only
implements extract_audio (in-process FFmpeg) and stubs for the rest.
"""

import asyncio
import json
import logging
import os
import subprocess
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

from db import get_conn, project_dir
from service_client import submit_job, poll_job
from state_machines import validate_source_transition

logger = logging.getLogger(__name__)


def _get_service_url(service_name: str) -> str:
    urls = {
        "vocal_separation": os.environ.get("VOCAL_SEPARATION_URL", "http://vocal-separation:8001"),
        "diarisation": os.environ.get("DIARISATION_URL", "http://diarisation:8002"),
        "transcription": os.environ.get("TRANSCRIPTION_URL", "http://transcription:8003"),
        "cleanup": os.environ.get("CLEANUP_URL", "http://cleanup:8004"),
    }
    return urls[service_name]


# One asyncio.Lock per project to enforce one-at-a-time execution.
_project_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# In-memory queue: project_id -> list of job_id strings (FIFO)
_queues: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)

# Background runner tasks: project_id -> Task
_runners: dict[str, asyncio.Task] = {}


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


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


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
            job_id = await asyncio.wait_for(_queues[project_id].get(), timeout=60)
        except asyncio.TimeoutError:
            break

        async with _project_locks[project_id]:
            await _execute_job(project_id, job_id)


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
        await handler(project_id, job_id, row["source_id"], params)
    except Exception as exc:
        logger.exception("Job %s (%s) raised an exception", job_id, job_type)
        _fail_job(project_id, job_id, str(exc))


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


def _update_progress(project_id: str, job_id: str, progress: int) -> None:
    conn = get_conn(project_id)
    conn.execute("UPDATE jobs SET progress=? WHERE id=?", (progress, job_id))
    conn.commit()


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------


async def _handle_extract_audio(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Extract audio from a video file using FFmpeg subprocess."""
    from db import project_dir, get_conn

    conn = get_conn(project_id)
    source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if source is None:
        _fail_job(project_id, job_id, "source_not_found")
        return

    pdir = project_dir(project_id)
    input_path = pdir / source["file_path"]
    output_path = pdir / "audio" / "raw" / f"{source_id}.wav"

    # Update source status to extracting
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

    # Get duration via ffprobe
    duration_secs = await _get_audio_duration(str(output_path))

    conn.execute(
        """
        UPDATE sources
        SET status='step1_pending', audio_path=?, duration_secs=?, updated_at=?
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


async def _handle_vocal_separation(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Submit to vocal-separation service; handle OOM retry with chunk_secs."""
    conn = get_conn(project_id)
    source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if source is None:
        _fail_job(project_id, job_id, "source_not_found")
        return

    if not source["audio_path"]:
        _fail_job(project_id, job_id, "audio_path_missing")
        return

    pdir = project_dir(project_id)
    input_path = str(pdir / source["audio_path"])
    output_path = str(pdir / "audio" / "vocals" / f"{source_id}.wav")

    model = params.get("demucs_model", "htdemucs")
    chunk_secs = params.get("chunk_secs", None)

    if not validate_source_transition(source["status"], "step1_running"):
        logger.warning(
            "Vocal separation for source %s: invalid transition %s → step1_running, failing job",
            source_id, source["status"],
        )
        _fail_job(project_id, job_id, f"invalid_source_state: {source['status']} → step1_running")
        _recompute_project_status(project_id)
        return

    conn.execute(
        "UPDATE sources SET status='step1_running', updated_at=? WHERE id=?",
        (_now(), source_id),
    )
    conn.commit()

    svc_url = _get_service_url("vocal_separation")
    service_job_id = job_id  # reuse orchestrator job_id as the service-side job_id for initial attempt

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
            retry_service_job_id = str(uuid.uuid4())
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

    if not validate_source_transition(source["status"], "step2_running"):
        logger.warning(
            "Diarisation for source %s: invalid transition %s → step2_running, failing job",
            source_id, source["status"],
        )
        _fail_job(project_id, job_id, f"invalid_source_state: {source['status']} → step2_running")
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


async def _handle_stub_service_job(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Stub handler for Wave 2 service jobs (vocal_separation, diarisation, etc.).

    In Wave 1 these are not called — the pipeline/start endpoint is not wired
    to external services yet. This exists so the job type is recognised and
    doesn't error with 'No handler registered'.
    """
    # Wave 3 will replace this with real HTTP polling against the service.
    logger.info("Stub handler called for job in project %s (Wave 3 will implement this)", project_id)
    _fail_job(project_id, job_id, "service_not_yet_integrated")


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Callable] = {
    "extract_audio": _handle_extract_audio,
    "vocal_separation": _handle_vocal_separation,
    "diarisation": _handle_diarisation,
    "transcription_bulk": _handle_transcription_bulk,
    "transcription_segment": _handle_transcription_segment,
    "export": _handle_stub_service_job,
}


# ---------------------------------------------------------------------------
# Project status recomputation (delegates to shared module)
# ---------------------------------------------------------------------------


def _recompute_project_status(project_id: str) -> None:
    from status import recompute_project_status
    recompute_project_status(project_id)


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------


async def recover_jobs() -> None:
    """On startup, re-queue any jobs that were queued or running when the
    process last died."""
    from db import list_project_ids, get_conn

    for project_id in list_project_ids():
        conn = get_conn(project_id)
        stuck = conn.execute(
            "SELECT id FROM jobs WHERE project_id=? AND status IN ('queued','running') ORDER BY created_at",
            (project_id,),
        ).fetchall()
        # Reset 'running' → 'queued' so they re-execute
        conn.execute(
            "UPDATE jobs SET status='queued', started_at=NULL WHERE project_id=? AND status='running'",
            (project_id,),
        )
        conn.commit()
        for row in stuck:
            _queues[project_id].put_nowait(row["id"])
        if stuck:
            _ensure_runner(project_id)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
