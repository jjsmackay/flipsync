"""GPT-SoVITS engine — fine-tune + synthesise boundary.

This module is the boundary between the FastAPI job layer (``main.py``) and
the heavy torch / vendored-GPT-SoVITS machinery. **All torch / GPT-SoVITS
imports are lazy (inside functions)** so the service — and its test suite —
can import this module without those packages installed. The API-layer tests
patch these functions as module attributes; only the GPU smoke script
(``scripts/gpu_smoke.py``) exercises the real implementations.

Training drives the vendored repo's prep + train stages as subprocesses,
reproducing webui.py's env-var / config-file launch contract exactly as
pinned in research-gpt-sovits.md §1–§2 (the vendored scripts are env-driven
with no CLI args, except the two train scripts which take a config path).
Synthesis imports the vendored ``TTS_infer_pack`` in-process for model
caching + idle-VRAM unload.

Everything above the "GPU-backed implementations" divider is pure stdlib and
unit-tested without torch.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Callable, Optional

import dataset
import progress

logger = logging.getLogger(__name__)

# The exact upstream commit vendored into the Docker image (also recorded in
# the Dockerfile). Baked into every bundle's config.json so a trained model
# records the code that produced it.
VENDORED_COMMIT = "be6a4f1e9d8a22d41b7d42c22df9d7ef36f225d2"

# FlipSync trains the v2Pro line only (spec §1): few-shot quality on
# average-quality audio, MIT-licensed, no acceptance gate.
MODEL_VERSION = "v2Pro"

# Experiment name used for upstream's checkpoint filenames
# ({exp}_e{N}_s{S}.pth / {exp}-e{N}.ckpt). Fixed: one fine-tune per job dir.
_EXP_NAME = "flipsync"

# Minimum free VRAM (GB) required to start a fine-tune. Provisional per spec
# §3/§11 (GPT-SoVITS trains lighter than XTTS); env-overridable so the real
# value can be pinned on the deploy host without an image rebuild.
try:
    FINETUNE_MIN_VRAM_GB = float(os.environ.get("FINETUNE_MIN_VRAM_GB") or 8.0)
except ValueError:
    FINETUNE_MIN_VRAM_GB = 8.0

# Public HF repo carrying every pretrained asset (no token, MIT) — research §3.
_HF_BASE = "https://huggingface.co/lj1995/GPT-SoVITS/resolve/main"

# Mandatory pretrained files for v2Pro training + inference, relative to
# GPT_SOVITS_PRETRAINED_DIR. Exact set pinned against the live HF file tree
# (research §3): BERT + HuBERT dirs, the shared s1v3 GPT base, the v2Pro
# SoVITS G (inference + prep stage 3) and D (training only), and the
# speaker-verification model — required at BOTH prep (2-get-sv.py) and
# inference (TTS.init_sv_model).
PRETRAINED_FILES = (
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
)

_STAGE_SCRIPTS = {
    "1": "GPT_SoVITS/prepare_datasets/1-get-text.py",
    "2a": "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py",
    "2b": "GPT_SoVITS/prepare_datasets/2-get-sv.py",
    "3": "GPT_SoVITS/prepare_datasets/3-get-semantic.py",
}

# One-slot synthesis-model cache, mirroring the xtts engine's convention:
# ``(checkpoint_dir, model)``. Safe without locks: the job layer runs
# everything on a single-worker executor.
_model_cache: Optional[tuple] = None


def _repo_dir() -> str:
    """Vendored GPT-SoVITS checkout (cloned into the image at the pin)."""
    return os.environ.get("GPT_SOVITS_REPO_DIR", "/opt/GPT-SoVITS")


def _pretrained_dir() -> str:
    """Pretrained-weight cache (bind-mounted; survives container recreate)."""
    return os.environ.get("GPT_SOVITS_PRETRAINED_DIR", "/models/gpt-sovits")


def is_model_loaded() -> bool:
    """True while a synthesis model is resident (idle-unload watcher gate)."""
    return _model_cache is not None


def release_cached_model() -> None:
    """Drop the cached synthesis model and free its VRAM.

    Called by the job layer before a fine-tune (a lingering preview model
    would eat into the VRAM preflight budget) and by the idle-unload watcher.
    A no-op (and import-safe without torch) when nothing is cached.
    """
    global _model_cache
    if _model_cache is None:
        return
    _model_cache = None
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def vram_available_gb() -> float:
    """Free VRAM in GB on the active CUDA device, or 0.0 if CUDA is absent."""
    try:
        import torch

        if not torch.cuda.is_available():
            return 0.0
        free_bytes, _total = torch.cuda.mem_get_info()
        return free_bytes / 2**30
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Pretrained-weight cache (idempotent first-use download)
# ---------------------------------------------------------------------------


def missing_pretrained_files(pretrained_dir: str) -> list[str]:
    """The subset of PRETRAINED_FILES not yet present under the cache dir."""
    return [
        rel
        for rel in PRETRAINED_FILES
        if not os.path.isfile(os.path.join(pretrained_dir, rel))
    ]


def _download_file(url: str, dest: str) -> None:
    """Stream one file to ``dest`` via a temp path (no torn files on crash).

    The socket timeout covers the connect and any inter-chunk read stall —
    without it a hung connection during the ~1.2 GB first-use fetch would
    occupy the single-worker executor forever (job stuck "running", container
    restart the only recovery).
    """
    import urllib.request

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)
    os.replace(tmp, dest)


def ensure_pretrained_weights(pretrained_dir: str) -> None:
    """Download any missing pretrained files from the public HF repo.

    Idempotent: files already present are never re-fetched, so the shared
    bind-mounted cache is populated exactly once per file. The service stays
    healthy before weights arrive; a download failure fails the *job* with a
    clear error naming the file.
    """
    for rel in missing_pretrained_files(pretrained_dir):
        url = f"{_HF_BASE}/{rel}"
        dest = os.path.join(pretrained_dir, rel)
        try:
            _download_file(url, dest)
        except Exception as exc:
            raise RuntimeError(
                f"pretrained weight download failed for {rel}: {exc}"
            ) from exc


def ensure_pretrained_symlink(repo_dir: str, pretrained_dir: str) -> None:
    """Point ``{repo}/GPT_SoVITS/pretrained_models`` at the weight cache.

    The speaker-verification model path is hardcoded upstream relative to the
    repo root (GPT_SoVITS/sv.py — not TTS_Config-configurable), so inference
    only finds it if the repo's pretrained_models dir resolves into the cache.
    Re-points a stale symlink (env override); replaces an empty real dir;
    leaves a populated real dir alone (a valid operator-managed layout).
    """
    link = os.path.join(repo_dir, "GPT_SoVITS", "pretrained_models")
    if os.path.realpath(link) == os.path.realpath(pretrained_dir):
        return
    if os.path.islink(link):
        os.remove(link)
    elif os.path.isdir(link):
        if os.listdir(link):
            return  # operator populated the real dir — don't clobber it
        os.rmdir(link)
    os.symlink(pretrained_dir, link)


# ---------------------------------------------------------------------------
# Pure builders — env dicts + train configs (contract: research §1–§2)
# ---------------------------------------------------------------------------


def build_prep_env(
    stage: str, *, list_path: str, exp_dir: str, repo_dir: str, pretrained_dir: str
) -> dict[str, str]:
    """Exact env-var dict one prep script reads (webui.py's launch contract).

    Single-shard on the one visible GPU: ``i_part=0, all_parts=1``.
    ``inp_wav_dir`` stays empty so each `.list` row's absolute wav path is
    used verbatim (research §6).
    """
    env = {
        "inp_text": list_path,
        "inp_wav_dir": "",
        "exp_name": _EXP_NAME,
        "i_part": "0",
        "all_parts": "1",
        "_CUDA_VISIBLE_DEVICES": "0",
        "opt_dir": exp_dir,
        "is_half": "True",
        "version": MODEL_VERSION,
    }
    if stage == "1":
        env["bert_pretrained_dir"] = os.path.join(
            pretrained_dir, "chinese-roberta-wwm-ext-large"
        )
    elif stage == "2a":
        env["cnhubert_base_dir"] = os.path.join(pretrained_dir, "chinese-hubert-base")
    elif stage == "2b":
        env["sv_path"] = os.path.join(
            pretrained_dir, "sv/pretrained_eres2netv2w24s4ep4.ckpt"
        )
    elif stage == "3":
        env["pretrained_s2G"] = os.path.join(pretrained_dir, "v2Pro/s2Gv2Pro.pth")
        env["s2config_path"] = os.path.join(
            repo_dir, "GPT_SoVITS/configs/s2v2Pro.json"
        )
    else:
        raise ValueError(f"unknown prep stage: {stage}")
    return env


def build_s2_config(
    template: dict,
    *,
    exp_dir: str,
    weights_dir: str,
    pretrained_dir: str,
    epochs: int,
    batch_size: int,
) -> dict:
    """Fill the s2v2Pro.json template the way webui.py open1Ba does.

    ``save_every_epoch = epochs``: a single final weights save — s2 progress
    is parsed from stdout, so intermediate weight files would only burn disk.
    """
    cfg = copy.deepcopy(template)
    cfg["train"].update(
        {
            "batch_size": batch_size,
            "epochs": epochs,
            "text_low_lr_rate": 0.4,
            "pretrained_s2G": os.path.join(pretrained_dir, "v2Pro/s2Gv2Pro.pth"),
            "pretrained_s2D": os.path.join(pretrained_dir, "v2Pro/s2Dv2Pro.pth"),
            "if_save_latest": True,
            "if_save_every_weights": True,
            "save_every_epoch": epochs,
            "gpu_numbers": "0",
            "grad_ckpt": False,
            "lora_rank": 32,
        }
    )
    cfg["model"]["version"] = MODEL_VERSION
    cfg["data"]["exp_dir"] = exp_dir
    cfg["s2_ckpt_dir"] = exp_dir
    cfg["save_weight_dir"] = weights_dir
    cfg["name"] = _EXP_NAME
    cfg["version"] = MODEL_VERSION
    return cfg


def build_s1_config(
    template: dict,
    *,
    exp_dir: str,
    weights_dir: str,
    pretrained_dir: str,
    epochs: int,
    batch_size: int,
) -> dict:
    """Fill the s1longer-v2.yaml template the way webui.py open1Bb does.

    ``save_every_n_epoch = 1``: s1 has no parseable stdout (a redrawing
    Lightning tqdm bar), so per-epoch half-weight saves double as the
    driver's progress signal. No ``version`` field — the GPT architecture is
    version-agnostic; only the pretrained checkpoint differs (research §2).
    """
    cfg = copy.deepcopy(template)
    cfg["train"].update(
        {
            "batch_size": batch_size,
            "epochs": epochs,
            "save_every_n_epoch": 1,
            "if_save_every_weights": True,
            "if_save_latest": True,
            "if_dpo": False,
            "half_weights_save_dir": weights_dir,
            "exp_name": _EXP_NAME,
        }
    )
    cfg["pretrained_s1"] = os.path.join(pretrained_dir, "s1v3.ckpt")
    cfg["train_semantic_path"] = os.path.join(exp_dir, "6-name2semantic.tsv")
    cfg["train_phoneme_path"] = os.path.join(exp_dir, "2-name2text.txt")
    cfg["output_dir"] = os.path.join(exp_dir, f"logs_s1_{MODEL_VERSION}")
    return cfg


# ---------------------------------------------------------------------------
# Shard-output merges — single shard still writes `-{i_part}` files; the
# train scripts read the merged names (webui merges after each stage).
# ---------------------------------------------------------------------------


def merge_stage1_output(exp_dir: str) -> str:
    """``2-name2text-0.txt`` → ``2-name2text.txt`` (no header)."""
    part = os.path.join(exp_dir, "2-name2text-0.txt")
    merged = os.path.join(exp_dir, "2-name2text.txt")
    if not os.path.isfile(part):
        raise RuntimeError(f"prep stage 1 produced no output file ({part})")
    with open(part, "r", encoding="utf-8") as fh:
        rows = [line for line in fh.read().split("\n") if line.strip()]
    if not rows:
        raise RuntimeError(
            "prep stage 1 produced no rows — no segment survived text cleaning"
        )
    with open(merged, "w", encoding="utf-8") as fh:
        fh.write("\n".join(rows) + "\n")
    os.remove(part)
    return merged


def merge_stage3_output(exp_dir: str) -> str:
    """``6-name2semantic-0.tsv`` → ``6-name2semantic.tsv`` with the
    ``item_name\\tsemantic_audio`` header line the s1 data module expects."""
    part = os.path.join(exp_dir, "6-name2semantic-0.tsv")
    merged = os.path.join(exp_dir, "6-name2semantic.tsv")
    if not os.path.isfile(part):
        raise RuntimeError(f"prep stage 3 produced no output file ({part})")
    with open(part, "r", encoding="utf-8") as fh:
        body = fh.read().strip("\n")
    with open(merged, "w", encoding="utf-8") as fh:
        fh.write("item_name\tsemantic_audio\n")
        if body:
            fh.write(body + "\n")
    os.remove(part)
    return merged


# ---------------------------------------------------------------------------
# Final-weight selection (filename patterns pinned in research §2)
# ---------------------------------------------------------------------------


def select_final_sovits_weight(filenames: list[str]) -> Optional[str]:
    """Highest-(epoch, step) ``{exp}_e{N}_s{S}.pth`` from a dir listing."""
    import re

    pattern = re.compile(rf"^{re.escape(_EXP_NAME)}_e(\d+)_s(\d+)\.pth$")
    best: Optional[tuple[int, int, str]] = None
    for name in filenames:
        m = pattern.match(name)
        if m:
            key = (int(m.group(1)), int(m.group(2)), name)
            if best is None or key[:2] > best[:2]:
                best = key
    return best[2] if best else None


def select_final_gpt_weight(filenames: list[str]) -> Optional[str]:
    """Highest-epoch ``{exp}-e{N}.ckpt`` from a dir listing."""
    import re

    pattern = re.compile(rf"^{re.escape(_EXP_NAME)}-e(\d+)\.ckpt$")
    best: Optional[tuple[int, str]] = None
    for name in filenames:
        m = pattern.match(name)
        if m:
            key = (int(m.group(1)), name)
            if best is None or key[0] > best[0]:
                best = key
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Bundle packaging (spec §4)
# ---------------------------------------------------------------------------


def _measured_duration_secs(path: str) -> Optional[float]:
    """Real decoded duration of a WAV on disk, or None if unreadable.

    The manifest's ``duration_secs`` is diarisation-time metadata; cleanup
    silence-trims afterwards, so only the measured value is safe to validate
    the upstream 3-10 s reference band against.
    """
    try:
        import soundfile as sf

        info = sf.info(path)
        return float(info.frames) / float(info.samplerate)
    except Exception:
        return None


def _trim_wav(src: str, dest: str, secs: float) -> None:
    """Write the first ``secs`` seconds of ``src`` to ``dest`` via ffmpeg."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-i", src, "-t", str(secs), dest],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-500:]
        raise RuntimeError(
            f"reference trim failed (ffmpeg exit {proc.returncode}): {tail or '<no output>'}"
        )


