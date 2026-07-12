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

    mock_pipeline = MagicMock(return_value=MagicMock(speaker_diarization=mock_diarization))

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
# Test: per-segment match scoring vs cluster-level speaker_match_confidence
# ---------------------------------------------------------------------------


def _mock_turn(start: float, end: float):
    from unittest.mock import MagicMock

    turn = MagicMock()
    turn.start = start
    turn.end = end
    return turn


def _mock_pipeline_for(tracks):
    from unittest.mock import MagicMock

    diarization = MagicMock()
    diarization.itertracks.return_value = tracks
    return MagicMock(return_value=MagicMock(speaker_diarization=diarization))


def test_per_segment_score_differs_from_cluster(sample_wav_path, reference_wav_path, output_dir):
    """A mixed cluster gets one averaged cluster score, but each segment keeps
    its own embedding's score as match_confidence."""
    from unittest.mock import MagicMock, patch
    from diariser import run_diarisation

    mock_pipeline = _mock_pipeline_for([
        (_mock_turn(0.0, 3.0), None, "SPEAKER_00"),
        (_mock_turn(4.0, 7.0), None, "SPEAKER_00"),
    ])

    ref_emb = np.array([1.0, 0.0])
    emb_match = np.array([1.0, 0.0])   # cosine vs ref = 1.0
    emb_off = np.array([0.0, 1.0])     # cosine vs ref = 0.0
    # cluster average = [0.5, 0.5] → cosine vs ref = 1/sqrt(2)
    expected_cluster = 1.0 / math.sqrt(2.0)

    with patch("diariser.extract_embedding", return_value=ref_emb), patch(
        "diariser.extract_segment_embedding", side_effect=[emb_match, emb_off]
    ):
        segments, _ = run_diarisation(
            pipeline=mock_pipeline,
            embedding_model=MagicMock(),
            input_path=sample_wav_path,
            reference_path=reference_wav_path,
            output_dir=output_dir,
            min_segment_duration=1.0,
        )

    assert len(segments) == 2
    seg_a, seg_b = segments

    # Per-segment scores follow each segment's OWN embedding
    assert abs(seg_a["match_confidence"] - 1.0) < 1e-6
    assert abs(seg_b["match_confidence"] - 0.0) < 1e-6

    # Cluster score is the averaged-embedding similarity, identical on both
    assert abs(seg_a["speaker_match_confidence"] - expected_cluster) < 1e-6
    assert abs(seg_b["speaker_match_confidence"] - expected_cluster) < 1e-6

    # And the per-segment score genuinely differs from the cluster score
    assert seg_a["match_confidence"] != seg_a["speaker_match_confidence"]
    assert seg_b["match_confidence"] != seg_b["speaker_match_confidence"]


def test_sub_second_segment_falls_back_to_cluster_score(sample_wav_path, reference_wav_path, output_dir):
    """Segments under 1.0 s use the cluster score — their own embedding is too noisy."""
    from unittest.mock import MagicMock, patch
    from diariser import run_diarisation

    mock_pipeline = _mock_pipeline_for([
        (_mock_turn(0.0, 0.6), None, "SPEAKER_00"),   # 0.6 s — below fallback threshold
        (_mock_turn(2.0, 5.0), None, "SPEAKER_00"),   # 3.0 s — scored individually
    ])

    ref_emb = np.array([1.0, 0.0])
    emb_off = np.array([0.0, 1.0])     # short segment's own embedding (would score 0.0)
    emb_match = np.array([1.0, 0.0])
    expected_cluster = 1.0 / math.sqrt(2.0)

    with patch("diariser.extract_embedding", return_value=ref_emb), patch(
        "diariser.extract_segment_embedding", side_effect=[emb_off, emb_match]
    ):
        segments, _ = run_diarisation(
            pipeline=mock_pipeline,
            embedding_model=MagicMock(),
            input_path=sample_wav_path,
            reference_path=reference_wav_path,
            output_dir=output_dir,
            min_segment_duration=0.5,   # let the sub-second segment through
        )

    assert len(segments) == 2
    short_seg = next(s for s in segments if s["end_secs"] == 0.6)
    long_seg = next(s for s in segments if s["end_secs"] == 5.0)

    # Short segment: own embedding ignored, cluster score used
    assert abs(short_seg["match_confidence"] - expected_cluster) < 1e-6
    assert short_seg["match_confidence"] == short_seg["speaker_match_confidence"]

    # Long segment: scored on its own embedding
    assert abs(long_seg["match_confidence"] - 1.0) < 1e-6


