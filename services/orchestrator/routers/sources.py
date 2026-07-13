"""Source upload and management endpoints."""

import os
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import APIRouter, UploadFile, File
from pydantic import BaseModel

from db import project_dir, require_project, utc_now
from errors import AppError
from jobs import delete_source_segments, enqueue
from status import invalidate_export

router = APIRouter(prefix="/projects/{project_id}/sources", tags=["sources"])

CHUNK_SIZE = 1024 * 1024  # 1 MB streaming chunks


@router.post("", status_code=202)
async def upload_source(project_id: str, file: UploadFile = File(...)):
    conn = require_project(project_id)

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

    now = utc_now()
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

    # A new source makes any prior export stale (the dataset has changed).
    invalidate_export(project_id)

    return {"id": source_id, "filename": filename, "status": "extracting"}


class SourceDelete(BaseModel):
    confirm: bool = False


@router.delete("/{source_id}")
async def delete_source(project_id: str, source_id: str, body: SourceDelete):
    conn = require_project(project_id)

    source = conn.execute(
        "SELECT * FROM sources WHERE id=? AND project_id=?", (source_id, project_id)
    ).fetchone()
    if source is None:
        raise AppError(404, "not_found", "Source not found.")

    approved_count = conn.execute(
        "SELECT COUNT(*) FROM segments WHERE source_id=? AND status='approved'",
        (source_id,),
    ).fetchone()[0]

    if approved_count > 0 and not body.confirm:
        raise AppError(
            409, "has_approved_segments",
            f"Source has {approved_count} approved segments. Pass confirm=true to delete anyway.",
            {"approved_count": approved_count},
        )

    deleted_count = conn.execute(
        "SELECT COUNT(*) FROM segments WHERE source_id=?", (source_id,)
    ).fetchone()[0]

    # Delete segment rows and their WAVs (shared with the reprocess endpoint
    # and the diarisation handler so segment deletion never orphans WAVs).
    pdir = project_dir(project_id)
    delete_source_segments(conn, project_id, source_id)

    # Delete source file and derived audio files
    for path_col in ["file_path", "audio_path", "vocals_path"]:
        p = source[path_col]
        if p:
            f = pdir / p
            if f.exists():
                f.unlink()

    conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    conn.commit()

    # Removing a source changes the dataset — any prior export is now stale.
    invalidate_export(project_id)

    return {"deleted_segment_count": deleted_count, "deleted_approved_count": approved_count}
