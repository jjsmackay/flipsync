"""Unit tests for transcriber.py — confidence scoring, short segment handling."""

import math
import struct
import wave
from unittest.mock import MagicMock, patch

import pytest

from transcriber import (
    SHORT_SEGMENT_THRESHOLD_SECS,
    compute_confidence,
    get_wav_duration,
    process_batch,
    transcribe_segment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_word(probability: float) -> MagicMock:
    w = MagicMock()
    w.probability = probability
    w.text = "word"
    return w


def _make_model_returning(words: list) -> MagicMock:
    """Return a mock WhisperModel whose transcribe() yields one segment with given words."""
    model = MagicMock()
    seg = MagicMock()
    seg.text = " ".join(w.text for w in words)
    seg.words = words
    info = MagicMock()
    model.transcribe.return_value = (iter([seg]), info)
    return model


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

class TestComputeConfidence:
    def test_mean_of_probabilities(self):
        """Given words with probabilities [0.8, 0.9, 1.0], mean should be 0.9."""
        words = [_make_word(0.8), _make_word(0.9), _make_word(1.0)]
        result = compute_confidence(words)
        assert abs(result - 0.9) < 1e-9

    def test_empty_words_returns_zero(self):
        assert compute_confidence([]) == 0.0

    def test_single_word(self):
        words = [_make_word(0.75)]
        assert abs(compute_confidence(words) - 0.75) < 1e-9

    def test_clamp_above_one(self):
        """Values above 1.0 are clamped to 1.0."""
        words = [_make_word(1.5), _make_word(2.0)]
        result = compute_confidence(words)
        assert result == 1.0

    def test_clamp_below_zero(self):
        """Values below 0.0 are clamped to 0.0."""
        words = [_make_word(-0.5), _make_word(-0.1)]
        result = compute_confidence(words)
        assert result == 0.0

    def test_clamp_mixed(self):
        """Mixed values: mean(1.5, 0.5) = 1.0, clamped to 1.0."""
        words = [_make_word(1.5), _make_word(0.5)]
        result = compute_confidence(words)
        assert result == 1.0

    def test_exactly_one(self):
        words = [_make_word(1.0)]
        assert compute_confidence(words) == 1.0

    def test_exactly_zero(self):
        words = [_make_word(0.0)]
        assert compute_confidence(words) == 0.0


# ---------------------------------------------------------------------------
# WAV duration
# ---------------------------------------------------------------------------

class TestGetWavDuration:
    def test_returns_correct_duration(self, sine_wav_path):
        duration = get_wav_duration(sine_wav_path)
        assert abs(duration - 5.0) < 0.1

    def test_short_file_below_threshold(self, short_wav_path):
        duration = get_wav_duration(short_wav_path)
        assert duration < SHORT_SEGMENT_THRESHOLD_SECS

    def test_nonexistent_file_raises(self, tmp_path):
        # Unreadable/missing files must raise so the caller records a per-segment
        # error rather than silently transcribing a bad file (no fake 999.0).
        with pytest.raises(Exception):
            get_wav_duration(str(tmp_path / "no_such_file.wav"))


# ---------------------------------------------------------------------------
# transcribe_segment — short segment bypass
# ---------------------------------------------------------------------------

class TestTranscribeSegment:
    def test_short_segment_returns_empty_without_model_call(self, short_wav_path):
        """Segments < 0.5s must return empty transcript without calling model.transcribe."""
        model = MagicMock()
        result = transcribe_segment(model, "seg-1", short_wav_path, language=None)
        assert result["id"] == "seg-1"
        assert result["transcript"] == ""
        assert result["transcript_confidence"] == 0.0
        model.transcribe.assert_not_called()

    def test_normal_segment_calls_model(self, sine_wav_path):
        """Normal-length segments call model.transcribe and return results."""
        words = [_make_word(0.8), _make_word(0.9)]
        model = _make_model_returning(words)

        result = transcribe_segment(model, "seg-2", sine_wav_path, language=None)
        assert result["id"] == "seg-2"
        assert isinstance(result["transcript"], str)
        assert 0.0 <= result["transcript_confidence"] <= 1.0
        model.transcribe.assert_called_once()

    def test_language_passed_to_model(self, sine_wav_path):
        words = [_make_word(0.85)]
        model = _make_model_returning(words)

        transcribe_segment(model, "seg-3", sine_wav_path, language="en")
        call_kwargs = model.transcribe.call_args[1]
        assert call_kwargs.get("language") == "en"

    def test_no_language_when_none(self, sine_wav_path):
        words = [_make_word(0.85)]
        model = _make_model_returning(words)

        transcribe_segment(model, "seg-4", sine_wav_path, language=None)
        call_kwargs = model.transcribe.call_args[1]
        assert "language" not in call_kwargs

    def test_empty_words_gives_zero_confidence(self, sine_wav_path):
        """If the model returns a segment with no words, confidence is 0.0."""
        model = _make_model_returning([])
        result = transcribe_segment(model, "seg-5", sine_wav_path, language=None)
        assert result["transcript_confidence"] == 0.0

    def test_transcript_joined_from_segments(self, sine_wav_path):
        """Transcript text is stripped and joined from all segment texts."""
        words = [_make_word(0.9)]
        words[0].text = "hello"
        model = _make_model_returning(words)
        # Override seg.text to include leading/trailing spaces
        seg = MagicMock()
        seg.text = "  hello  "
        seg.words = words
        model.transcribe.return_value = (iter([seg]), MagicMock())

        result = transcribe_segment(model, "seg-6", sine_wav_path, language=None)
        assert result["transcript"] == "hello"


# ---------------------------------------------------------------------------
# process_batch — per-segment failure isolation and batch_size concurrency
# ---------------------------------------------------------------------------

class TestProcessBatch:
    def test_one_bad_segment_does_not_abort_batch(self, sine_wav_path, tmp_path):
        """A single unreadable WAV yields a per-segment error; siblings succeed."""
        words = [_make_word(0.9)]
        model = _make_model_returning(words)

        batch = [
            {"id": "ok-1", "wav_path": sine_wav_path},
            {"id": "bad", "wav_path": str(tmp_path / "missing.wav")},
            {"id": "ok-2", "wav_path": sine_wav_path},
        ]
        results = process_batch(model, batch, language=None, max_workers=1)

        assert len(results) == 3
        by_id = {r["id"]: r for r in results}
        # Bad segment: error recorded, contract fields still present
        assert by_id["bad"]["error"]
        assert by_id["bad"]["transcript"] == ""
        assert by_id["bad"]["transcript_confidence"] == 0.0
        # Good segments transcribed normally with no error field
        assert "error" not in by_id["ok-1"]
        assert "error" not in by_id["ok-2"]

    def test_results_preserve_input_order_concurrent(self, sine_wav_path):
        """With max_workers>1, results still come back in input order."""
        model = _make_model_returning([_make_word(0.9)])
        batch = [{"id": f"seg-{i}", "wav_path": sine_wav_path} for i in range(6)]

        results = process_batch(model, batch, language=None, max_workers=4)

        assert [r["id"] for r in results] == [f"seg-{i}" for i in range(6)]
        assert all(0.0 <= r["transcript_confidence"] <= 1.0 for r in results)
