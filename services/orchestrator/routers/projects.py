"""Project CRUD endpoints."""

import asyncio
import json
import shutil
import uuid
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from db import create_project_db, get_conn, list_project_ids, close_conn, project_dir, require_project, utc_now
from errors import AppError
from state_machines import APPROVED_STATUSES, sql_status_list
from status import auto_approve_demote, auto_approve_promote, invalidate_export

router = APIRouter(prefix="/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_stats(project_id: str) -> dict:
    conn = get_conn(project_id)
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total_segments,
            SUM(status='approved') AS approved_count,
            SUM(status='auto_approved') AS auto_approved_count,
            SUM(CASE WHEN status IN ({sql_status_list(APPROVED_STATUSES)}) THEN duration_secs ELSE 0 END) AS approved_duration_secs,
            SUM(status='pending') AS pending_count,
            SUM(status='maybe') AS maybe_count,
            SUM(status='rejected') AS rejected_count,
            SUM(status='below_threshold') AS below_threshold_count
        FROM segments WHERE project_id=?
        """,
        (project_id,),
    ).fetchone()

    sources = conn.execute(
        "SELECT id, filename, status, coverage_ratio, separation_error, diarisation_error FROM sources WHERE project_id=?",
        (project_id,),
    ).fetchall()

    source_coverage = []
    for s in sources:
        ratio = s["coverage_ratio"] or 0.0
        err = s["separation_error"] or s["diarisation_error"]
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
        "auto_approved_count": row["auto_approved_count"] or 0,
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
        "SELECT id, type, status, progress, progress_detail FROM jobs WHERE project_id=? AND status IN ('queued','running')",
        (project_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["progress_detail"] = json.loads(r["progress_detail"]) if r["progress_detail"] else None
        out.append(d)
    return out


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
    conn = require_project(project_id)
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return {
        "id": p["id"],
        "name": p["name"],
        "status": p["status"],
        "created_at": p["created_at"],
        "updated_at": p["updated_at"],
        "target_duration_secs": p["target_duration_secs"],
        "reference_path": p["reference_path"],
        "reference_origin": json.loads(p["reference_origin"]) if p["reference_origin"] else None,
        "config": {
            "whisper_model": p["whisper_model"],
            "language": p["language"],
            "match_threshold": p["match_threshold"],
            "target_duration_secs": p["target_duration_secs"],
            "auto_approve_enabled": bool(p["auto_approve_enabled"]),
            "auto_approve_match_threshold": p["auto_approve_match_threshold"],
            "auto_approve_transcript_threshold": p["auto_approve_transcript_threshold"],
            "whisper_batch_size": p["whisper_batch_size"],
            "whisper_compute_type": p["whisper_compute_type"],
            "demucs_model": p["demucs_model"],
            "demucs_shifts": p["demucs_shifts"],
            "diar_min_speakers": p["diar_min_speakers"],
            "diar_max_speakers": p["diar_max_speakers"],
            "diar_min_segment_duration": p["diar_min_segment_duration"],
            "whisper_beam_size": p["whisper_beam_size"],
            "whisper_vad_filter": bool(p["whisper_vad_filter"]),
            "target_lufs": p["target_lufs"],
            "highpass_hz": p["highpass_hz"],
            "silence_threshold_db": p["silence_threshold_db"],
            "silence_min_duration_secs": p["silence_min_duration_secs"],
            "xtts_epochs": p["xtts_epochs"],
            "xtts_batch_size": p["xtts_batch_size"],
            "xtts_grad_accum": p["xtts_grad_accum"],
            "xtts_learning_rate": p["xtts_learning_rate"],
        },
        "stats": _project_stats(project_id),
        "active_jobs": _active_jobs(project_id),
        "recent_failed_jobs": _recent_failed_jobs(project_id),
    }


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


# faster-whisper compute types we allow. 'default' lets the transcription
# service pick per device (float16 on GPU, int8 on CPU); the others let a
# VRAM-constrained GPU trade precision for memory.
WHISPER_COMPUTE_TYPES = {"default", "float16", "int8_float16", "int8"}
_WHISPER_BATCH = Field(default=16, ge=1, le=64)

# Demucs models the vocal-separation service accepts.
DEMUCS_MODELS = {"htdemucs", "mdx_extra"}


def _enum_validator(field_name: str, allowed: set[str]):
    """Build a field validator rejecting non-None values outside ``allowed``."""
    def _validate(v: Optional[str]) -> Optional[str]:
        if v is not None and v not in allowed:
            raise ValueError(f"{field_name} must be one of {sorted(allowed)}")
        return v
    return _validate


_validate_compute_type = _enum_validator("whisper_compute_type", WHISPER_COMPUTE_TYPES)
_validate_demucs_model = _enum_validator("demucs_model", DEMUCS_MODELS)


# Per-stage tuning knobs promoted to project config (migration 011).
# target_lufs already exists as a column (migration 001) but had no API path
# until now. Each knob's bounds are spelled out on both ProjectCreate (with a
# concrete default) and ProjectPatch (Optional), matching this file's existing
# per-field style — keep the two in sync when changing a bound.
#
# Ranges are deliberately generous "sanity" bounds — enough to reject nonsense
# (a positive LUFS, a zero learning rate) without second-guessing an operator
# who knows what they want.


class ProjectCreate(BaseModel):
    name: str
    whisper_model: str = "large-v2"
    language: Optional[str] = None
    match_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    target_duration_secs: float = Field(default=1800.0, gt=0)
    auto_approve_enabled: bool = True
    auto_approve_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    auto_approve_transcript_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    whisper_batch_size: int = _WHISPER_BATCH
    whisper_compute_type: str = "default"
    # Separation
    demucs_model: str = "htdemucs"
    demucs_shifts: int = Field(default=0, ge=0, le=10)
    # Diarisation
    diar_min_speakers: int = Field(default=1, ge=1, le=20)
    diar_max_speakers: int = Field(default=10, ge=1, le=20)
    diar_min_segment_duration: float = Field(default=1.0, gt=0, le=30.0)
    # Transcription
    whisper_beam_size: int = Field(default=5, ge=1, le=10)
    whisper_vad_filter: bool = False
    # Cleanup
    target_lufs: float = Field(default=-23.0, ge=-70.0, le=-5.0)
    highpass_hz: int = Field(default=80, ge=0, le=1000)
    silence_threshold_db: float = Field(default=-50.0, ge=-90.0, le=0.0)
    silence_min_duration_secs: float = Field(default=0.1, ge=0.0, le=10.0)
    # XTTS fine-tune
    xtts_epochs: int = Field(default=10, ge=1, le=200)
    xtts_batch_size: int = Field(default=3, ge=1, le=64)
    xtts_grad_accum: int = Field(default=1, ge=1, le=64)
    xtts_learning_rate: float = Field(default=5e-6, gt=0.0, le=1.0)

    _check_compute = field_validator("whisper_compute_type")(_validate_compute_type)
    _check_demucs = field_validator("demucs_model")(_validate_demucs_model)


class ProjectPatch(BaseModel):
    name: Optional[str] = None
    match_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    target_duration_secs: Optional[float] = Field(default=None, gt=0)
    whisper_model: Optional[str] = None
    language: Optional[str] = None
    auto_approve_enabled: Optional[bool] = None
    auto_approve_match_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    auto_approve_transcript_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    whisper_batch_size: Optional[int] = Field(default=None, ge=1, le=64)
    whisper_compute_type: Optional[str] = None
    # Separation
    demucs_model: Optional[str] = None
    demucs_shifts: Optional[int] = Field(default=None, ge=0, le=10)
    # Diarisation
    diar_min_speakers: Optional[int] = Field(default=None, ge=1, le=20)
    diar_max_speakers: Optional[int] = Field(default=None, ge=1, le=20)
    diar_min_segment_duration: Optional[float] = Field(default=None, gt=0, le=30.0)
    # Transcription
    whisper_beam_size: Optional[int] = Field(default=None, ge=1, le=10)
    whisper_vad_filter: Optional[bool] = None
    # Cleanup
    target_lufs: Optional[float] = Field(default=None, ge=-70.0, le=-5.0)
    highpass_hz: Optional[int] = Field(default=None, ge=0, le=1000)
    silence_threshold_db: Optional[float] = Field(default=None, ge=-90.0, le=0.0)
    silence_min_duration_secs: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    # XTTS fine-tune
    xtts_epochs: Optional[int] = Field(default=None, ge=1, le=200)
    xtts_batch_size: Optional[int] = Field(default=None, ge=1, le=64)
    xtts_grad_accum: Optional[int] = Field(default=None, ge=1, le=64)
    xtts_learning_rate: Optional[float] = Field(default=None, gt=0.0, le=1.0)

    _check_compute = field_validator("whisper_compute_type")(_validate_compute_type)
    _check_demucs = field_validator("demucs_model")(_validate_demucs_model)


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
            f"""
            SELECT
                SUM(status='approved') AS approved_count,
                SUM(status='auto_approved') AS auto_approved_count,
                SUM(CASE WHEN status IN ({sql_status_list(APPROVED_STATUSES)}) THEN duration_secs ELSE 0 END) AS approved_duration_secs,
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
                "auto_approved_count": stats_row["auto_approved_count"] or 0,
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
            whisper_model, language, match_threshold, target_duration_secs,
            auto_approve_enabled, auto_approve_match_threshold, auto_approve_transcript_threshold,
            whisper_batch_size, whisper_compute_type,
            demucs_model, demucs_shifts,
            diar_min_speakers, diar_max_speakers, diar_min_segment_duration,
            whisper_beam_size, whisper_vad_filter,
            target_lufs, highpass_hz, silence_threshold_db, silence_min_duration_secs,
            xtts_epochs, xtts_batch_size, xtts_grad_accum, xtts_learning_rate)
        VALUES (?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, body.name, now, now, body.whisper_model,
         body.language, body.match_threshold, body.target_duration_secs,
         int(body.auto_approve_enabled), body.auto_approve_match_threshold,
         body.auto_approve_transcript_threshold,
         body.whisper_batch_size, body.whisper_compute_type,
         body.demucs_model, body.demucs_shifts,
         body.diar_min_speakers, body.diar_max_speakers, body.diar_min_segment_duration,
         body.whisper_beam_size, int(body.whisper_vad_filter),
         body.target_lufs, body.highpass_hz, body.silence_threshold_db,
         body.silence_min_duration_secs,
         body.xtts_epochs, body.xtts_batch_size, body.xtts_grad_accum,
         body.xtts_learning_rate),
    )
    conn.commit()
    return {"id": project_id, "name": body.name, "status": "new"}


