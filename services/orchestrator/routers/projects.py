"""Project CRUD endpoints."""

import json
import shutil
import uuid
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from db import create_project_db, get_conn, list_project_ids, close_conn, project_dir, project_exists, utc_now
from errors import AppError
from state_machines import validate_segment_transition

router = APIRouter(prefix="/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_stats(project_id: str) -> dict:
    conn = get_conn(project_id)
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_segments,
            SUM(status='approved') AS approved_count,
            SUM(CASE WHEN status='approved' THEN duration_secs ELSE 0 END) AS approved_duration_secs,
            SUM(status='pending') AS pending_count,
            SUM(status='maybe') AS maybe_count,
            SUM(status='rejected') AS rejected_count,
            SUM(status='below_threshold') AS below_threshold_count
        FROM segments WHERE project_id=?
        """,
        (project_id,),
    ).fetchone()

    sources = conn.execute(
        "SELECT id, filename, status, coverage_ratio, step1_error, step2_error FROM sources WHERE project_id=?",
        (project_id,),
    ).fetchall()

    source_coverage = []
    for s in sources:
        ratio = s["coverage_ratio"] or 0.0
        err = s["step1_error"] or s["step2_error"]
        source_coverage.append({
            "source_id": s["id"],
            "filename": s["filename"],
            "status": s["status"],
            "coverage_ratio": ratio,
            "low_coverage_warning": s["coverage_ratio"] is not None and ratio < 0.15,
            "error": err,
        })

    return {
        "total_segments": row["total_segments"] or 0,
        "approved_count": row["approved_count"] or 0,
        "approved_duration_secs": row["approved_duration_secs"] or 0.0,
        "pending_count": row["pending_count"] or 0,
        "maybe_count": row["maybe_count"] or 0,
        "rejected_count": row["rejected_count"] or 0,
        "below_threshold_count": row["below_threshold_count"] or 0,
        "source_coverage": source_coverage,
    }


def _active_jobs(project_id: str) -> list[dict]:
    conn = get_conn(project_id)
    rows = conn.execute(
        "SELECT id, type, status, progress FROM jobs WHERE project_id=? AND status IN ('queued','running')",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _recent_failed_jobs(project_id: str) -> list[dict]:
    conn = get_conn(project_id)
    rows = conn.execute(
        """
        SELECT id, type, source_id, error, completed_at
        FROM jobs WHERE project_id=? AND status='failed'
        ORDER BY completed_at DESC LIMIT 10
        """,
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _project_detail(project_id: str) -> dict:
    if not project_exists(project_id):
        return None
    conn = get_conn(project_id)
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if p is None:
        return None
    return {
        "id": p["id"],
        "name": p["name"],
        "status": p["status"],
        "created_at": p["created_at"],
        "updated_at": p["updated_at"],
        "target_duration_secs": p["target_duration_secs"],
        "config": {
            "whisper_model": p["whisper_model"],
            "language": p["language"],
            "match_threshold": p["match_threshold"],
            "target_duration_secs": p["target_duration_secs"],
        },
        "stats": _project_stats(project_id),
        "active_jobs": _active_jobs(project_id),
        "recent_failed_jobs": _recent_failed_jobs(project_id),
    }


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str
    whisper_model: str = "large-v2"
    language: Optional[str] = None
    match_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    target_duration_secs: float = Field(default=1800.0, gt=0)


class ProjectPatch(BaseModel):
    name: Optional[str] = None
    match_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    target_duration_secs: Optional[float] = Field(default=None, gt=0)
    whisper_model: Optional[str] = None
    language: Optional[str] = None


class ProjectDelete(BaseModel):
    confirm: bool = False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_projects():
    projects = []
    for pid in list_project_ids():
        conn = get_conn(pid)
        p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if p is None:
            continue
        stats_row = conn.execute(
            """
            SELECT
                SUM(status='approved') AS approved_count,
                SUM(CASE WHEN status='approved' THEN duration_secs ELSE 0 END) AS approved_duration_secs,
                SUM(status='pending') AS pending_count
            FROM segments WHERE project_id=?
            """,
            (pid,),
        ).fetchone()
        projects.append({
            "id": p["id"],
            "name": p["name"],
            "status": p["status"],
            "created_at": p["created_at"],
            "updated_at": p["updated_at"],
            "target_duration_secs": p["target_duration_secs"],
            "stats": {
                "approved_count": stats_row["approved_count"] or 0,
                "approved_duration_secs": stats_row["approved_duration_secs"] or 0.0,
                "pending_count": stats_row["pending_count"] or 0,
            },
        })
    return {"projects": projects}


@router.post("", status_code=201)
async def create_project(body: ProjectCreate):
    project_id = str(uuid.uuid4())
    now = utc_now()
    create_project_db(project_id)
    conn = get_conn(project_id)
    conn.execute(
        """
        INSERT INTO projects (id, name, created_at, updated_at, status,
            whisper_model, language, match_threshold, target_duration_secs)
        VALUES (?, ?, ?, ?, 'new', ?, ?, ?, ?)
        """,
        (project_id, body.name, now, now, body.whisper_model,
         body.language, body.match_threshold, body.target_duration_secs),
    )
    conn.commit()
    return {"id": project_id, "name": body.name, "status": "new"}


@router.get("/{project_id}")
async def get_project(project_id: str):
    detail = _project_detail(project_id)
    if detail is None:
        raise AppError(404, "not_found", "Project not found.")
    return detail


@router.patch("/{project_id}")
async def patch_project(project_id: str, body: ProjectPatch):
    if not project_exists(project_id):
        raise AppError(404, "not_found", "Project not found.")
    conn = get_conn(project_id)
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if p is None:
        raise AppError(404, "not_found", "Project not found.")

    provided = body.model_fields_set
    updates: dict = {}
    if "name" in provided and body.name is not None:
        updates["name"] = body.name
    if "whisper_model" in provided and body.whisper_model is not None:
        updates["whisper_model"] = body.whisper_model
    if "language" in provided:
        updates["language"] = body.language  # allows setting to None (auto-detect)
    if "target_duration_secs" in provided and body.target_duration_secs is not None:
        updates["target_duration_secs"] = body.target_duration_secs

    old_threshold = p["match_threshold"]
    new_threshold = body.match_threshold if "match_threshold" in provided else None

    if new_threshold is not None:
        updates["match_threshold"] = new_threshold
        # Bidirectional threshold re-evaluation (synchronous, inline SQL)
        if new_threshold != old_threshold:
            conn.execute(
                """
                UPDATE segments SET status='pending', updated_at=?
                WHERE project_id=? AND status='below_threshold'
                  AND match_confidence >= ?
                """,
                (utc_now(), project_id, new_threshold),
            )
            conn.execute(
                """
                UPDATE segments SET status='below_threshold', updated_at=?
                WHERE project_id=? AND status='pending'
                  AND match_confidence < ?
                """,
                (utc_now(), project_id, new_threshold),
            )

    if updates:
        updates["updated_at"] = utc_now()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE projects SET {set_clause} WHERE id=?",
            (*updates.values(), project_id),
        )
        conn.commit()

    return _project_detail(project_id)


@router.delete("/{project_id}")
async def delete_project(project_id: str, body: ProjectDelete):
    if not body.confirm:
        raise AppError(422, "confirm_required", "Pass confirm=true to delete.")

    if not project_exists(project_id):
        raise AppError(404, "not_found", "Project not found.")
    conn = get_conn(project_id)
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if p is None:
        raise AppError(404, "not_found", "Project not found.")

    # Reject if jobs are actively running
    active = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE project_id=? AND status IN ('queued','running')",
        (project_id,),
    ).fetchone()[0]
    if active > 0:
        raise AppError(
            409, "jobs_active",
            "Cannot delete project while jobs are running. Cancel or wait for completion.",
        )

    pdir = project_dir(project_id)
    close_conn(project_id)
    if pdir.exists():
        shutil.rmtree(pdir)

    return {"deleted": True}
