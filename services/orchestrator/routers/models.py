"""Models API (v1.5) — XTTS-v2 fine-tune trigger, listing, deletion."""

import json
import shutil
import uuid

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

import service_client
from db import project_dir, require_project, utc_now
from errors import AppError
from jobs import enqueue, _select_dataset_segments

router = APIRouter(prefix="/projects/{project_id}/models", tags=["models"])

REQUIRED_DATASET_SECS = 300.0


class DatasetSpec(BaseModel):
    mode: str = "approved"
    min_confidence: float = 0.85


class TrainParams(BaseModel):
    epochs: int = 10
    batch_size: int = 3
    grad_accum: int = 1
    learning_rate: float = 5e-6


class CreateModelRequest(BaseModel):
    dataset: DatasetSpec = Field(default_factory=DatasetSpec)
    params: TrainParams = Field(default_factory=TrainParams)


def _serialize_model(row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d["params"]) if d["params"] else None
    return d


@router.post("", status_code=202)
async def create_model(project_id: str, body: CreateModelRequest = CreateModelRequest()):
    conn = require_project(project_id)

    if not await service_client.is_healthy("xtts"):
        raise AppError(
            503, "xtts_unavailable",
            "The XTTS service is not deployed or not healthy.",
        )

    in_progress = conn.execute(
        "SELECT COUNT(*) FROM models WHERE project_id=? AND status IN ('pending','training')",
        (project_id,),
    ).fetchone()[0]
    if in_progress > 0:
        raise AppError(
            409, "finetune_in_progress",
            "A fine-tune is already in progress for this project.",
        )

    mode = body.dataset.mode
    min_conf = body.dataset.min_confidence if mode == "auto" else None
    kept, _dropped = _select_dataset_segments(conn, mode, min_conf)
    duration = sum((r["duration_secs"] or 0.0) for r in kept)
    if duration < REQUIRED_DATASET_SECS:
        raise AppError(
            409, "insufficient_dataset",
            f"Selected segments total {duration:.1f}s; at least {REQUIRED_DATASET_SECS:g}s is required.",
            {"selected_duration_secs": duration, "required_secs": REQUIRED_DATASET_SECS},
        )

    model_id = str(uuid.uuid4())
    now = utc_now()
    conn.execute(
        """
        INSERT INTO models
            (id, project_id, status, dataset_mode, min_confidence, params, created_at, updated_at)
        VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (model_id, project_id, mode, min_conf, json.dumps(body.params.model_dump()), now, now),
    )
    conn.commit()

    # FIFO queue (one job at a time per project) guarantees dataset_build runs
    # before finetune.
    ds_job = enqueue(
        project_id, "dataset_build",
        params={"model_id": model_id, "mode": mode, "min_confidence": min_conf},
    )
    ft_job = enqueue(
        project_id, "finetune",
        params={"model_id": model_id, "params": body.params.model_dump()},
    )

    return {
        "model": {"id": model_id, "status": "pending", "dataset_mode": mode},
        "enqueued_jobs": [
            {"id": ds_job, "type": "dataset_build"},
            {"id": ft_job, "type": "finetune"},
        ],
    }


@router.get("")
async def list_models(project_id: str):
    conn = require_project(project_id)
    rows = conn.execute(
        "SELECT * FROM models WHERE project_id=? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return {"models": [_serialize_model(r) for r in rows]}


@router.delete("/{model_id}")
async def delete_model(project_id: str, model_id: str):
    conn = require_project(project_id)
    model = conn.execute(
        "SELECT * FROM models WHERE id=? AND project_id=?", (model_id, project_id)
    ).fetchone()
    if model is None:
        raise AppError(404, "model_not_found", "Model not found.")
    if model["status"] in ("pending", "training"):
        raise AppError(
            409, "model_training",
            "Cannot delete a model while it is pending or training. Cancel the job first.",
        )

    cp = model["checkpoint_dir"] or f"models/{model_id}"
    shutil.rmtree(project_dir(project_id) / cp, ignore_errors=True)
    conn.execute("DELETE FROM models WHERE id=?", (model_id,))
    conn.commit()
    return Response(status_code=204)
