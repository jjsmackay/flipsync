"""Source upload and management endpoints."""

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from pydantic import BaseModel

from db import get_conn, project_dir, project_exists
from jobs import enqueue

router = APIRouter(prefix="/projects/{project_id}/sources", tags=["sources"])

CHUNK_SIZE = 1024 * 1024  # 1 MB streaming chunks


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


@router.post("", status_code=202)
async def upload_source(project_id: str, file: UploadFile = File(...)):
    conn = _require_project(project_id)

    source_id = str(uuid.uuid4())
    filename = file.filename or "upload"
    ext = Path(filename).suffix or ".bin"
    relative_path = f"source/{source_id}{ext}"
    dest = project_dir(project_id) / relative_path

    # Stream to disk without buffering in memory
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(CHUNK_SIZE)
            if not chunk:
                break
            await out.write(chunk)

    now = _now()
    conn.execute(
        """
        INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'uploaded', ?, ?)
        """,
        (source_id, project_id, filename, relative_path, now, now),
    )
    # First source upload → project moves from 'new' to 'ready'
    conn.execute(
        "UPDATE projects SET status=CASE WHEN status='new' THEN 'ready' ELSE status END, updated_at=? WHERE id=?",
        (now, project_id),
    )
    conn.commit()

    # Enqueue extraction immediately
    enqueue(project_id, "extract_audio", source_id=source_id)

    return {"id": source_id, "filename": filename, "status": "extracting"}


class SourceDelete(BaseModel):
    confirm: bool = False


@router.delete("/{source_id}")
async def delete_source(project_id: str, source_id: str, body: SourceDelete):
    conn = _require_project(project_id)

    source = conn.execute(
        "SELECT * FROM sources WHERE id=? AND project_id=?", (source_id, project_id)
    ).fetchone()
    if source is None:
        raise HTTPException(
            404,
            detail={"error": "not_found", "message": "Source not found.", "detail": {}},
        )

    approved_count = conn.execute(
        "SELECT COUNT(*) FROM segments WHERE source_id=? AND status='approved'",
        (source_id,),
    ).fetchone()[0]

    if approved_count > 0 and not body.confirm:
        raise HTTPException(
            409,
            detail={
                "error": "has_approved_segments",
                "message": f"Source has {approved_count} approved segments. Pass confirm=true to delete anyway.",
                "detail": {"approved_count": approved_count},
            },
        )

    deleted_count = conn.execute(
        "SELECT COUNT(*) FROM segments WHERE source_id=?", (source_id,)
    ).fetchone()[0]

    # Delete segment WAV files
    pdir = project_dir(project_id)
    segments = conn.execute("SELECT raw_path, export_path FROM segments WHERE source_id=?", (source_id,)).fetchall()
    for seg in segments:
        for p in [seg["raw_path"], seg["export_path"]]:
            if p:
                f = pdir / p
                if f.exists():
                    f.unlink()

    # Delete source file
    if source["file_path"]:
        f = pdir / source["file_path"]
        if f.exists():
            f.unlink()

    conn.execute("DELETE FROM segments WHERE source_id=?", (source_id,))
    conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    conn.commit()

    return {"deleted_segment_count": deleted_count, "deleted_approved_count": approved_count}
