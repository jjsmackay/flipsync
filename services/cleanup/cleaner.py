"""Cleanup service processing logic.

Applies FFmpeg audio processing to segments:
1. EBU R128 two-pass loudness normalisation
2. Silence trimming + high-pass filter
3. Clipping detection via numpy
"""

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


class BinaryNotFoundError(Exception):
    """Raised when the ffmpeg/ffprobe binary is not installed.

    This is a job-level failure, not a per-segment failure: if the binary is
    missing for one segment it is missing for all of them. The caller must fail
    the whole job rather than record N per-segment errors.
    """


class ProbeError(Exception):
    """Raised when ffprobe runs but cannot determine a file's duration.

    Distinct from genuine silence (a valid, near-zero-duration file): a probe
    error means we could not measure the segment, so it becomes a per-segment
    error rather than a false auto-reject.
    """


@dataclass
class CleanupParams:
    target_lufs: float = -23.0
    true_peak_dbtp: float = -2.0
    lra: float = 7.0
    highpass_hz: int = 80
    silence_threshold_db: float = -50.0
    silence_min_duration_secs: float = 0.1
    clipping_threshold_db: float = -0.1
    clipping_min_consecutive_samples: int = 3
    output_sample_rate: int = 22050
    output_channels: int = 1


@dataclass
class SegmentInput:
    id: str
    input_path: str
    output_path: str


@dataclass
class SegmentResult:
    id: str
    output_path: Optional[str]
    clipping_warning: bool
    auto_rejected: bool
    error: Optional[str]


