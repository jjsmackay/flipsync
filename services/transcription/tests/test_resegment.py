"""Unit tests for sentence-aligned re-segmentation.

Covers the splitting rules from spec/pipeline.md §Sentence-aligned
re-segmentation: punctuation/gap splits, force-split of long utterances,
short-utterance merging, boundary midpoint math, absolute timestamps,
sample-accurate child WAV slicing, and the unsplit fallbacks.
"""

import os
import uuid
import wave
from unittest.mock import MagicMock, patch

import pytest

from resegment import (
    GAP_SPLIT_SECS,
    MAX_UTTERANCE_SECS,
    MIN_UTTERANCE_SECS,
    compute_boundaries,
    ends_sentence,
    normalise_utterances,
    slice_children,
    split_into_utterances,
)
from transcriber import process_batch, transcribe_segment


class W:
    """Minimal stand-in for a faster-whisper Word."""

    def __init__(self, word: str, start: float, end: float, probability: float = 0.9):
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability


def _model_with_words(words, text=None):
    """Mock WhisperModel whose transcribe() yields one segment with `words`.

    Uses side_effect so the generator is fresh on every call.
    """
    model = MagicMock()

    def _transcribe(wav_path, **kwargs):
        seg = MagicMock()
        seg.text = text if text is not None else "".join(w.word for w in words)
        seg.words = list(words)
        return iter([seg]), MagicMock()

    model.transcribe.side_effect = _transcribe
    return model


# ---------------------------------------------------------------------------
# Sentence-terminal detection
# ---------------------------------------------------------------------------

class TestEndsSentence:
    @pytest.mark.parametrize("token", [" done.", " done!", " what?", " wait…"])
    def test_terminal_punctuation(self, token):
        assert ends_sentence(token)

    @pytest.mark.parametrize("token", [' mean."', " over.'", " right?)", " sure.”", " done.’"])
    def test_terminal_followed_by_closing_quote_or_bracket(self, token):
        assert ends_sentence(token)

    @pytest.mark.parametrize("token", [" hello", " mid,", " dash-", ' "quoted', ""])
    def test_non_terminal(self, token):
        assert not ends_sentence(token)


# ---------------------------------------------------------------------------
# Initial splitting: punctuation + gaps
# ---------------------------------------------------------------------------

class TestSplitIntoUtterances:
    def test_splits_at_period(self):
        words = [
            W(" Hello", 0.0, 0.4),
            W(" there.", 0.5, 1.0),
            W(" Bye", 1.1, 1.5),
            W(" now", 1.6, 2.0),
        ]
        utts = split_into_utterances(words)
        assert len(utts) == 2
        assert [w.word for w in utts[0]] == [" Hello", " there."]
        assert [w.word for w in utts[1]] == [" Bye", " now"]

    @pytest.mark.parametrize("punct", ["!", "?", "…"])
    def test_splits_at_other_terminals(self, punct):
        words = [W(f" one{punct}", 0.0, 0.5), W(" two", 0.6, 1.0)]
        assert len(split_into_utterances(words)) == 2

    def test_splits_at_terminal_with_closing_quote(self):
        words = [W(' mean."', 0.0, 0.5), W(" And", 0.6, 1.0)]
        assert len(split_into_utterances(words)) == 2

    def test_splits_at_gap_at_threshold(self):
        words = [W(" one", 0.0, 0.5), W(" two", 0.5 + GAP_SPLIT_SECS, 1.5)]
        assert len(split_into_utterances(words)) == 2

    def test_no_split_below_gap_threshold(self):
        words = [W(" one", 0.0, 0.5), W(" two", 1.09, 1.5)]
        assert len(split_into_utterances(words)) == 1

    def test_no_split_without_punctuation_or_gap(self):
        words = [W(f" w{i}", i * 0.3, i * 0.3 + 0.25) for i in range(10)]
        assert len(split_into_utterances(words)) == 1

    def test_no_words_gives_no_utterances(self):
        assert split_into_utterances([]) == []

    def test_trailing_punctuation_does_not_create_empty_utterance(self):
        words = [W(" Done.", 0.0, 0.5)]
        utts = split_into_utterances(words)
        assert len(utts) == 1