def test_embedding_failure_falls_back_to_cluster_score(sample_wav_path, reference_wav_path, output_dir):
    """A per-segment embedding failure gets the cluster score and never fails the job."""
    from unittest.mock import MagicMock, patch
    from diariser import run_diarisation

    mock_pipeline = _mock_pipeline_for([
        (_mock_turn(0.0, 3.0), None, "SPEAKER_00"),   # extraction will fail
        (_mock_turn(4.0, 7.0), None, "SPEAKER_00"),   # extraction succeeds
    ])

    ref_emb = np.array([1.0, 0.0])
    emb_match = np.array([1.0, 0.0])
    # Cluster average is built from successful embeddings only → [1, 0] → score 1.0

    with patch("diariser.extract_embedding", return_value=ref_emb), patch(
        "diariser.extract_segment_embedding",
        side_effect=[RuntimeError("window too small"), emb_match],
    ):
        segments, _ = run_diarisation(
            pipeline=mock_pipeline,
            embedding_model=MagicMock(),
            input_path=sample_wav_path,
            reference_path=reference_wav_path,
            output_dir=output_dir,
            min_segment_duration=1.0,
        )

    # Job completed with both segments despite the extraction failure
    assert len(segments) == 2
    failed_seg = next(s for s in segments if s["end_secs"] == 3.0)
    ok_seg = next(s for s in segments if s["end_secs"] == 7.0)

    assert abs(failed_seg["match_confidence"] - 1.0) < 1e-6
    assert failed_seg["match_confidence"] == failed_seg["speaker_match_confidence"]
    assert abs(ok_seg["match_confidence"] - 1.0) < 1e-6


def test_all_embeddings_failed_yields_zero_confidence(sample_wav_path, reference_wav_path, output_dir):
    """If every extraction for a speaker fails, both scores are 0.0 and the job still completes."""
    from unittest.mock import MagicMock, patch
    from diariser import run_diarisation

    mock_pipeline = _mock_pipeline_for([
        (_mock_turn(0.0, 3.0), None, "SPEAKER_00"),
    ])

    ref_emb = np.array([1.0, 0.0])

    with patch("diariser.extract_embedding", return_value=ref_emb), patch(
        "diariser.extract_segment_embedding", side_effect=RuntimeError("boom")
    ):
        segments, _ = run_diarisation(
            pipeline=mock_pipeline,
            embedding_model=MagicMock(),
            input_path=sample_wav_path,
            reference_path=reference_wav_path,
            output_dir=output_dir,
            min_segment_duration=1.0,
        )

    assert len(segments) == 1
    assert segments[0]["match_confidence"] == 0.0
    assert segments[0]["speaker_match_confidence"] == 0.0


def test_segment_dict_includes_both_confidence_fields(sample_wav_path, reference_wav_path, output_dir):
    """Every returned segment carries both match_confidence and speaker_match_confidence in [0, 1]."""
    from unittest.mock import MagicMock, patch
    from diariser import run_diarisation

    mock_pipeline = _mock_pipeline_for([
        (_mock_turn(0.0, 3.0), None, "SPEAKER_00"),
        (_mock_turn(4.0, 6.0), None, "SPEAKER_01"),
    ])

    ref_emb = np.array([1.0, 0.0])

    with patch("diariser.extract_embedding", return_value=ref_emb), patch(
        "diariser.extract_segment_embedding",
        side_effect=[np.array([0.9, 0.1]), np.array([-0.5, 0.5])],
    ):
        segments, _ = run_diarisation(
            pipeline=mock_pipeline,
            embedding_model=MagicMock(),
            input_path=sample_wav_path,
            reference_path=reference_wav_path,
            output_dir=output_dir,
            min_segment_duration=1.0,
        )

    assert len(segments) == 2
    for seg in segments:
        assert "match_confidence" in seg
        assert "speaker_match_confidence" in seg
        assert 0.0 <= seg["match_confidence"] <= 1.0
        assert 0.0 <= seg["speaker_match_confidence"] <= 1.0


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

    mock_pipeline = MagicMock(return_value=MagicMock(speaker_diarization=mock_diarization))

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

    mock_pipeline = MagicMock(return_value=MagicMock(speaker_diarization=mock_diarization))

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

    mock_pipeline = MagicMock(return_value=MagicMock(speaker_diarization=mock_diarization))

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

    mock_pipeline = MagicMock(return_value=MagicMock(speaker_diarization=mock_diarization))

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


