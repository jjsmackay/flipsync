"""Reference clip upload endpoint."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import aiofiles
from fastapi import APIRouter, HTTPException, UploadFile, File

from db import get_conn, project_dir, project_exists

router = APIRouter(prefix="/projects/{project_id}/reference", tags=["reference"])

CHUNK_SIZE = 1024 * 1024  # 1 MB
MIN_DURATION_SECS = 5.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_project(project_id: str):
    if not project_exists(project_id):
        raise HTTPException(
            404,
            detail={"error": "not_found", "message": "Project not found.", "detail": {}},
        )
    conn = get_conn(project_id)
    p = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
    if p is None:
        raise HTTPException(
            404,
            detail={"error": "not_found", "message": "Project not found.", "detail": {}},
        )
    return conn


def _get_duration(path: str) -> float:
    """Return audio duration in seconds.

    Tries ffprobe first (works for any format), falls back to Python's wave
    module for WAV files (works in test environments without ffprobe).
    """
    # Try ffprobe
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        val = float(result.stdout.strip())
        if val > 0:
            return val
    except Exception:
        pass

    # Fallback: Python's wave module (WAV only)
    try:
        import wave as _wave
        with _wave.open(path, "r") as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        return 0.0


@router.post("")
async def upload_reference(project_id: str, file: UploadFile = File(...)):
    conn = _require_project(project_id)

    pdir = project_dir(project_id)
    dest = pdir / "reference.wav"

    # Stream to disk
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(CHUNK_SIZE)
            if not chunk:
                break
            await out.write(chunk)

    duration = _get_duration(str(dest))

    if duration < MIN_DURATION_SECS:
        # Remove the file — it's invalid
        dest.unlink(missing_ok=True)
        raise HTTPException(
            422,
            detail={
                "error": "reference_too_short",
                "message": f"Reference clip must be at least {MIN_DURATION_SECS} seconds. Uploaded clip is {duration:.1f} seconds.",
                "detail": {"duration_secs": duration, "minimum_secs": MIN_DURATION_SECS},
            },
        )

    conn.execute(
        "UPDATE projects SET reference_path='reference.wav', updated_at=? WHERE id=?",
        (_now(), project_id),
    )
    conn.commit()

    return {"reference_path": "reference.wav", "duration_secs": duration}