# ---------------------------------------------------------------------------
# Normalisation: force-split and merge
# ---------------------------------------------------------------------------

class TestNormaliseForceSplit:
    def test_long_utterance_split_at_largest_gap(self):
        # Span 18 s, largest internal gap between 8.0 and 9.5.
        words = [
            W(" a", 0.0, 2.0),
            W(" b", 2.1, 4.0),
            W(" c", 4.2, 6.0),
            W(" d", 6.1, 8.0),
            W(" e", 9.5, 11.0),
            W(" f", 11.1, 13.0),
            W(" g", 13.1, 16.0),
            W(" h", 16.1, 18.0),
        ]
        out = normalise_utterances([words])
        assert len(out) == 2
        assert out[0][-1].word == " d"
        assert out[1][0].word == " e"
        assert all(u[-1].end - u[0].start <= MAX_UTTERANCE_SECS for u in out)

    def test_force_split_repeats_until_all_within_limit(self):
        # Span 40 s; needs three splits to get all pieces <= 15 s.
        words = [
            W(" a", 0.0, 10.0),
            W(" b", 10.5, 20.0),
            W(" c", 21.0, 31.0),
            W(" d", 31.2, 40.0),
        ]
        out = normalise_utterances([words])
        assert len(out) == 4
        assert all(u[-1].end - u[0].start <= MAX_UTTERANCE_SECS for u in out)
        # Order preserved
        flat = [w.word for u in out for w in u]
        assert flat == [" a", " b", " c", " d"]

    def test_single_word_long_utterance_left_alone(self):
        # Cannot split a single word; must not loop forever.
        words = [W(" loooong", 0.0, 20.0)]
        out = normalise_utterances([words])
        assert len(out) == 1
        assert out[0] == words


class TestNormaliseMerge:
    def test_short_utterance_merges_into_following(self):
        u1 = [W(" Hm.", 0.0, 0.4)]
        u2 = [W(" Right", 0.8, 2.0), W(" then.", 2.1, 3.0)]
        out = normalise_utterances([u1, u2])
        assert len(out) == 1
        assert [w.word for w in out[0]] == [" Hm.", " Right", " then."]

    def test_short_last_utterance_merges_into_preceding(self):
        u1 = [W(" Long", 0.0, 1.5), W(" enough.", 1.6, 3.0)]
        u2 = [W(" Bye.", 3.5, 3.9)]
        out = normalise_utterances([u1, u2])
        assert len(out) == 1
        assert [w.word for w in out[0]] == [" Long", " enough.", " Bye."]

    def test_merge_skipped_when_result_would_exceed_max(self):
        u1 = [W(" Hm.", 0.0, 0.5)]
        u2 = [W(" Monologue", 1.0, 8.0), W(" continues.", 8.1, 16.0)]
        # Merged span would be 16.0 s > 15 s: keep separate.
        out = normalise_utterances([u1, u2])
        assert len(out) == 2

    def test_chain_of_short_utterances_merges_forward(self):
        u1 = [W(" a.", 0.0, 0.3)]
        u2 = [W(" b.", 0.4, 0.7)]
        u3 = [W(" c", 0.8, 2.5)]
        out = normalise_utterances([u1, u2, u3])
        assert len(out) == 1

    def test_in_band_utterances_untouched(self):
        u1 = [W(" one", 0.0, 1.0), W(" two.", 1.1, 2.0)]
        u2 = [W(" three", 3.0, 4.0), W(" four.", 4.1, 5.5)]
        out = normalise_utterances([u1, u2])
        assert out == [u1, u2]


# ---------------------------------------------------------------------------
# Boundary midpoint math
# ---------------------------------------------------------------------------