# ---------------------------------------------------------------------------
# Test: select_pool_turns — ordering, bounds (whole turns, no truncation)
# ---------------------------------------------------------------------------


def _turn(start, end, label="SPEAKER_00"):
    return {"start": start, "end": end, "speaker_label": label}


def test_select_pool_turns_longest_first():
    from diariser import select_pool_turns

    turns = [_turn(0.0, 2.0), _turn(10.0, 15.0), _turn(20.0, 23.0)]
    pool = select_pool_turns(turns, pool_max_secs=90.0, pool_max_turns=20)

    # Durations 2, 5, 3 → ordered 5, 3, 2; whole turns, none truncated.
    take_durations = [round(t["end"] - t["start"], 6) for t in pool]
    assert take_durations == [5.0, 3.0, 2.0]


def test_select_pool_turns_bounded_by_secs():
    from diariser import select_pool_turns

    # Durations 10, 8, 6 → ordered 10, 8, 6. Cap 15s: take 10 (total 10), take 8
    # (total 18 ≥ 15) then stop — whole turns, so it may overshoot the cap.
    turns = [_turn(0.0, 10.0), _turn(20.0, 28.0), _turn(40.0, 46.0)]
    pool = select_pool_turns(turns, pool_max_secs=15.0, pool_max_turns=20)

    assert [round(t["end"] - t["start"], 6) for t in pool] == [10.0, 8.0]


def test_select_pool_turns_bounded_by_count():
    from diariser import select_pool_turns

    turns = [_turn(i * 10.0, i * 10.0 + (5 - i), "SPEAKER_00") for i in range(5)]
    pool = select_pool_turns(turns, pool_max_secs=1e9, pool_max_turns=2)

    assert len(pool) == 2


def test_select_pool_turns_empty():
    from diariser import select_pool_turns

    assert select_pool_turns([], pool_max_secs=90.0, pool_max_turns=20) == []


# ---------------------------------------------------------------------------
# Test: run_scout — pool slices, stats, sorting, num_speakers
# ---------------------------------------------------------------------------


def _mock_scout_pipeline(tracks):
    """Build a mock pyannote pipeline whose diarization yields the given tracks.

    tracks: list of (start, end, speaker_label).
    """
    from unittest.mock import MagicMock

    itertracks = []
    for start, end, label in tracks:
        turn = MagicMock()
        turn.start = start
        turn.end = end
        itertracks.append((turn, None, label))

    diarization = MagicMock()
    diarization.itertracks.return_value = itertracks
    return MagicMock(return_value=MagicMock(speaker_diarization=diarization))


def test_run_scout_writes_pool_slices_and_stats(sample_wav_path, output_dir):
    import soundfile as sf
    from diariser import run_scout

    # SPEAKER_00: 2.5 + 3.0 = 5.5s over 2 turns; SPEAKER_01: 2.0s over 1 turn.
    pipeline = _mock_scout_pipeline([
        (0.5, 3.0, "SPEAKER_00"),
        (3.5, 5.5, "SPEAKER_01"),
        (6.0, 9.0, "SPEAKER_00"),
    ])

    speakers = run_scout(
        pipeline=pipeline,
        input_path=sample_wav_path,
        output_dir=output_dir,
    )

    assert len(speakers) == 2
    by_label = {s["speaker_label"]: s for s in speakers}

    assert abs(by_label["SPEAKER_00"]["total_secs"] - 5.5) < 1e-6
    assert by_label["SPEAKER_00"]["segment_count"] == 2
    assert abs(by_label["SPEAKER_01"]["total_secs"] - 2.0) < 1e-6
    assert by_label["SPEAKER_01"]["segment_count"] == 1

    # SPEAKER_00's pool is longest-first: (6.0-9.0)=3.0 before (0.5-3.0)=2.5.
    pool00 = by_label["SPEAKER_00"]["pool"]
    assert [p["index"] for p in pool00] == [0, 1]
    assert abs(pool00[0]["duration"] - 3.0) < 1e-6
    assert abs(pool00[1]["duration"] - 2.5) < 1e-6

    # Slices exist at {output_dir}/{label}/{index}.wav and are readable.
    for label, spk in by_label.items():
        for turn in spk["pool"]:
            path = Path(output_dir) / label / f"{turn['index']}.wav"
            assert path.exists()
            data, sr = sf.read(str(path))
            assert len(data) > 0


