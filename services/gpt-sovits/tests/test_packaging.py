"""Unit tests for engine.package_bundle — fake checkpoint files, no torch.

The bundle layout is spec §4: gpt.ckpt + sovits.pth + config.json +
reference.wav + reference.txt, all five mandatory. The SoVITS weight must be
copied byte-for-byte: v2Pro fine-tuned ``.pth`` files carry a 2-byte ``b"05"``
version header in place of the zip magic (process_ckpt.my_save2) — anything
that round-trips them through torch.load would corrupt them.

Reference WAVs are real files: packaging measures the audio actually on disk
(upstream hard-raises at synthesis time on references outside 3-10 s, so an
out-of-band reference must fail packaging, never ship).
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

import engine


# Fake fine-tuned SoVITS bytes with the v2Pro header where "PK" would be.
_SOVITS_BYTES = b"05" + b"\x03\x04fake-sovits-weights"
_GPT_BYTES = b"PK\x03\x04fake-gpt-weights"


@pytest.fixture
def staged(tmp_path, wav_file):
    """Fake work-dir weight files + manifest segments + real reference WAVs."""
    sovits = tmp_path / "SoVITS_weights" / "flipsync_e8_s800.pth"
    sovits.parent.mkdir()
    sovits.write_bytes(_SOVITS_BYTES)
    gpt = tmp_path / "GPT_weights" / "flipsync-e15.ckpt"
    gpt.parent.mkdir()
    gpt.write_bytes(_GPT_BYTES)

    ref_wav = wav_file("seg-in-band.wav", 6.4)
    other_wav = wav_file("seg-too-short.wav", 1.2)

    segments = [
        {
            "id": "aaa",
            "audio_file": other_wav,
            "text": "Too short to condition on.",
            "duration_secs": 1.2,
        },
        {
            "id": "bbb",
            "audio_file": ref_wav,
            "text": "A nicely sized reference line for conditioning.",
            "duration_secs": 6.4,
        },
    ]
    out = tmp_path / "bundle"
    return {"sovits": str(sovits), "gpt": str(gpt), "segments": segments,
            "ref_wav": ref_wav, "out": str(out)}


def _package(staged):
    return engine.package_bundle(
        output_dir=staged["out"],
        sovits_weight_path=staged["sovits"],
        gpt_weight_path=staged["gpt"],
        segments=staged["segments"],
        sample_rate=32000,
        train_params={"sovits_epochs": 8, "gpt_epochs": 15, "batch_size": 4},
    )


class TestPackageBundle:
    def test_writes_all_five_bundle_files(self, staged):
        _package(staged)
        for name in ("gpt.ckpt", "sovits.pth", "config.json", "reference.wav", "reference.txt"):
            assert os.path.exists(os.path.join(staged["out"], name)), name

    def test_weights_copied_byte_for_byte(self, staged):
        _package(staged)
        with open(os.path.join(staged["out"], "sovits.pth"), "rb") as fh:
            assert fh.read() == _SOVITS_BYTES  # b"05" header intact
        with open(os.path.join(staged["out"], "gpt.ckpt"), "rb") as fh:
            assert fh.read() == _GPT_BYTES

    def test_reference_is_the_in_band_segment(self, staged):
        _package(staged)
        with open(staged["ref_wav"], "rb") as fh:
            source_bytes = fh.read()
        with open(os.path.join(staged["out"], "reference.wav"), "rb") as fh:
            assert fh.read() == source_bytes
        with open(os.path.join(staged["out"], "reference.txt"), encoding="utf-8") as fh:
            assert fh.read() == "A nicely sized reference line for conditioning."

    def test_config_json_contents(self, staged):
        _package(staged)
        with open(os.path.join(staged["out"], "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        assert cfg["engine"] == "gpt_sovits"
        assert cfg["version"] == "v2Pro"
        assert cfg["vendored_commit"] == engine.VENDORED_COMMIT
        assert cfg["sample_rate"] == 32000
        # Relative filenames — the bundle must stay relocatable.
        assert cfg["files"] == {
            "gpt": "gpt.ckpt",
            "sovits": "sovits.pth",
            "reference_wav": "reference.wav",
            "reference_text": "reference.txt",
        }
        assert cfg["train_params"] == {
            "sovits_epochs": 8,
            "gpt_epochs": 15,
            "batch_size": 4,
        }

    def test_result_payload_shape(self, staged):
        result = _package(staged)
        out = staged["out"]
        assert result == {
            "checkpoint_dir": out,
            "gpt_path": os.path.join(out, "gpt.ckpt"),
            "sovits_path": os.path.join(out, "sovits.pth"),
            "config_path": os.path.join(out, "config.json"),
            "reference_wav_path": os.path.join(out, "reference.wav"),
            "reference_text_path": os.path.join(out, "reference.txt"),
            "final_eval_loss": None,
        }

    def test_creates_output_dir(self, staged, tmp_path):
        staged["out"] = str(tmp_path / "deep" / "nested" / "bundle")
        _package(staged)
        assert os.path.exists(os.path.join(staged["out"], "config.json"))


class TestReferenceEnforcement:
    """Packaging must never ship a bundle whose reference can't synthesise:
    upstream raises on any reference outside 3-10 s of 16 kHz audio, which
    would turn a 'ready' model into one where every preview fails forever."""

    def test_all_out_of_band_fails_with_reference_unavailable(self, staged, wav_file):
        staged["segments"] = [
            {"id": "aaa", "audio_file": wav_file("short-a.wav", 1.2),
             "text": "Too short.", "duration_secs": 1.2},
            {"id": "bbb", "audio_file": wav_file("short-b.wav", 2.5),
             "text": "Also too short.", "duration_secs": 2.5},
        ]
        with pytest.raises(RuntimeError, match="reference_unavailable"):
            _package(staged)

    def test_failure_writes_no_bundle_files(self, staged, wav_file):
        staged["segments"] = [
            {"id": "aaa", "audio_file": wav_file("short.wav", 1.0),
             "text": "Too short.", "duration_secs": 1.0},
        ]
        with pytest.raises(RuntimeError):
            _package(staged)
        assert not os.path.exists(os.path.join(staged["out"], "gpt.ckpt"))
        assert not os.path.exists(os.path.join(staged["out"], "sovits.pth"))

    def test_over_band_candidate_is_trimmed(self, staged, wav_file, monkeypatch):
        long_wav = wav_file("seg-long.wav", 12.0)
        staged["segments"] = [
            {"id": "ccc", "audio_file": long_wav,
             "text": "A long ramble that runs past the band.", "duration_secs": 12.0},
        ]

        trims = []

        def fake_trim(src, dest, secs):
            trims.append((src, dest, secs))
            with open(dest, "wb") as fh:
                fh.write(b"trimmed")

        monkeypatch.setattr(engine, "_trim_wav", fake_trim)
        _package(staged)

        ref_path = os.path.join(staged["out"], "reference.wav")
        assert trims == [(long_wav, ref_path, 9.5)]
        with open(ref_path, "rb") as fh:
            assert fh.read() == b"trimmed"
        with open(os.path.join(staged["out"], "reference.txt"), encoding="utf-8") as fh:
            assert fh.read() == "A long ramble that runs past the band."

    def test_measured_duration_beats_manifest_metadata(self, staged, wav_file):
        """Cleanup silence-trims after diarisation set duration_secs, so the
        WAV on disk can be shorter than the manifest claims — only the
        measured duration is safe to validate against the upstream band."""
        staged["segments"] = [
            {"id": "aaa", "audio_file": wav_file("claims-six.wav", 2.0),
             "text": "Manifest says six seconds.", "duration_secs": 6.0},
        ]
        with pytest.raises(RuntimeError, match="reference_unavailable"):
            _package(staged)

    def test_unreadable_wav_candidate_skipped(self, staged, tmp_path):
        garbage = tmp_path / "garbage.wav"
        garbage.write_bytes(b"RIFFnot-really-a-wav")
        staged["segments"] = [
            {"id": "aaa", "audio_file": str(garbage),
             "text": "This transcript is the longest of them all.", "duration_secs": 6.0},
        ] + staged["segments"]
        _package(staged)
        with open(os.path.join(staged["out"], "reference.txt"), encoding="utf-8") as fh:
            assert fh.read() == "A nicely sized reference line for conditioning."


class TestTrimWav:
    def test_builds_ffmpeg_trim_command(self, tmp_path, monkeypatch):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        engine._trim_wav("/in/src.wav", "/out/reference.wav", 9.5)

        (argv,) = calls
        assert argv[0] == "ffmpeg"
        assert argv[-1] == "/out/reference.wav"
        assert "/in/src.wav" in argv
        t_index = argv.index("-t")
        assert argv[t_index + 1] == "9.5"

    def test_ffmpeg_failure_raises_with_stderr_tail(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom-detail")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="boom-detail"):
            engine._trim_wav("/in/src.wav", "/out/reference.wav", 9.5)
