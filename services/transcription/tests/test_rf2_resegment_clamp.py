"""Review-fix wave 2 — resegment boundary clamping (Worker A, item A4).

Whisper word timestamps can overshoot the parent WAV's real duration (or,
pathologically, precede 0.0). Unclamped, an overshooting last utterance
inverted the final (start, end) pair, producing a negative-duration child
row and a zero-frame WAV. Boundaries are now clamped to [0, file_duration]
and forced non-decreasing, and children collapsed to near-zero length merge
into a neighbour instead of being emitted.
"""

import os

import pytest

from resegment import (
    MIN_CHILD_SECS,
    compute_boundaries,
    merge_degenerate_children,
)
from transcriber import transcribe_segment
from tests.test_resegment import W, _model_with_words


# ---------------------------------------------------------------------------
# compute_boundaries clamping
# ---------------------------------------------------------------------------

class TestBoundaryClamping:
    def test_overshooting_last_utterance_does_not_invert(self):
        # Midpoint of the gap (5.4 -> 5.6) is 5.5, past the 5.0 s file end.
        u1 = [W(" One.", 0.1, 5.4)]
        u2 = [W(" Two", 5.6, 6.0)]
        bounds = compute_boundaries([u1, u2], file_duration=5.0)
        assert bounds == [(0.0, 5.0), (5.0, 5.0)]
        for start, end in bounds:
            assert end >= start

    def test_all_edges_within_file_and_non_decreasing(self):
        # Second midpoint (3.2) would fall below the first (4.1) — forced
        # non-decreasing; nothing may leave [0, file_duration].
        u1 = [W(" a.", 0.0, 4.0)]
        u2 = [W(" b.", 4.2, 4.4)]
        u3 = [W(" c", 2.0, 6.5)]
        bounds = compute_boundaries([u1, u2, u3], file_duration=5.0)
        edges = [bounds[0][0]] + [end for _, end in bounds]
        assert edges == sorted(edges)
        assert all(0.0 <= e <= 5.0 for e in edges)
        assert bounds[0][0] == 0.0
        assert bounds[-1][1] == 5.0

    def test_negative_midpoint_clamped_to_zero(self):
        u1 = [W(" a.", -1.0, -0.6)]
        u2 = [W(" b", -0.2, 3.0)]
        bounds = compute_boundaries([u1, u2], file_duration=5.0)
        assert bounds == [(0.0, 0.0), (0.0, 5.0)]

    def test_exact_boundary_untouched(self):
        # Midpoint exactly at the file end is legal and unchanged.
        u1 = [W(" a.", 0.1, 4.9)]
        u2 = [W(" b", 5.1, 5.5)]
        bounds = compute_boundaries([u1, u2], file_duration=5.0)
        assert bounds == [(0.0, 5.0), (5.0, 5.0)]

    def test_in_range_midpoints_unchanged(self):
        # Regression guard: normal inputs keep the old midpoint math.
        u1 = [W(" one.", 0.2, 1.4)]
        u2 = [W(" two", 2.4, 4.0)]
        bounds = compute_boundaries([u1, u2], file_duration=5.0)
        assert bounds == [(0.0, 1.9), (1.9, 5.0)]


# ---------------------------------------------------------------------------
# merge_degenerate_children
# ---------------------------------------------------------------------------

