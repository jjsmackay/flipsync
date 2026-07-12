"""Unit tests for diariser.py logic.

Tests run without any real models — only math and utility functions.
"""

import math
import uuid
from pathlib import Path

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Test: cosine_similarity math
# ---------------------------------------------------------------------------


def test_cosine_similarity_identical_vectors():
    from diariser import cosine_similarity

    a = np.array([1.0, 0.0, 0.0])
    result = cosine_similarity(a, a)
    assert abs(result - 1.0) < 1e-6, f"Expected 1.0, got {result}"


def test_cosine_similarity_orthogonal_vectors():
    from diariser import cosine_similarity

    a = np.array([1.0, 0.0, 0.0])
    b = np.array([0.0, 1.0, 0.0])
    result = cosine_similarity(a, b)
    assert abs(result - 0.0) < 1e-6, f"Expected 0.0 for orthogonal vectors, got {result}"


def test_cosine_similarity_opposite_vectors():
    from diariser import cosine_similarity

    a = np.array([1.0, 0.0])
    b = np.array([-1.0, 0.0])
    result = cosine_similarity(a, b)
    assert abs(result - (-1.0)) < 1e-6, f"Expected -1.0 for opposite vectors, got {result}"


def test_cosine_similarity_known_pair():
    """Verify formula: similarity = 1 - cosine_distance = dot(a,b)/(|a||b|)."""
    from diariser import cosine_similarity

    a = np.array([3.0, 4.0])
    b = np.array([1.0, 0.0])
    # cos(angle) = dot / (|a||b|) = 3 / (5 * 1) = 0.6
    expected = 0.6
    result = cosine_similarity(a, b)
    assert abs(result - expected) < 1e-6, f"Expected {expected}, got {result}"


def test_cosine_similarity_zero_vector():
    from diariser import cosine_similarity

    a = np.zeros(3)
    b = np.array([1.0, 0.0, 0.0])
    # By convention, returns 0.0 when a norm is zero
    result = cosine_similarity(a, b)
    assert result == 0.0


# ---------------------------------------------------------------------------
# Test: match_confidence clamping to [0, 1]
# ---------------------------------------------------------------------------


def test_clamp_confidence_bounds():
    from diariser import clamp_confidence

    assert clamp_confidence(-1.0) == 0.0
    assert clamp_confidence(-0.3) == 0.0
    assert clamp_confidence(0.42) == 0.42
    assert clamp_confidence(1.0) == 1.0
    assert clamp_confidence(1.5) == 1.0


def test_match_confidence_clamped_in_pipeline(sample_wav_path, reference_wav_path, output_dir):
    """A dissimilar speaker (negative cosine) yields match_confidence 0.0, never negative."""
    from unittest.mock import MagicMock
    from diariser import run_diarisation

    mock_turn = MagicMock()
    mock_turn.start = 1.0
    mock_turn.end = 4.0

    mock_diarization = MagicMock()
    mock_diarization.itertracks.return_value = [(mock_turn, None, "SPEAKER_00")]

    mock_pipeline = MagicMock(return_value=mock_diarization)

    # Reference and speaker embeddings point in opposite directions → cosine = -1.
    ref_emb = np.array([1.0, 0.0])
    seg_emb = np.array([-1.0, 0.0])

    mock_inference = MagicMock()
    mock_inference.__call__ = MagicMock(return_value=ref_emb)
    mock_inference.crop = MagicMock(return_value=seg_emb)

    segments, _ = run_diarisation(
        pipeline=mock_pipeline,
        embedding_model=mock_inference,
        input_path=sample_wav_path,
        reference_path=reference_wav_path,
        output_dir=output_dir,
        min_segment_duration=1.0,
    )

    assert len(segments) == 1
    assert segments[0]["match_confidence"] == 0.0


# ---------------------------------------------------------------------------
# Test: coverage_ratio calculation
# ---------------------------------------------------------------------------


def test_coverage_ratio_basic():
    from diariser import compute_coverage_ratio

    segments = [
        {"speaker_label": "SPEAKER_00", "start_secs": 0.0, "end_secs": 2.0},
        {"speaker_label": "SPEAKER_00", "start_secs": 5.0, "end_secs": 7.0},
        {"speaker_label": "SPEAKER_01", "start_secs": 3.0, "end_secs": 4.5},
    ]
    total_duration = 20.0
    # SPEAKER_00 has 4.0 seconds total; ratio = 4.0 / 20.0 = 0.2
    ratio = compute_coverage_ratio(segments, "SPEAKER_00", total_duration)
    assert abs(ratio - 0.2) < 1e-9


def test_coverage_ratio_zero_duration():
    from diariser import compute_coverage_ratio

    segments = [{"speaker_label": "SPEAKER_00", "start_secs": 0.0, "end_secs": 2.0}]
    ratio = compute_coverage_ratio(segments, "SPEAKER_00", 0.0)
    assert ratio == 0.0


def test_coverage_ratio_no_matching_speaker():
    from diariser import compute_coverage_ratio

    segments = [
        {"speaker_label": "SPEAKER_01", "start_secs": 0.0, "end_secs": 5.0},
    ]
    ratio = compute_coverage_ratio(segments, "SPEAKER_00", 10.0)
    assert ratio == 0.0


