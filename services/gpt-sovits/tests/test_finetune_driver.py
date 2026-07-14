"""Driver-level tests for engine.finetune / engine.run_stage against a FAKE
vendored repo — real subprocesses, no torch, no GPU, no network.

The fake repo mirrors the vendored GPT-SoVITS layout (prep scripts, train
scripts, config templates) with tiny stand-ins that honour the same env-var /
config / output-file contract (research §1-§2), so the whole driver path —
manifest → .list → 4 prep stages → s2 → s1 → packaging — runs end-to-end in CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

import engine


# ---------------------------------------------------------------------------
# Fake vendored repo
# ---------------------------------------------------------------------------

_FAKE_PREP_TEXT = """
import os
rows = [r for r in open(os.environ["inp_text"], encoding="utf-8").read().splitlines() if r]
assert os.environ["inp_wav_dir"] == ""
assert os.environ["all_parts"] == "1"
opt = os.environ["opt_dir"]
with open(os.path.join(opt, "2-name2text-%s.txt" % os.environ["i_part"]), "w", encoding="utf-8") as f:
    for r in rows:
        f.write(r.split("|")[0] + "\\tph\\t1\\ttext\\n")
"""

_FAKE_PREP_HUBERT = """
import os
# The real script resolves imports via PYTHONPATH (repo root + GPT_SoVITS) —
# fail loudly here if the driver forgot to provide it.
pp = os.environ.get("PYTHONPATH", "").split(os.pathsep)
assert any(p.endswith("GPT_SoVITS") for p in pp), pp
assert os.environ["cnhubert_base_dir"].endswith("chinese-hubert-base")
"""

_FAKE_PREP_SV = """
import os
assert os.environ["sv_path"].endswith("pretrained_eres2netv2w24s4ep4.ckpt")
"""

_FAKE_PREP_SEMANTIC = """
import os
rows = [r for r in open(os.environ["inp_text"], encoding="utf-8").read().splitlines() if r]
assert os.environ["s2config_path"].endswith("GPT_SoVITS/configs/s2v2Pro.json")
opt = os.environ["opt_dir"]
with open(os.path.join(opt, "6-name2semantic-%s.tsv" % os.environ["i_part"]), "w", encoding="utf-8") as f:
    for r in rows:
        f.write(r.split("|")[0] + "\\t1 2 3\\n")
"""

_FAKE_S2_TRAIN = """
import argparse, json, os
p = argparse.ArgumentParser()
p.add_argument("-c", "--config")
a = p.parse_args()
cfg = json.load(open(a.config, encoding="utf-8"))
epochs = cfg["train"]["epochs"]
name = cfg["name"]
assert cfg["model"]["version"] == "v2Pro"
assert os.path.isfile(os.path.join(cfg["data"]["exp_dir"], "2-name2text.txt"))
print("start training from epoch 1", flush=True)
for e in range(1, epochs + 1):
    print("INFO:%s:Train Epoch: %d [50%%]" % (name, e), flush=True)
    print("INFO:%s:[1.1, 2.2, 3.3, 4.4, 0.5, 0.6, %d, 0.0001]" % (name, e * 10), flush=True)
    print("====> Epoch: %d" % e, flush=True)
with open(os.path.join(cfg["save_weight_dir"], "%s_e%d_s%d.pth" % (name, epochs, epochs * 10)), "wb") as f:
    f.write(b"05" + b"fake-sovits")
print("training done", flush=True)
"""

_FAKE_S1_TRAIN = """
import argparse, os, yaml
p = argparse.ArgumentParser()
p.add_argument("-c", "--config_file")
a = p.parse_args()
cfg = yaml.safe_load(open(a.config_file, encoding="utf-8"))
assert os.environ.get("hz") == "25hz"
assert os.path.isfile(cfg["train_semantic_path"])
with open(cfg["train_semantic_path"], encoding="utf-8") as f:
    assert f.readline().startswith("item_name\\t")
train = cfg["train"]
for e in range(1, train["epochs"] + 1):
    path = os.path.join(train["half_weights_save_dir"], "%s-e%d.ckpt" % (train["exp_name"], e))
    with open(path, "wb") as f:
        f.write(b"PK" + b"fake-gpt")
