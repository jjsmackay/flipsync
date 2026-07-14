"""Shared fixtures for the GPT-SoVITS service tests.

The engine module (torch / vendored GPT-SoVITS) is never imported here: the
API-layer tests patch ``engine.finetune`` / ``engine.synthesise`` /
``engine.vram_available_gb`` as module attributes, so the suite runs without
any ML deps. No CPML-style acceptance gate exists for this service (the
GPT-SoVITS pretrained weights are MIT and public) so, unlike xtts, there is no
env var to set before the app's lifespan runs.
"""

from __future__ import annotations

import json
import wave

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """Module-scoped TestClient with a clean job store."""
    import main as svc

    svc._jobs.clear()
    with TestClient(svc.app) as c:
        yield c
    svc._jobs.clear()


@pytest.fixture
def wav_file(tmp_path):
    """Factory writing a silent 16 kHz mono PCM WAV of a given real duration.

    Packaging measures the audio actually on disk (not manifest metadata), so
    tests exercising reference selection need real, readable WAV files.
    """

    def _write(name: str, secs: float, sample_rate: int = 16000) -> str:
        path = str(tmp_path / name)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(b"\x00\x00" * int(secs * sample_rate))
        return path

    return _write


@pytest.fixture
def manifest_file(tmp_path):
    """Factory writing a dataset manifest and returning its path."""

    def _write(segments: list[dict]) -> str:
        path = str(tmp_path / "dataset.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "version": "1",
                    "project_id": "p1",
                    "speaker": "target",
                    "segments": segments,
                },
                fh,
            )
        return path

    return _write
