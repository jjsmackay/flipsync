"""Audio helpers: concat_wavs (stitch) crossfade join."""

import asyncio
import math
import shutil
import struct
import wave

import pytest

from audio import CROSSFADE_SECS, concat_wavs, get_duration


def _tone_wav(path, secs, sr=44100, freq=220.0):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(int(sr * secs)):
            frames += struct.pack("<h", int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / sr)))
        w.writeframes(bytes(frames))


def test_concat_wavs_single_input_returns_false(tmp_path):
    a = tmp_path / "a.wav"
    _tone_wav(a, 0.5)
    # Fewer than 2 inputs → nothing to join; guarded before ffmpeg runs.
    assert asyncio.run(concat_wavs([str(a)], str(tmp_path / "out.wav"))) is False


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_concat_wavs_joins_with_crossfade(tmp_path):
    a, b, out = tmp_path / "a.wav", tmp_path / "b.wav", tmp_path / "out.wav"
    _tone_wav(a, 1.0)
    _tone_wav(b, 1.0)
    assert asyncio.run(concat_wavs([str(a), str(b)], str(out)))
    assert out.exists()
    dur = asyncio.run(get_duration(str(out)))
    # Two 1 s clips joined with one crossfade overlap ≈ 2.0 − CROSSFADE_SECS.
    assert abs(dur - (2.0 - CROSSFADE_SECS)) < 0.15


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_concat_wavs_three_inputs(tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"{i}.wav"
        _tone_wav(p, 1.0)
        paths.append(str(p))
    out = tmp_path / "out.wav"
    assert asyncio.run(concat_wavs(paths, str(out)))
    dur = asyncio.run(get_duration(str(out)))
    # Three 1 s clips, two crossfade overlaps ≈ 3.0 − 2*CROSSFADE_SECS.
    assert abs(dur - (3.0 - 2 * CROSSFADE_SECS)) < 0.2