def _plan_reference(segments: list[dict]) -> dict:
    """Measure candidates on disk and pick the reference (dataset's pure plan).

    Segments without a transcript or with unreadable audio are skipped. An
    unusable dataset raises ``reference_unavailable`` — failing the finetune
    job is strictly better than shipping a bundle upstream would reject on
    every synthesis (TTS.py hard-raises outside 3-10 s of reference audio).
    """
    candidates = []
    for seg in segments:
        if not (seg.get("text") or "").strip():
            continue
        secs = _measured_duration_secs(seg["audio_file"])
        if secs is None:
            continue
        candidates.append({**seg, "measured_secs": secs})
    try:
        return dataset.select_reference_plan(candidates)
    except ValueError as exc:
        raise RuntimeError(f"reference_unavailable: {exc}") from exc


def package_bundle(
    *,
    output_dir: str,
    sovits_weight_path: str,
    gpt_weight_path: str,
    segments: list[dict],
    sample_rate: int,
    train_params: dict,
) -> dict:
    """Assemble the five-file model bundle; return the job result payload.

    Weights are copied byte-for-byte (shutil.copyfile): v2Pro SoVITS ``.pth``
    files carry a 2-byte ``b"05"`` version header in place of the zip magic
    (process_ckpt.my_save2) — the inference loader detects the version from
    those bytes, so they must survive packaging untouched.

    The reference is planned first (measured on-disk durations, 3-10 s band
    enforced, over-band candidates trimmed) so an unusable dataset fails
    before any bundle file is written.
    """
    plan = _plan_reference(segments)

    os.makedirs(output_dir, exist_ok=True)
    gpt_path = os.path.join(output_dir, "gpt.ckpt")
    sovits_path = os.path.join(output_dir, "sovits.pth")
    shutil.copyfile(gpt_weight_path, gpt_path)
    shutil.copyfile(sovits_weight_path, sovits_path)

    ref = plan["segment"]
    ref_wav_path = os.path.join(output_dir, "reference.wav")
    ref_text_path = os.path.join(output_dir, "reference.txt")
    if plan["trim_to_secs"] is not None:
        _trim_wav(ref["audio_file"], ref_wav_path, plan["trim_to_secs"])
    else:
        shutil.copyfile(ref["audio_file"], ref_wav_path)
    with open(ref_text_path, "w", encoding="utf-8") as fh:
        fh.write((ref.get("text") or "").strip())

    config_path = os.path.join(output_dir, "config.json")
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "engine": "gpt_sovits",
                "version": MODEL_VERSION,
                "vendored_commit": VENDORED_COMMIT,
                "sample_rate": sample_rate,
                "files": {
                    "gpt": "gpt.ckpt",
                    "sovits": "sovits.pth",
                    "reference_wav": "reference.wav",
                    "reference_text": "reference.txt",
                },
                "train_params": train_params,
            },
            fh,
            indent=2,
        )

    return {
        "checkpoint_dir": output_dir,
        "gpt_path": gpt_path,
        "sovits_path": sovits_path,
        "config_path": config_path,
        "reference_wav_path": ref_wav_path,
        "reference_text_path": ref_text_path,
        # GPT-SoVITS training runs no eval pass; the orchestrator stores NULL.
        "final_eval_loss": None,
    }


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------