def run_ffmpeg(args: list[str]) -> tuple[int, str, str]:
    """Run an FFmpeg command. Returns (returncode, stdout, stderr)."""
    cmd = ["ffmpeg", "-y"] + args
    logger.debug("Running ffmpeg: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as e:
        raise BinaryNotFoundError("ffmpeg binary not found") from e
    return result.returncode, result.stdout, result.stderr


def _extract_loudnorm_json(stderr: str) -> dict:
    """Extract the loudnorm JSON block from ffmpeg stderr output."""
    # Find the last JSON block in stderr (loudnorm prints it at the end)
    json_pattern = re.compile(r'\{[^{}]+\}', re.DOTALL)
    matches = json_pattern.findall(stderr)
    for candidate in reversed(matches):
        try:
            data = json.loads(candidate)
            # Check it looks like loudnorm output
            if "input_i" in data:
                return data
        except json.JSONDecodeError:
            continue
    raise ValueError(f"Could not find loudnorm JSON in stderr:\n{stderr}")


def _get_audio_duration(path: str) -> float:
    """Get the duration of an audio file in seconds using ffprobe.

    Raises:
        BinaryNotFoundError: ffprobe is not installed (job-level failure).
        ProbeError: ffprobe ran but could not determine the duration. This is
            distinct from a genuine near-zero duration (silence), which returns
            a small float rather than raising.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as e:
        raise BinaryNotFoundError("ffprobe binary not found") from e

    if result.returncode != 0:
        raise ProbeError(f"ffprobe failed (exit {result.returncode}): {result.stderr.strip()[:300]}")
    try:
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        raise ProbeError(f"could not parse ffprobe output: {e}")


def _is_silent_measurement(measured_i) -> bool:
    """Return True if a loudnorm pass-1 integrated loudness indicates silence.

    Digital silence measures as ``-inf`` LUFS; near-silent content measures far
    below any real speech (which sits around -30 to -16 LUFS). Feeding ``-inf``
    into the pass-2 ``measured_I`` makes ffmpeg exit non-zero, which would
    otherwise be misreported as an ffmpeg_error instead of an auto-reject.
    """
    try:
        value = float(measured_i)
    except (TypeError, ValueError):
        # Non-numeric such as the literal string "-inf".
        return str(measured_i).strip().lower().lstrip("+").startswith("-inf")
    return value == float("-inf") or value < -99.0


def detect_clipping(
    audio_path: str,
    threshold_db: float,
    min_consecutive_samples: int,
) -> bool:
    """
    Read a WAV file and check if it has consecutive clipping samples.

    A segment is clipping if it has `min_consecutive_samples` or more
    consecutive samples at or above `threshold_db` dBFS.
    """
    try:
        data, _ = sf.read(audio_path, dtype="float32")
    except Exception as e:
        logger.warning("Could not read audio for clipping detection: %s", e)
        return False

    # Flatten to 1D (handle mono or stereo)
    if data.ndim > 1:
        # Check each channel independently; flag if any channel clips
        for ch in range(data.shape[1]):
            if _check_channel_clipping(data[:, ch], threshold_db, min_consecutive_samples):
                return True
        return False
    else:
        return _check_channel_clipping(data, threshold_db, min_consecutive_samples)


def _check_channel_clipping(
    samples: np.ndarray,
    threshold_db: float,
    min_consecutive: int,
) -> bool:
    """Check a 1D float array for consecutive clipping samples."""
    # Convert to dBFS: 20 * log10(|sample| + 1e-9)
    dbfs = 20.0 * np.log10(np.abs(samples.astype(np.float64)) + 1e-9)
    clipping = dbfs >= threshold_db

    if not np.any(clipping):
        return False

    # Count consecutive runs
    count = 0
    for val in clipping:
        if val:
            count += 1
            if count >= min_consecutive:
                return True
        else:
            count = 0
    return False


def process_segment(segment: SegmentInput, params: CleanupParams) -> SegmentResult:
    """
    Apply FFmpeg audio processing to a single segment.

    Processing chain:
    1. EBU R128 two-pass loudness normalisation
    2. Silence trimming + high-pass filter (combined)
    3. Clipping detection

    Returns a SegmentResult. Per-segment failures are captured in result.error.
    Raises BinaryNotFoundError if ffmpeg/ffprobe is missing — that is a
    job-level failure the caller must propagate, not a per-segment error.
    """
    segment_id = segment.id
    input_path = segment.input_path
    output_path = segment.output_path

    # Ensure output directory exists
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return SegmentResult(
            id=segment_id,
            output_path=None,
            clipping_warning=False,
            auto_rejected=False,
            error=f"ffmpeg_error: could not create output directory: {e}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        intermediate = os.path.join(tmpdir, "normalised.wav")
        trimmed = os.path.join(tmpdir, "trimmed.wav")

        # -----------------------------------------------------------------------
        # Pass 1: Analyse loudness
        # -----------------------------------------------------------------------
        pass1_args = [
            "-i", input_path,
            "-af",
            f"loudnorm=I={params.target_lufs}:TP={params.true_peak_dbtp}:LRA={params.lra}:print_format=json",
            "-f", "null",
            "-",
        ]
        rc, stdout, stderr = run_ffmpeg(pass1_args)
        if rc != 0:
            return SegmentResult(
                id=segment_id,
                output_path=None,
                clipping_warning=False,
                auto_rejected=False,
                error=f"ffmpeg_error: loudnorm pass 1 failed (exit {rc}): {stderr.strip()[:300]}",
            )

        try:
            loudnorm_data = _extract_loudnorm_json(stderr)
        except ValueError as e:
            return SegmentResult(
                id=segment_id,
                output_path=None,
                clipping_warning=False,
                auto_rejected=False,
                error=f"ffmpeg_error: could not parse loudnorm output: {e}",
            )

        measured_i = loudnorm_data["input_i"]
        measured_tp = loudnorm_data["input_tp"]
        measured_lra = loudnorm_data["input_lra"]
        measured_thresh = loudnorm_data["input_thresh"]
        target_offset = loudnorm_data["target_offset"]

        # If pass 1 measured (near-)silence, the segment has no usable audio.
        # Auto-reject it now: feeding measured_I=-inf into pass 2 makes ffmpeg
        # exit non-zero, which would be misreported as an ffmpeg_error.
        if _is_silent_measurement(measured_i):
            return SegmentResult(
                id=segment_id,
                output_path=None,
                clipping_warning=False,
                auto_rejected=True,
                error=None,
            )

        # -----------------------------------------------------------------------
        # Pass 2: Apply loudness normalisation + sample rate / channel conversion
        # -----------------------------------------------------------------------
        loudnorm_filter = (
            f"loudnorm=I={params.target_lufs}:TP={params.true_peak_dbtp}:LRA={params.lra}"
            f":measured_I={measured_i}:measured_TP={measured_tp}:measured_LRA={measured_lra}"
            f":measured_thresh={measured_thresh}:offset={target_offset}:linear=true"
        )
        pass2_args = [
            "-i", input_path,
            "-af", loudnorm_filter,
            "-ar", str(params.output_sample_rate),
            "-ac", str(params.output_channels),
            "-acodec", "pcm_s16le",
            intermediate,
        ]
        rc, stdout, stderr = run_ffmpeg(pass2_args)
        if rc != 0:
            return SegmentResult(
                id=segment_id,
                output_path=None,
                clipping_warning=False,
                auto_rejected=False,
                error=f"ffmpeg_error: loudnorm pass 2 failed (exit {rc}): {stderr.strip()[:300]}",
            )

        # -----------------------------------------------------------------------
        # Silence trimming (leading + trailing only) + high-pass filter
        # -----------------------------------------------------------------------
        # Trim ONLY leading and trailing silence; mid-segment pauses (natural
        # breaths, beats between phrases) must survive — they carry the pacing
        # the voice-clone dataset depends on. `stop_periods=-1` would strip
        # every internal pause, so instead: trim the head with one
        # start-trigger pass, then reverse the stream, trim the (now-leading)
        # tail with a second start-trigger pass, and reverse back.
        trim_leading = (
            f"silenceremove=start_periods=1"
            f":start_duration={params.silence_min_duration_secs}"
            f":start_threshold={params.silence_threshold_db}dB"
        )
        combined_filter = (
            f"{trim_leading},"
            f"areverse,{trim_leading},areverse,"
            f"highpass=f={params.highpass_hz}"
        )
        trim_args = [
            "-i", intermediate,
            "-af", combined_filter,
            "-acodec", "pcm_s16le",
            trimmed,
        ]
        rc, stdout, stderr = run_ffmpeg(trim_args)
        if rc != 0:
            return SegmentResult(
                id=segment_id,
                output_path=None,
                clipping_warning=False,
                auto_rejected=False,
                error=f"ffmpeg_error: silence trim failed (exit {rc}): {stderr.strip()[:300]}",
            )

        # -----------------------------------------------------------------------
        # Silent detection: check if output is < 0.05 seconds
        # -----------------------------------------------------------------------
        # A ProbeError means we could not measure the file, NOT that it is
        # silent — report it as a per-segment error rather than auto-rejecting
        # a potentially healthy segment. (BinaryNotFoundError propagates: a
        # missing ffprobe is a job-level failure.)
        try:
            duration = _get_audio_duration(trimmed)
        except ProbeError as e:
            return SegmentResult(
                id=segment_id,
                output_path=None,
                clipping_warning=False,
                auto_rejected=False,
                error=f"ffmpeg_error: could not probe trimmed output: {e}",
            )
        if duration < 0.05:
            return SegmentResult(
                id=segment_id,
                output_path=None,
                clipping_warning=False,
                auto_rejected=True,
                error=None,
            )

        # -----------------------------------------------------------------------
        # Copy trimmed output to final destination
        # -----------------------------------------------------------------------
        import shutil
        try:
            shutil.copy2(trimmed, output_path)
        except Exception as e:
            return SegmentResult(
                id=segment_id,
                output_path=None,
                clipping_warning=False,
                auto_rejected=False,
                error=f"ffmpeg_error: could not write output file: {e}",
            )

        # -----------------------------------------------------------------------
        # Clipping detection on the final output
        # -----------------------------------------------------------------------
        clipping = detect_clipping(
            output_path,
            params.clipping_threshold_db,
            params.clipping_min_consecutive_samples,
        )

        return SegmentResult(
            id=segment_id,
            output_path=output_path,
            clipping_warning=clipping,
            auto_rejected=False,
            error=None,
        )
