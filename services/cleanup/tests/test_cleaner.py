"""Unit tests for cleaner.py — clipping detection, silence detection, processing logic."""

import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

# Add service root to path so imports work when running tests from repo root
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cleaner import (
    BinaryNotFoundError,
    CleanupParams,
    ProbeError,
    SegmentInput,
    SegmentResult,
    _check_channel_clipping,
    _extract_loudnorm_json,
    _is_silent_measurement,
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


# ---------------------------------------------------------------------------
# C1 — silence trim removes ONLY leading/trailing silence
# ---------------------------------------------------------------------------


class TestSilenceTrimFilter:
    def test_trim_filter_is_leading_and_trailing_only(self, tmp_dir, clean_wav, output_dir):
        """The trim pass must use two start-trigger passes wrapped in areverse,
        never stop_periods=-1 (which strips mid-segment pauses)."""
        params = CleanupParams()
        out_path = os.path.join(output_dir, "seg_filter.wav")
        seg = SegmentInput(id="seg-filter", input_path=clean_wav, output_path=out_path)

        captured_filters = []

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            # Record the -af filter string of every call.
            if "-af" in cmd:
                captured_filters.append(cmd[cmd.index("-af") + 1])
            if len(captured_filters) == 1:
                result.stderr = LOUDNORM_JSON
            else:
                result.stderr = ""
                sf.write(cmd[-1], np.zeros(22050, dtype=np.float32), 22050, subtype="PCM_16")
            return result

        with patch("cleaner.subprocess.run", side_effect=mock_run):
            with patch("cleaner._get_audio_duration", return_value=1.0):
                process_segment(seg, params)

        # The trim pass is the third ffmpeg invocation.
        trim_filter = captured_filters[2]
        # Mid-file silence must NOT be stripped.
        assert "stop_periods" not in trim_filter
        # Leading + trailing trim via two start passes wrapped in areverse.
        assert trim_filter.count("silenceremove=start_periods=1") == 2
        assert trim_filter.count("areverse") == 2
        assert f"highpass=f={params.highpass_hz}" in trim_filter
        # Exact chain shape.
        start = (
            f"silenceremove=start_periods=1"
            f":start_duration={params.silence_min_duration_secs}"
            f":start_threshold={params.silence_threshold_db}dB"
        )
        assert trim_filter == (
            f"{start},areverse,{start},areverse,highpass=f={params.highpass_hz}"
        )

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
    @pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
    def test_real_ffmpeg_preserves_mid_pause(self, tmp_dir, output_dir):
        """Real FFmpeg run: leading/trailing silence trimmed, mid pause survives.

        Input layout (22050 Hz mono):
          0.5 s silence | 0.5 s tone | 0.4 s silence (mid pause) | 0.5 s tone | 0.5 s silence
        Total 2.4 s. After trimming lead + tail (~1.0 s), ~1.4 s should remain,
        which is only possible if the 0.4 s mid pause was preserved (speech alone
        is ~1.0 s).
        """
        sr = 22050

        def _silence(secs):
            return np.zeros(int(sr * secs), dtype=np.float32)

        def _tone(secs):
            t = np.linspace(0, secs, int(sr * secs), endpoint=False)
            return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

        signal = np.concatenate([
            _silence(0.5),
            _tone(0.5),
            _silence(0.4),  # mid pause — must survive
            _tone(0.5),
            _silence(0.5),
        ])
        in_path = os.path.join(tmp_dir, "layered.wav")
        sf.write(in_path, signal, sr, subtype="PCM_16")

        out_path = os.path.join(output_dir, "layered_out.wav")
        seg = SegmentInput(id="seg-real", input_path=in_path, output_path=out_path)
        result = process_segment(seg, CleanupParams())

        assert result.error is None, result.error
        assert result.auto_rejected is False
        assert result.output_path == out_path

        data, out_sr = sf.read(out_path)
        out_secs = len(data) / out_sr
        # Mid pause survived: clearly more than the ~1.0 s of speech alone.
        assert out_secs > 1.15, f"mid pause was stripped (output {out_secs:.3f}s)"
        # Leading + trailing silence trimmed: clearly less than the 2.4 s input.
        assert out_secs < 1.9, f"lead/tail silence not trimmed (output {out_secs:.3f}s)"


# ---------------------------------------------------------------------------
# C2 — all-silence / sub-threshold input auto-rejected (loudnorm -inf)
# ---------------------------------------------------------------------------


SILENT_LOUDNORM_JSON = """{
    "input_i" : "-inf",
    "input_tp" : "-inf",
    "input_lra" : "0.00",
    "input_thresh" : "-inf",
    "output_i" : "-23.00",
    "output_tp" : "-2.00",
    "output_lra" : "0.00",
    "output_thresh" : "-33.32",
    "target_offset" : "0.00"
}"""


class TestIsSilentMeasurement:
    def test_negative_infinity_string(self):
        assert _is_silent_measurement("-inf") is True

    def test_float_negative_infinity(self):
        assert _is_silent_measurement(float("-inf")) is True

    def test_below_99_lufs(self):
        assert _is_silent_measurement("-120.5") is True

    def test_real_speech_loudness(self):
        assert _is_silent_measurement("-23.25") is False
        assert _is_silent_measurement("-9.0") is False


class TestSilentInputAutoRejected:
    def test_loudnorm_inf_short_circuits_to_auto_reject(self, tmp_dir, clean_wav, output_dir):
        """A pass-1 measurement of -inf must auto-reject with error=None and
        must NOT proceed to pass 2 (which would exit non-zero and mislabel it
        as ffmpeg_error)."""
        params = CleanupParams()
        out_path = os.path.join(output_dir, "seg_inf.wav")
        seg = SegmentInput(id="seg-inf", input_path=clean_wav, output_path=out_path)

        call_count = [0]

        def mock_run(cmd, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = SILENT_LOUDNORM_JSON if call_count[0] == 1 else ""
            return result

        with patch("cleaner.subprocess.run", side_effect=mock_run):
            result = process_segment(seg, params)

        assert result.auto_rejected is True
        assert result.error is None
        assert result.output_path is None
        # Only pass 1 ran — no pass 2, no trim.
        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# C3 — missing binary is a job-level failure, not a per-segment error
# ---------------------------------------------------------------------------


class TestBinaryNotFound:
    def test_missing_ffmpeg_raises_binary_not_found(self, tmp_dir, clean_wav, output_dir):
        """A missing ffmpeg binary must propagate BinaryNotFoundError, not be
        captured as a per-segment result."""
        params = CleanupParams()
        out_path = os.path.join(output_dir, "seg_nobin.wav")
        seg = SegmentInput(id="seg-nobin", input_path=clean_wav, output_path=out_path)

        with patch("cleaner.subprocess.run", side_effect=FileNotFoundError("ffmpeg")):
            with pytest.raises(BinaryNotFoundError):
                process_segment(seg, params)


# ---------------------------------------------------------------------------
# C4 — ffprobe failure is a per-segment error, not a false auto-reject
# ---------------------------------------------------------------------------


class TestProbeErrorNotAutoReject:
    def test_probe_error_becomes_segment_error(self, tmp_dir, clean_wav, output_dir):
        """If ffprobe fails on the trimmed output, the segment is a per-segment
        error (auto_rejected=False), never a silent auto-reject."""
        params = CleanupParams()
        out_path = os.path.join(output_dir, "seg_probe.wav")
        seg = SegmentInput(id="seg-probe", input_path=clean_wav, output_path=out_path)

        call_count = [0]

        def mock_run(cmd, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            if call_count[0] == 1:
                result.stderr = LOUDNORM_JSON
            else:
                result.stderr = ""
                sf.write(cmd[-1], np.zeros(22050, dtype=np.float32), 22050, subtype="PCM_16")
            return result

        with patch("cleaner.subprocess.run", side_effect=mock_run):
            with patch("cleaner._get_audio_duration", side_effect=ProbeError("boom")):
                result = process_segment(seg, params)

        assert result.auto_rejected is False
        assert result.output_path is None
        assert result.error is not None
        assert "ffmpeg_error" in result.error

    def test_get_audio_duration_raises_on_probe_failure(self):
        """_get_audio_duration raises ProbeError (not returns 0.0) when ffprobe
        exits non-zero."""
        from cleaner import _get_audio_duration

        failed = MagicMock()
        failed.returncode = 1
        failed.stdout = ""
        failed.stderr = "corrupt"
        with patch("cleaner.subprocess.run", return_value=failed):
            with pytest.raises(ProbeError):
                _get_audio_duration("/some/path.wav")

    def test_get_audio_duration_raises_binary_not_found(self):
        """_get_audio_duration raises BinaryNotFoundError when ffprobe is
        missing."""
        from cleaner import _get_audio_duration

        with patch("cleaner.subprocess.run", side_effect=FileNotFoundError("ffprobe")):
            with pytest.raises(BinaryNotFoundError):
                _get_audio_duration("/some/path.wav")