def run_stage(
    stage: str,
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    on_line: Optional[Callable[[str], None]] = None,
    poll: Optional[Callable[[], None]] = None,
    poll_interval: float = 2.0,
    tail_limit: int = 40,
) -> None:
    """Run one pipeline stage; raise with the stage name + output tail on failure.

    stdout/stderr are merged and streamed line-by-line to ``on_line`` from a
    reader thread (text mode's universal newlines turn tqdm's ``\\r`` redraws
    into separate lines, which the parsers simply ignore). ``poll`` fires
    every ``poll_interval`` seconds while the process runs — the s1 driver
    uses it to scan the checkpoint dir. The process is launched in its own
    session so a failure kills the whole tree (s2 spawns DDP children).
    """
    tail: deque[str] = deque(maxlen=tail_limit)

    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    def _read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stripped = line.rstrip("\n")
            if stripped.strip():
                tail.append(stripped)
            if on_line is not None:
                try:
                    on_line(stripped)
                except Exception:
                    # A progress hiccup must never kill the stage, but a
                    # parser bug must stay visible in the service logs.
                    logger.warning(
                        "%s: on_line handler failed for %r", stage, stripped,
                        exc_info=True,
                    )

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    try:
        last_poll = time.monotonic()
        while proc.poll() is None:
            time.sleep(0.1)
            if poll is not None and time.monotonic() - last_poll >= poll_interval:
                last_poll = time.monotonic()
                try:
                    poll()
                except Exception:
                    logger.warning("%s: poll callback failed", stage, exc_info=True)
    finally:
        if proc.poll() is None:
            import signal

            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait()
    reader.join(timeout=10)

    if proc.returncode != 0:
        tail_text = "\n".join(tail)[-1500:]
        raise RuntimeError(
            f"{stage} failed (exit {proc.returncode}): {tail_text or '<no output>'}"
        )


