"""Unit tests for cleaner.py — clipping detection, silence detection, processing logic."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

# Add service root to path so imports work when running tests from repo root
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cleaner import (
    CleanupParams,
    SegmentInput,
    SegmentResult,
    _check_channel_clipping,
    _extract_loudnorm_json,
    detect_clipping,
    process_segment,
)


# ---------------------------------------------------------------------------
# Clipping detection unit tests
# ---------------------------------------------------------------------------


class TestCheckChannelClipping:
    def test_detects_clipping_with_consecutive_samples(self):
        """5 consecutive samples at 0 dBFS should be detected as clipping."""
        samples = np.zeros(100, dtype=np.float32)
        samples[40:45] = 1.0  # 5 consecutive full-scale samples
        result = _check_channel_clipping(samples, threshold_db=-0.1, min_consecutive=3)
        assert result is True

    def test_no_clipping_below_threshold(self):
        """Samples at 0.3 amplitude should not trigger clipping at -0.1 dBFS."""
        samples = np.full(100, 0.3, dtype=np.float32)
        result = _check_channel_clipping(samples, threshold_db=-0.1, min_consecutive=3)
        assert result is False

    def test_insufficient_consecutive_samples(self):
        """2 consecutive clipping samples with min_consecutive=3 should not flag."""
        samples = np.zeros(100, dtype=np.float32)
        samples[50:52] = 1.0  # only 2 consecutive
        result = _check_channel_clipping(samples, threshold_db=-0.1, min_consecutive=3)
        assert result is False

    def test_exactly_min_consecutive(self):
        """Exactly min_consecutive clipping samples should be detected."""
        samples = np.zeros(100, dtype=np.float32)
        samples[50:53] = 1.0  # exactly 3 consecutive
        result = _check_channel_clipping(samples, threshold_db=-0.1, min_consecutive=3)
        assert result is True

    def test_no_clipping_on_zero_array(self):
        """All-zero array should not trigger clipping."""
        samples = np.zeros(100, dtype=np.float32)
        result = _check_channel_clipping(samples, threshold_db=-0.1, min_consecutive=3)
        assert result is False


class TestDetectClipping:
    def test_detects_clipping_in_wav(self, clipping_wav):
        """The clipping fixture should be detected as clipping."""
        result = detect_clipping(clipping_wav, threshold_db=-0.1, min_consecutive_samples=3)
        assert result is True

    def test_no_clipping_in_clean_wav(self, clean_wav):
        """The clean fixture should not be detected as clipping."""
        result = detect_clipping(clean_wav, threshold_db=-0.1, min_consecutive_samples=3)
        assert result is False

    def test_no_clipping_in_silent_wav(self, silent_wav):
        """Silent WAV should not trigger clipping."""
        result = detect_clipping(silent_wav, threshold_db=-0.1, min_consecutive_samples=3)
        assert result is False

    def test_returns_false_for_missing_file(self):
        """Missing file should return False (not raise)."""
        result = detect_clipping("/nonexistent/path.wav", threshold_db=-0.1, min_consecutive_samples=3)
        assert result is False


# ---------------------------------------------------------------------------
# Loudnorm JSON extraction
# ---------------------------------------------------------------------------


class TestExtractLoudnormJson:
    def test_extracts_json_from_stderr(self):
        stderr = """
[Parsed_loudnorm_0 @ 0x...] Input Integrated:    -23.3 LUFS
{
    "input_i" : "-23.25",
    "input_tp" : "-3.59",
    "input_lra" : "5.30",
    "input_thresh" : "-33.57",
    "output_i" : "-23.00",
    "output_tp" : "-2.00",
    "output_lra" : "5.30",
    "output_thresh" : "-33.32",
    "target_offset" : "-0.25"
}
"""
        data = _extract_loudnorm_json(stderr)
        assert data["input_i"] == "-23.25"
        assert data["target_offset"] == "-0.25"

    def test_raises_on_missing_json(self):
        stderr = "Error: something went wrong\nNo JSON here"
        with pytest.raises(ValueError):
            _extract_loudnorm_json(stderr)


# ---------------------------------------------------------------------------
# process_segment with mocked FFmpeg
# ---------------------------------------------------------------------------


def _make_mock_run(returncode=0, stdout="", stderr=""):
    """Create a mock subprocess.run result."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