def test_coverage_ratio_full_coverage():
    from diariser import compute_coverage_ratio

    segments = [
        {"speaker_label": "SPEAKER_00", "start_secs": 0.0, "end_secs": 10.0},
    ]
    ratio = compute_coverage_ratio(segments, "SPEAKER_00", 10.0)
    assert abs(ratio - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Test: UUID filenames — full, not truncated
# ---------------------------------------------------------------------------


def test_uuid_filename_full_length(sample_wav_path, reference_wav_path, output_dir):
    """Segment WAV paths use full 36-character UUIDs."""
    from unittest.mock import MagicMock
    from diariser import run_diarisation

    mock_turn = MagicMock()
    mock_turn.start = 1.0
    mock_turn.end = 4.0

    mock_diarization = MagicMock()
    mock_diarization.itertracks.return_value = [(mock_turn, None, "SPEAKER_00")]

    mock_pipeline = MagicMock(return_value=mock_diarization)

    ref_emb = np.array([1.0, 0.0, 0.0])
    seg_emb = np.array([0.9, 0.1, 0.0])

    mock_inference = MagicMock()
    mock_inference.__call__ = MagicMock(return_value=ref_emb)
    mock_inference.crop = MagicMock(return_value=seg_emb)

    segments, _ = run_diarisation(
        pipeline=mock_pipeline,
        embedding_model=mock_inference,
        input_path=sample_wav_path,
        reference_path=reference_wav_path,
        output_dir=output_dir,
        min_segment_duration=1.0,
    )

    assert len(segments) == 1
    seg = segments[0]

    # UUID must be 36 chars (8-4-4-4-12 with hyphens)
    assert len(seg["id"]) == 36
    # Validate it parses as a UUID
    parsed = uuid.UUID(seg["id"])
    assert str(parsed) == seg["id"]

    # wav_path must end with the full UUID + .wav
    assert seg["wav_path"].endswith(f"{seg['id']}.wav")
    # File must exist
    assert Path(seg["wav_path"]).exists()


# ---------------------------------------------------------------------------
# Test: output_dir is created if missing
# ---------------------------------------------------------------------------


def test_output_dir_created(tmp_path, sample_wav_path, reference_wav_path):
    """run_diarisation creates output_dir even if it doesn't exist."""
    from unittest.mock import MagicMock
    from diariser import run_diarisation

    new_dir = str(tmp_path / "nested" / "segments")
    assert not Path(new_dir).exists()

    mock_turn = MagicMock()
    mock_turn.start = 1.0
    mock_turn.end = 4.0

    mock_diarization = MagicMock()
    mock_diarization.itertracks.return_value = [(mock_turn, None, "SPEAKER_00")]

    mock_pipeline = MagicMock(return_value=mock_diarization)

    ref_emb = np.array([1.0, 0.0, 0.0])
    seg_emb = np.array([0.8, 0.2, 0.0])

    mock_inference = MagicMock()
    mock_inference.__call__ = MagicMock(return_value=ref_emb)
    mock_inference.crop = MagicMock(return_value=seg_emb)

    segments, _ = run_diarisation(
        pipeline=mock_pipeline,
        embedding_model=mock_inference,
        input_path=sample_wav_path,
        reference_path=reference_wav_path,
        output_dir=new_dir,
        min_segment_duration=1.0,
    )

    assert Path(new_dir).exists()
    assert len(segments) == 1


# ---------------------------------------------------------------------------
# Test: segments shorter than min_segment_duration are excluded
# ---------------------------------------------------------------------------


def test_short_segment_filtered(sample_wav_path, reference_wav_path, output_dir):
    from unittest.mock import MagicMock
    from diariser import run_diarisation

    short_turn = MagicMock()
    short_turn.start = 0.0
    short_turn.end = 0.5   # 0.5s < 1.0s threshold

    long_turn = MagicMock()
    long_turn.start = 2.0
    long_turn.end = 5.0

    mock_diarization = MagicMock()
    mock_diarization.itertracks.return_value = [
        (short_turn, None, "SPEAKER_00"),
        (long_turn, None, "SPEAKER_00"),
    ]

    mock_pipeline = MagicMock(return_value=mock_diarization)

    ref_emb = np.array([1.0, 0.0])
    seg_emb = np.array([0.9, 0.1])

    mock_inference = MagicMock()
    mock_inference.__call__ = MagicMock(return_value=ref_emb)
    mock_inference.crop = MagicMock(return_value=seg_emb)

    segments, _ = run_diarisation(
        pipeline=mock_pipeline,
        embedding_model=mock_inference,
        input_path=sample_wav_path,
        reference_path=reference_wav_path,
        output_dir=output_dir,
        min_segment_duration=1.0,
    )

    # Only the long segment passes the duration filter
    assert len(segments) == 1
    assert segments[0]["start_secs"] == 2.0


# ---------------------------------------------------------------------------
# Test: progress callback is called at milestones
# ---------------------------------------------------------------------------


def test_progress_callback_milestones(sample_wav_path, reference_wav_path, output_dir):
    from unittest.mock import MagicMock
    from diariser import run_diarisation

    mock_turn = MagicMock()
    mock_turn.start = 1.0
    mock_turn.end = 4.0

    mock_diarization = MagicMock()
    mock_diarization.itertracks.return_value = [(mock_turn, None, "SPEAKER_00")]

    mock_pipeline = MagicMock(return_value=mock_diarization)

    ref_emb = np.array([1.0, 0.0])
    seg_emb = np.array([0.9, 0.1])

    mock_inference = MagicMock()
    mock_inference.__call__ = MagicMock(return_value=ref_emb)
    mock_inference.crop = MagicMock(return_value=seg_emb)

    progress_values = []

    def on_progress(pct):
        progress_values.append(pct)

    run_diarisation(
        pipeline=mock_pipeline,
        embedding_model=mock_inference,
        input_path=sample_wav_path,
        reference_path=reference_wav_path,
        output_dir=output_dir,
        min_segment_duration=1.0,
        progress_callback=on_progress,
    )

    # Must hit 10, 50, 75, 100
    for milestone in (10, 50, 75, 100):
        assert milestone in progress_values, f"Missing milestone {milestone} in {progress_values}"
