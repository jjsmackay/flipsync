"""Project-level source file upload and deletion endpoints.

This module handles uploading source files to projects and deleting source files.
Each source is tracked in the database with its file path and processing status.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, UploadFile, File, HTTPException
from fastapi.responses import FileResponse

from db import create_source_db, get_conn, project_exists, project_dir, close_conn
from errors import AppError
from state_machines import validate_source_transition

router = APIRouter(prefix="/projects/{project_id}/sources", tags=["sources"])


def _now() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_project(project_id: str):
    """Validate project exists and return connection."""
    if not project_exists(project_id):
        raise AppError(404, "not_found", "Project not found.")
    conn = get_conn(project_id)
    p = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
    if p is None:
        raise AppError(404, "not_found", "Project not found.")
    return conn


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SourceCreate(BaseModel):
    """Schema for creating a source with a name only; file uploaded separately.

    The file must be uploaded to a file path matching the source name.
    """
    name: str
    """Name of the source file. The actual file must be uploaded separately
    to {project_dir}/{name} after creating the source record."""

    description: Optional[str] = None
    """Optional description for the source file."""


class SourceDelete(BaseModel):
    """Schema for deleting a source file."""
    confirm: bool = False
    """Must be True to confirm deletion of the source and its file."""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/upload")
async def upload_source(project_id: str, file: UploadFile = File(...), name: str = Query(...),
                        description: Optional[str] = None):
    """Upload a source file to a project.

    Upload the file to {project_dir}/{name} and create a source record.
    """
    conn = _require_project(project_id)
    pdir = project_dir(project_id)

    source_path = pdir / name
    if source_path.exists():
        existing = conn.execute(
            "SELECT id, name, status FROM sources WHERE project_id=? AND name=?",
            (project_id, name),
        ).fetchone()
        if existing:
            # File exists and we have a record
            raise AppError(
                409, "source_file_exists",
                f"Source file '{name}' already exists in the project.",
            )
        else:
            # File exists but no record - orphaned file
            raise AppError(
                409, "orphaned_source_file",
                f"Source file '{name}' exists without a record.",
            )

    # Store file
    if not file.filename:
        raise AppError(422, "invalid_file", "No file provided.")
    if not file.filename.endswith(('.wav', '.flac', '.mp3', '.m4a', '.ogg')):
        raise AppError(400, "invalid_file_type", f"Unsupported file type: {file.filename}")

    try:
        source_path.write_bytes(file.file.read())
    except Exception as e:
        close_conn(project_id)
        raise AppError(500, "file_write_failed", f"Failed to write file: {e}")

    source_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO sources (id, project_id, name, description, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'uploaded', ?, ?)
        """,
        (source_id, project_id, name, description, _now(), _now()),
    )
    conn.commit()

    return conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()


