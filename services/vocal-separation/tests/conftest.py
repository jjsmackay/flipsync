"""Shared test fixtures for vocal-separation service tests."""

from __future__ import annotations

import math
import os
import struct
import wave

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# WAV fixture helpers
# ---------------------------------------------------------------------------

SAMPLE_RATE = 44100
DURATION_SECS = 5
FREQUENCY_HZ = 440  # A4 sine tone


def _write_wav(path: str, sample_rate: int, duration_secs: float, frequency: float = 440.0) -> None:
    """Write a stereo sine-wave WAV file."""
    n_samples = int(sample_rate * duration_secs)
    t = np.linspace(0.0, duration_secs, n_samples, endpoint=False)
    mono = (np.sin(2 * math.pi * frequency * t) * 32767).astype(np.int16)
    stereo = np.stack([mono, mono], axis=1)  # (samples, channels)

    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(stereo.tobytes())


@pytest.fixture(scope="session")
def tmp_audio_dir(tmp_path_factory):
    """Session-scoped temp directory for audio fixtures."""
    return tmp_path_factory.mktemp("audio")


@pytest.fixture(scope="session")
def sample_wav(tmp_audio_dir) -> str:
    """Path to a short stereo 44.1 kHz WAV fixture (5 seconds, 440 Hz sine)."""
    path = str(tmp_audio_dir / "sample.wav")
    _write_wav(path, SAMPLE_RATE, DURATION_SECS, FREQUENCY_HZ)
    return path


@pytest.fixture(scope="session")
def long_wav(tmp_audio_dir) -> str:
    """Path to a longer WAV fixture used for chunking tests (12 seconds)."""
    path = str(tmp_audio_dir / "long.wav")
    _write_wav(path, SAMPLE_RATE, 12.0, 440.0)
    return path
