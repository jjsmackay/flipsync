"""Project status recomputation — shared by jobs and routers."""

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

    # A project is 'exported' only when a completed export recorded exported_at
    # AND the archive is still on disk. exported_at is cleared whenever approvals
    # or sources change (see invalidate_export), so a stale archive no longer
    # holds the project in 'exported' — it falls back to 'review'/'ready' per the
    # spec's exported -> review / exported -> processing transitions.
    pdir = project_dir(project_id)
    archive_exists = (pdir / "export.tar.gz").exists()
    export_complete = archive_exists and project["exported_at"] is not None

    new_status = compute_project_status(
        project["status"], has_sources, has_active_jobs, all_sources_complete, export_complete
    )

    if new_status != project["status"]:
        conn.execute(
            "UPDATE projects SET status=?, updated_at=? WHERE id=?",
            (new_status, utc_now(), project_id),
        )
        conn.commit()


def invalidate_export(project_id: str) -> None:
    """Mark any existing export as stale and recompute project status.

    Called after user actions that change the approved segment set, transcripts,
    or the source list. Clearing exported_at means recompute_project_status will
    no longer report 'exported', so the project returns to 'review' (or
    'processing' if jobs are active).
    """
    conn = get_conn(project_id)
    conn.execute(
        "UPDATE projects SET exported_at=NULL WHERE id=? AND exported_at IS NOT NULL",
        (project_id,),
    )
    conn.commit()
    recompute_project_status(project_id)
