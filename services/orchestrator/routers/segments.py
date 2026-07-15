"""Segments API — list, audio streaming, review actions, bulk operations.

This is Wave 4 scope but included in Wave 1 to allow endpoint tests.
"""

import json
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from audio import get_duration, slice_wav
from db import project_dir, require_project, utc_now
from errors import AppError
from state_machines import validate_segment_transition, BULK_ACTION_SOURCES
from status import invalidate_export, recompute_project_status

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
        "speaker_match_confidence": row["speaker_match_confidence"],
        "transcript": row["transcript"],
        "transcript_edited": row["transcript_edited"],
        "transcript_confidence": row["transcript_confidence"],
        "status": row["status"],
        "clipping_warning": bool(row["clipping_warning"]),
        "flags": json.loads(row["flags"]) if row["flags"] else [],
        "audio_url": f"/projects/{project_id}/segments/{row['id']}/audio",
    }


# Maps accepted `sort` values (spec + column aliases) to the actual column name.
# `uncertainty` is not a column: it sorts by ABS(match_confidence - the
# project's match_threshold), so borderline segments come first (default asc).
SORT_FIELD_MAP = {
    "match_confidence": "match_confidence",
    "duration": "duration_secs",
    "duration_secs": "duration_secs",
    "start_secs": "start_secs",
    "transcript_confidence": "transcript_confidence",
    "uncertainty": None,
}
VALID_ORDERS = {"asc", "desc"}

# Default status filter, shared by GET /segments and POST /segments/bulk.
DEFAULT_STATUS_FILTER = ("pending", "maybe")


def _has_no_transcript(seg) -> bool:
    """True when a segment has neither a machine nor a user-edited transcript."""
    return seg["transcript"] is None and seg["transcript_edited"] is None


def _range_filter_conditions(
    source_id: Optional[str],
    min_confidence: Optional[float],
    max_confidence: Optional[float],
    min_duration: Optional[float],
    max_duration: Optional[float],
    prefix: str = "",
) -> tuple[list[str], list]:
    """The five optional segment filters shared by GET /segments and
    POST /segments/bulk — one builder so the bulk preview (queried via GET)
    always matches what bulk Apply affects."""
    conditions: list[str] = []
    params: list = []
    if source_id:
        conditions.append(f"{prefix}source_id=?")
        params.append(source_id)
    if min_confidence is not None:
        conditions.append(f"{prefix}match_confidence>=?")
        params.append(min_confidence)
    if max_confidence is not None:
        conditions.append(f"{prefix}match_confidence<=?")
        params.append(max_confidence)
    if min_duration is not None:
        conditions.append(f"{prefix}duration_secs>=?")
        params.append(min_duration)
    if max_duration is not None:
        conditions.append(f"{prefix}duration_secs<=?")
        params.append(max_duration)
    return conditions, params


def _fetch_segment(conn, project_id: str, segment_id: str):
    """Fetch one segment row (with source_filename) or None."""
    return conn.execute(
        """
        SELECT seg.*, src.filename AS source_filename
        FROM segments seg
        JOIN sources src ON src.id = seg.source_id
        WHERE seg.id=? AND seg.project_id=?
        """,
        (segment_id, project_id),
    ).fetchone()


