"""Project status recomputation — shared by jobs and routers."""

from datetime import datetime, timezone

from db import get_conn
from state_machines import compute_project_status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def recompute_project_status(project_id: str) -> None:
    """Derive and persist project status from current DB state.

    Called after every job completion and user action that may affect
    project status (segment review, source deletion, etc.).
    """
    conn = get_conn(project_id)
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        return

    sources = conn.execute("SELECT status FROM sources WHERE project_id=?", (project_id,)).fetchall()
    active_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE project_id=? AND status IN ('queued','running')",
        (project_id,),
    ).fetchone()[0]

    has_sources = len(sources) > 0
    has_active_jobs = active_jobs > 0
    all_sources_complete = has_sources and all(s["status"] == "complete" for s in sources)
    completed_export = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE project_id=? AND type='export' AND status='complete'",
        (project_id,),
    ).fetchone()[0]
    export_complete = completed_export > 0

    new_status = compute_project_status(
        project["status"], has_sources, has_active_jobs, all_sources_complete, export_complete
    )

    conn.execute(
        "UPDATE projects SET status=?, updated_at=? WHERE id=?",
        (new_status, _now(), project_id),
    )
    conn.commit()