@router.post("")
async def create_source(project_id: str, body: SourceCreate):
    """Create a new source record for a project.

    The source file must be uploaded separately to the project directory
    using the source name as the filename. This endpoint creates the
    database record and tracks the file for processing.

    After creation, use a separate file upload mechanism to store the
    actual file content at {project_dir}/{body.name}.
    """
    conn = _require_project(project_id)
    pdir = project_dir(project_id)

    # Check if source file already exists at expected path
    source_path = pdir / body.name
    if source_path.exists():
        existing = conn.execute(
            "SELECT id, name, status FROM sources WHERE project_id=? AND name=?",
            (project_id, body.name),
        ).fetchone()
        if existing:
            # File exists and we have a record - status might be stale
            # Update to processing
            conn.execute(
                "UPDATE sources SET status='processing', updated_at=? WHERE id=?",
                (_now(), existing["id"]),
            )
            conn.commit()
        else:
            # File exists but no record - this is an orphaned file
            raise AppError(
                409, "source_file_exists",
                f"Source file '{body.name}' already exists in the project.",
            )

    # Create source record with 'uploaded' status
    source_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO sources (id, project_id, name, description, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'uploaded', ?, ?)
        """,
        (source_id, project_id, body.name, body.description, _now(), _now()),
    )
    conn.commit()

    # Verify file doesn't exist at upload time (it may have been created between checks)
    if source_path.exists():
        existing = conn.execute(
            "SELECT id, name, status FROM sources WHERE id=?",
            (source_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE sources SET status='processing', updated_at=? WHERE id=?",
                (_now(), source_id),
            )
            conn.commit()

    source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    return dict(source)


@router.get("/{source_id}")
async def get_source(project_id: str, source_id: str):
    """Get details of a source file."""
    conn = _require_project(project_id)
    s = conn.execute(
        "SELECT * FROM sources WHERE id=? AND project_id=?",
        (source_id, project_id),
    ).fetchone()
    if s is None:
        raise AppError(404, "not_found", "Source not found.")

    pdir = project_dir(project_id)
    source_path = pdir / s["name"]
    file_size = source_path.stat().st_size if source_path.exists() else None

    return {
        "id": s["id"],
        "name": s["name"],
        "description": s["description"],
        "status": s["status"],
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "file_path": str(source_path),
        "file_size": file_size,
    }


class SourcePatch(BaseModel):
    """Schema for updating a source."""
    status: Optional[str] = None
    description: Optional[str] = None


@router.patch("/{source_id}")
async def patch_source(project_id: str, source_id: str, body: SourcePatch):
    """Update a source record.

    Only status and description fields can be updated. Status transitions
    must follow the state machine rules.
    """
    conn = _require_project(project_id)
    s = conn.execute(
        "SELECT * FROM sources WHERE id=? AND project_id=?",
        (source_id, project_id),
    ).fetchone()
    if s is None:
        raise AppError(404, "not_found", "Source not found.")

    updates: dict = {}

    if body.status is not None:
        if not validate_source_transition(s["status"], body.status):
            raise AppError(
                409, "invalid_transition",
                f"Cannot transition from '{s['status']}' to '{body.status}'.",
                {"from": s["status"], "to": body.status},
            )
        updates["status"] = body.status

    if body.description is not None:
        updates["description"] = body.description

    if updates:
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE sources SET {set_clause} WHERE id=?",
            (*updates.values(), source_id),
        )
        conn.commit()

    return conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()


@router.delete("/{source_id}")
async def delete_source(project_id: str, source_id: str, body: SourceDelete):
    """Delete a source file and its record.

    This operation is only allowed when the source is in 'processing' or 'deleted' status.
    If the source is actively being processed ('processing'), deletion is rejected.

    After deletion, the source file may be removed from the filesystem if the caller
    explicitly requests it.
    """
    if not body.confirm:
        raise AppError(422, "confirm_required", "Pass confirm=true to delete.")

    conn = _require_project(project_id)
    s = conn.execute(
        "SELECT * FROM sources WHERE id=? AND project_id=?",
        (source_id, project_id),
    ).fetchone()
    if s is None:
        raise AppError(404, "not_found", "Source not found.")

    # Reject deletion if actively processing
    if s["status"] == "processing":
        raise AppError(
            409, "source_processing",
            f"Cannot delete source '{s['name']}' while it is being processed.",
        )

    # Reject deletion if source was deleted but file still exists
    # (file should be cleaned up separately)
    if s["status"] == "deleted":
        raise AppError(
            409, "source_already_deleted",
            f"Source '{s['name']}' is already marked as deleted.",
        )

    pdir = project_dir(project_id)
    source_path = pdir / s["name"]

    # Remove file from filesystem if it exists
    if source_path.exists():
        close_conn(project_id)
        try:
            source_path.unlink()
        except Exception:
            pass  # Ignore if file can't be removed

    # Delete database record
    conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    conn.commit()

    return {"deleted": True, "source_id": source_id}
