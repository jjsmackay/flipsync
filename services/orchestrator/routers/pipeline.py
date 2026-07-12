"""Pipeline control endpoints: start, reprocess, transcription triggers."""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from db import project_dir, require_project, utc_now
from errors import AppError
from jobs import enqueue
from state_machines import validate_source_transition
from status import invalidate_export

router = APIRouter(prefix="/projects/{project_id}", tags=["pipeline"])


# ---------------------------------------------------------------------------
# Pipeline start
# ---------------------------------------------------------------------------


@router.post("/pipeline/start", status_code=202)
async def start_pipeline(project_id: str):
    conn = require_project(project_id)

    pending_sources = conn.execute(
        "SELECT id FROM sources WHERE project_id=? AND status='step1_pending'",
        (project_id,),
    ).fetchall()

    if not pending_sources:
        raise AppError(
            409, "no_pending_sources",
            "No sources in step1_pending status. Upload sources or check source status.",
        )

    # Step 2 (diarisation) needs a reference clip. Fail fast at start rather
    # than leaving sources stranded in step2_pending after step 1 succeeds.
    project = conn.execute("SELECT reference_path FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project["reference_path"]:
        raise AppError(
            409, "no_reference_clip",
            "Upload a reference clip before starting the pipeline; diarisation needs it to match the target speaker.",
        )

    enqueued = []
    for s in pending_sources:
        job_id = enqueue(project_id, "vocal_separation", source_id=s["id"])
        enqueued.append({"id": job_id, "type": "vocal_separation", "source_id": s["id"]})

    conn.execute(
        "UPDATE projects SET status='processing', updated_at=? WHERE id=?",
        (utc_now(), project_id),
    )
    conn.commit()

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

    valid_step_combos = [["step1"], ["step2"], ["step1", "step2"]]
    if body.steps not in valid_step_combos:
        raise AppError(
            422, "invalid_steps",
            "steps must be ['step1'], ['step2'], or ['step1', 'step2'].",
        )

    # Check for approved segments that would be invalidated. Re-running either
    # step deletes and recreates this source's segments, so both discard
    # approvals; the message names the steps actually requested.
    approved_count = conn.execute(
        "SELECT COUNT(*) FROM segments WHERE source_id=? AND status IN ('approved', 'auto_approved')",
        (source_id,),
    ).fetchone()[0]
    if approved_count > 0 and not body.confirm:
        step_label = {
            ("step1",): "step 1",
            ("step2",): "step 2",
            ("step1", "step2"): "steps 1 and 2",
        }[tuple(body.steps)]
        raise AppError(
            409, "would_invalidate_approvals",
            f"Re-running {step_label} will discard {approved_count} approved segments from this source.",
            {"approved_count": approved_count},
        )

    enqueued = []
    now = utc_now()
    current_status = source["status"]

    if "step1" in body.steps:
        if not validate_source_transition(current_status, "step1_pending"):
            raise AppError(
                409, "invalid_source_status",
                f"Cannot reprocess step 1 from source status '{current_status}'.",
                {"from": current_status, "to": "step1_pending"},
            )
        _delete_source_segments(conn, project_id, source_id)
        conn.execute(
            "UPDATE sources SET status='step1_pending', vocals_path=NULL, step1_error=NULL, step2_error=NULL, updated_at=? WHERE id=?",
            (now, source_id),
        )
        conn.commit()
        params = {}
        if "demucs_model" in body.params:
            params["demucs_model"] = body.params["demucs_model"]
        job_id = enqueue(project_id, "vocal_separation", source_id=source_id, params=params)
        enqueued.append({"id": job_id, "type": "vocal_separation", "source_id": source_id})

    elif "step2" in body.steps:
        if not validate_source_transition(current_status, "step2_pending"):
            raise AppError(
                409, "invalid_source_status",
                f"Cannot reprocess step 2 from source status '{current_status}'.",
                {"from": current_status, "to": "step2_pending"},
            )
        _delete_source_segments(conn, project_id, source_id)
        conn.execute(
            "UPDATE sources SET status='step2_pending', step2_error=NULL, updated_at=? WHERE id=?",
            (now, source_id),
        )
        conn.commit()
        job_id = enqueue(project_id, "diarisation", source_id=source_id)
        enqueued.append({"id": job_id, "type": "diarisation", "source_id": source_id})

    # Update project status to processing and mark any prior export stale.
    conn.execute(
        "UPDATE projects SET status='processing', updated_at=? WHERE id=?",
        (now, project_id),
    )
    conn.commit()
    invalidate_export(project_id)

    return {"enqueued_jobs": enqueued}


def _delete_source_segments(conn, project_id: str, source_id: str) -> None:
    """Delete a source's segment rows and their on-disk WAVs (raw + export)."""
    pdir = project_dir(project_id)
    rows = conn.execute(
        "SELECT raw_path, export_path FROM segments WHERE source_id=?", (source_id,)
    ).fetchall()
    for row in rows:
        for rel in (row["raw_path"], row["export_path"]):
            if rel:
                f = pdir / rel
                if f.exists():
                    f.unlink()
    conn.execute("DELETE FROM segments WHERE source_id=?", (source_id,))


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


@router.post("/transcription/run", status_code=202)
async def run_transcription(project_id: str):
    conn = require_project(project_id)

    segments = conn.execute(
        """
        SELECT id, raw_path FROM segments
        WHERE project_id=? AND status IN ('pending','maybe') AND transcript IS NULL
        """,
        (project_id,),
    ).fetchall()

    if not segments:
        raise AppError(
            409, "no_segments_to_transcribe",
            "No pending segments without transcripts.",
        )

    project = conn.execute("SELECT whisper_model, language FROM projects WHERE id=?", (project_id,)).fetchone()
    params = {
        "segment_ids": [s["id"] for s in segments],
        "model": project["whisper_model"],
        "language": project["language"],
    }
    job_id = enqueue(project_id, "transcription_bulk", params=params)

    return {"enqueued_job": {"id": job_id, "type": "transcription_bulk", "segment_count": len(segments)}}


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

    return {"jobs": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@router.post("/export", status_code=202)
async def trigger_export(project_id: str):
    conn = require_project(project_id)

    approved_count = conn.execute(
        "SELECT COUNT(*) FROM segments WHERE project_id=? AND status IN ('approved','auto_approved')",
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

    conn.execute(
        "UPDATE projects SET status='exporting', updated_at=? WHERE id=?",
        (utc_now(), project_id),
    )
    conn.commit()

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
