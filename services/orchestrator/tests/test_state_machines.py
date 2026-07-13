"""State machine transition tests — segment, source, and project status machines."""

import pytest
from state_machines import (
    validate_segment_transition,
    validate_source_transition,
    compute_project_status,
    SEGMENT_TRANSITIONS,
    SOURCE_TRANSITIONS,
    BULK_ACTION_SOURCES,
)


# ---------------------------------------------------------------------------
# Segment state machine
# ---------------------------------------------------------------------------

class TestSegmentTransitions:
    # Valid transitions
    def test_pending_to_approved(self):
        assert validate_segment_transition("pending", "approved") is True

    def test_pending_to_rejected(self):
        assert validate_segment_transition("pending", "rejected") is True

    def test_pending_to_maybe(self):
        assert validate_segment_transition("pending", "maybe") is True

    def test_pending_to_below_threshold(self):
        assert validate_segment_transition("pending", "below_threshold") is True

    def test_maybe_to_approved(self):
        assert validate_segment_transition("maybe", "approved") is True

    def test_maybe_to_rejected(self):
        assert validate_segment_transition("maybe", "rejected") is True

    def test_maybe_to_pending(self):
        assert validate_segment_transition("maybe", "pending") is True

    def test_approved_to_rejected(self):
        assert validate_segment_transition("approved", "rejected") is True

    def test_approved_to_maybe(self):
        assert validate_segment_transition("approved", "maybe") is True

    def test_approved_to_clipping_warning(self):
        assert validate_segment_transition("approved", "clipping_warning") is True

    def test_below_threshold_to_pending(self):
        assert validate_segment_transition("below_threshold", "pending") is True

    def test_clipping_warning_to_approved(self):
        assert validate_segment_transition("clipping_warning", "approved") is True

    def test_clipping_warning_to_rejected(self):
        assert validate_segment_transition("clipping_warning", "rejected") is True

    def test_rejected_to_pending_is_valid(self):
        """Misclick recovery: a rejected segment can be un-rejected back to pending."""
        assert validate_segment_transition("rejected", "pending") is True

    # Invalid transitions
    def test_rejected_to_approved_is_invalid(self):
        assert validate_segment_transition("rejected", "approved") is False

    def test_rejected_to_maybe_is_invalid(self):
        assert validate_segment_transition("rejected", "maybe") is False

    def test_auto_rejected_is_terminal(self):
        assert validate_segment_transition("auto_rejected", "pending") is False
        assert validate_segment_transition("auto_rejected", "approved") is False
        assert validate_segment_transition("auto_rejected", "rejected") is False

    def test_below_threshold_to_approved_is_invalid(self):
        assert validate_segment_transition("below_threshold", "approved") is False

    def test_below_threshold_to_rejected_is_invalid(self):
        assert validate_segment_transition("below_threshold", "rejected") is False

    def test_pending_to_clipping_warning_is_invalid(self):
        assert validate_segment_transition("pending", "clipping_warning") is False

    def test_pending_to_auto_rejected_is_invalid(self):
        assert validate_segment_transition("pending", "auto_rejected") is False

    def test_unknown_from_status_returns_false(self):
        assert validate_segment_transition("nonexistent_status", "approved") is False

    def test_approved_to_below_threshold_is_invalid(self):
        assert validate_segment_transition("approved", "below_threshold") is False

    def test_all_listed_valid_transitions_pass(self):
        """Exhaustive check: every transition in SEGMENT_TRANSITIONS validates."""
        for from_status, targets in SEGMENT_TRANSITIONS.items():
            for to_status in targets:
                assert validate_segment_transition(from_status, to_status), \
                    f"Expected {from_status} -> {to_status} to be valid"

    def test_rejected_only_transitions_to_pending(self):
        assert SEGMENT_TRANSITIONS["rejected"] == {"pending"}

    def test_auto_rejected_has_no_valid_transitions(self):
        """auto_rejected records a fact about the audio (silent after trim),
        not a reviewer decision — it stays terminal even though rejected
        can now be un-done."""
        assert SEGMENT_TRANSITIONS["auto_rejected"] == set()
        for to_status in ("pending", "approved", "rejected", "maybe"):
            assert validate_segment_transition("auto_rejected", to_status) is False


# ---------------------------------------------------------------------------
# Source state machine
# ---------------------------------------------------------------------------