# ---------------------------------------------------------------------------
# Fine-tune driver
# ---------------------------------------------------------------------------


def _subprocess_env(extra: dict[str, str], repo_dir: str) -> dict[str, str]:
    """Full env for a vendored-script subprocess.

    The vendored scripts import both top-level packages (``tools``) and
    GPT_SoVITS-internal modules (``text``, ``utils``, ``module``, ``AR``…) as
    if both roots were on sys.path — upstream's own Docker image does this
    with an ENV PYTHONPATH; we do the same per-subprocess.
    """
    env = {**os.environ, **extra}
    env["PYTHONPATH"] = os.pathsep.join(
        [repo_dir, os.path.join(repo_dir, "GPT_SoVITS")]
    )
    return env


def finetune(
    manifest_path: str,
    output_dir: str,
    params: dict,
    progress_cb: Callable[[dict], None],
) -> dict:
    """Fine-tune GPT-SoVITS v2Pro on the dataset manifest; return the result.

    Pipeline (spec §3): manifest → `.list` → prep stages 1/2a/2b/3 → s2
    (SoVITS) fine-tune → s1 (GPT) fine-tune → bundle packaging. Progress is
    reported through the shared dict shape; phases ``preparing`` /
    ``training_sovits`` / ``training_gpt`` / ``packaging`` map onto the
    orchestrator's phase-percent bands.
    """
    repo = _repo_dir()
    pretrained = _pretrained_dir()
    sovits_epochs = int(params.get("sovits_epochs", 8))
    gpt_epochs = int(params.get("gpt_epochs", 15))
    batch_size = int(params.get("batch_size", 4))
    resolved_params = {
        "sovits_epochs": sovits_epochs,
        "gpt_epochs": gpt_epochs,
        "batch_size": batch_size,
    }
    python_exec = sys.executable

    def prep_progress(step: int) -> None:
        progress_cb(
            {
                "phase": progress.PHASE_PREPARING,
                "epoch": 1,
                "total_epochs": 1,
                "step": step,
                "total_steps": 4,
                "train_loss": None,
                "eta_secs": None,
            }
        )

    prep_progress(0)

    # Fresh work dir: leftovers from a previous failed run would trip the
    # prep scripts' per-file skip logic and the final-weight selection.
    work = os.path.join(output_dir, "work")
    shutil.rmtree(work, ignore_errors=True)
    exp_dir = os.path.join(work, "exp")
    sovits_weights = os.path.join(work, "SoVITS_weights")
    gpt_weights = os.path.join(work, "GPT_weights")
    # logs_s2_* is the one output dir s2_train.py assumes already exists
    # (webui pre-creates it); s1_train.py mkdirs its own output_dir.
    for d in (
        exp_dir,
        sovits_weights,
        gpt_weights,
        os.path.join(exp_dir, f"logs_s2_{MODEL_VERSION}"),
    ):
        os.makedirs(d, exist_ok=True)

    ensure_pretrained_weights(pretrained)

    segments = dataset.load_manifest(manifest_path)
    list_path = os.path.join(work, "dataset.list")
    dataset.manifest_to_list(manifest_path, list_path)
    with open(list_path, "r", encoding="utf-8") as fh:
        if not fh.read().strip():
            raise RuntimeError(
                "no segment in the dataset manifest has a usable transcript"
            )

    # --- Prep stages 1, 2a, 2b (sv — v2Pro needs it), 3 -------------------
    stages = [
        ("prep stage 1 (text/BERT)", "1"),
        ("prep stage 2a (HuBERT)", "2a"),
        ("prep stage 2b (speaker verification)", "2b"),
        ("prep stage 3 (semantic tokens)", "3"),
    ]
    for i, (label, key) in enumerate(stages):
        prep_progress(i)
        env = _subprocess_env(
            build_prep_env(
                key,
                list_path=list_path,
                exp_dir=exp_dir,
                repo_dir=repo,
                pretrained_dir=pretrained,
            ),
            repo,
        )
        run_stage(label, [python_exec, "-s", _STAGE_SCRIPTS[key]], cwd=repo, env=env)
        if key == "1":
            merge_stage1_output(exp_dir)
        elif key == "3":
            merge_stage3_output(exp_dir)

    # --- s2 (SoVITS) fine-tune --------------------------------------------
    with open(
        os.path.join(repo, "GPT_SoVITS/configs/s2v2Pro.json"), encoding="utf-8"
    ) as fh:
        s2_template = json.load(fh)
    s2_cfg = build_s2_config(
        s2_template,
        exp_dir=exp_dir,
        weights_dir=sovits_weights,
        pretrained_dir=pretrained,
        epochs=sovits_epochs,
        batch_size=batch_size,
    )
    s2_cfg_path = os.path.join(work, "tmp_s2.json")
    with open(s2_cfg_path, "w", encoding="utf-8") as fh:
        json.dump(s2_cfg, fh)

    tracker = progress.SovitsProgressTracker(total_epochs=sovits_epochs)
    s2_start = time.monotonic()

    def on_s2_line(line: str) -> None:
        state = tracker.feed(line)
        # Pseudo-steps are percent-of-epoch (0-100); linear ETA over the
        # whole s2 stage from the flattened step counter.
        steps_done = max(0, (state["epoch"] - 1) * 100 + state["step"])
        total_steps = sovits_epochs * 100
        if steps_done > 0:
            state["eta_secs"] = round(
                progress.compute_eta_secs(
                    time.monotonic() - s2_start, steps_done, total_steps
                )
            )
        progress_cb(state)

    progress_cb(tracker.state())
    run_stage(
        "SoVITS training (s2)",
        [python_exec, "-s", "GPT_SoVITS/s2_train.py", "--config", s2_cfg_path],
        cwd=repo,
        env=_subprocess_env({}, repo),
        on_line=on_s2_line,
    )
    sovits_file = select_final_sovits_weight(os.listdir(sovits_weights))
    if sovits_file is None:
        raise RuntimeError(
            "SoVITS training (s2) completed but produced no weights file under "
            f"{sovits_weights} — check save_every_epoch/if_save_every_weights"
        )

    # --- s1 (GPT) fine-tune -------------------------------------------------
    import yaml

    with open(
        os.path.join(repo, "GPT_SoVITS/configs/s1longer-v2.yaml"), encoding="utf-8"
    ) as fh:
        s1_template = yaml.safe_load(fh)
    s1_cfg = build_s1_config(
        s1_template,
        exp_dir=exp_dir,
        weights_dir=gpt_weights,
        pretrained_dir=pretrained,
        epochs=gpt_epochs,
        batch_size=batch_size,
    )
    s1_cfg_path = os.path.join(work, "tmp_s1.yaml")
    with open(s1_cfg_path, "w", encoding="utf-8") as fh:
        yaml.dump(s1_cfg, fh, default_flow_style=False)

    s1_start = time.monotonic()

    def gpt_state(epoch_done: int) -> dict:
        state = {
            "phase": progress.PHASE_TRAINING_GPT,
            "epoch": epoch_done,
            "total_epochs": gpt_epochs,
            "step": 100 if epoch_done else 0,
            "total_steps": 100,
            "train_loss": None,
            "eta_secs": None,
        }
        if epoch_done > 0:
            state["eta_secs"] = round(
                progress.compute_eta_secs(
                    time.monotonic() - s1_start, epoch_done, gpt_epochs
                )
            )
        return state

    def poll_gpt() -> None:
        epoch = progress.latest_gpt_checkpoint_epoch(
            os.listdir(gpt_weights), gpt_epochs
        )
        if epoch:
            progress_cb(gpt_state(epoch))

    progress_cb(gpt_state(0))
    run_stage(
        "GPT training (s1)",
        [python_exec, "-s", "GPT_SoVITS/s1_train.py", "--config_file", s1_cfg_path],
        cwd=repo,
        # s1_train reads these from the process env, not the config file.
        env=_subprocess_env({"_CUDA_VISIBLE_DEVICES": "0", "hz": "25hz"}, repo),
        poll=poll_gpt,
    )
    poll_gpt()  # final state — short runs can finish inside one poll interval
    gpt_file = select_final_gpt_weight(os.listdir(gpt_weights))
    if gpt_file is None:
        raise RuntimeError(
            "GPT training (s1) completed but produced no weights file under "
            f"{gpt_weights} — check save_every_n_epoch/if_save_every_weights"
        )

    # --- Package -------------------------------------------------------------
    progress_cb(
        {
            "phase": progress.PHASE_PACKAGING,
            "epoch": 1,
            "total_epochs": 1,
            "step": 0,
            "total_steps": 1,
            "train_loss": None,
            "eta_secs": None,
        }
    )
    result = package_bundle(
        output_dir=output_dir,
        sovits_weight_path=os.path.join(sovits_weights, sovits_file),
        gpt_weight_path=os.path.join(gpt_weights, gpt_file),
        segments=segments,
        # Output SR comes from the s2 config the model was trained with
        # (data.sampling_rate, 32000 for v2Pro) — never hardcoded here.
        sample_rate=int(s2_cfg["data"]["sampling_rate"]),
        train_params=resolved_params,
    )
    # Success: the multi-GB work dir (features, resume ckpts, per-epoch
    # weights) has served its purpose. Kept on failure for diagnosis.
    shutil.rmtree(work, ignore_errors=True)
    return result


