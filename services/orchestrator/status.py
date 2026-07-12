"""Project status recomputation and auto-approve re-evaluation — shared by
jobs and routers."""

import sqlite3

from db import get_conn, project_dir, utc_now
from state_machines import compute_project_status

# Auto-approve eligibility (spec/pipeline.md §Auto-approval), minus the two
# project-level conditions (auto_approve_enabled, status='pending') which the
# callers below apply. Parameters, in order:
#   1. max(match_threshold, auto_approve_match_threshold)
#   2. auto_approve_transcript_threshold
# The IS NOT NULL guards keep three-valued logic honest so that NOT(<this>)
# in the demotion query is TRUE (not NULL) for rows with missing confidences.
_AUTO_APPROVE_CONDITIONS = """(
    COALESCE(transcript_edited, transcript) IS NOT NULL
    AND COALESCE(transcript_edited, transcript) != ''
    AND match_confidence >= ?
    AND transcript_confidence IS NOT NULL
    AND transcript_confidence >= ?
    AND (flags IS NULL OR flags='' OR flags='[]')
    AND clipping_warning = 0
)"""


def _auto_approve_config(conn: sqlite3.Connection, project_id: str):
    return conn.execute(
        """SELECT auto_approve_enabled, auto_approve_match_threshold,
                  auto_approve_transcript_threshold, match_threshold
           FROM projects WHERE id=?""",
        (project_id,),
    ).fetchone()


def auto_approve_promote(
    conn: sqlite3.Connection,
    project_id: str,
    now: str,
    segment_ids: list[str] | None = None,
) -> int:
    """Move eligible `pending` segments to `auto_approved`.

    Optionally restricted to segment_ids (used when transcription results
    land). Does NOT commit — the caller owns the transaction. Returns the
    number of rows changed.
    """
    cfg = _auto_approve_config(conn, project_id)
    if cfg is None or not cfg["auto_approve_enabled"]:
        return 0
    min_match = max(cfg["match_threshold"], cfg["auto_approve_match_threshold"])
    sql = f"""
        UPDATE segments SET status='auto_approved', updated_at=?
        WHERE project_id=? AND status='pending' AND {_AUTO_APPROVE_CONDITIONS}
    """
    params: list = [now, project_id, min_match, cfg["auto_approve_transcript_threshold"]]
    if segment_ids is not None:
        if not segment_ids:
            return 0
        sql += f" AND id IN ({','.join('?' * len(segment_ids))})"
        params.extend(segment_ids)
    return conn.execute(sql, params).rowcount


def auto_approve_demote(conn: sqlite3.Connection, project_id: str, now: str) -> int:
    """Move `auto_approved` segments that no longer meet the eligibility rule
    back to `pending`. Disabling auto-approve demotes all of them.

    Does NOT commit — the caller owns the transaction. Returns rows changed.
    """
    cfg = _auto_approve_config(conn, project_id)
    if cfg is None:
        return 0
    if not cfg["auto_approve_enabled"]:
        return conn.execute(
            "UPDATE segments SET status='pending', updated_at=? WHERE project_id=? AND status='auto_approved'",
            (now, project_id),
        ).rowcount
    min_match = max(cfg["match_threshold"], cfg["auto_approve_match_threshold"])
    return conn.execute(
        f"""
        UPDATE segments SET status='pending', updated_at=?
        WHERE project_id=? AND status='auto_approved' AND NOT {_AUTO_APPROVE_CONDITIONS}
        """,
        (now, project_id, min_match, cfg["auto_approve_transcript_threshold"]),
    ).rowcount


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
    reference_set = project["reference_path"] is not None
    has_step2_pending = any(s["status"] == "step2_pending" for s in sources)

    # A project is 'exported' only when a completed export recorded exported_at
    # AND the archive is still on disk. exported_at is cleared whenever approvals
    # or sources change (see invalidate_export), so a stale archive no longer
    # holds the project in 'exported' — it falls back to 'review'/'ready' per the
    # spec's exported -> review / exported -> processing transitions.
    pdir = project_dir(project_id)
    archive_exists = (pdir / "export.tar.gz").exists()
    export_complete = archive_exists and project["exported_at"] is not None

    new_status = compute_project_status(
        project["status"], has_sources, has_active_jobs, all_sources_complete, export_complete,
        reference_set=reference_set, has_step2_pending=has_step2_pending,
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
