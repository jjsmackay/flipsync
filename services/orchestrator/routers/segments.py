"""Segments API — list, audio streaming, review actions, bulk operations.

This is Wave 4 scope but included in Wave 1 to allow endpoint tests.
"""

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from db import get_conn, project_dir, project_exists
from state_machines import validate_segment_transition, BULK_ACTION_SOURCES

router = APIRouter(prefix="/projects/{project_id}/segments", tags=["segments"])


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


VALID_SORT_FIELDS = {"match_confidence", "duration_secs", "start_secs", "transcript_confidence"}
VALID_ORDERS = {"asc", "desc"}


@router.get("")
async def list_segments(
    project_id: str,
    status: str = "pending,maybe",
    source_id: Optional[str] = None,
    min_confidence: Optional[float] = None,
    max_confidence: Optional[float] = None,
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
    sort: str = "match_confidence",
    order: str = "desc",
    page: int = 1,
    per_page: int = 50,
    count_only: bool = False,
):
    conn = _require_project(project_id)

    if sort not in VALID_SORT_FIELDS:
        raise HTTPException(
            422,
            detail={"error": "invalid_sort", "message": f"sort must be one of {sorted(VALID_SORT_FIELDS)}.", "detail": {}},
        )
    if order not in VALID_ORDERS:
        raise HTTPException(
            422,
            detail={"error": "invalid_order", "message": "order must be 'asc' or 'desc'.", "detail": {}},
        )
    per_page = min(per_page, 200)
    page = max(page, 1)

    statuses = [s.strip() for s in status.split(",") if s.strip()]
    placeholders = ",".join("?" * len(statuses))

    conditions = [f"seg.project_id=?", f"seg.status IN ({placeholders})"]
    params: list = [project_id, *statuses]

    if source_id:
        conditions.append("seg.source_id=?")
        params.append(source_id)
    if min_confidence is not None:
        conditions.append("seg.match_confidence>=?")
        params.append(min_confidence)
    if max_confidence is not None:
        conditions.append("seg.match_confidence<=?")
        params.append(max_confidence)
    if min_duration is not None:
        conditions.append("seg.duration_secs>=?")
        params.append(min_duration)
    if max_duration is not None:
        conditions.append("seg.duration_secs<=?")
        params.append(max_duration)

    where = " AND ".join(conditions)

    if count_only:
        total = conn.execute(
            f"SELECT COUNT(*) FROM segments seg WHERE {where}", params
        ).fetchone()[0]
        return {"total": total}

    total = conn.execute(
        f"SELECT COUNT(*) FROM segments seg WHERE {where}", params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    rows = conn.execute(
        f"""
        SELECT seg.*, src.filename AS source_filename
        FROM segments seg
        JOIN sources src ON src.id = seg.source_id
        WHERE {where}
        ORDER BY seg.{sort} {order.upper()}
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, offset],
    ).fetchall()

    segments_out = []
    for r in rows:
        segments_out.append({
            "id": r["id"],
            "source_id": r["source_id"],
            "source_filename": r["source_filename"],
            "start_secs": r["start_secs"],
            "end_secs": r["end_secs"],
            "duration_secs": r["duration_secs"],
            "match_confidence": r["match_confidence"],
            "transcript": r["transcript"],
            "transcript_edited": r["transcript_edited"],
            "transcript_confidence": r["transcript_confidence"],
            "status": r["status"],
            "clipping_warning": bool(r["clipping_warning"]),
            "audio_url": f"/projects/{project_id}/segments/{r['id']}/audio",
        })

    return {
        "segments": segments_out,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": max(1, (total + per_page - 1) // per_page),
        },
    }


@router.get("/{segment_id}/audio")
async def get_segment_audio(project_id: str, segment_id: str):
    conn = _require_project(project_id)
    seg = conn.execute(
        "SELECT raw_path FROM segments WHERE id=? AND project_id=?", (segment_id, project_id)
    ).fetchone()
    if seg is None:
        raise HTTPException(
            404,
            detail={"error": "not_found", "message": "Segment not found.", "detail": {}},
        )

    wav = project_dir(project_id) / seg["raw_path"]
    if not wav.exists():
        raise HTTPException(
            404,
            detail={"error": "audio_not_found", "message": "WAV file not yet written.", "detail": {}},
        )

    return FileResponse(str(wav), media_type="audio/wav")


class SegmentPatch(BaseModel):
    status: Optional[str] = None
    transcript_edited: Optional[str] = None


@router.patch("/{segment_id}")
async def patch_segment(project_id: str, segment_id: str, body: SegmentPatch):
    conn = _require_project(project_id)
    seg = conn.execute(
        "SELECT * FROM segments WHERE id=? AND project_id=?", (segment_id, project_id)
    ).fetchone()
    if seg is None:
        raise HTTPException(
            404,
            detail={"error": "not_found", "message": "Segment not found.", "detail": {}},
        )

    updates: dict = {}

    if body.status is not None:
        if not validate_segment_transition(seg["status"], body.status):
            raise HTTPException(
                409,
                detail={
                    "error": "invalid_transition",
                    "message": f"Cannot transition from '{seg['status']}' to '{body.status}'.",
                    "detail": {"from": seg["status"], "to": body.status},
                },
            )
        updates["status"] = body.status

    if body.transcript_edited is not None:
        updates["transcript_edited"] = body.transcript_edited

    if updates:
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE segments SET {set_clause} WHERE id=?",
            (*updates.values(), segment_id),
        )
        conn.commit()

    updated = conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
    return dict(updated)


class BulkFilter(BaseModel):
    status: Optional[str] = None
    source_id: Optional[str] = None
    min_confidence: Optional[float] = None
    max_confidence: Optional[float] = None
    min_duration: Optional[float] = None
    max_duration: Optional[float] = None


class BulkRequest(BaseModel):
    action: str
    filter: BulkFilter = BulkFilter()


BULK_ACTION_TARGET: dict[str, str] = {
    "approve": "approved",
    "reject": "rejected",
    "maybe": "maybe",
    "pending": "pending",
}


@router.post("/bulk")
async def bulk_action(project_id: str, body: BulkRequest):
    conn = _require_project(project_id)

    if body.action not in BULK_ACTION_TARGET:
        raise HTTPException(
            422,
            detail={
                "error": "invalid_action",
                "message": "action must be one of: approve, reject, maybe, pending.",
                "detail": {},
            },
        )

    to_status = BULK_ACTION_TARGET[body.action]
    allowed_sources = BULK_ACTION_SOURCES[body.action]

    placeholders = ",".join("?" * len(allowed_sources))
    conditions = [f"project_id=?", f"status IN ({placeholders})"]
    params: list = [project_id, *allowed_sources]

    f = body.filter
    if f.status:
        # Intersect filter status with allowed sources
        filter_statuses = {s.strip() for s in f.status.split(",")}
        intersected = allowed_sources & filter_statuses
        if not intersected:
            return {"affected_count": 0}
        placeholders2 = ",".join("?" * len(intersected))
        conditions.append(f"status IN ({placeholders2})")
        params.extend(intersected)
    if f.source_id:
        conditions.append("source_id=?")
        params.append(f.source_id)
    if f.min_confidence is not None:
        conditions.append("match_confidence>=?")
        params.append(f.min_confidence)
    if f.max_confidence is not None:
        conditions.append("match_confidence<=?")
        params.append(f.max_confidence)
    if f.min_duration is not None:
        conditions.append("duration_secs>=?")
        params.append(f.min_duration)
    if f.max_duration is not None:
        conditions.append("duration_secs<=?")
        params.append(f.max_duration)

    where = " AND ".join(conditions)
    now = _now()
    result = conn.execute(
        f"UPDATE segments SET status=?, updated_at=? WHERE {where}",
        [to_status, now, *params],
    )
    conn.commit()

    return {"affected_count": result.rowcount}
