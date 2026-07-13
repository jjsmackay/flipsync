"""Unit tests for engine pure helpers.

Only the torch-free helpers are covered here; everything touching TTS/torch is
exercised by the manual GPU smoke script (``scripts/gpu_smoke.py``), not CI. A
guard test asserts the module imports without torch installed.
"""

from __future__ import annotations

import engine


def test_import_engine_requires_no_torch():
    # If this module needed torch at import time the suite would already have
    # failed to collect; assert the public seam is present regardless.
    assert engine.FINETUNE_MIN_VRAM_GB == 12.0
    assert callable(engine.finetune)
    assert callable(engine.synthesise)
    assert callable(engine.vram_available_gb)


class TestComputeEta:
    def test_zero_before_first_step(self):
        assert engine.compute_eta_secs(10.0, 0, 100) == 0.0

    def test_zero_at_completion(self):
        assert engine.compute_eta_secs(100.0, 100, 100) == 0.0

    def test_linear_extrapolation(self):
        # 10 s for 100 of 400 steps ⇒ 0.1 s/step × 300 remaining = 30 s.
        assert engine.compute_eta_secs(10.0, 100, 400) == 30.0

    def test_guards_zero_total(self):
        assert engine.compute_eta_secs(10.0, 5, 0) == 0.0


class TestOutputSampleRate:
    class _FakeArgs:
        def __init__(self, sr):
            self.output_sample_rate = sr

    class _FakeModel:
        def __init__(self, sr):
            self.args = TestOutputSampleRate._FakeArgs(sr)

    def test_reads_model_args(self):
        # XTTS-v2's HiFi-GAN decoder emits at model_args.output_sample_rate
        # (24000), not the 22050 the GPT operates at — read the real rate off
        # the model so the WAV header matches the samples.
        assert engine.output_sample_rate(self._FakeModel(24000)) == 24000

    def test_coerces_to_int(self):
        rate = engine.output_sample_rate(self._FakeModel(24000.0))
        assert rate == 24000
        assert isinstance(rate, int)


class TestSelectLatentWavs:
    def test_picks_largest_first_and_caps(self, tmp_path):
        rows = []
        for i, size in enumerate([100, 500, 300, 900, 50, 700, 20]):
            p = tmp_path / f"{i}.wav"
            p.write_bytes(b"x" * size)
            rows.append([str(p), "text", "target"])
        picked = engine.select_latent_wavs(rows, limit=5)
        assert len(picked) == 5
        # Sizes by index: 0=100 1=500 2=300 3=900 4=50 5=700 6=20.
        # Largest five, descending: 3(900) 5(700) 1(500) 2(300) 0(100).
        assert [p[-5] for p in picked] == ["3", "5", "1", "2", "0"]

    def test_missing_files_sort_last(self, tmp_path):
        real = tmp_path / "real.wav"
        real.write_bytes(b"x" * 1000)
        rows = [["/does/not/exist.wav", "t", "target"], [str(real), "t", "target"]]
        picked = engine.select_latent_wavs(rows, limit=5)
        assert picked[0] == str(real)
