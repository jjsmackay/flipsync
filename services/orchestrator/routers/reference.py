"""Reference clip upload endpoint."""

import asyncio
import os
import uuid

import aiofiles
from fastapi import APIRouter, UploadFile, File

from db import project_dir, require_project, utc_now
from errors import AppError

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
        "UPDATE projects SET reference_path='reference.wav', updated_at=? WHERE id=?",
        (utc_now(), project_id),
    )
    conn.commit()

    return {"reference_path": "reference.wav", "duration_secs": duration}
