"""Project status recomputation — shared by jobs and routers."""

from pathlib import Path

from db import get_conn, project_dir, utc_now
from state_machines import compute_project_status


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

    # Fix for deferred bug: check for actual export archive existence rather than
    # circular status check. A completed export job means the archive exists.
    pdir = project_dir(project_id)
    archive_exists = (pdir / "export.tar.gz").exists()
    export_complete = archive_exists and not has_active_jobs

    new_status = compute_project_status(
        project["status"], has_sources, has_active_jobs, all_sources_complete, export_complete
    )

    if new_status != project["status"]:
        conn.execute(
            "UPDATE projects SET status=?, updated_at=? WHERE id=?",
            (new_status, utc_now(), project_id),
        )
        conn.commit()
