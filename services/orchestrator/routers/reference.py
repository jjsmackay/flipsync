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
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        val = float(stdout.decode().strip())
        if val > 0:
            return val
    except Exception:
        pass

    # Fallback: WAV header read, offloaded to a thread to stay non-blocking.
    return await asyncio.to_thread(_wave_duration, path)


@router.post("")
async def upload_reference(project_id: str, file: UploadFile = File(...)):
    conn = require_project(project_id)

    pdir = project_dir(project_id)
    dest = pdir / "reference.wav"
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

        duration = await _get_duration(str(tmp))

        if duration < MIN_DURATION_SECS:
            raise AppError(
                422, "reference_too_short",
                f"Reference clip must be at least {MIN_DURATION_SECS} seconds. Uploaded clip is {duration:.1f} seconds.",
                {"duration_secs": duration, "minimum_secs": MIN_DURATION_SECS},
            )

        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    conn.execute(
        "UPDATE projects SET reference_path='reference.wav', reference_origin=?, updated_at=? WHERE id=?",
        (json.dumps({"type": "uploaded"}), utc_now(), project_id),
    )
    conn.commit()

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


@router.get("/scout")
async def get_scout(project_id: str):
    conn = require_project(project_id)

    job = conn.execute(
        "SELECT * FROM jobs WHERE project_id=? AND type='scout_speakers' ORDER BY created_at DESC LIMIT 1",
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
        return {
            "status": "failed",
            "source_id": job["source_id"],
            "error": job["error"],
            "speakers": [],
        }

    # complete
    candidates = conn.execute(
        "SELECT * FROM speaker_candidates WHERE project_id=? ORDER BY total_secs DESC",
        (project_id,),
    ).fetchall()
    speakers = [
        {
            "speaker_label": c["speaker_label"],
            "total_secs": c["total_secs"],
            "segment_count": c["segment_count"],
            "sample_url": f"/projects/{project_id}/reference/scout/samples/{c['speaker_label']}",
        }
        for c in candidates
    ]
    return {"status": "complete", "source_id": job["source_id"], "speakers": speakers}


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

    pdir = project_dir(project_id)
    montage = pdir / cand["montage_path"]
    duration = await _get_duration(str(montage))
    if duration < MIN_DURATION_SECS:
        raise AppError(
            422, "reference_too_short",
            f"Reference must be at least {MIN_DURATION_SECS} seconds. Candidate montage is {duration:.1f} seconds.",
            {"duration_secs": duration, "minimum_secs": MIN_DURATION_SECS},
        )

    dest = pdir / "reference.wav"
    await asyncio.to_thread(shutil.copyfile, str(montage), str(dest))

    origin = json.dumps({
        "type": "diarise_pick",
        "source_id": cand["source_id"],
        "speaker_label": cand["speaker_label"],
    })
    conn.execute(
        "UPDATE projects SET reference_path='reference.wav', reference_origin=?, updated_at=? WHERE id=?",
        (origin, utc_now(), project_id),
    )
    conn.commit()
    recompute_project_status(project_id)

    return {"reference_path": "reference.wav", "duration_secs": duration}
