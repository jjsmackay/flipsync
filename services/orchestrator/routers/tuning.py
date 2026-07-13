"""Tuning previews — ephemeral A/B renders of stage settings on a sample.

Stage-generic by design: `stage` is a Literal with cleanup as the only value
this wave; separation is the planned follow-on. Results are scratch files
under projects/{id}/tuning_previews/ (TTL-swept, excluded from export and
dataset builds) and are never written to segment tables.
"""

import time
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import service_client
from db import project_dir, require_project
from errors import AppError
from jobs import enqueue

router = APIRouter(prefix="/projects/{project_id}/tuning-preview", tags=["tuning"])

TTL_SECS = 24 * 3600


class CleanupTuningParams(BaseModel):
    # Bounds mirror the project-config validators in routers/projects.py.
    target_lufs: float = Field(ge=-70.0, le=-5.0)
    highpass_hz: int = Field(ge=0, le=1000)
    silence_threshold_db: float = Field(ge=-90.0, le=0.0)
    silence_min_duration_secs: float = Field(ge=0.0, le=10.0)


class TuningTarget(BaseModel):
    segment_id: str


class CreateTuningPreviewRequest(BaseModel):
    stage: Literal["cleanup"]
    params: CleanupTuningParams
    target: TuningTarget


def _sweep_stale(project_id: str) -> None:
    """Best-effort removal of scratch WAVs older than TTL_SECS."""
    d = project_dir(project_id) / "tuning_previews"
    if not d.exists():
        return
    cutoff = time.time() - TTL_SECS
    for f in d.glob("*.wav"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


@router.post("", status_code=202)
async def create_tuning_preview(project_id: str, body: CreateTuningPreviewRequest):
    conn = require_project(project_id)
    if not await service_client.is_healthy("cleanup"):
        raise AppError(503, "cleanup_unavailable", "The cleanup service is not deployed or not healthy.")
    seg = conn.execute(
        "SELECT id FROM segments WHERE id=? AND project_id=?", (body.target.segment_id, project_id)
    ).fetchone()
    if seg is None:
        raise AppError(404, "not_found", "Segment not found.")
    _sweep_stale(project_id)
    job_id = enqueue(
        project_id, "tuning_preview",
        params={
            "stage": body.stage,
            "segment_id": body.target.segment_id,
            "params": body.params.model_dump(),
        },
    )
    return {"enqueued_job": {"id": job_id, "type": "tuning_preview"}}


@router.get("/{preview_id}")
async def get_tuning_preview(project_id: str, preview_id: str):
    conn = require_project(project_id)
    job = conn.execute(
        "SELECT id, type, status, error FROM jobs WHERE id=? AND project_id=?",
        (preview_id, project_id),
    ).fetchone()
    if job is None or job["type"] != "tuning_preview":
        raise AppError(404, "not_found", "Tuning preview not found.")
    return {"id": job["id"], "status": job["status"], "error": job["error"]}


@router.get("/{preview_id}/audio")
async def get_tuning_preview_audio(project_id: str, preview_id: str):
    conn = require_project(project_id)
    job = conn.execute(
        "SELECT id, type, status FROM jobs WHERE id=? AND project_id=?",
        (preview_id, project_id),
    ).fetchone()
    if job is None or job["type"] != "tuning_preview" or job["status"] != "complete":
        raise AppError(404, "preview_not_ready", "Tuning preview is not ready.")
    wav = project_dir(project_id) / "tuning_previews" / f"{preview_id}.wav"
    if not wav.exists():
        raise AppError(404, "preview_not_ready", "Tuning preview audio not found.")
    return FileResponse(str(wav), media_type="audio/wav")
