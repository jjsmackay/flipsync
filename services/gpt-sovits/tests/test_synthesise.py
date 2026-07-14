"""Tests for the torch-free front half of engine.synthesise: bundle loading
and validation. The bundle must be verified *before* any vendored import —
TTS_Config silently falls back to the base pretrained weights when a path is
missing (research §4), which would make a broken bundle preview the wrong
voice instead of failing.
"""

from __future__ import annotations

import json
import os

import pytest

import engine


def _write_bundle(dir_path, *, skip=()):
    os.makedirs(dir_path, exist_ok=True)
    files = {
        "gpt.ckpt": b"PKgpt",
        "sovits.pth": b"05sovits",
        "reference.wav": b"RIFFref",
        "reference.txt": "A reference transcript.".encode(),
    }
    for name, content in files.items():
        if name in skip:
            continue
        with open(os.path.join(dir_path, name), "wb") as fh:
            fh.write(content)
    if "config.json" not in skip:
        with open(os.path.join(dir_path, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "engine": "gpt_sovits",
                    "version": "v2Pro",
                    "vendored_commit": engine.VENDORED_COMMIT,
                    "sample_rate": 32000,
                    "files": {
                        "gpt": "gpt.ckpt",
                        "sovits": "sovits.pth",
                        "reference_wav": "reference.wav",
                        "reference_text": "reference.txt",
                    },
                },
                fh,
            )


class TestLoadBundle:
    def test_resolves_absolute_paths_and_prompt_text(self, tmp_path):
        _write_bundle(str(tmp_path))
        bundle = engine.load_bundle(str(tmp_path))
        assert bundle["gpt_path"] == str(tmp_path / "gpt.ckpt")
        assert bundle["sovits_path"] == str(tmp_path / "sovits.pth")
        assert bundle["reference_wav_path"] == str(tmp_path / "reference.wav")
        assert bundle["prompt_text"] == "A reference transcript."

    def test_missing_config_json(self, tmp_path):
        _write_bundle(str(tmp_path), skip=("config.json",))
        with pytest.raises(RuntimeError, match="config.json"):
            engine.load_bundle(str(tmp_path))

    @pytest.mark.parametrize("missing", ["gpt.ckpt", "sovits.pth", "reference.wav", "reference.txt"])
    def test_missing_bundle_file(self, tmp_path, missing):
        _write_bundle(str(tmp_path), skip=(missing,))
        with pytest.raises(RuntimeError, match=missing):
            engine.load_bundle(str(tmp_path))


class TestSynthesiseValidation:
    def test_requires_checkpoint_dir(self, tmp_path):
        # Base-model (untrained) preview is not offered for this engine
        # (spec §5.3) — the orchestrator never sends a bare synthesise, but a
        # clear error beats a confusing upstream fallback if one slips in.
        with pytest.raises(RuntimeError, match="trained model"):
            engine.synthesise(
                text="hello",
                language="en",
                reference_wavs=[],
                checkpoint_dir=None,
                output_path=str(tmp_path / "out.wav"),
                params={},
            )

    def test_broken_bundle_fails_before_ml_imports(self, tmp_path):
        # No torch is installed in the test env: reaching this error proves
        # bundle validation happens before any vendored/ML import.
        bundle = tmp_path / "bundle"
        _write_bundle(str(bundle), skip=("sovits.pth",))
        with pytest.raises(RuntimeError, match="sovits.pth"):
            engine.synthesise(
                text="hello",
                language="en",
                reference_wavs=[],
                checkpoint_dir=str(bundle),
                output_path=str(tmp_path / "out.wav"),
                params={},
            )
