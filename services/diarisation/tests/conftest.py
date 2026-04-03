"""Shared test fixtures for the diarisation service."""

import io
import math
import os
import struct
import tempfile
from pathlib import Path

import numpy as np
import pytest


def _write_wav(path: str, samples: np.ndarray, sample_rate: int = 16000):
    """Write a numpy float32 array as a 16-bit PCM WAV file (no external deps)."""
    # Convert float32 [-1,1] to int16
    int_samples = (samples * 32767).astype(np.int16)
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(int_samples) * 2  # 2 bytes per int16 sample

    with open(path, "wb") as f:
        # RIFF header
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))  # ChunkSize
        f.write(b"WAVE")
        # fmt sub-chunk
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))  # SubChunk1Size (PCM)
        f.write(struct.pack("<H", 1))   # AudioFormat (PCM=1)
        f.write(struct.pack("<H", num_channels))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", bits_per_sample))
        # data sub-chunk
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(int_samples.tobytes())


@pytest.fixture(scope="session")
def sample_wav_path(tmp_path_factory):
    """10-second 440 Hz sine wave WAV at 16 kHz."""
    duration = 10.0
    sample_rate = 16000
    t = np.linspace(0, duration, int(duration * sample_rate), endpoint=False)
    samples = 0.5 * np.sin(2 * math.pi * 440 * t).astype(np.float32)

    tmp_dir = tmp_path_factory.mktemp("audio")
    wav_path = str(tmp_dir / "test_audio.wav")
    _write_wav(wav_path, samples, sample_rate)
    return wav_path


@pytest.fixture(scope="session")
def reference_wav_path(tmp_path_factory):
    """2-second 880 Hz sine wave WAV at 16 kHz (used as reference clip)."""
    duration = 2.0
    sample_rate = 16000
    t = np.linspace(0, duration, int(duration * sample_rate), endpoint=False)
    samples = 0.5 * np.sin(2 * math.pi * 880 * t).astype(np.float32)

    tmp_dir = tmp_path_factory.mktemp("ref")
    wav_path = str(tmp_dir / "reference.wav")
    _write_wav(wav_path, samples, sample_rate)
    return wav_path


@pytest.fixture()
def output_dir(tmp_path):
    """Temporary output directory for segment WAVs."""
    out = tmp_path / "segments"
    out.mkdir()
    return str(out)
