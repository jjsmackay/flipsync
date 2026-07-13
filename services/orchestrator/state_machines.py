"""State machine definitions for segments, sources, and projects.

All valid transitions are explicitly listed. The orchestrator rejects any
transition not in these sets with HTTP 409.
"""

# ---------------------------------------------------------------------------
# Segment status transitions
# ---------------------------------------------------------------------------

SEGMENT_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"approved", "rejected", "maybe", "below_threshold"},
    "maybe": {"approved", "rejected", "pending"},
    "approved": {"rejected", "maybe", "clipping_warning"},
    # NOTE: nothing transitions INTO auto_approved via this table — only the
    # system assigns it (directly, when transcription results land or on
    # auto-approve re-evaluation). PATCH /segments 409s any request to set it.
    "auto_approved": {"approved", "rejected", "maybe", "pending", "clipping_warning"},
    "below_threshold": {"pending"},
    "clipping_warning": {"approved", "rejected"},
    # rejected can return to pending (user un-rejects; misclick recovery).
    "rejected": {"pending"},
    # Terminal state — no outgoing transitions. Records a fact about the
    # audio (silent after trimming), not a reviewer decision, so it is not
    # eligible for the rejected -> pending undo.
    "auto_rejected": set(),
}

# Transitions valid from bulk actions (subset of all transitions)
# Maps action name -> allowed source statuses
BULK_ACTION_SOURCES: dict[str, set[str]] = {
    "approve": {"pending", "maybe", "clipping_warning", "auto_approved"},
    "reject": {"pending", "maybe", "clipping_warning", "approved", "auto_approved"},
    "maybe": {"pending", "approved", "auto_approved"},
    "pending": {"maybe", "auto_approved", "rejected"},
}


def validate_segment_transition(from_status: str, to_status: str) -> bool:
    allowed = SEGMENT_TRANSITIONS.get(from_status, set())
    return to_status in allowed


# Statuses counted as approved (auto_approved is system-assigned but treated
# identically for export and duration stats), and the superset the export
# actually ships — segments a previous export flagged clipping_warning stay in
# the archive until re-reviewed. Interpolate via sql_status_list so every
# selector stays in lockstep; the frontend mirrors these in src/constants.ts.
APPROVED_STATUSES: tuple[str, ...] = ("approved", "auto_approved")
EXPORTABLE_STATUSES: tuple[str, ...] = APPROVED_STATUSES + ("clipping_warning",)


def sql_status_list(statuses: tuple[str, ...]) -> str:
    """Render a status tuple as a quoted SQL IN-list fragment."""
    return ",".join(f"'{s}'" for s in statuses)


# ---------------------------------------------------------------------------
# Source status transitions
# ---------------------------------------------------------------------------

SOURCE_TRANSITIONS: dict[str, set[str]] = {
    "uploaded": {"extracting"},
    "extracting": {"separation_pending", "extraction_failed"},
    "extraction_failed": set(),  # terminal — user must delete and re-upload
    "separation_pending": {"separation_running"},
    "separation_running": {"diarisation_pending", "separation_failed"},
    "separation_failed": {"separation_pending"},
    "diarisation_pending": {"diarisation_running"},
    "diarisation_running": {"complete", "diarisation_failed"},
    "diarisation_failed": {"diarisation_pending"},
    "complete": {"separation_pending", "diarisation_pending"},
}


def validate_source_transition(from_status: str, to_status: str) -> bool:
    allowed = SOURCE_TRANSITIONS.get(from_status, set())
    return to_status in allowed


# ---------------------------------------------------------------------------
# Project status computation (derived, not a direct transition table)
# ---------------------------------------------------------------------------

PROJECT_STATUSES = {
    "new",
    "ready",
    "processing",
    "review",
    "awaiting_reference",
    "exporting",
    "exported",
}


def compute_project_status(
    active_job_types: frozenset[str] | set[str],
    has_sources: bool,
    all_sources_complete: bool,
    export_complete: bool,
    reference_set: bool = True,
    has_diarisation_pending: bool = False,
) -> str:
    """Compute project status from observed state.

    This is called after each job completion or user action; it does not
    enforce transitions, it derives the correct status from facts.
    ``active_job_types`` is the set of queued/running job types, voice jobs
    already excluded (they never drive project status).

    Precedence: exporting (an active export job) → processing (any other
    active job, incl. a running scout) → review (all sources complete) →
    awaiting_reference (no reference yet, step 1 done on ≥1 source) → ready.
    """
    if "export" in active_job_types:
        return "exporting"
    if active_job_types:
        return "processing"

    if export_complete:
        return "exported"

    if not has_sources:
        return "new"

    if all_sources_complete:
        return "review"

    if not reference_set and has_diarisation_pending:
        return "awaiting_reference"

    return "ready"
