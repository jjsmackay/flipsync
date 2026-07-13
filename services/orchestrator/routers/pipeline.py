"""Pipeline control endpoints: start, reprocess, transcription triggers."""

import json
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from db import require_project, utc_now
from errors import AppError
from jobs import delete_source_segments, enqueue, enqueue_bulk_transcription, has_active_diarisation_job
from state_machines import (
    APPROVED_STATUSES,
    EXPORTABLE_STATUSES,
    sql_status_list,
    validate_source_transition,
)
from status import invalidate_export, recompute_project_status

router = APIRouter(prefix="/projects/{project_id}", tags=["pipeline"])


# ---------------------------------------------------------------------------
# Pipeline start
# ---------------------------------------------------------------------------


@router.post("/pipeline/start", status_code=202)
async def start_pipeline(project_id: str):
    conn = require_project(project_id)

    pending_sources = conn.execute(
        "SELECT id FROM sources WHERE project_id=? AND status='separation_pending'",
        (project_id,),
    ).fetchall()

    if not pending_sources:
        raise AppError(
            409, "no_pending_sources",
            "No sources in separation_pending status. Upload sources or check source status.",
        )

    # Separation always runs regardless of reference. If no reference is set, diarisation
    # is not chained (see jobs._auto_enqueue_diarisation) — the project rests in
    # awaiting_reference until the user sets a reference and calls pipeline/continue.
    enqueued = []
    for s in pending_sources:
        job_id = enqueue(project_id, "vocal_separation", source_id=s["id"])
        enqueued.append({"id": job_id, "type": "vocal_separation", "source_id": s["id"]})

    # The queued jobs drive the derived status to 'processing'.
    recompute_project_status(project_id)

    return {"enqueued_jobs": enqueued}


# ---------------------------------------------------------------------------
# Pipeline continue (reference gate)
# ---------------------------------------------------------------------------


@router.post("/pipeline/continue", status_code=202)
async def continue_pipeline(project_id: str):
    conn = require_project(project_id)

    project = conn.execute("SELECT reference_path FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project["reference_path"]:
        raise AppError(
            409, "no_reference",
            "Set a reference before continuing; diarisation needs it to match the target speaker.",
        )

    pending_sources = conn.execute(
        "SELECT id FROM sources WHERE project_id=? AND status='diarisation_pending'",
        (project_id,),
    ).fetchall()

    # Idempotence: a double-click (or retry) must not enqueue a second
    # diarisation job for a source that already has one queued/running.
    to_enqueue = [s for s in pending_sources if not has_active_diarisation_job(conn, s["id"])]
    active_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE project_id=? AND type='diarisation' AND status IN ('queued','running')",
        (project_id,),
    ).fetchone()[0]

    if not pending_sources and active_jobs == 0:
        raise AppError(
            409, "no_pending_sources",
            "No sources in diarisation_pending status. Run vocal separation first.",
        )

    if not to_enqueue:
        # Everything eligible is already queued/running — report current state
        # instead of erroring or double-enqueueing.
        recompute_project_status(project_id)
        return JSONResponse(status_code=200, content={"enqueued_jobs": []})

    enqueued = []
    for s in to_enqueue:
        job_id = enqueue(project_id, "diarisation", source_id=s["id"])
        enqueued.append({"id": job_id, "type": "diarisation", "source_id": s["id"]})

    # The queued jobs drive the derived status to 'processing'.
    recompute_project_status(project_id)

    return {"enqueued_jobs": enqueued}


# ---------------------------------------------------------------------------
# Reprocess
# ---------------------------------------------------------------------------


class ReprocessRequest(BaseModel):
    steps: list[str]
    params: dict = {}
    confirm: bool = False


@router.post("/sources/{source_id}/reprocess", status_code=202)
async def reprocess_source(project_id: str, source_id: str, body: ReprocessRequest):
    conn = require_project(project_id)

    source = conn.execute(
        "SELECT * FROM sources WHERE id=? AND project_id=?", (source_id, project_id)
    ).fetchone()
    if source is None:
        raise AppError(404, "not_found", "Source not found.")

    valid_step_combos = [["separation"], ["diarisation"], ["separation", "diarisation"]]
    if body.steps not in valid_step_combos:
        raise AppError(
            422, "invalid_steps",
            "steps must be ['separation'], ['diarisation'], or ['separation', 'diarisation'].",
        )

    # Check for approved segments that would be invalidated. Re-running either
    # step deletes and recreates this source's segments, so both discard
    # approvals; the message names the steps actually requested.
    approved_count = conn.execute(
        f"SELECT COUNT(*) FROM segments WHERE source_id=? AND status IN ({sql_status_list(APPROVED_STATUSES)})",
        (source_id,),
    ).fetchone()[0]
    if approved_count > 0 and not body.confirm:
        step_label = {
            ("separation",): "vocal separation",
            ("diarisation",): "speaker matching",
            ("separation", "diarisation"): "vocal separation and speaker matching",
        }[tuple(body.steps)]
        raise AppError(
            409, "would_invalidate_approvals",
            f"Re-running {step_label} will discard {approved_count} approved segments from this source.",
            {"approved_count": approved_count},
        )

    enqueued = []
    now = utc_now()
    current_status = source["status"]

    # A source already sitting at the target pending status (e.g. its job
    # failed before the handler ran, such as a service-readiness timeout) is
    # a re-enqueue, not a state transition — skip transition validation.
    if "separation" in body.steps:
        if current_status != "separation_pending" and not validate_source_transition(current_status, "separation_pending"):
            raise AppError(
                409, "invalid_source_status",
                f"Cannot re-run vocal separation from source status '{current_status}'.",
                {"from": current_status, "to": "separation_pending"},
            )
        delete_source_segments(conn, project_id, source_id)
        conn.execute(
            "UPDATE sources SET status='separation_pending', vocals_path=NULL, separation_error=NULL, diarisation_error=NULL, updated_at=? WHERE id=?",
            (now, source_id),
        )
        conn.commit()
        params = {}
        if "demucs_model" in body.params:
            params["demucs_model"] = body.params["demucs_model"]
        job_id = enqueue(project_id, "vocal_separation", source_id=source_id, params=params)
        enqueued.append({"id": job_id, "type": "vocal_separation", "source_id": source_id})

    elif "diarisation" in body.steps:
        if current_status != "diarisation_pending" and not validate_source_transition(current_status, "diarisation_pending"):
            raise AppError(
                409, "invalid_source_status",
                f"Cannot re-run speaker matching from source status '{current_status}'.",
                {"from": current_status, "to": "diarisation_pending"},
            )
        delete_source_segments(conn, project_id, source_id)
        conn.execute(
            "UPDATE sources SET status='diarisation_pending', diarisation_error=NULL, updated_at=? WHERE id=?",
            (now, source_id),
        )
        conn.commit()
        job_id = enqueue(project_id, "diarisation", source_id=source_id)
        enqueued.append({"id": job_id, "type": "diarisation", "source_id": source_id})

    # Mark any prior export stale; its recompute derives 'processing' from the
    # queued jobs.
    invalidate_export(project_id)

    return {"enqueued_jobs": enqueued}


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