class TestMergeDegenerateChildren:
    def test_zero_length_last_child_merges_into_previous(self):
        u1 = [W(" One.", 0.1, 1.0)]
        u2 = [W(" Two", 5.6, 6.0)]
        bounds = [(0.0, 5.0), (5.0, 5.0)]
        utts, merged = merge_degenerate_children([u1, u2], bounds)
        assert merged == [(0.0, 5.0)]
        assert [w.word for w in utts[0]] == [" One.", " Two"]

    def test_zero_length_first_child_merges_into_next(self):
        u1 = [W(" a.", -1.0, -0.6)]
        u2 = [W(" b", -0.2, 3.0)]
        bounds = [(0.0, 0.0), (0.0, 5.0)]
        utts, merged = merge_degenerate_children([u1, u2], bounds)
        assert merged == [(0.0, 5.0)]
        assert [w.word for w in utts[0]] == [" a.", " b"]

    def test_near_zero_below_threshold_merges(self):
        u1 = [W(" a.", 0.0, 2.0)]
        u2 = [W(" b", 4.99, 5.2)]
        eps = MIN_CHILD_SECS / 2
        bounds = [(0.0, 5.0 - eps), (5.0 - eps, 5.0)]
        utts, merged = merge_degenerate_children([u1, u2], bounds)
        assert len(merged) == 1
        assert merged[0] == (0.0, 5.0)

    def test_all_degenerate_collapses_to_single_child(self):
        u1 = [W(" a.", 0.0, 0.01)]
        u2 = [W(" b", 0.01, 0.02)]
        bounds = [(0.0, 0.01), (0.01, 0.02)]
        utts, merged = merge_degenerate_children([u1, u2], bounds)
        assert merged == [(0.0, 0.02)]
        assert [w.word for w in utts[0]] == [" a.", " b"]

    def test_healthy_children_untouched(self):
        u1 = [W(" a.", 0.0, 1.5)]
        u2 = [W(" b", 2.0, 4.5)]
        bounds = [(0.0, 1.75), (1.75, 5.0)]
        utts, merged = merge_degenerate_children([u1, u2], bounds)
        assert merged == bounds
        assert utts == [u1, u2]

    def test_contiguity_preserved_after_merge(self):
        u1 = [W(" a.", 0.0, 1.0)]
        u2 = [W(" b.", 1.2, 4.9)]
        u3 = [W(" c", 5.5, 6.6)]
        bounds = compute_boundaries([u1, u2, u3], file_duration=5.0)
        _, merged = merge_degenerate_children([u1, u2, u3], bounds)
        assert merged[0][0] == 0.0
        assert merged[-1][1] == 5.0
        for (_, prev_end), (next_start, _) in zip(merged, merged[1:]):
            assert prev_end == next_start


# ---------------------------------------------------------------------------
# transcribe_segment end-to-end with overshooting timestamps
# ---------------------------------------------------------------------------

class TestTranscribeSegmentClamp:
    def test_last_utterance_overshoot_merges_text_into_previous_child(self, write_wav):
        parent = write_wav(duration_secs=5.0)
        # Three sentences; the last overshoots the 5.0 s file, so its
        # midpoint clamps to 5.0 and its child collapses -> merged back.
        words = [
            W(" One.", 0.1, 1.2, probability=0.9),
            W(" Two.", 2.0, 4.9, probability=0.9),
            W(" Three", 5.5, 6.6, probability=0.9),
        ]
        model = _model_with_words(words)

        result = transcribe_segment(
            model, "parent-a", parent, language=None, start_secs=50.0, resegment=True
        )

        children = result["children"]
        assert len(children) == 2
        assert children[0]["transcript"] == "One."
        assert children[1]["transcript"] == "Two. Three"
        # No child may exceed the parent's real extent or invert.
        for c in children:
            assert c["end_secs"] > c["start_secs"]
            assert c["end_secs"] <= 50.0 + 5.0
        assert children[-1]["end_secs"] == pytest.approx(55.0)
        # Each child WAV has audible length.
        for c in children:
            assert os.path.getsize(c["wav_path"]) > 44  # more than a bare header

    def test_two_utterances_collapsing_to_one_falls_back_to_unsplit(self, write_wav):
        parent = write_wav(duration_secs=5.0)
        # Midpoint 5.5 clamps to 5.0: the second child is zero-length and
        # merges into the first, leaving one child -> unsplit shape instead.
        words = [
            W(" One.", 0.1, 5.4, probability=0.9),
            W(" Two", 5.6, 6.0, probability=0.9),
        ]
        model = _model_with_words(words)

        before = set(os.listdir(os.path.dirname(parent)))
        result = transcribe_segment(
            model, "parent-b", parent, language=None, start_secs=0.0, resegment=True
        )
        after = set(os.listdir(os.path.dirname(parent)))

        assert "children" not in result
        assert result["transcript"] == "One. Two"
        assert result["transcript_confidence"] == pytest.approx(0.9)
        assert before == after  # no child WAVs written

    def test_first_utterance_overshoot_merges_forward(self, write_wav):
        parent = write_wav(duration_secs=5.0)
        # Pathological negative timestamps on the first sentence: its child
        # collapses at 0.0 and merges into the next one.
        words = [
            W(" Zero.", -1.5, -0.4, probability=0.9),
            W(" One.", -0.2, 2.0, probability=0.9),
            W(" Two", 2.8, 4.5, probability=0.9),
        ]
        model = _model_with_words(words)

        result = transcribe_segment(
            model, "parent-c", parent, language=None, start_secs=0.0, resegment=True
        )

        children = result["children"]
        assert len(children) == 2
        assert children[0]["transcript"] == "Zero. One."
        assert children[1]["transcript"] == "Two"
        assert children[0]["start_secs"] == pytest.approx(0.0)
        for c in children:
            assert c["end_secs"] > c["start_secs"]
