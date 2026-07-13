"""Previews API (v1.5) — XTTS-v2 speech synthesis previews."""

import json
from typing import Literal, Optional

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

import service_client
from db import project_dir, require_project
from errors import AppError
from jobs import enqueue, _resolve_conditioning

router = APIRouter(prefix="/projects/{project_id}/previews", tags=["previews"])


class ConditioningSpec(BaseModel):
    source: Optional[Literal["reference_clip", "segments_raw", "segments_cleaned"]] = None
    segment_count: int = 5


class CreatePreviewRequest(BaseModel):
    # Either free text or a segment to compare against. When segment_id is
    # set, text is ignored — the segment's transcript is synthesised so the
    # clone says exactly what the original says.
    text: Optional[str] = Field(default=None, min_length=1, max_length=500)
    segment_id: Optional[str] = Field(default=None, min_length=1)
    model_id: Optional[str] = None
    conditioning: ConditioningSpec = Field(default_factory=ConditioningSpec)
    # XTTS sampling knobs. Per-run (not project config) — the point of a
    # preview is to try a few takes and pick one. Defaults match the service's
    # (coqui's inference defaults, except our 0.65 house temperature).
    temperature: float = Field(default=0.65, gt=0.0, le=2.0)
    speed: float = Field(default=1.0, ge=0.25, le=2.0)
    repetition_penalty: float = Field(default=10.0, ge=1.0, le=20.0)
    top_k: int = Field(default=50, ge=1, le=100)
    top_p: float = Field(default=0.85, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _require_text_or_segment(self):
        if self.text is None and self.segment_id is None:
            raise ValueError("either text or segment_id is required")
        return self


@router.post("", status_code=202)
async def create_preview(project_id: str, body: CreatePreviewRequest):
    conn = require_project(project_id)

    if not await service_client.is_healthy("xtts"):
        raise AppError(
            503, "xtts_unavailable",
            "The XTTS service is not deployed or not healthy.",
        )

    if body.model_id:
        model = conn.execute(
            "SELECT status FROM models WHERE id=? AND project_id=?", (body.model_id, project_id)
        ).fetchone()
        if model is None or model["status"] != "ready":
            raise AppError(
                409, "model_not_ready",
                "The requested model is not ready for previews.",
            )

    # Resolve the effective text: use segment transcript if segment_id is set.
    text = body.text
    if body.segment_id is not None:
        seg = conn.execute(
            "SELECT COALESCE(transcript_edited, transcript) AS t FROM segments "
            "WHERE id=? AND project_id=?",
            (body.segment_id, project_id),
        ).fetchone()
        if seg is None or not (seg["t"] or "").strip():
            raise AppError(
                409, "segment_not_comparable",
                "The segment does not exist or has no transcript.",
            )
        text = seg["t"]

    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    try:
        _resolve_conditioning(
            conn, project, project_id, body.conditioning.source, body.conditioning.segment_count,
            exclude_segment_id=body.segment_id,
        )
    except LookupError as exc:
        raise AppError(
            409, "conditioning_unavailable",
            "No audio is available for the requested conditioning source.",
            {"reason": str(exc)},
        )

    job_id = enqueue(
        project_id, "preview",
        params={
            "text": text,
            "segment_id": body.segment_id,
            "model_id": body.model_id,
            "conditioning": {
                "source": body.conditioning.source,
                "segment_count": body.conditioning.segment_count,
            },
            "temperature": body.temperature,
            "speed": body.speed,
            "repetition_penalty": body.repetition_penalty,
            "top_k": body.top_k,
            "top_p": body.top_p,
        },
    )
    return {"enqueued_job": {"id": job_id, "type": "preview"}}


@router.get("")
async def list_previews(project_id: str, limit: int = 20):
    conn = require_project(project_id)
    rows = conn.execute(
        "SELECT id, status, params, created_at FROM jobs WHERE project_id=? AND type='preview' ORDER BY created_at DESC LIMIT ?",
        (project_id, limit),
    ).fetchall()

    previews = []
    for r in rows:
        p = json.loads(r["params"]) if r["params"] else {}
        previews.append({
            "id": r["id"],
            "status": r["status"],
            "text": p.get("text"),
            "segment_id": p.get("segment_id"),
            "model_id": p.get("model_id"),
            "conditioning": p.get("conditioning"),
            "created_at": r["created_at"],
        })
    return {"previews": previews}


@router.get("/{preview_id}/audio")
async def get_preview_audio(project_id: str, preview_id: str):
    conn = require_project(project_id)
    job = conn.execute(
        "SELECT id, type, status FROM jobs WHERE id=? AND project_id=?", (preview_id, project_id)
    ).fetchone()
    if job is None or job["type"] != "preview" or job["status"] != "complete":
        raise AppError(404, "preview_not_ready", "Preview is not ready.")

    wav = project_dir(project_id) / "previews" / f"{preview_id}.wav"
    if not wav.exists():
        raise AppError(404, "preview_not_ready", "Preview audio not found.")

    return FileResponse(str(wav), media_type="audio/wav")