class TestSourceTransitions:
    # Valid transitions
    def test_uploaded_to_extracting(self):
        assert validate_source_transition("uploaded", "extracting") is True

    def test_extracting_to_separation_pending(self):
        assert validate_source_transition("extracting", "separation_pending") is True

    def test_extracting_to_extraction_failed(self):
        assert validate_source_transition("extracting", "extraction_failed") is True

    def test_separation_pending_to_separation_running(self):
        assert validate_source_transition("separation_pending", "separation_running") is True

    def test_separation_running_to_diarisation_pending(self):
        assert validate_source_transition("separation_running", "diarisation_pending") is True

    def test_separation_running_to_separation_failed(self):
        assert validate_source_transition("separation_running", "separation_failed") is True

    def test_separation_failed_to_separation_pending(self):
        assert validate_source_transition("separation_failed", "separation_pending") is True

    def test_diarisation_pending_to_diarisation_running(self):
        assert validate_source_transition("diarisation_pending", "diarisation_running") is True

    def test_diarisation_running_to_complete(self):
        assert validate_source_transition("diarisation_running", "complete") is True

    def test_diarisation_running_to_diarisation_failed(self):
        assert validate_source_transition("diarisation_running", "diarisation_failed") is True

    def test_diarisation_failed_to_diarisation_pending(self):
        assert validate_source_transition("diarisation_failed", "diarisation_pending") is True

    def test_complete_to_separation_pending(self):
        assert validate_source_transition("complete", "separation_pending") is True

    def test_complete_to_diarisation_pending(self):
        assert validate_source_transition("complete", "diarisation_pending") is True

    # Invalid transitions
    def test_extraction_failed_is_terminal(self):
        assert validate_source_transition("extraction_failed", "extracting") is False
        assert validate_source_transition("extraction_failed", "separation_pending") is False
        assert validate_source_transition("extraction_failed", "uploaded") is False

    def test_uploaded_to_complete_is_invalid(self):
        assert validate_source_transition("uploaded", "complete") is False

    def test_complete_to_uploaded_is_invalid(self):
        assert validate_source_transition("complete", "uploaded") is False

    def test_separation_running_to_uploaded_is_invalid(self):
        assert validate_source_transition("separation_running", "uploaded") is False

    def test_unknown_status_returns_false(self):
        assert validate_source_transition("made_up_status", "complete") is False

    def test_all_listed_valid_transitions_pass(self):
        for from_status, targets in SOURCE_TRANSITIONS.items():
            for to_status in targets:
                assert validate_source_transition(from_status, to_status), \
                    f"Expected {from_status} -> {to_status} to be valid"

    def test_extraction_failed_has_no_exits(self):
        assert SOURCE_TRANSITIONS["extraction_failed"] == set()


# ---------------------------------------------------------------------------
# Project status computation
# ---------------------------------------------------------------------------

class TestProjectStatusComputation:
    def test_no_sources_is_new(self):
        assert compute_project_status(frozenset(), False, False, False) == "new"

    def test_has_sources_no_jobs_not_complete_is_ready(self):
        assert compute_project_status(frozenset(), True, False, False) == "ready"

    def test_has_active_jobs_is_processing(self):
        assert compute_project_status({"vocal_separation"}, True, False, False) == "processing"

    def test_all_sources_complete_no_active_jobs_is_review(self):
        assert compute_project_status(frozenset(), True, True, False) == "review"

    def test_export_complete_is_exported(self):
        assert compute_project_status(frozenset(), True, True, True) == "exported"

    def test_active_export_job_is_exporting(self):
        # An active export job derives 'exporting' — even alongside other jobs.
        assert compute_project_status({"export"}, True, False, False) == "exporting"
        assert compute_project_status({"export", "extract_audio"}, True, False, False) == "exporting"

    def test_active_jobs_override_all_else(self):
        assert compute_project_status({"diarisation"}, True, True, False) == "processing"


# ---------------------------------------------------------------------------
# Bulk action source status allowlists
# ---------------------------------------------------------------------------

class TestBulkActionSources:
    def test_approve_allowed_statuses(self):
        assert "pending" in BULK_ACTION_SOURCES["approve"]
        assert "maybe" in BULK_ACTION_SOURCES["approve"]
        assert "clipping_warning" in BULK_ACTION_SOURCES["approve"]
        assert "rejected" not in BULK_ACTION_SOURCES["approve"]
        assert "auto_rejected" not in BULK_ACTION_SOURCES["approve"]

    def test_reject_allowed_statuses(self):
        assert "pending" in BULK_ACTION_SOURCES["reject"]
        assert "maybe" in BULK_ACTION_SOURCES["reject"]
        assert "approved" in BULK_ACTION_SOURCES["reject"]

    def test_pending_action_allowed_statuses(self):
        assert "maybe" in BULK_ACTION_SOURCES["pending"]
        assert "auto_approved" in BULK_ACTION_SOURCES["pending"]
        assert "rejected" in BULK_ACTION_SOURCES["pending"]
        assert "approved" not in BULK_ACTION_SOURCES["pending"]
        assert "below_threshold" not in BULK_ACTION_SOURCES["pending"]


# ---------------------------------------------------------------------------
# auto_approved segment status
# ---------------------------------------------------------------------------

class TestAutoApprovedTransitions:
    def test_auto_approved_exits(self):
        for target in ("approved", "rejected", "maybe", "pending", "clipping_warning"):
            assert validate_segment_transition("auto_approved", target) is True

    def test_nothing_enters_auto_approved_via_table(self):
        """The system assigns auto_approved directly; the transition table has
        no path into it, so user PATCHes always 409."""
        for from_status in SEGMENT_TRANSITIONS:
            assert validate_segment_transition(from_status, "auto_approved") is False

    def test_bulk_approve_includes_auto_approved(self):
        assert "auto_approved" in BULK_ACTION_SOURCES["approve"]

    def test_bulk_reject_includes_auto_approved(self):
        assert "auto_approved" in BULK_ACTION_SOURCES["reject"]

    def test_bulk_maybe_includes_auto_approved(self):
        assert "auto_approved" in BULK_ACTION_SOURCES["maybe"]

    def test_bulk_pending_includes_auto_approved(self):
        assert BULK_ACTION_SOURCES["pending"] == {"maybe", "auto_approved", "rejected"}