@router.post("/transcription/run", status_code=202)
async def run_transcription(project_id: str):
    require_project(project_id)

    result = enqueue_bulk_transcription(project_id)
    if result is None:
        raise AppError(
            409, "no_segments_to_transcribe",
            "No pending segments without transcripts.",
        )
    job_id, segment_count = result

    return {"enqueued_job": {"id": job_id, "type": "transcription_bulk", "segment_count": segment_count}}


@router.post("/segments/{segment_id}/transcription/rerun", status_code=202)
async def rerun_segment_transcription(project_id: str, segment_id: str):
    conn = require_project(project_id)

    segment = conn.execute(
        "SELECT id FROM segments WHERE id=? AND project_id=?", (segment_id, project_id)
    ).fetchone()
    if segment is None:
        raise AppError(404, "not_found", "Segment not found.")

    params = {"segment_ids": [segment_id]}
    job_id = enqueue(project_id, "transcription_segment", params=params)

    return {"enqueued_job": {"id": job_id, "type": "transcription_segment"}}


# ---------------------------------------------------------------------------
# Jobs list
# ---------------------------------------------------------------------------


@router.get("/jobs")
async def list_jobs(project_id: str, status: Optional[str] = None, limit: int = 20):
    conn = require_project(project_id)

    if status:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE project_id=? AND status=? ORDER BY created_at DESC LIMIT ?",
            (project_id, status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()

    jobs_out = []
    for r in rows:
        d = dict(r)
        if "progress_detail" in d:
            d["progress_detail"] = json.loads(d["progress_detail"]) if d["progress_detail"] else None
        jobs_out.append(d)
    return {"jobs": jobs_out}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@router.post("/export", status_code=202)
async def trigger_export(project_id: str):
    conn = require_project(project_id)

    approved_count = conn.execute(
        f"SELECT COUNT(*) FROM segments WHERE project_id=? AND status IN ({sql_status_list(EXPORTABLE_STATUSES)})",
        (project_id,),
    ).fetchone()[0]

    if approved_count == 0:
        raise AppError(
            409, "no_approved_segments",
            "There are no approved segments to export.",
        )

    # Guard against a concurrent second export.
    active_export = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE project_id=? AND type='export' AND status IN ('queued','running')",
        (project_id,),
    ).fetchone()[0]
    if active_export > 0:
        raise AppError(
            409, "export_in_progress",
            "An export is already running for this project.",
        )

    # Export is only valid from review or exported (re-export). Any other state
    # means the pipeline hasn't finished or a job is running.
    status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"]
    if status not in ("review", "exported"):
        raise AppError(
            409, "invalid_project_state",
            f"Cannot export a project in status '{status}'. Finish processing and review first.",
            {"status": status},
        )

    job_id = enqueue(project_id, "export", params={"segment_count": approved_count})

    # The queued export job drives the derived status to 'exporting'.
    recompute_project_status(project_id)

    return {"enqueued_job": {"id": job_id, "type": "export", "segment_count": approved_count}}


@router.get("/export/download")
async def download_export(project_id: str):
    from fastapi.responses import FileResponse
    from db import project_dir

    conn = require_project(project_id)
    p = conn.execute("SELECT name, status FROM projects WHERE id=?", (project_id,)).fetchone()
    if p["status"] != "exported":
        raise AppError(404, "export_not_ready", "Export has not completed.")

    pdir = project_dir(project_id)
    archive = pdir / "export.tar.gz"
    if not archive.exists():
        raise AppError(404, "export_not_found", "Export archive not found.")

    safe_name = p["name"].replace(" ", "_").replace("/", "-")
    return FileResponse(
        str(archive),
        media_type="application/gzip",
        filename=f"{safe_name}_export.tar.gz",
    )