"""

_S2_TEMPLATE = {
    "train": {
        "log_interval": 100,
        "epochs": 100,
        "batch_size": 32,
        "fp16_run": True,
        "text_low_lr_rate": 0.4,
        "grad_ckpt": False,
    },
    "data": {"sampling_rate": 32000, "exp_dir": None},
    "model": {"version": None},
    "s2_ckpt_dir": "logs/s2/big2k1",
}

_S1_TEMPLATE_YAML = textwrap.dedent(
    """
    train:
      seed: 1234
      epochs: 20
      batch_size: 8
      save_every_n_epoch: 1
      precision: 16-mixed
    optimizer:
      lr: 0.01
    data:
      max_sec: 54
    model:
      vocab_size: 1025
    """
)


@pytest.fixture
def fake_repo(tmp_path):
    repo = tmp_path / "repo"
    prep = repo / "GPT_SoVITS" / "prepare_datasets"
    configs = repo / "GPT_SoVITS" / "configs"
    prep.mkdir(parents=True)
    configs.mkdir(parents=True)
    (prep / "1-get-text.py").write_text(_FAKE_PREP_TEXT, encoding="utf-8")
    (prep / "2-get-hubert-wav32k.py").write_text(_FAKE_PREP_HUBERT, encoding="utf-8")
    (prep / "2-get-sv.py").write_text(_FAKE_PREP_SV, encoding="utf-8")
    (prep / "3-get-semantic.py").write_text(_FAKE_PREP_SEMANTIC, encoding="utf-8")
    (repo / "GPT_SoVITS" / "s2_train.py").write_text(_FAKE_S2_TRAIN, encoding="utf-8")
    (repo / "GPT_SoVITS" / "s1_train.py").write_text(_FAKE_S1_TRAIN, encoding="utf-8")
    (configs / "s2v2Pro.json").write_text(json.dumps(_S2_TEMPLATE), encoding="utf-8")
    (configs / "s1longer-v2.yaml").write_text(_S1_TEMPLATE_YAML, encoding="utf-8")
    return repo


@pytest.fixture
def pretrained_cache(tmp_path):
    cache = tmp_path / "pretrained"
    for rel in engine.PRETRAINED_FILES:
        path = cache / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return cache


@pytest.fixture
def train_env(fake_repo, pretrained_cache, monkeypatch):
    monkeypatch.setenv("GPT_SOVITS_REPO_DIR", str(fake_repo))
    monkeypatch.setenv("GPT_SOVITS_PRETRAINED_DIR", str(pretrained_cache))
    return fake_repo


@pytest.fixture
def manifest(manifest_file, wav_file):
    # Real WAVs: packaging measures the audio on disk to enforce the 3-10 s
    # reference band, so fake RIFF bytes would fail the whole driver run.
    return manifest_file(
        [
            {
                "id": "a",
                "audio_file": wav_file("seg-a.wav", 4.0),
                "text": "First training line.",
                "duration_secs": 4.0,
            },
            {
                "id": "b",
                "audio_file": wav_file("seg-b.wav", 6.0),
                "text": "Second training line, a little longer.",
                "duration_secs": 6.0,
            },
        ]
    )


# ---------------------------------------------------------------------------
# run_stage — the shared subprocess runner
# ---------------------------------------------------------------------------


class TestRunStage:
    def test_success_streams_lines(self, tmp_path):
        seen = []
        engine.run_stage(
            "demo stage",
            [sys.executable, "-c", "print('one'); print('two')"],
            cwd=str(tmp_path),
            env={**os.environ},
            on_line=seen.append,
        )
        assert [s.strip() for s in seen] == ["one", "two"]

    def test_failure_raises_with_stage_and_stderr_tail(self, tmp_path):
        code = "import sys; print('ctx line'); print('boom-detail', file=sys.stderr); sys.exit(3)"
        with pytest.raises(RuntimeError) as exc:
            engine.run_stage(
                "prep stage 1 (text)",
                [sys.executable, "-c", code],
                cwd=str(tmp_path),
                env={**os.environ},
            )
        msg = str(exc.value)
        assert "prep stage 1 (text)" in msg
        assert "exit 3" in msg
        assert "boom-detail" in msg

    def test_poll_callback_invoked_while_running(self, tmp_path):
        polls = []
        engine.run_stage(
            "slow stage",
            [sys.executable, "-c", "import time; time.sleep(0.4)"],
            cwd=str(tmp_path),
            env={**os.environ},
            poll=lambda: polls.append(1),
            poll_interval=0.05,
        )
        assert polls  # fired at least once mid-run


# ---------------------------------------------------------------------------
# finetune — full driver against the fake repo
# ---------------------------------------------------------------------------


class TestFinetuneDriver:
    def _run(self, manifest, out_dir, progress):
        return engine.finetune(
            manifest_path=manifest,
            output_dir=str(out_dir),
            params={"sovits_epochs": 2, "gpt_epochs": 2, "batch_size": 1},
            progress_cb=progress.append,
        )

    def test_happy_path_produces_bundle(self, train_env, manifest, tmp_path):
        out = tmp_path / "model"
        progress: list[dict] = []
        result = self._run(manifest, out, progress)

        for name in ("gpt.ckpt", "sovits.pth", "config.json", "reference.wav", "reference.txt"):
            assert (out / name).exists(), name
        with open(out / "sovits.pth", "rb") as fh:
            assert fh.read().startswith(b"05")  # header untouched
        with open(out / "config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
        assert cfg["sample_rate"] == 32000  # read off the s2 template, not hardcoded
        assert cfg["train_params"]["sovits_epochs"] == 2
        assert result["checkpoint_dir"] == str(out)
        assert result["final_eval_loss"] is None
        # Longest-transcript in-band segment wins the reference slot.
        with open(out / "reference.txt", encoding="utf-8") as fh:
            assert fh.read() == "Second training line, a little longer."

    def test_progress_phases_in_order(self, train_env, manifest, tmp_path):
        progress: list[dict] = []
        self._run(manifest, tmp_path / "model", progress)
        phases = [p["phase"] for p in progress]
        # Deduplicate consecutive repeats to get the phase sequence.
        seq = [p for i, p in enumerate(phases) if i == 0 or phases[i - 1] != p]
        assert seq == ["preparing", "training_sovits", "training_gpt", "packaging"]

        sovits = [p for p in progress if p["phase"] == "training_sovits"]
        assert sovits[-1]["epoch"] == 2  # 1-indexed epochs, final boundary seen
        assert sovits[-1]["total_epochs"] == 2
        gpt = [p for p in progress if p["phase"] == "training_gpt"]
        assert gpt[-1]["epoch"] == 2
        assert gpt[-1]["total_epochs"] == 2

    def test_work_dir_removed_on_success(self, train_env, manifest, tmp_path):
        out = tmp_path / "model"
        self._run(manifest, out, [])
        assert not (out / "work").exists()

    def test_prep_failure_surfaces_stage_and_stderr(self, train_env, manifest, tmp_path):
        bad = train_env / "GPT_SoVITS" / "prepare_datasets" / "1-get-text.py"
        bad.write_text(
            "import sys; print('boom-detail', file=sys.stderr); sys.exit(2)",
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError) as exc:
            self._run(manifest, tmp_path / "model", [])
        msg = str(exc.value)
        assert "prep stage 1" in msg
        assert "boom-detail" in msg
        # Work dir is kept on failure for diagnosis.
        assert (tmp_path / "model" / "work").exists()

    def test_s2_without_weight_file_fails_clearly(self, train_env, manifest, tmp_path):
        (train_env / "GPT_SoVITS" / "s2_train.py").write_text(
            "print('training done')", encoding="utf-8"
        )
        with pytest.raises(RuntimeError, match="no .*weight"):
            self._run(manifest, tmp_path / "model", [])

    def test_empty_manifest_transcripts_fail_before_subprocesses(
        self, train_env, manifest_file, tmp_path
    ):
        wav = tmp_path / "seg.wav"
        wav.write_bytes(b"RIFF")
        path = manifest_file(
            [{"id": "a", "audio_file": str(wav), "text": "   ", "duration_secs": 4.0}]
        )
        with pytest.raises(Exception, match="segment|transcript"):
            self._run(path, tmp_path / "model", [])

    def test_download_failure_fails_job_with_clear_error(
        self, train_env, manifest, tmp_path, monkeypatch
    ):
        # Wipe one cached file so a download is needed, then make it fail.
        missing = os.path.join(os.environ["GPT_SOVITS_PRETRAINED_DIR"], "s1v3.ckpt")
        os.remove(missing)

        def _fail(url, dest):
            raise OSError("connection reset")

        monkeypatch.setattr(engine, "_download_file", _fail)
        with pytest.raises(RuntimeError, match="pretrained.*s1v3.ckpt"):
            self._run(manifest, tmp_path / "model", [])
