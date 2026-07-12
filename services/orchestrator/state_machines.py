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
    current_status: str,
    has_sources: bool,
    has_active_jobs: bool,
    all_sources_complete: bool,
    export_complete: bool,
    reference_set: bool = True,
    has_diarisation_pending: bool = False,
) -> str:
    """Compute project status from observed state.

    This is called after each job completion or user action; it does not
    enforce transitions, it derives the correct status from facts.

    Precedence: processing (any active job, incl. a running scout) → review
    (all sources complete) → awaiting_reference (no reference yet, step 1 done
    on ≥1 source) → ready.
    """
    if has_active_jobs:
        if current_status == "exporting":
            return "exporting"
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
