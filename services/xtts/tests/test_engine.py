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


class TestPruneRunDirs:
    """`run-*/` are the trainer's timestamped scratch dirs (~11 GB of duplicate
    checkpoints). Once the best checkpoint is copied to the root `model.pth`
    bundle they are dead weight and left the live host's /data at 100%."""

    def _bundle(self, d):
        for name in ("model.pth", "config.json", "vocab.json", "speaker_latents.pt"):
            (d / name).write_bytes(b"x")

    def test_removes_run_dirs_keeps_bundle(self, tmp_path):
        self._bundle(tmp_path)
        run = tmp_path / "run-July-14-2026_05+35AM-0000000"
        (run / "sub").mkdir(parents=True)
        (run / "best_model.pth").write_bytes(b"x" * 10)
        (run / "best_model_390.pth").write_bytes(b"x" * 10)

        removed = engine._prune_run_dirs(str(tmp_path))

        assert removed == 1
        assert not run.exists()
        for name in ("model.pth", "config.json", "vocab.json", "speaker_latents.pt"):
            assert (tmp_path / name).exists()

    def test_removes_multiple_run_dirs(self, tmp_path):
        for stamp in ("run-a", "run-b", "run-c"):
            (tmp_path / stamp).mkdir()
        assert engine._prune_run_dirs(str(tmp_path)) == 3
        assert not list(tmp_path.glob("run-*"))

    def test_leaves_non_run_dirs_and_files(self, tmp_path):
        (tmp_path / "dataset").mkdir()
        (tmp_path / "model.pth").write_bytes(b"x")
        (tmp_path / "run-x").mkdir()
        engine._prune_run_dirs(str(tmp_path))
        assert (tmp_path / "dataset").exists()
        assert (tmp_path / "model.pth").exists()
        assert not (tmp_path / "run-x").exists()

    def test_missing_output_dir_is_noop(self, tmp_path):
        assert engine._prune_run_dirs(str(tmp_path / "nope")) == 0