class TestComputeBoundaries:
    def test_midpoints_first_and_last(self):
        u1 = [W(" one.", 0.2, 1.4)]
        u2 = [W(" two.", 2.4, 4.0)]
        u3 = [W(" three", 4.6, 6.1)]
        bounds = compute_boundaries([u1, u2, u3], file_duration=7.0)
        # Gap 1: 1.4 -> 2.4, midpoint 1.9. Gap 2: 4.0 -> 4.6, midpoint 4.3.
        assert bounds == [(0.0, 1.9), (1.9, 4.3), (4.3, 7.0)]

    def test_no_audio_lost_or_duplicated(self):
        u1 = [W(" a.", 0.5, 1.0)]
        u2 = [W(" b", 2.0, 3.0)]
        bounds = compute_boundaries([u1, u2], file_duration=4.2)
        assert bounds[0][0] == 0.0
        assert bounds[-1][1] == 4.2
        for (_, prev_end), (next_start, _) in zip(bounds, bounds[1:]):
            assert prev_end == next_start


# ---------------------------------------------------------------------------
# Child WAV slicing
# ---------------------------------------------------------------------------

class TestSliceChildren:
    def test_children_written_to_parent_dir_with_uuid_names(self, write_wav):
        parent = write_wav(duration_secs=5.0)
        results = slice_children(parent, [(0.0, 1.5), (1.5, 3.2), (3.2, 5.0)])

        assert len(results) == 3
        for r in results:
            assert os.path.dirname(r["wav_path"]) == os.path.dirname(parent)
            assert os.path.isfile(r["wav_path"])
            stem = os.path.splitext(os.path.basename(r["wav_path"]))[0]
            # Full (non-truncated) UUID filename matching the returned id
            assert stem == r["id"]
            assert str(uuid.UUID(stem)) == stem

    def test_sample_accurate_durations_and_params(self, write_wav):
        rate = 16000
        parent = write_wav(duration_secs=5.0, sample_rate=rate)
        boundaries = [(0.0, 1.5), (1.5, 3.2), (3.2, 5.0)]
        results = slice_children(parent, boundaries)

        expected_edges = [0, round(1.5 * rate), round(3.2 * rate), 5 * rate]
        for r, start_frame, end_frame in zip(
            results, expected_edges, expected_edges[1:]
        ):
            with wave.open(r["wav_path"], "rb") as wf:
                assert wf.getnframes() == end_frame - start_frame
                assert wf.getframerate() == rate
                assert wf.getnchannels() == 1
                assert wf.getsampwidth() == 2

    def test_concatenated_children_equal_parent_audio(self, write_wav):
        parent = write_wav(duration_secs=2.0)
        results = slice_children(parent, [(0.0, 0.7), (0.7, 1.3), (1.3, 2.0)])

        child_bytes = b""
        for r in results:
            with wave.open(r["wav_path"], "rb") as wf:
                child_bytes += wf.readframes(wf.getnframes())
        with wave.open(parent, "rb") as wf:
            parent_bytes = wf.readframes(wf.getnframes())
        assert child_bytes == parent_bytes


# ---------------------------------------------------------------------------
# transcribe_segment with resegment=True
# ---------------------------------------------------------------------------

