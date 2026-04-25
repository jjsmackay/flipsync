"""In-memory FIFO job queue backed by the jobs SQLite table.

Jobs execute one at a time per project. Each job type has a handler registered
in HANDLERS. Wave 3 implements real handlers for external service jobs.
"""

import asyncio
import json
import logging
import os
import tarfile
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

from db import get_conn, project_dir

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Vocal Separation handler (calls external service)
# ---------------------------------------------------------------------------


async def _handle_vocal_separation(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Submit vocal separation to external service and poll until complete.

    On OOM failure with retry_with_chunk_secs: resubmit with chunk_secs.
    On second failure: mark source step1_failed.
    """
    from service_client import submit_job, poll_until_complete

    conn = get_conn(project_id)
    source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if source is None:
        _fail_job(project_id, job_id, "source_not_found")
        _recompute_project_status(project_id)
        return

    # Transition source to step1_running
    conn.execute(
        "UPDATE sources SET status='step1_running', step1_error=NULL, updated_at=? WHERE id=?",
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
        await submit_job("vocal_separation", payload)
    except Exception as exc:
        conn.execute(
            "UPDATE sources SET status='step1_failed', step1_error=?, updated_at=? WHERE id=?",
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

    result = await poll_until_complete("vocal_separation", job_id, on_progress=on_progress)

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
                await submit_job("vocal_separation", retry_payload)
                result = await poll_until_complete("vocal_separation", retry_job_id, on_progress=on_progress)
            except Exception as exc:
                result = {"status": "failed", "error": str(exc)}

        if result["status"] == "failed":
            error = result.get("error", "unknown_error")
            conn.execute(
                "UPDATE sources SET status='step1_failed', step1_error=?, updated_at=? WHERE id=?",
                (error, _now(), source_id),
            )
            conn.commit()
            _fail_job(project_id, job_id, error)
            _recompute_project_status(project_id)
            return

    # Success: update source
    conn.execute(
        """
        UPDATE sources
        SET status='step2_pending', vocals_path=?, step1_model=?, step1_error=NULL, updated_at=?
        WHERE id=?
        """,
        (f"audio/vocals/{source_id}.wav", model, _now(), source_id),
    )
    conn.commit()
    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)

    # Auto-enqueue diarisation
    _auto_enqueue_diarisation(project_id, source_id)


def _auto_enqueue_diarisation(project_id: str, source_id: str) -> None:
    """After vocal separation succeeds, auto-enqueue diarisation for this source."""
    conn = get_conn(project_id)
    project = conn.execute("SELECT reference_path FROM projects WHERE id=?", (project_id,)).fetchone()
    if project and project["reference_path"]:
        enqueue(project_id, "diarisation", source_id=source_id)


# ---------------------------------------------------------------------------
# Diarisation handler (calls external service)
# ---------------------------------------------------------------------------


async def _handle_diarisation(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Submit diarisation to external service, write segments to DB on completion."""
    from service_client import submit_job, poll_until_complete

    conn = get_conn(project_id)
    source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if source is None:
        _fail_job(project_id, job_id, "source_not_found")
        _recompute_project_status(project_id)
        return

    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project["reference_path"]:
        _fail_job(project_id, job_id, "no_reference_clip")
        conn.execute(
            "UPDATE sources SET status='step2_failed', step2_error='no_reference_clip', updated_at=? WHERE id=?",
            (_now(), source_id),
        )
        conn.commit()
        _recompute_project_status(project_id)
        return

    # Transition source to step2_running
    conn.execute(
        "UPDATE sources SET status='step2_running', step2_error=NULL, updated_at=? WHERE id=?",
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
        await submit_job("diarisation", payload)
    except Exception as exc:
        conn.execute(
            "UPDATE sources SET status='step2_failed', step2_error=?, updated_at=? WHERE id=?",
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

    result = await poll_until_complete("diarisation", job_id, on_progress=on_progress)

    if result["status"] == "failed":
        error = result.get("error", "unknown_error")
        conn.execute(
            "UPDATE sources SET status='step2_failed', step2_error=?, updated_at=? WHERE id=?",
            (error, _now(), source_id),
        )
        conn.commit()
        _fail_job(project_id, job_id, error)
        _recompute_project_status(project_id)
        return

    # Write segments to DB
    match_threshold = project["match_threshold"]
    now = _now()
    segments = result.get("segments", [])
    for seg in segments:
        seg_status = "pending" if seg["match_confidence"] >= match_threshold else "below_threshold"
        # wav_path from service -> raw_path in DB
        conn.execute(
            """
            INSERT INTO segments
                (id, project_id, source_id, raw_path, start_secs, end_secs,
                 speaker_label, match_confidence, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seg["id"], project_id, source_id, seg["wav_path"],
                seg["start_secs"], seg["end_secs"],
                seg["speaker_label"], seg["match_confidence"],
                seg_status, now, now,
            ),
        )

    # Update source coverage_ratio and status
    coverage = result.get("coverage_ratio", 0.0)
    conn.execute(
        """
        UPDATE sources
        SET status='complete', coverage_ratio=?, step2_error=NULL, updated_at=?
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
    """After all sources complete diarisation, auto-trigger transcription for
    pending segments with no transcript."""
    conn = get_conn(project_id)
    incomplete = conn.execute(
        "SELECT COUNT(*) FROM sources WHERE project_id=? AND status != 'complete'",
        (project_id,),
    ).fetchone()[0]
    if incomplete > 0:
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
# Transcription handlers (calls external service)
# ---------------------------------------------------------------------------


async def _handle_transcription_bulk(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Submit bulk transcription to external service. Writes results incrementally,
    deduplicating cumulative completed_segments."""
    from service_client import submit_job, poll_until_complete

    conn = get_conn(project_id)
    segment_ids = params.get("segment_ids", [])
    if not segment_ids:
        _fail_job(project_id, job_id, "no_segment_ids")
        _recompute_project_status(project_id)
        return

    project = conn.execute("SELECT whisper_model, language FROM projects WHERE id=?", (project_id,)).fetchone()

    # Build segment list with wav_paths
    data_prefix = _data_prefix()
    seg_list = []
    for sid in segment_ids:
        seg = conn.execute("SELECT id, raw_path FROM segments WHERE id=?", (sid,)).fetchone()
        if seg:
            seg_list.append({
                "id": seg["id"],
                "wav_path": f"{data_prefix}/projects/{project_id}/{seg['raw_path']}",
            })

    if not seg_list:
        _fail_job(project_id, job_id, "no_valid_segments")
        _recompute_project_status(project_id)
        return

    model = params.get("model") or project["whisper_model"]
    language = params.get("language", project["language"])

    payload = {
        "job_id": job_id,
        "segments": seg_list,
        "model": model,
        "language": language,
        "batch_size": 16,
    }

    try:
        await submit_job("transcription", payload)
    except Exception as exc:
        _fail_job(project_id, job_id, f"submit_failed: {exc}")
        _recompute_project_status(project_id)
        return

    # Track already-written segment IDs to deduplicate cumulative results
    written_ids: set[str] = set()

    def on_progress(result):
        progress = result.get("progress", 0)
        if progress:
            _update_progress(project_id, job_id, progress)

        # Write completed segments incrementally
        completed = result.get("completed_segments", [])
        now = _now()
        for cs in completed:
            seg_id = cs["id"]
            if seg_id in written_ids:
                continue
            written_ids.add(seg_id)
            conn.execute(
                """
                UPDATE segments
                SET transcript=?, transcript_confidence=?, updated_at=?
                WHERE id=? AND project_id=?
                """,
                (cs["transcript"], cs.get("transcript_confidence"), now, seg_id, project_id),
            )
            # Flag short segments
            seg_row = conn.execute("SELECT duration_secs FROM segments WHERE id=?", (seg_id,)).fetchone()
            if seg_row and seg_row["duration_secs"] is not None and seg_row["duration_secs"] < 2.0:
                _add_flag(conn, seg_id, "short_transcript")
        conn.commit()

    result = await poll_until_complete("transcription", job_id, on_progress=on_progress)

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
    from service_client import submit_job, poll_until_complete

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

    project = conn.execute("SELECT whisper_model, language FROM projects WHERE id=?", (project_id,)).fetchone()
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
    }

    try:
        await submit_job("transcription", payload)
    except Exception as exc:
        _fail_job(project_id, job_id, f"submit_failed: {exc}")
        _recompute_project_status(project_id)
        return

    result = await poll_until_complete("transcription", job_id)

    if result["status"] == "failed":
        _fail_job(project_id, job_id, result.get("error", "unknown_error"))
        _recompute_project_status(project_id)
        return

    # Write the result — overwrites transcript + confidence, preserves transcript_edited
    completed = result.get("completed_segments", [])
    now = _now()
    for cs in completed:
        if cs["id"] == seg_id:
            conn.execute(
                """
                UPDATE segments
                SET transcript=?, transcript_confidence=?, updated_at=?
                WHERE id=? AND project_id=?
                """,
                (cs["transcript"], cs.get("transcript_confidence"), now, seg_id, project_id),
            )
            break
    conn.commit()

    _complete_job(project_id, job_id)
    _recompute_project_status(project_id)


# ---------------------------------------------------------------------------
# Export handler (cleanup service + manifest + tar.gz)
# ---------------------------------------------------------------------------


async def _handle_export(
    project_id: str, job_id: str, source_id: str | None, params: dict
) -> None:
    """Export pipeline: submit cleanup for approved segments, write manifest, package archive."""
    from service_client import submit_job, poll_until_complete

    conn = get_conn(project_id)
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    # Get all approved segments
    approved = conn.execute(
        """
        SELECT seg.*, src.filename AS source_filename
        FROM segments seg
        JOIN sources src ON src.id = seg.source_id
        WHERE seg.project_id=? AND seg.status='approved'
        """,
        (project_id,),
    ).fetchall()

    if not approved:
        _fail_job(project_id, job_id, "no_approved_segments")
        _recompute_project_status(project_id)
        return

    pdir = project_dir(project_id)
    data_prefix = _data_prefix()
    export_dir = pdir / "export"
    export_dir.mkdir(exist_ok=True)

    # Build cleanup payload
    cleanup_segments = []
    for seg in approved:
        cleanup_segments.append({
            "id": seg["id"],
            "input_path": f"{data_prefix}/projects/{project_id}/{seg['raw_path']}",
            "output_path": f"{data_prefix}/projects/{project_id}/export/{seg['id']}.wav",
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
        await submit_job("cleanup", cleanup_payload)
    except Exception as exc:
        _fail_job(project_id, job_id, f"cleanup_submit_failed: {exc}")
        _recompute_project_status(project_id)
        return

    def on_progress(result):
        progress = result.get("progress", 0)
        if progress:
            _update_progress(project_id, job_id, progress)

    result = await poll_until_complete("cleanup", job_id, on_progress=on_progress)

    if result["status"] == "failed":
        _fail_job(project_id, job_id, result.get("error", "unknown_error"))
        _recompute_project_status(project_id)
        return

    # Process cleanup results per segment
    now = _now()
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
            # Clipping detected — set both the column AND the status
            conn.execute(
                """
                UPDATE segments
                SET status='clipping_warning', clipping_warning=1, updated_at=?
                WHERE id=?
                """,
                (now, seg_id),
            )

        else:
            # Success — record export_path
            conn.execute(
                "UPDATE segments SET export_path=?, updated_at=? WHERE id=?",
                (f"export/{seg_id}.wav", now, seg_id),
            )

    conn.commit()

    # Write manifest.json from DB
    _write_manifest(project_id, project, pdir)

    # Package tar.gz
    _package_archive(pdir)

    _complete_job(project_id, job_id)

    # Mark project as exported
    conn.execute(
        "UPDATE projects SET status='exported', updated_at=? WHERE id=?",
        (_now(), project_id),
    )
    conn.commit()


def _write_manifest(project_id: str, project: Any, pdir: Path) -> None:
    """Write export/manifest.json from the database."""
    conn = get_conn(project_id)

    # Get segments that were successfully cleaned (have export_path and are still approved)
    rows = conn.execute(
        """
        SELECT seg.*, src.filename AS source_filename
        FROM segments seg
        JOIN sources src ON src.id = seg.source_id
        WHERE seg.project_id=? AND seg.export_path IS NOT NULL
          AND seg.status IN ('approved', 'clipping_warning')
        """,
        (project_id,),
    ).fetchall()

    manifest_segments = []
    total_duration = 0.0
    for r in rows:
        transcript = r["transcript_edited"] if r["transcript_edited"] else r["transcript"]
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
        })

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

    manifest_path = pdir / "export" / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))


def _package_archive(pdir: Path) -> None:
    """Package export/ directory into export.tar.gz at the project root."""
    export_dir = pdir / "export"
    archive_path = pdir / "export.tar.gz"

    with tarfile.open(archive_path, "w:gz") as tar:
        for f in export_dir.iterdir():
            tar.add(str(f), arcname=f.name)


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
    "transcription_bulk": _handle_transcription_bulk,
    "transcription_segment": _handle_transcription_segment,
    "export": _handle_export,
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
