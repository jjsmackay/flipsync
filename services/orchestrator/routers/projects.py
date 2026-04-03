"""Project CRUD endpoints."""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from db import create_project_db, get_conn, list_project_ids, close_conn, project_dir, project_exists
from state_machines import validate_segment_transition
import shutil

router = APIRouter(prefix="/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
            "low_coverage_warning": ratio > 0 and ratio < 0.15,
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
    now = _now()
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
        raise HTTPException(404, detail={"error": "not_found", "message": "Project not found.", "detail": {}})
    return detail


@router.patch("/{project_id}")
async def patch_project(project_id: str, body: ProjectPatch):
    if not project_exists(project_id):
        raise HTTPException(404, detail={"error": "not_found", "message": "Project not found.", "detail": {}})
    conn = get_conn(project_id)
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if p is None:
        raise HTTPException(404, detail={"error": "not_found", "message": "Project not found.", "detail": {}})

    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.whisper_model is not None:
        updates["whisper_model"] = body.whisper_model
    if body.language is not None:
        updates["language"] = body.language
    if body.target_duration_secs is not None:
        updates["target_duration_secs"] = body.target_duration_secs

    old_threshold = p["match_threshold"]
    new_threshold = body.match_threshold

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
                (_now(), project_id, new_threshold),
            )
            conn.execute(
                """
                UPDATE segments SET status='below_threshold', updated_at=?
                WHERE project_id=? AND status='pending'
                  AND match_confidence < ?
                """,
                (_now(), project_id, new_threshold),
            )

    if updates:
        updates["updated_at"] = _now()
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
        raise HTTPException(
            422,
            detail={"error": "confirm_required", "message": "Pass confirm=true to delete.", "detail": {}},
        )

    if not project_exists(project_id):
        raise HTTPException(404, detail={"error": "not_found", "message": "Project not found.", "detail": {}})
    conn = get_conn(project_id)
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if p is None:
        raise HTTPException(404, detail={"error": "not_found", "message": "Project not found.", "detail": {}})

    # Reject if jobs are actively running
    active = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE project_id=? AND status IN ('queued','running')",
        (project_id,),
    ).fetchone()[0]
    if active > 0:
        raise HTTPException(
            409,
            detail={
                "error": "jobs_active",
                "message": "Cannot delete project while jobs are running. Cancel or wait for completion.",
                "detail": {},
            },
        )

    pdir = project_dir(project_id)
    close_conn(project_id)
    if pdir.exists():
        shutil.rmtree(pdir)

    return {"deleted": True}