@router.get("/{project_id}")
async def get_project(project_id: str):
    return _project_detail(project_id)


@router.patch("/{project_id}")
async def patch_project(project_id: str, body: ProjectPatch):
    conn = require_project(project_id)
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    provided = body.model_fields_set
    updates: dict = {}
    if "language" in provided:
        updates["language"] = body.language  # allows setting to None (auto-detect)

    # Every other field follows "provided and not null". SQLite has no bool
    # type; boolean knobs are stored as 0/1.
    for f in (
        "name", "whisper_model", "target_duration_secs",
        "match_threshold", "auto_approve_enabled",
        "auto_approve_match_threshold", "auto_approve_transcript_threshold",
        "whisper_batch_size", "whisper_compute_type",
        # Pipeline tuning knobs (migration 011). Plain per-project config: a
        # change applies to the next run of that stage; no re-evaluation.
        "demucs_model", "demucs_shifts",
        "diar_min_speakers", "diar_max_speakers", "diar_min_segment_duration",
        "whisper_beam_size", "whisper_vad_filter",
        "target_lufs", "highpass_hz", "silence_threshold_db", "silence_min_duration_secs",
        "xtts_epochs", "xtts_batch_size", "xtts_grad_accum", "xtts_learning_rate",
    ):
        if f in provided and getattr(body, f) is not None:
            val = getattr(body, f)
            updates[f] = int(val) if isinstance(val, bool) else val

    # Changing match_threshold or any auto-approve field triggers a synchronous
    # segment status re-evaluation (spec/api-contracts.md PATCH /projects).
    reeval_fields = (
        "match_threshold",
        "auto_approve_enabled",
        "auto_approve_match_threshold",
        "auto_approve_transcript_threshold",
    )
    needs_reeval = any(f in updates and updates[f] != p[f] for f in reeval_fields)

    if updates:
        updates["updated_at"] = utc_now()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE projects SET {set_clause} WHERE id=?",
            (*updates.values(), project_id),
        )
        conn.commit()

    if needs_reeval:
        # Spec order: (1) demote ineligible auto_approved -> pending,
        # (2) promote eligible pending -> auto_approved, (3) bidirectional
        # pending <-> below_threshold swap against the new match_threshold.
        now = utc_now()
        changed = auto_approve_demote(conn, project_id, now)
        changed += auto_approve_promote(conn, project_id, now)
        match_threshold = updates.get("match_threshold", p["match_threshold"])
        changed += conn.execute(
            """
            UPDATE segments SET status='pending', updated_at=?
            WHERE project_id=? AND status='below_threshold'
              AND match_confidence >= ?
            """,
            (now, project_id, match_threshold),
        ).rowcount
        changed += conn.execute(
            """
            UPDATE segments SET status='below_threshold', updated_at=?
            WHERE project_id=? AND status='pending'
              AND match_confidence < ?
            """,
            (now, project_id, match_threshold),
        ).rowcount
        conn.commit()
        if changed:
            # The approved set (auto_approved is exported) may have changed —
            # any prior export archive is stale.
            invalidate_export(project_id)

    return _project_detail(project_id)


@router.delete("/{project_id}")
async def delete_project(project_id: str, body: ProjectDelete):
    if not body.confirm:
        raise AppError(422, "confirm_required", "Pass confirm=true to delete.")

    conn = require_project(project_id)

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
        # Project dirs hold multi-GB videos — keep the event loop free.
        await asyncio.to_thread(shutil.rmtree, pdir)

    return {"deleted": True}
