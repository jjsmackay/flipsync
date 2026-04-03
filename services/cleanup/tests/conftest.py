"""Shared test fixtures for the cleanup service tests."""

import os
import tempfile

import numpy as np
import pytest
import soundfile as sf


def _write_wav(path: str, samples: np.ndarray, sample_rate: int = 22050) -> str:
    """Write a float32 numpy array as a 16-bit PCM WAV file."""
    # soundfile accepts float arrays; specify subtype for 16-bit PCM
    sf.write(path, samples, sample_rate, subtype="PCM_16")
    return path


@pytest.fixture
def tmp_dir():
    """Provide a temporary directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def clean_wav(tmp_dir):
    """Normal sine wave, 3 seconds, 22050 Hz mono, amplitude 0.3."""
    sample_rate = 22050
    duration = 3.0
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    samples = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    path = os.path.join(tmp_dir, "clean.wav")
    return _write_wav(path, samples, sample_rate)


@pytest.fixture
def clipping_wav(tmp_dir):
    """
    Sine wave where 15 consecutive samples are at max amplitude (will clip at -0.1 dBFS).
    The rest of the signal is a normal amplitude sine wave.
    """
    sample_rate = 22050
    duration = 3.0
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    samples = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    # Insert 15 consecutive clipping samples near the middle
    mid = len(samples) // 2
    samples[mid : mid + 15] = 1.0  # full scale = 0 dBFS
    path = os.path.join(tmp_dir, "clipping.wav")
    return _write_wav(path, samples, sample_rate)


@pytest.fixture
def silent_wav(tmp_dir):
    """WAV file filled with zeros (pure silence)."""
    sample_rate = 22050
    duration = 3.0
    samples = np.zeros(int(sample_rate * duration), dtype=np.float32)
    path = os.path.join(tmp_dir, "silent.wav")
    return _write_wav(path, samples, sample_rate)


@pytest.fixture
def output_dir(tmp_dir):
    """A writable output directory for processed files."""
    out = os.path.join(tmp_dir, "output")
    os.makedirs(out, exist_ok=True)
    return out
