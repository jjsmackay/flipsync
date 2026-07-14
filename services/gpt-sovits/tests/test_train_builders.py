"""Unit tests for engine.py's pure training builders — no torch, no network.

Every expected value here is pinned against the vendored GPT-SoVITS commit
(research-gpt-sovits.md §1-§3): the env-var contract of the four prep scripts,
the s2 JSON / s1 YAML config fields webui sets before launching the training
scripts, the shard-merge file naming, and the checkpoint filename patterns.
"""

from __future__ import annotations

import copy
import os

import pytest

import engine


REPO = "/opt/GPT-SoVITS"
PRE = "/models/gpt-sovits"


# ---------------------------------------------------------------------------
# Prep-stage env dicts (research §1 — exact env-var contract per script)
# ---------------------------------------------------------------------------


class TestBuildPrepEnv:
    def _common(self):
        return {
            "inp_text": "/w/dataset.list",
            "inp_wav_dir": "",
            "exp_name": "flipsync",
            "i_part": "0",
            "all_parts": "1",
            "_CUDA_VISIBLE_DEVICES": "0",
            "opt_dir": "/w/exp",
            "is_half": "True",
            "version": "v2Pro",
        }

    def _build(self, stage):
        return engine.build_prep_env(
            stage,
            list_path="/w/dataset.list",
            exp_dir="/w/exp",
            repo_dir=REPO,
            pretrained_dir=PRE,
        )

    def test_stage_1_text(self):
        expected = self._common()
        expected["bert_pretrained_dir"] = f"{PRE}/chinese-roberta-wwm-ext-large"
        assert self._build("1") == expected

    def test_stage_2a_hubert(self):
        expected = self._common()
        expected["cnhubert_base_dir"] = f"{PRE}/chinese-hubert-base"
        assert self._build("2a") == expected

    def test_stage_2b_sv(self):
        expected = self._common()
        expected["sv_path"] = f"{PRE}/sv/pretrained_eres2netv2w24s4ep4.ckpt"
        assert self._build("2b") == expected

    def test_stage_3_semantic(self):
        expected = self._common()
        expected["pretrained_s2G"] = f"{PRE}/v2Pro/s2Gv2Pro.pth"
        expected["s2config_path"] = f"{REPO}/GPT_SoVITS/configs/s2v2Pro.json"
        assert self._build("3") == expected

    def test_unknown_stage_rejected(self):
        with pytest.raises(ValueError):
            self._build("4")

    def test_all_values_are_strings(self):
        # os.environ only takes strings; a stray int here fails at Popen time.
        for stage in ("1", "2a", "2b", "3"):
            assert all(isinstance(v, str) for v in self._build(stage).values())


# ---------------------------------------------------------------------------
# s2 (SoVITS) JSON config — fields per webui.py open1Ba (research §2)
# ---------------------------------------------------------------------------


