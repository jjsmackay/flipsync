"""Reference clip endpoints: upload, and diarise + pick (scout)."""

import asyncio
import json
import os
import shutil
import uuid

import aiofiles
from fastapi import APIRouter, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel

from db import get_conn, project_dir, require_project, utc_now
from errors import AppError
from jobs import enqueue
from status import recompute_project_status

router = APIRouter(prefix="/projects/{project_id}/reference", tags=["reference"])

CHUNK_SIZE = 1024 * 1024  # 1 MB
MIN_DURATION_SECS = 5.0
_FFPROBE_TIMEOUT_SECS = 10.0


def _wave_duration(path: str) -> float:
    """Duration of a WAV file via the stdlib wave module (0.0 on failure)."""
    try:
        import wave as _wave
        with _wave.open(path, "r") as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        return 0.0


async def _get_duration(path: str) -> float:
    """Return audio duration in seconds without blocking the event loop.

    Tries ffprobe (any format) via an async subprocess, falls back to the wave
    module for WAV files (works in test environments without ffprobe).
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_FFPROBE_TIMEOUT_SECS)
        val = float(stdout.decode().strip())
        if val > 0:
            return val
    except Exception:
        # ffprobe missing, hung, or emitted junk. Kill a still-running process
        # before falling back — a timed-out wait_for leaves it alive otherwise.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

    # Fallback: WAV header read, offloaded to a thread to stay non-blocking.
    return await asyncio.to_thread(_wave_duration, path)


async def _finalise_reference(conn, project_id: str, origin: dict, staged_wav, *, keep_source: bool = False) -> float:
    """Duration-gate `staged_wav`, install it as reference.wav, record its
    provenance, and recompute project status (a project resting in
    awaiting_reference moves on once a reference exists).

    Shared by the upload and diarise+pick paths so the finalisation steps
    cannot drift. The gate runs before installation, so a too-short candidate
    never destroys an existing valid reference. Returns the duration.
    """
    duration = await _get_duration(str(staged_wav))
    if duration < MIN_DURATION_SECS:
        raise AppError(
            422, "reference_too_short",
            f"Reference clip must be at least {MIN_DURATION_SECS} seconds. Provided clip is {duration:.1f} seconds.",
            {"duration_secs": duration, "minimum_secs": MIN_DURATION_SECS},
        )

    dest = project_dir(project_id) / "reference.wav"
    if keep_source:
        # Candidate montages stay on disk so the user can re-pick.
        await asyncio.to_thread(shutil.copyfile, str(staged_wav), str(dest))
    else:
        os.replace(staged_wav, dest)

    conn.execute(
        "UPDATE projects SET reference_path='reference.wav', reference_origin=?, updated_at=? WHERE id=?",
        (json.dumps(origin), utc_now(), project_id),
    )
    conn.commit()
    recompute_project_status(project_id)
    return duration


@router.post("")
async def upload_reference(project_id: str, file: UploadFile = File(...)):
    conn = require_project(project_id)

    pdir = project_dir(project_id)
    # Write to a temp file first so a failed/too-short upload never destroys the
    # existing valid reference. Only an atomic rename replaces it.
    tmp = pdir / f".reference.{uuid.uuid4().hex}.tmp"

    try:
        async with aiofiles.open(tmp, "wb") as out:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                await out.write(chunk)

        duration = await _finalise_reference(conn, project_id, {"type": "uploaded"}, tmp)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    return {"reference_path": "reference.wav", "duration_secs": duration}


# ---------------------------------------------------------------------------
# Diarise + pick (scout)
# ---------------------------------------------------------------------------


class ScoutRequest(BaseModel):
    source_id: str


class SelectRequest(BaseModel):
    speaker_label: str


@router.post("/scout", status_code=202)
async def scout_speakers(project_id: str, body: ScoutRequest):
    conn = require_project(project_id)

    source = conn.execute(
        "SELECT id, vocals_path FROM sources WHERE id=? AND project_id=?",
        (body.source_id, project_id),
    ).fetchone()
    if source is None:
        raise AppError(404, "not_found", "Source not found.")
    if not source["vocals_path"]:
        raise AppError(
            422, "vocals_not_ready",
            "Source has no vocals stem yet; run step 1 first.",
        )

    job_id = enqueue(project_id, "scout_speakers", source_id=body.source_id)
    recompute_project_status(project_id)
    return {"job_id": job_id, "type": "scout_speakers"}


def _candidate_speakers(conn, project_id: str) -> list[dict]:
    """Serialise the project's current speaker_candidates rows."""
    candidates = conn.execute(
        "SELECT * FROM speaker_candidates WHERE project_id=? ORDER BY total_secs DESC",
        (project_id,),
    ).fetchall()
    return [
        {
            "speaker_label": c["speaker_label"],
            "total_secs": c["total_secs"],
            "segment_count": c["segment_count"],
            "sample_url": f"/projects/{project_id}/reference/scout/samples/{c['speaker_label']}",
        }
        for c in candidates
    ]


@router.get("/scout")
async def get_scout(project_id: str):
    conn = require_project(project_id)

    # created_at has second resolution; rowid breaks ties deterministically in
    # favour of the most recently inserted job.
    job = conn.execute(
        "SELECT * FROM jobs WHERE project_id=? AND type='scout_speakers' ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if job is None:
        raise AppError(404, "no_scout", "No scout has been run for this project.")

    if job["status"] in ("queued", "running"):
        return {
            "status": "running",
            "progress": job["progress"] or 0,
            "source_id": job["source_id"],
            "speakers": [],
        }

    if job["status"] in ("failed", "cancelled"):
        # A failed re-scan must not hide candidates from an earlier successful
        # scan — they are still pickable, so return them alongside the failure.
        return {
            "status": "failed",
            "source_id": job["source_id"],
            "error": job["error"],
            "speakers": _candidate_speakers(conn, project_id),
        }

    # complete
    return {"status": "complete", "source_id": job["source_id"], "speakers": _candidate_speakers(conn, project_id)}


@router.get("/scout/samples/{speaker_label}")
async def get_scout_sample(project_id: str, speaker_label: str):
    conn = require_project(project_id)

    cand = conn.execute(
        "SELECT montage_path FROM speaker_candidates WHERE project_id=? AND speaker_label=?",
        (project_id, speaker_label),
    ).fetchone()
    if cand is None:
        raise AppError(404, "unknown_speaker", "Speaker not in the current candidate set.")

    wav = project_dir(project_id) / cand["montage_path"]
    if not wav.exists():
        raise AppError(404, "audio_not_found", "Montage WAV not found.")

    return FileResponse(str(wav), media_type="audio/wav")


@router.post("/scout/select")
async def select_speaker(project_id: str, body: SelectRequest):
    conn = require_project(project_id)

    cand = conn.execute(
        "SELECT * FROM speaker_candidates WHERE project_id=? AND speaker_label=?",
        (project_id, body.speaker_label),
    ).fetchone()
    if cand is None:
        raise AppError(404, "unknown_speaker", "Speaker not in the current candidate set.")

    montage = project_dir(project_id) / cand["montage_path"]
    origin = {
        "type": "diarise_pick",
        "source_id": cand["source_id"],
        "speaker_label": cand["speaker_label"],
    }
    duration = await _finalise_reference(conn, project_id, origin, montage, keep_source=True)

    return {"reference_path": "reference.wav", "duration_secs": duration}