LOUDNORM_JSON = """{
    "input_i" : "-23.25",
    "input_tp" : "-3.59",
    "input_lra" : "5.30",
    "input_thresh" : "-33.57",
    "output_i" : "-23.00",
    "output_tp" : "-2.00",
    "output_lra" : "5.30",
    "output_thresh" : "-33.32",
    "target_offset" : "-0.25"
}"""


class TestProcessSegment:
    def test_ffmpeg_error_does_not_raise(self, tmp_dir, clean_wav):
        """FFmpeg failure returns error result without raising."""
        params = CleanupParams()
        out_path = os.path.join(tmp_dir, "output", "seg1.wav")
        seg = SegmentInput(id="seg-1", input_path=clean_wav, output_path=out_path)

        fail_result = _make_mock_run(returncode=1, stderr="Invalid data found")

        with patch("cleaner.subprocess.run", return_value=fail_result):
            result = process_segment(seg, params)

        assert result.id == "seg-1"
        assert result.output_path is None
        assert result.error is not None
        assert "ffmpeg_error" in result.error
        assert result.auto_rejected is False

    def test_success_path(self, tmp_dir, clean_wav, output_dir):
        """Successful processing returns output_path and no error."""
        params = CleanupParams()
        out_path = os.path.join(output_dir, "seg_success.wav")
        seg = SegmentInput(id="seg-ok", input_path=clean_wav, output_path=out_path)

        # We need to mock subprocess.run to simulate full pipeline
        # Pass 1 returns loudnorm JSON in stderr
        # Pass 2 writes intermediate file
        # Pass 3 (silence+highpass) writes trimmed file
        pass1_result = _make_mock_run(returncode=0, stderr=LOUDNORM_JSON)
        pass2_result = _make_mock_run(returncode=0)
        pass3_result = _make_mock_run(returncode=0)

        call_count = 0

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return pass1_result
            elif call_count == 2:
                # Pass 2: write a real WAV to the intermediate path
                # Find the intermediate path from the cmd args
                intermediate = cmd[-1]
                sf.write(intermediate, np.zeros(22050, dtype=np.float32), 22050, subtype="PCM_16")
                return pass2_result
            else:
                # Pass 3: write a real WAV to the trimmed path
                trimmed = cmd[-1]
                sf.write(trimmed, np.zeros(22050, dtype=np.float32), 22050, subtype="PCM_16")
                return pass3_result

        with patch("cleaner.subprocess.run", side_effect=mock_run):
            # Also mock ffprobe duration check
            with patch("cleaner._get_audio_duration", return_value=1.0):
                result = process_segment(seg, params)

        assert result.id == "seg-ok"
        assert result.output_path == out_path
        assert result.error is None
        assert result.auto_rejected is False

    def test_silent_segment_is_auto_rejected(self, tmp_dir, clean_wav, output_dir):
        """Segment with duration < 0.05 s after trim is auto_rejected."""
        params = CleanupParams()
        out_path = os.path.join(output_dir, "seg_silent.wav")
        seg = SegmentInput(id="seg-silent", input_path=clean_wav, output_path=out_path)

        call_count = 0

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_mock_run(returncode=0, stderr=LOUDNORM_JSON)
            elif call_count == 2:
                intermediate = cmd[-1]
                sf.write(intermediate, np.zeros(22050, dtype=np.float32), 22050, subtype="PCM_16")
                return _make_mock_run(returncode=0)
            else:
                trimmed = cmd[-1]
                # Write a near-empty WAV
                sf.write(trimmed, np.zeros(100, dtype=np.float32), 22050, subtype="PCM_16")
                return _make_mock_run(returncode=0)

        with patch("cleaner.subprocess.run", side_effect=mock_run):
            # Duration < 0.05 seconds → auto_rejected
            with patch("cleaner._get_audio_duration", return_value=0.001):
                result = process_segment(seg, params)

        assert result.auto_rejected is True
        assert result.output_path is None
        assert result.error is None