@router.get("")
async def list_segments(
    project_id: str,
    status: str = "pending,maybe",
    source_id: Optional[str] = None,
    q: Optional[str] = None,
    min_confidence: Optional[float] = None,
    max_confidence: Optional[float] = None,
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
    sort: str = "match_confidence",
    order: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
    count_only: bool = False,
):
    conn = require_project(project_id)

    if sort not in SORT_FIELD_MAP:
        raise AppError(422, "invalid_sort", f"sort must be one of {sorted(SORT_FIELD_MAP)}.")
    if order is None:
        # Spec default: desc, except uncertainty where the most borderline
        # (smallest distance from the threshold) come first.
        order = "asc" if sort == "uncertainty" else "desc"
    if order not in VALID_ORDERS:
        raise AppError(422, "invalid_order", "order must be 'asc' or 'desc'.")
    if sort == "uncertainty":
        match_threshold = conn.execute(
            "SELECT match_threshold FROM projects WHERE id=?", (project_id,)
        ).fetchone()["match_threshold"]
        order_expr = "ABS(seg.match_confidence - ?)"
        order_params: list = [match_threshold]
    else:
        order_expr = f"seg.{SORT_FIELD_MAP[sort]}"
        order_params = []
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
    extra_conditions, extra_params = _range_filter_conditions(
        source_id, min_confidence, max_confidence, min_duration, max_duration,
        prefix="seg.",
    )
    conditions += extra_conditions
    params += extra_params

    if q:
        # Case-insensitive substring match on the effective transcript.
        # % and _ in the query are literals, not LIKE wildcards.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(
            "COALESCE(seg.transcript_edited, seg.transcript) LIKE ? ESCAPE '\\'"
        )
        params.append(f"%{escaped}%")

    where = " AND ".join(conditions)

    total = conn.execute(
        f"SELECT COUNT(*) FROM segments seg WHERE {where}", params
    ).fetchone()[0]

    if count_only:
        return {"total": total}

    offset = (page - 1) * per_page
    rows = conn.execute(
        f"""
        SELECT seg.*, src.filename AS source_filename
        FROM segments seg
        JOIN sources src ON src.id = seg.source_id
        WHERE {where}
        ORDER BY {order_expr} {order.upper()}
        LIMIT ? OFFSET ?
        """,
        [*params, *order_params, per_page, offset],
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
    seg = _fetch_segment(conn, project_id, segment_id)
    if seg is None:
        raise AppError(404, "not_found", "Segment not found.")

    updates: dict = {}

    if body.status is not None:
        # auto_approved is system-assigned only — reject regardless of the
        # current status (spec/api-contracts.md PATCH /segments).
        if body.status == "auto_approved":
            raise AppError(
                409, "invalid_transition",
                "auto_approved is assigned by the system and cannot be set directly.",
                {"from": seg["status"], "to": body.status},
            )
        if not validate_segment_transition(seg["status"], body.status):
            raise AppError(
                409, "invalid_transition",
                f"Cannot transition from '{seg['status']}' to '{body.status}'.",
                {"from": seg["status"], "to": body.status},
            )
        # An approved segment must carry a transcript — export builds the manifest
        # from COALESCE(transcript_edited, transcript) and silently omits NULLs, so
        # approving an untranscribed segment would drop it from the dataset unseen.
        if body.status == "approved" and _has_no_transcript(seg):
            raise AppError(
                409, "no_transcript",
                "Cannot approve a segment with no transcript. Transcribe it first.",
                {"segment_id": segment_id},
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

    updated = _fetch_segment(conn, project_id, segment_id)
    return _serialize_segment(updated, project_id)


# Minimum length a segment may be trimmed to. Below this the cleanup service
# auto-rejects it as silent-after-trim anyway, so reject the edit up front.
MIN_SEGMENT_SECS = 0.1


class SegmentBoundaries(BaseModel):
    # Absolute new boundaries in seconds (the frontend computes these from the
    # current values ± the user's nudge amount). Either may be omitted to leave
    # that edge unchanged.
    start_secs: Optional[float] = Field(default=None, ge=0.0)
    end_secs: Optional[float] = Field(default=None, gt=0.0)


@router.post("/{segment_id}/boundaries")
async def adjust_segment_boundaries(project_id: str, segment_id: str, body: SegmentBoundaries):
    """Re-cut a segment's WAV from the source's separated-vocals file with new
    start/end boundaries — the trim/extend control in the review detail panel.

    The raw slice was cut once at diarisation time, but the full vocals WAV is
    retained, so extending a boundary recovers real neighbouring audio (a word
    the diariser clipped), and trimming drops bleed from an adjacent speaker.
    Invalidates the cleaned cache (the old clean is stale) and any prior export.
    The transcript is left as-is but flagged ``boundary_edited`` — re-transcribe
    if the recut changed the words.
    """
    conn = require_project(project_id)
    seg = _fetch_segment(conn, project_id, segment_id)
    if seg is None:
        raise AppError(404, "not_found", "Segment not found.")
    if body.start_secs is None and body.end_secs is None:
        raise AppError(422, "no_change", "Provide start_secs and/or end_secs.")

    new_start = body.start_secs if body.start_secs is not None else seg["start_secs"]
    new_end = body.end_secs if body.end_secs is not None else seg["end_secs"]

    src = conn.execute(
        "SELECT vocals_path FROM sources WHERE id=? AND project_id=?",
        (seg["source_id"], project_id),
    ).fetchone()
    if src is None or not src["vocals_path"]:
        raise AppError(
            409, "vocals_unavailable",
            "The source's separated audio is not available; cannot re-cut this segment.",
        )
    pdir = project_dir(project_id)
    vocals = pdir / src["vocals_path"]
    if not vocals.exists():
        raise AppError(
            409, "vocals_unavailable",
            "The source's separated audio file is missing on disk.",
        )

    # Clamp to the available audio. get_duration returns 0.0 when it cannot
    # measure the file — in that case skip the upper clamp rather than reject a
    # healthy edit (ffmpeg will still stop at end-of-file).
    total = await get_duration(str(vocals))
    new_start = max(0.0, new_start)
    if total > 0:
        new_end = min(new_end, total)
    if new_end - new_start < MIN_SEGMENT_SECS:
        raise AppError(
            422, "invalid_boundaries",
            f"Adjusted segment must be at least {MIN_SEGMENT_SECS:g}s long.",
            {"start_secs": new_start, "end_secs": new_end},
        )

    raw = pdir / seg["raw_path"]
    raw.parent.mkdir(parents=True, exist_ok=True)
    tmp = raw.with_suffix(".tmp.wav")
    ok = await slice_wav(str(vocals), str(tmp), new_start, new_end)
    if not ok:
        tmp.unlink(missing_ok=True)
        raise AppError(500, "reslice_failed", "Failed to re-cut the segment audio.")
    tmp.replace(raw)

    flags = json.loads(seg["flags"]) if seg["flags"] else []
    if "boundary_edited" not in flags:
        flags.append("boundary_edited")

    old_cleaned = seg["cleaned_path"]
    conn.execute(
        """
        UPDATE segments
        SET start_secs=?, end_secs=?, cleaned_path=NULL, flags=?, updated_at=?
        WHERE id=?
        """,
        (new_start, new_end, json.dumps(flags), utc_now(), segment_id),
    )
    conn.commit()

    # The cached cleaned WAV was cut from the old boundaries — drop it so the
    # next dataset build re-cleans from the new raw slice.
    if old_cleaned:
        try:
            (pdir / old_cleaned).unlink(missing_ok=True)
        except OSError:
            pass

    invalidate_export(project_id)
    recompute_project_status(project_id)

    updated = _fetch_segment(conn, project_id, segment_id)
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
    extra_conditions, extra_params = _range_filter_conditions(
        f.source_id, f.min_confidence, f.max_confidence, f.min_duration, f.max_duration,
    )
    conditions += extra_conditions
    params += extra_params

    where = " AND ".join(conditions)

    # Approving requires a transcript (see PATCH guard). Count the transcript-less
    # matches so the caller can surface them, then exclude them from the UPDATE.
    skipped_no_transcript = 0
    if body.action == "approve":
        skipped_no_transcript = conn.execute(
            f"SELECT COUNT(*) FROM segments WHERE {where} "
            "AND transcript IS NULL AND transcript_edited IS NULL",
            params,
        ).fetchone()[0]
        conditions.append("(transcript IS NOT NULL OR transcript_edited IS NOT NULL)")
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

    return {"affected_count": affected, "skipped_no_transcript": skipped_no_transcript}