def _s2_template():
    return {
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


class TestBuildS2Config:
    def _build(self, template=None):
        return engine.build_s2_config(
            template if template is not None else _s2_template(),
            exp_dir="/w/exp",
            weights_dir="/w/SoVITS_weights",
            pretrained_dir=PRE,
            epochs=8,
            batch_size=4,
        )

    def test_train_fields(self):
        cfg = self._build()
        train = cfg["train"]
        assert train["batch_size"] == 4
        assert train["epochs"] == 8
        assert train["text_low_lr_rate"] == 0.4
        assert train["pretrained_s2G"] == f"{PRE}/v2Pro/s2Gv2Pro.pth"
        assert train["pretrained_s2D"] == f"{PRE}/v2Pro/s2Dv2Pro.pth"
        assert train["if_save_latest"] is True
        assert train["if_save_every_weights"] is True
        # Single final save: the driver reads s2 progress from stdout, so
        # intermediate weight files would only burn disk/IO.
        assert train["save_every_epoch"] == 8
        assert train["gpu_numbers"] == "0"
        assert train["grad_ckpt"] is False
        assert train["lora_rank"] == 32
        assert train["fp16_run"] is True

    def test_paths_names_and_version(self):
        cfg = self._build()
        assert cfg["model"]["version"] == "v2Pro"
        assert cfg["data"]["exp_dir"] == "/w/exp"
        assert cfg["s2_ckpt_dir"] == "/w/exp"
        assert cfg["save_weight_dir"] == "/w/SoVITS_weights"
        assert cfg["name"] == "flipsync"
        assert cfg["version"] == "v2Pro"

    def test_template_not_mutated(self):
        template = _s2_template()
        snapshot = copy.deepcopy(template)
        self._build(template)
        assert template == snapshot


# ---------------------------------------------------------------------------
# s1 (GPT) YAML config — fields per webui.py open1Bb (research §2)
# ---------------------------------------------------------------------------


def _s1_template():
    return {
        "train": {
            "seed": 1234,
            "epochs": 20,
            "batch_size": 8,
            "save_every_n_epoch": 1,
            "precision": "16-mixed",
            "gradient_clip": 1.0,
        },
        "optimizer": {"lr": 0.01},
        "data": {"max_sec": 54},
        "model": {"vocab_size": 1025},
        "inference": {"top_k": 15},
    }


class TestBuildS1Config:
    def _build(self, template=None):
        return engine.build_s1_config(
            template if template is not None else _s1_template(),
            exp_dir="/w/exp",
            weights_dir="/w/GPT_weights",
            pretrained_dir=PRE,
            epochs=15,
            batch_size=4,
        )

    def test_train_fields(self):
        cfg = self._build()
        train = cfg["train"]
        assert train["batch_size"] == 4
        assert train["epochs"] == 15
        # Per-epoch saves: s1 has no parseable stdout, so the driver polls the
        # half-weights dir for new epoch checkpoints as its progress signal.
        assert train["save_every_n_epoch"] == 1
        assert train["if_save_every_weights"] is True
        assert train["if_save_latest"] is True
        assert train["if_dpo"] is False
        assert train["half_weights_save_dir"] == "/w/GPT_weights"
        assert train["exp_name"] == "flipsync"
        assert train["precision"] == "16-mixed"

    def test_paths(self):
        cfg = self._build()
        assert cfg["pretrained_s1"] == f"{PRE}/s1v3.ckpt"
        assert cfg["train_semantic_path"] == "/w/exp/6-name2semantic.tsv"
        assert cfg["train_phoneme_path"] == "/w/exp/2-name2text.txt"
        assert cfg["output_dir"] == "/w/exp/logs_s1_v2Pro"

    def test_no_version_field(self):
        # webui.py:628 deliberately comments out data["version"] for s1 — the
        # GPT model is version-agnostic; only the pretrained ckpt differs.
        assert "version" not in self._build()

    def test_template_not_mutated(self):
        template = _s1_template()
        snapshot = copy.deepcopy(template)
        self._build(template)
        assert template == snapshot


# ---------------------------------------------------------------------------
# Shard-output merges (single shard still writes `-0`-suffixed files;
# training reads the merged names — research §1)
# ---------------------------------------------------------------------------


class TestMergeStageOutputs:
    def test_stage1_merges_and_removes_part_file(self, tmp_path):
        exp = tmp_path / "exp"
        exp.mkdir()
        (exp / "2-name2text-0.txt").write_text(
            "a.wav\tph1\tw1\ttext one\nb.wav\tph2\tw2\ttext two\n", encoding="utf-8"
        )
        merged = engine.merge_stage1_output(str(exp))
        assert merged == str(exp / "2-name2text.txt")
        content = (exp / "2-name2text.txt").read_text(encoding="utf-8")
        assert content == "a.wav\tph1\tw1\ttext one\nb.wav\tph2\tw2\ttext two\n"
        assert not (exp / "2-name2text-0.txt").exists()

    def test_stage1_empty_output_raises(self, tmp_path):
        exp = tmp_path / "exp"
        exp.mkdir()
        (exp / "2-name2text-0.txt").write_text("\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="no rows"):
            engine.merge_stage1_output(str(exp))

    def test_stage3_merges_with_header(self, tmp_path):
        # webui's merge prepends the item_name/semantic_audio header line that
        # the s1 data module (pandas read_csv) expects.
        exp = tmp_path / "exp"
        exp.mkdir()
        (exp / "6-name2semantic-0.tsv").write_text(
            "a.wav\t1 2 3\nb.wav\t4 5 6\n", encoding="utf-8"
        )
        merged = engine.merge_stage3_output(str(exp))
        assert merged == str(exp / "6-name2semantic.tsv")
        content = (exp / "6-name2semantic.tsv").read_text(encoding="utf-8")
        assert content == "item_name\tsemantic_audio\na.wav\t1 2 3\nb.wav\t4 5 6\n"
        assert not (exp / "6-name2semantic-0.tsv").exists()

    def test_stage3_missing_part_file_raises(self, tmp_path):
        exp = tmp_path / "exp"
        exp.mkdir()
        with pytest.raises(RuntimeError, match="6-name2semantic"):
            engine.merge_stage3_output(str(exp))


# ---------------------------------------------------------------------------
# Final-weight selection (filename patterns pinned in research §2)
# ---------------------------------------------------------------------------


class TestSelectFinalWeights:
    def test_sovits_picks_highest_epoch_then_step(self):
        files = [
            "flipsync_e4_s400.pth",
            "flipsync_e8_s800.pth",
            "flipsync_e8_s900.pth",
            "junk.txt",
            "other_e9_s999.pth",  # different exp_name — not ours
        ]
        assert engine.select_final_sovits_weight(files) == "flipsync_e8_s900.pth"

    def test_sovits_none_when_no_match(self):
        assert engine.select_final_sovits_weight(["G_233.pth", "train.log"]) is None

    def test_gpt_picks_highest_epoch(self):
        files = ["flipsync-e5.ckpt", "flipsync-e15.ckpt", "flipsync-e9.ckpt", "last.ckpt"]
        assert engine.select_final_gpt_weight(files) == "flipsync-e15.ckpt"

    def test_gpt_none_when_no_match(self):
        assert engine.select_final_gpt_weight(["epoch=4-step=100.ckpt"]) is None


# ---------------------------------------------------------------------------
# Pretrained-weight manifest + idempotent skip (no network here)
# ---------------------------------------------------------------------------


class TestPretrainedManifest:
    def test_mandatory_file_set(self):
        # The exact v2Pro set from research §3: hubert + roberta dirs, shared
        # s1v3 GPT base, v2Pro G+D, and the speaker-verification ckpt (needed
        # at BOTH prep and inference).
        assert set(engine.PRETRAINED_FILES) == {
            "chinese-hubert-base/config.json",
            "chinese-hubert-base/preprocessor_config.json",
            "chinese-hubert-base/pytorch_model.bin",
            "chinese-roberta-wwm-ext-large/config.json",
            "chinese-roberta-wwm-ext-large/pytorch_model.bin",
            "chinese-roberta-wwm-ext-large/tokenizer.json",
            "s1v3.ckpt",
            "v2Pro/s2Gv2Pro.pth",
            "v2Pro/s2Dv2Pro.pth",
            "sv/pretrained_eres2netv2w24s4ep4.ckpt",
        }

    def _populate(self, root, files):
        for rel in files:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")

    def test_missing_lists_absent_files_only(self, tmp_path):
        present = ["s1v3.ckpt", "v2Pro/s2Gv2Pro.pth"]
        self._populate(tmp_path, present)
        missing = engine.missing_pretrained_files(str(tmp_path))
        assert set(missing) == set(engine.PRETRAINED_FILES) - set(present)

    def test_missing_empty_when_all_present(self, tmp_path):
        self._populate(tmp_path, engine.PRETRAINED_FILES)
        assert engine.missing_pretrained_files(str(tmp_path)) == []

    def test_ensure_skips_download_when_all_present(self, tmp_path, monkeypatch):
        self._populate(tmp_path, engine.PRETRAINED_FILES)

        def _boom(*a, **k):  # any network attempt fails the test
            raise AssertionError("download attempted despite complete cache")

        monkeypatch.setattr(engine, "_download_file", _boom)
        engine.ensure_pretrained_weights(str(tmp_path))

    def test_download_uses_socket_timeout(self, tmp_path, monkeypatch):
        # A stalled connection must not hang the single-worker executor
        # forever — _download_file must pass a socket timeout to urlopen
        # (covers both connect and inter-chunk read stalls).
        import io
        import urllib.request

        seen = {}

        def fake_urlopen(url, timeout=None):
            seen["url"] = url
            seen["timeout"] = timeout
            return io.BytesIO(b"weights")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        dest = tmp_path / "sub" / "file.bin"
        engine._download_file("https://example.invalid/file.bin", str(dest))

        assert seen["url"] == "https://example.invalid/file.bin"
        assert seen["timeout"] == 60
        assert dest.read_bytes() == b"weights"  # .part atomically renamed
        assert not (tmp_path / "sub" / "file.bin.part").exists()


# ---------------------------------------------------------------------------
# pretrained_models symlink (the sv model path is hardcoded upstream relative
# to the repo root — research §4; the link makes it resolve into the cache)
# ---------------------------------------------------------------------------


class TestEnsurePretrainedSymlink:
    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "GPT_SoVITS").mkdir(parents=True)
        return repo

    def test_creates_link_when_missing(self, tmp_path):
        repo = self._repo(tmp_path)
        pre = tmp_path / "cache"
        pre.mkdir()
        engine.ensure_pretrained_symlink(str(repo), str(pre))
        link = repo / "GPT_SoVITS" / "pretrained_models"
        assert link.is_symlink()
        assert os.path.realpath(link) == os.path.realpath(pre)

    def test_repoints_stale_link(self, tmp_path):
        repo = self._repo(tmp_path)
        old = tmp_path / "old"
        old.mkdir()
        new = tmp_path / "new"
        new.mkdir()
        link = repo / "GPT_SoVITS" / "pretrained_models"
        link.symlink_to(old)
        engine.ensure_pretrained_symlink(str(repo), str(new))
        assert os.path.realpath(link) == os.path.realpath(new)

    def test_replaces_empty_real_dir(self, tmp_path):
        repo = self._repo(tmp_path)
        (repo / "GPT_SoVITS" / "pretrained_models").mkdir()
        pre = tmp_path / "cache"
        pre.mkdir()
        engine.ensure_pretrained_symlink(str(repo), str(pre))
        assert (repo / "GPT_SoVITS" / "pretrained_models").is_symlink()

    def test_leaves_populated_real_dir_alone(self, tmp_path):
        # An operator-populated real directory is a valid layout — never
        # clobber it.
        repo = self._repo(tmp_path)
        target = repo / "GPT_SoVITS" / "pretrained_models"
        target.mkdir()
        (target / "s1v3.ckpt").write_bytes(b"x")
        pre = tmp_path / "cache"
        pre.mkdir()
        engine.ensure_pretrained_symlink(str(repo), str(pre))
        assert not target.is_symlink()
        assert (target / "s1v3.ckpt").exists()