def test_run_scout_sorted_by_total_secs_desc(sample_wav_path, output_dir):
    from diariser import run_scout

    # SPEAKER_01 is the most talkative; must sort first.
    pipeline = _mock_scout_pipeline([
        (0.5, 2.0, "SPEAKER_00"),
        (2.5, 8.5, "SPEAKER_01"),
        (9.0, 9.9, "SPEAKER_02"),  # 0.9s < 1.0s filter → excluded
    ])

    speakers = run_scout(
        pipeline=pipeline,
        input_path=sample_wav_path,
        output_dir=output_dir,
    )

    labels = [s["speaker_label"] for s in speakers]
    # SPEAKER_02 filtered out; remaining sorted by talk time desc.
    assert labels == ["SPEAKER_01", "SPEAKER_00"]
    totals = [s["total_secs"] for s in speakers]
    assert totals == sorted(totals, reverse=True)


def test_run_scout_pool_bounded_by_turns(sample_wav_path, output_dir):
    from diariser import run_scout

    # Four qualifying turns for one speaker; pool_max_turns caps at 2.
    pipeline = _mock_scout_pipeline([
        (0.0, 2.0, "SPEAKER_00"),
        (2.5, 5.0, "SPEAKER_00"),
        (5.5, 7.0, "SPEAKER_00"),
        (7.5, 9.5, "SPEAKER_00"),
    ])

    speakers = run_scout(
        pipeline=pipeline,
        input_path=sample_wav_path,
        output_dir=output_dir,
        pool_max_turns=2,
    )

    spk = speakers[0]
    # All four turns count toward talk time, but only two are pooled.
    assert spk["segment_count"] == 4
    assert len(spk["pool"]) == 2
    slices = sorted(p.name for p in (Path(output_dir) / "SPEAKER_00").glob("*.wav"))
    assert slices == ["0.wav", "1.wav"]


def test_run_scout_num_speakers_forces_exact_count(sample_wav_path, output_dir):
    from diariser import run_scout

    pipeline = _mock_scout_pipeline([(0.5, 4.0, "SPEAKER_00")])

    run_scout(
        pipeline=pipeline,
        input_path=sample_wav_path,
        output_dir=output_dir,
        num_speakers=2,
    )

    # num_speakers is passed exactly; min/max are NOT sent alongside it.
    _, kwargs = pipeline.call_args
    assert kwargs == {"num_speakers": 2}


def test_run_scout_without_num_speakers_uses_range(sample_wav_path, output_dir):
    from diariser import run_scout

    pipeline = _mock_scout_pipeline([(0.5, 4.0, "SPEAKER_00")])

    run_scout(
        pipeline=pipeline,
        input_path=sample_wav_path,
        output_dir=output_dir,
        min_speakers=1,
        max_speakers=5,
    )

    _, kwargs = pipeline.call_args
    assert kwargs == {"min_speakers": 1, "max_speakers": 5}


def test_run_scout_respects_min_segment_duration(sample_wav_path, output_dir):
    from diariser import run_scout

    pipeline = _mock_scout_pipeline([
        (0.0, 0.5, "SPEAKER_00"),   # 0.5s < 1.0s → excluded
        (2.0, 5.0, "SPEAKER_00"),   # 3.0s → kept
    ])

    speakers = run_scout(
        pipeline=pipeline,
        input_path=sample_wav_path,
        output_dir=output_dir,
        min_segment_duration=1.0,
    )

    assert len(speakers) == 1
    assert speakers[0]["segment_count"] == 1
    assert abs(speakers[0]["total_secs"] - 3.0) < 1e-6


def test_run_scout_creates_output_dir(tmp_path, sample_wav_path):
    from diariser import run_scout

    new_dir = str(tmp_path / "nested" / "candidates")
    assert not Path(new_dir).exists()

    pipeline = _mock_scout_pipeline([(0.5, 4.0, "SPEAKER_00")])

    speakers = run_scout(
        pipeline=pipeline,
        input_path=sample_wav_path,
        output_dir=new_dir,
    )

    assert Path(new_dir).exists()
    assert len(speakers) == 1


def test_run_scout_no_turns_returns_empty(sample_wav_path, output_dir):
    from diariser import run_scout

    # All turns below the duration filter.
    pipeline = _mock_scout_pipeline([(0.0, 0.4, "SPEAKER_00")])

    speakers = run_scout(
        pipeline=pipeline,
        input_path=sample_wav_path,
        output_dir=output_dir,
        min_segment_duration=1.0,
    )

    assert speakers == []
