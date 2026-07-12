"""Shared test fixtures for the transcription service."""

import math
import struct
import wave
import pytest
import tempfile
import os


def _write_sine_wav(path: str, duration_secs: float = 5.0, sample_rate: int = 16000, frequency: float = 440.0) -> str:
    """Write a sine-wave WAV file to path. Returns path."""
    n_samples = int(sample_rate * duration_secs)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        for i in range(n_samples):
            sample = int(32767 * math.sin(2 * math.pi * frequency * i / sample_rate))
            wf.writeframes(struct.pack("<h", sample))
    return path


@pytest.fixture(scope="session")
def sine_wav_path(tmp_path_factory):
    """5-second 440 Hz sine WAV file. Created once per test session."""
    d = tmp_path_factory.mktemp("wav_fixtures")
    p = str(d / "sine_5s.wav")
    _write_sine_wav(p, duration_secs=5.0)
    return p


@pytest.fixture()
def write_wav(tmp_path):
    """Factory fixture: write a sine WAV into this test's tmp dir.

    Used by re-segmentation tests that slice child WAVs next to the parent —
    a per-test directory keeps assertions about created files clean.
    """
    def _write(name: str = "parent.wav", duration_secs: float = 5.0, sample_rate: int = 16000) -> str:
        return _write_sine_wav(str(tmp_path / name), duration_secs, sample_rate)

    return _write


@pytest.fixture(scope="session")
def short_wav_path(tmp_path_factory):
    """0.3-second WAV file (shorter than the 0.5s threshold)."""
    d = tmp_path_factory.mktemp("wav_fixtures")
    p = str(d / "short_0.3s.wav")
    _write_sine_wav(p, duration_secs=0.3)
    return p
