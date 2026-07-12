"""Segments API — list, audio streaming, review actions, bulk operations.

This is Wave 4 scope but included in Wave 1 to allow endpoint tests.
"""

import json
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from db import project_dir, require_project, utc_now
from errors import AppError
from state_machines import validate_segment_transition, BULK_ACTION_SOURCES
from status import invalidate_export

router = APIRouter(prefix="/projects/{project_id}/segments", tags=["segments"])


def _serialize_segment(row, project_id: str) -> dict:
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "source_filename": row["source_filename"],
        "start_secs": row["start_secs"],
        "end_secs": row["end_secs"],
        "duration_secs": row["duration_secs"],
        "match_confidence": row["match_confidence"],
        "transcript": row["transcript"],
        "transcript_edited": row["transcript_edited"],
        "transcript_confidence": row["transcript_confidence"],
        "status": row["status"],
        "clipping_warning": bool(row["clipping_warning"]),
        "flags": json.loads(row["flags"]) if row["flags"] else [],
        "audio_url": f"/projects/{project_id}/segments/{row['id']}/audio",
    }


# Maps accepted `sort` values (spec + column aliases) to the actual column name.
SORT_FIELD_MAP = {
    "match_confidence": "match_confidence",
    "duration": "duration_secs",
    "duration_secs": "duration_secs",
    "start_secs": "start_secs",
    "transcript_confidence": "transcript_confidence",
}
VALID_ORDERS = {"asc", "desc"}

# Default status filter, shared by GET /segments and POST /segments/bulk.
DEFAULT_STATUS_FILTER = ("pending", "maybe")


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
    conn = require_project(project_id)

    if sort not in SORT_FIELD_MAP:
        raise AppError(422, "invalid_sort", f"sort must be one of {sorted(SORT_FIELD_MAP)}.")
    if order not in VALID_ORDERS:
        raise AppError(422, "invalid_order", "order must be 'asc' or 'desc'.")
    sort_col = SORT_FIELD_MAP[sort]
    per_page = min(per_page, 200)
    page = max(page, 1)

    statuses = [s.strip() for s in status.split(",") if s.strip()]
    if not statuses:
        # An explicit empty ?status= filters to no statuses — return an empty
        # result set rather than building invalid `IN ()` SQL.
        if count_only:
            return {"total": 0}
        return {
            "segments": [],
            "pagination": {"page": page, "per_page": per_page, "total": 0, "pages": 1},
        }
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
        ORDER BY seg.{sort_col} {order.upper()}
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, offset],
    ).fetchall()

    segments_out = [_serialize_segment(r, project_id) for r in rows]

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
    conn = require_project(project_id)
    seg = conn.execute(
        "SELECT raw_path FROM segments WHERE id=? AND project_id=?", (segment_id, project_id)
    ).fetchone()
    if seg is None:
        raise AppError(404, "not_found", "Segment not found.")

    wav = project_dir(project_id) / seg["raw_path"]
    if not wav.exists():
        raise AppError(404, "audio_not_found", "WAV file not yet written.")

    return FileResponse(str(wav), media_type="audio/wav")


class SegmentPatch(BaseModel):
    status: Optional[str] = None
    transcript_edited: Optional[str] = None


@router.patch("/{segment_id}")
async def patch_segment(project_id: str, segment_id: str, body: SegmentPatch):
    conn = require_project(project_id)
    seg = conn.execute(
        """
        SELECT seg.*, src.filename AS source_filename
        FROM segments seg
        JOIN sources src ON src.id = seg.source_id
        WHERE seg.id=? AND seg.project_id=?
        """,
        (segment_id, project_id),
    ).fetchone()
    if seg is None:
        raise AppError(404, "not_found", "Segment not found.")

    updates: dict = {}

    if body.status is not None:
        if not validate_segment_transition(seg["status"], body.status):
            raise AppError(
                409, "invalid_transition",
                f"Cannot transition from '{seg['status']}' to '{body.status}'.",
                {"from": seg["status"], "to": body.status},
            )
        updates["status"] = body.status

    # Distinguish an absent field (no change) from an explicit null (clear the
    # edit) via model_fields_set — SC2. transcript_edited=null resets to NULL.
    if "transcript_edited" in body.model_fields_set:
        updates["transcript_edited"] = body.transcript_edited

    if not updates:
        return _serialize_segment(seg, project_id)

    updates["updated_at"] = utc_now()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    conn.execute(
        f"UPDATE segments SET {set_clause} WHERE id=?",
        (*updates.values(), segment_id),
    )
    conn.commit()
    # Any approval or transcript change makes a prior export stale (the manifest
    # is derived from approvals + COALESCE(transcript_edited, transcript)).
    invalidate_export(project_id)

    updated = conn.execute(
        """
        SELECT seg.*, src.filename AS source_filename
        FROM segments seg
        JOIN sources src ON src.id = seg.source_id
        WHERE seg.id=?
        """,
        (segment_id,),
    ).fetchone()
    return _serialize_segment(updated, project_id)


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
    conn = require_project(project_id)

    if body.action not in BULK_ACTION_TARGET:
        raise AppError(
            422, "invalid_action",
            "action must be one of: approve, reject, maybe, pending.",
        )

    to_status = BULK_ACTION_TARGET[body.action]
    allowed_sources = BULK_ACTION_SOURCES[body.action]

    f = body.filter
    # No status filter defaults to pending+maybe (SC4) — the same default as
    # GET /segments — so a filterless reject never touches approved segments.
    # A caller wanting a wider set (e.g. "All") sends the explicit status list.
    if f.status:
        filter_statuses = {s.strip() for s in f.status.split(",") if s.strip()}
    else:
        filter_statuses = set(DEFAULT_STATUS_FILTER)

    # Only act on statuses the action can legally transition from.
    target_statuses = allowed_sources & filter_statuses
    if not target_statuses:
        return {"affected_count": 0}

    placeholders = ",".join("?" * len(target_statuses))
    conditions = ["project_id=?", f"status IN ({placeholders})"]
    params: list = [project_id, *target_statuses]

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
    now = utc_now()
    result = conn.execute(
        f"UPDATE segments SET status=?, updated_at=? WHERE {where}",
        [to_status, now, *params],
    )
    conn.commit()

    affected = result.rowcount
    if affected > 0:
        invalidate_export(project_id)

    return {"affected_count": affected}