class TestTranscribeSegmentResegment:
    def test_split_returns_children_with_absolute_timestamps(self, write_wav):
        parent = write_wav(duration_secs=5.0)
        words = [
            W(" Hello", 0.1, 0.6, probability=0.8),
            W(" there.", 0.7, 1.4, probability=0.9),
            W(" Bye", 2.4, 3.0, probability=1.0),
            W(" now", 3.1, 4.0, probability=0.6),
        ]
        model = _model_with_words(words)

        result = transcribe_segment(
            model, "parent-1", parent, language=None, start_secs=100.0, resegment=True
        )

        assert result["id"] == "parent-1"
        assert "transcript" not in result
        children = result["children"]
        assert len(children) == 2

        # Boundary midpoint: gap 1.4 -> 2.4, midpoint 1.9. Absolute = 100 + offset.
        assert children[0]["start_secs"] == pytest.approx(100.0)
        assert children[0]["end_secs"] == pytest.approx(101.9)
        assert children[1]["start_secs"] == pytest.approx(101.9)
        assert children[1]["end_secs"] == pytest.approx(105.0)

        # Transcripts respect whisper token spacing, stripped at the edges.
        assert children[0]["transcript"] == "Hello there."
        assert children[1]["transcript"] == "Bye now"

        # Per-child confidence = mean of its word probabilities.
        assert children[0]["transcript_confidence"] == pytest.approx(0.85)
        assert children[1]["transcript_confidence"] == pytest.approx(0.8)

        # Child WAVs exist with service-generated UUID ids.
        for c in children:
            assert os.path.isfile(c["wav_path"])
            assert str(uuid.UUID(c["id"])) == c["id"]

    def test_single_utterance_returns_unsplit(self, write_wav):
        parent = write_wav(duration_secs=5.0)
        words = [W(" just", 0.1, 0.5), W(" talking", 0.6, 1.2)]
        model = _model_with_words(words, text=" just talking")

        before = set(os.listdir(os.path.dirname(parent)))
        result = transcribe_segment(
            model, "parent-2", parent, language=None, start_secs=10.0, resegment=True
        )
        after = set(os.listdir(os.path.dirname(parent)))

        assert result == {
            "id": "parent-2",
            "transcript": "just talking",
            "transcript_confidence": pytest.approx(0.9),
        }
        assert before == after  # no child files written

    def test_no_words_returns_unsplit(self, write_wav):
        parent = write_wav(duration_secs=5.0)
        model = _model_with_words([], text="")

        result = transcribe_segment(
            model, "parent-3", parent, language=None, start_secs=0.0, resegment=True
        )
        assert result == {
            "id": "parent-3",
            "transcript": "",
            "transcript_confidence": 0.0,
        }

    def test_resegment_false_never_splits(self, write_wav):
        parent = write_wav(duration_secs=5.0)
        # Words that WOULD split (punctuation + big gap) if resegment were on.
        words = [W(" One.", 0.1, 1.4), W(" Two", 2.4, 4.0)]
        model = _model_with_words(words, text=" One. Two")

        before = set(os.listdir(os.path.dirname(parent)))
        result = transcribe_segment(model, "parent-4", parent, language=None)
        after = set(os.listdir(os.path.dirname(parent)))

        assert "children" not in result
        assert result["transcript"] == "One. Two"
        assert before == after

    def test_short_segment_bypass_wins_over_resegment(self, short_wav_path):
        model = MagicMock()
        result = transcribe_segment(
            model, "parent-5", short_wav_path, language=None, start_secs=0.0, resegment=True
        )
        assert result["transcript"] == ""
        assert result["transcript_confidence"] == 0.0
        model.transcribe.assert_not_called()


# ---------------------------------------------------------------------------
# Per-segment failure isolation with resegmentation
# ---------------------------------------------------------------------------

class TestResegmentFailureIsolation:
    def test_failing_split_fails_only_that_segment(self, write_wav):
        parent = write_wav(duration_secs=5.0)
        words = [W(" One.", 0.1, 1.4), W(" Two", 2.4, 4.0)]
        model = _model_with_words(words, text=" One. Two")

        batch = [
            {"id": "split-me", "wav_path": parent, "start_secs": 0.0, "resegment": True},
            {"id": "plain", "wav_path": parent},
        ]

        with patch("transcriber.slice_children", side_effect=OSError("disk full")):
            results = process_batch(model, batch, language=None, max_workers=1)

        by_id = {r["id"]: r for r in results}
        assert "disk full" in by_id["split-me"]["error"]
        assert by_id["split-me"]["transcript"] == ""
        assert by_id["split-me"]["transcript_confidence"] == 0.0
        assert "error" not in by_id["plain"]
        assert by_id["plain"]["transcript"] == "One. Two"