# ---------------------------------------------------------------------------
# Synthesis (in-process vendored TTS_infer_pack)
# ---------------------------------------------------------------------------


def load_bundle(checkpoint_dir: str) -> dict:
    """Load and validate a trained bundle's config.json; resolve its files.

    Verifies every bundle file exists BEFORE any vendored import: TTS_Config
    silently falls back to the base pretrained weights when a weights path is
    missing (research §4), which would preview the wrong voice instead of
    failing.
    """
    config_path = os.path.join(checkpoint_dir, "config.json")
    if not os.path.isfile(config_path):
        raise RuntimeError(f"model bundle is missing config.json: {config_path}")
    with open(config_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    files = cfg.get("files") or {}
    resolved = {
        "gpt_path": os.path.join(checkpoint_dir, files.get("gpt", "gpt.ckpt")),
        "sovits_path": os.path.join(checkpoint_dir, files.get("sovits", "sovits.pth")),
        "reference_wav_path": os.path.join(
            checkpoint_dir, files.get("reference_wav", "reference.wav")
        ),
        "reference_text_path": os.path.join(
            checkpoint_dir, files.get("reference_text", "reference.txt")
        ),
    }
    for path in resolved.values():
        if not os.path.isfile(path):
            raise RuntimeError(
                f"model bundle is missing {os.path.basename(path)}: {path}"
            )
    with open(resolved["reference_text_path"], encoding="utf-8") as fh:
        resolved["prompt_text"] = fh.read().strip()
    resolved["config"] = cfg
    return resolved


def _get_model(checkpoint_dir: str, bundle: dict):
    """Return a loaded vendored ``TTS`` pipeline for the bundle (one-slot cache).

    A hit returns the cached pipeline; a miss drops the old one (freeing its
    VRAM) before loading the new one. Single-worker executor upstream — no
    locking needed.
    """
    global _model_cache
    if _model_cache is not None and _model_cache[0] == checkpoint_dir:
        return _model_cache[1]

    release_cached_model()

    repo = _repo_dir()
    pretrained = _pretrained_dir()
    ensure_pretrained_weights(pretrained)
    # The sv model path is hardcoded upstream relative to the repo root (not
    # TTS_Config-configurable) and TTS_Config itself resolves its default
    # paths against the CWD — so link the cache into the repo and run from
    # there. The chdir is process-wide but safe: every path this service
    # exchanges with the orchestrator is absolute.
    ensure_pretrained_symlink(repo, pretrained)
    os.chdir(repo)
    for entry in (repo, os.path.join(repo, "GPT_SoVITS")):
        if entry not in sys.path:
            sys.path.append(entry)

    import torch
    from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config

    use_cuda = torch.cuda.is_available()
    config = TTS_Config(
        {
            "custom": {
                "device": "cuda" if use_cuda else "cpu",
                "is_half": use_cuda,
                "version": MODEL_VERSION,
                "t2s_weights_path": bundle["gpt_path"],
                "vits_weights_path": bundle["sovits_path"],
                "bert_base_path": os.path.join(
                    pretrained, "chinese-roberta-wwm-ext-large"
                ),
                "cnhuhbert_base_path": os.path.join(pretrained, "chinese-hubert-base"),
            }
        }
    )
    model = TTS(config)
    _model_cache = (checkpoint_dir, model)
    return model


def synthesise(
    text: str,
    language: str,
    reference_wavs: list[str],
    checkpoint_dir: Optional[str],
    output_path: str,
    params: dict,
) -> dict:
    """Synthesise speech to ``output_path``; return ``{output_path, duration_secs}``.

    Loads the fine-tuned bundle and conditions on its stored ``reference.wav``
    + ``reference.txt`` — GPT-SoVITS requires a reference clip AND its
    transcript at inference time (spec §4), so ``reference_wavs`` from the
    request is unused for this engine. Base-model preview is not offered
    (spec §5.3): a bundle is mandatory.
    """
    if not checkpoint_dir:
        raise RuntimeError(
            "gpt_sovits preview requires a trained model bundle "
            "(base-model preview is not offered for this engine)"
        )
    bundle = load_bundle(checkpoint_dir)
    model = _get_model(checkpoint_dir, bundle)

    import numpy as np
    import soundfile as sf

    inputs = {
        "text": text,
        "text_lang": (language or "en").lower(),
        "ref_audio_path": bundle["reference_wav_path"],
        "prompt_text": bundle["prompt_text"],
        "prompt_lang": "en",
        "top_k": int(params.get("top_k", 15)),
        "top_p": float(params.get("top_p", 1.0)),
        "temperature": float(params.get("temperature", 1.0)),
        "speed_factor": float(params.get("speed", 1.0)),
        "repetition_penalty": float(params.get("repetition_penalty", 1.35)),
        # Punctuation-based splitting (upstream api_v2's own default): preview
        # text can run long, and each sentence gets its own prosody contour.
        "text_split_method": "cut5",
        "return_fragment": False,
        "streaming_mode": False,
    }

    # TTS.run is a generator of (sample_rate, int16-ndarray) fragments; with
    # fragmenting off it usually yields once, but concatenate defensively.
    fragments = list(model.run(inputs))
    if not fragments:
        raise RuntimeError("synthesis produced no audio")
    # Output SR is read off the loaded pipeline's own config (set from the
    # checkpoint at weight-load time, research §4) — never hardcoded.
    sample_rate = int(fragments[0][0])
    audio = np.concatenate([frag for _sr, frag in fragments])

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    sf.write(output_path, audio, sample_rate)

    duration_secs = audio.shape[-1] / sample_rate
    return {"output_path": output_path, "duration_secs": round(duration_secs, 2)}
