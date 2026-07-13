"""XTTS-v2 engine — fine-tune + synthesise.

This module is the boundary between the FastAPI job layer (``main.py``) and the
heavy Coqui-TTS / torch machinery. **All torch / TTS / trainer imports are lazy
(inside functions)** so the service — and its test-suite — can import this
module without those packages installed. The API-layer tests patch these
functions as module attributes; only the GPU smoke script
(``scripts/gpu_smoke.py``) exercises the real implementations end-to-end.

The fine-tune path follows the upstream XTTS-v2 GPT recipe
(``recipes/ljspeech/xtts_v2/train_gpt_xtts.py`` in the coqui-tts repo):
``GPTArgs`` + ``GPTTrainerConfig`` + ``XttsAudioConfig`` with the ``coqui``
formatter, base checkpoint auto-downloaded into the TTS cache
(``~/.local/share/tts``).
"""

from __future__ import annotations

import os
from typing import Callable, Optional

import dataset

# Minimum free VRAM (GB) required to start a fine-tune. Surfaced to the job
# layer for the pre-flight check so an under-provisioned GPU fails fast with a
# clear message rather than OOM-ing partway through training.
FINETUNE_MIN_VRAM_GB = 12.0

# XTTS-v2 operates at 22.05 kHz mono; the whole pipeline standardises on it.
_SAMPLE_RATE = 22050
# Base model identifier for the TTS ModelManager download.
_BASE_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"

# One-slot synthesis-model cache: ``(key, model)`` where key is the
# checkpoint_dir, or ``_BASE_MODEL_KEY`` for the base model. Safe without locks
# because the job layer runs everything on a single-worker executor. Repeated
# previews against the same model (the common case) skip the multi-second
# checkpoint reload on every call.
_BASE_MODEL_KEY = "__base__"
_model_cache: Optional[tuple] = None


def release_cached_model() -> None:
    """Drop the cached synthesis model and free its VRAM.

    Called by the job layer before a fine-tune so a lingering preview model
    doesn't eat into the VRAM preflight budget. A no-op (and import-safe
    without torch) when nothing is cached.
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


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without torch)
# ---------------------------------------------------------------------------


def compute_eta_secs(elapsed_secs: float, steps_done: int, total_steps: int) -> float:
    """Linear ETA: elapsed / steps_done × steps_remaining.

    Returns 0.0 before any step completes (no rate to extrapolate from) and at
    completion (no steps remaining).
    """
    if steps_done <= 0 or total_steps <= 0:
        return 0.0
    remaining = max(total_steps - steps_done, 0)
    return (elapsed_secs / steps_done) * remaining


def select_latent_wavs(train_rows: list[list[str]], limit: int = 5) -> list[str]:
    """Pick up to ``limit`` audio files for speaker-latent conditioning.

    Longest clips give the most stable conditioning; without duration metadata
    at this layer we approximate "longest" by on-disk file size (WAVs here are
    uniform format, so bytes ∝ duration). ``train_rows`` are Coqui CSV rows
    ``[audio_file, text, speaker_name]``.
    """
    paths = [row[0] for row in train_rows if row and row[0]]

    def _size(p: str) -> int:
        try:
            return os.path.getsize(p)
        except OSError:
            return 0

    return sorted(paths, key=_size, reverse=True)[:limit]


# ---------------------------------------------------------------------------
# GPU-backed implementations (lazy ML imports)
# ---------------------------------------------------------------------------


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


def finetune(
    manifest_path: str,
    output_dir: str,
    params: dict,
    progress_cb: Callable[[dict], None],
) -> dict:
    """Fine-tune XTTS-v2 on the dataset manifest; return the result payload.

    ``params`` carries ``epochs``, ``batch_size``, ``grad_accum``,
    ``learning_rate``, ``language`` and ``eval_split``. ``progress_cb`` is
    invoked with the progress dict (``phase``/``epoch``/``total_epochs``/
    ``step``/``total_steps``/``train_loss``/``eval_loss``/``eta_secs``).
    """
    import gc
    import shutil
    import time

    import torch
    from trainer import Trainer, TrainerArgs
    from TTS.tts.datasets import load_tts_samples
    from TTS.tts.layers.xtts.trainer.gpt_trainer import (
        GPTArgs,
        GPTTrainer,
        GPTTrainerConfig,
    )
    # XttsAudioConfig lives in TTS.tts.models.xtts in coqui-tts 0.27.5, not in
    # gpt_trainer (which only re-exports GPTArgs/GPTTrainerConfig/GPTTrainer).
    from TTS.tts.models.xtts import XttsAudioConfig
    from TTS.utils.manage import ModelManager

    os.makedirs(output_dir, exist_ok=True)

    progress_cb({"phase": "preparing"})

    # 1. Manifest → Coqui train/eval CSVs.
    dataset_dir = os.path.join(output_dir, "dataset")
    train_csv, eval_csv = dataset.manifest_to_coqui_csv(
        manifest_path, dataset_dir, eval_split=params.get("eval_split", 0.1)
    )

    # 2. Ensure the base XTTS-v2 checkpoint is present in the TTS cache.
    manager = ModelManager()
    manager.download_model(_BASE_MODEL)
    base_dir = os.path.join(
        manager.output_prefix, _BASE_MODEL.replace("/", "--")
    )
    base_checkpoint = os.path.join(base_dir, "model.pth")
    base_config = os.path.join(base_dir, "config.json")
    base_vocab = os.path.join(base_dir, "vocab.json")
    dvae_checkpoint = os.path.join(base_dir, "dvae.pth")
    mel_norm = os.path.join(base_dir, "mel_stats.pth")

    # 3. Recipe config (mirrors recipes/ljspeech/xtts_v2/train_gpt_xtts.py).
    model_args = GPTArgs(
        max_conditioning_length=132300,
        min_conditioning_length=66150,
        max_wav_length=255995,
        max_text_length=200,
        mel_norm_file=mel_norm,
        dvae_checkpoint=dvae_checkpoint,
        xtts_checkpoint=base_checkpoint,
        tokenizer_file=base_vocab,
        gpt_num_audio_tokens=1026,
        gpt_start_audio_token=1024,
        gpt_stop_audio_token=1025,
        gpt_use_masking_gt_prompt_approach=True,
        gpt_use_perceiver_resampler=True,
    )
    audio_config = XttsAudioConfig(
        sample_rate=22050, dvae_sample_rate=22050, output_sample_rate=_SAMPLE_RATE
    )

    dataset_config = {
        "formatter": "coqui",
        "dataset_name": "flipsync",
        "path": dataset_dir,
        "meta_file_train": os.path.basename(train_csv),
        "meta_file_val": os.path.basename(eval_csv),
        "language": params["language"],
    }

    config = GPTTrainerConfig(
        epochs=params.get("epochs", 10),
        output_path=output_dir,
        model_args=model_args,
        audio=audio_config,
        batch_size=params.get("batch_size", 3),
        lr=params.get("learning_rate", 5e-6),
        run_eval=True,
        optimizer="AdamW",
        optimizer_params={"betas": [0.9, 0.96], "eps": 1e-8, "weight_decay": 1e-2},
        lr_scheduler="MultiStepLR",
        lr_scheduler_params={
            "milestones": [50000, 150000, 300000],
            "gamma": 0.5,
            "last_epoch": -1,
        },
    )

    train_samples, eval_samples = load_tts_samples(
        [dataset_config],
        eval_split=True,
        eval_split_size=params.get("eval_split", 0.1),
    )

    model = GPTTrainer.init_from_config(config)

    total_epochs = params.get("epochs", 10)
    start = time.time()

    # Trainer callbacks map the training loop onto our progress dict. Coqui's
    # Trainer exposes callback hooks keyed by name; we translate step/epoch
    # counters and the running losses into the poll shape.
    def _on_train_step(trainer_obj) -> None:
        steps_done = int(getattr(trainer_obj, "total_steps_done", 0))
        total_steps = int(
            getattr(trainer_obj, "estimated_total_steps", 0) or steps_done
        )
        keep_avg = getattr(trainer_obj, "keep_avg_train", None)
        train_loss = _avg_loss(keep_avg)
        progress_cb(
            {
                "phase": "training",
                "epoch": int(getattr(trainer_obj, "epochs_done", 0)) + 1,
                "total_epochs": total_epochs,
                "step": steps_done,
                "total_steps": total_steps,
                "train_loss": train_loss,
                "eval_loss": _last_eval_loss(trainer_obj),
                "eta_secs": compute_eta_secs(
                    time.time() - start, steps_done, total_steps
                ),
            }
        )

    callbacks = {"on_train_step_end": _on_train_step}

    trainer = Trainer(
        # grad_accum_steps is a TrainerArgs field (read as args.grad_accum_steps
        # by the trainer loop), not a GPTTrainerConfig field.
        TrainerArgs(
            restore_path=None,
            skip_train_epoch=False,
            grad_accum_steps=params.get("grad_accum", 1),
        ),
        config,
        output_path=output_dir,
        model=model,
        train_samples=train_samples,
        eval_samples=eval_samples,
        callbacks=callbacks,
    )
    trainer.fit()

    # Capture the eval loss NOW — the trainer is released below, before the
    # latent-extraction reload, so the trained model and its reloaded copy
    # never occupy VRAM at the same time.
    final_eval_loss = _last_eval_loss(trainer)

    # 4. Package the checkpoint bundle into output_dir.
    progress_cb({"phase": "packaging", "total_epochs": total_epochs})
    best_ckpt = _find_best_checkpoint(trainer, output_dir)
    model_path = os.path.join(output_dir, "model.pth")
    shutil.copyfile(best_ckpt, model_path)
    config_path = os.path.join(output_dir, "config.json")
    vocab_path = os.path.join(output_dir, "vocab.json")
    shutil.copyfile(base_config, config_path)
    shutil.copyfile(base_vocab, vocab_path)

    # Release the trainer (and the model it holds) before reloading the
    # checkpoint for latent extraction.
    del trainer
    del model
    gc.collect()
    torch.cuda.empty_cache()

    # 5. Speaker conditioning latents from the longest training clips.
    latents_path = os.path.join(output_dir, "speaker_latents.pt")
    _save_speaker_latents(
        config_path=config_path,
        vocab_path=vocab_path,
        checkpoint_dir=output_dir,
        train_csv=train_csv,
        out_path=latents_path,
    )

    return {
        "checkpoint_dir": output_dir,
        "model_path": model_path,
        "config_path": config_path,
        "vocab_path": vocab_path,
        "speaker_latents_path": latents_path,
        "final_eval_loss": final_eval_loss,
    }


def synthesise(
    text: str,
    language: str,
    reference_wavs: list[str],
    checkpoint_dir: Optional[str],
    output_path: str,
    params: dict,
) -> dict:
    """Synthesise speech to ``output_path``; return ``{output_path, duration_secs}``.

    With ``checkpoint_dir`` the fine-tuned bundle is loaded and, when a
    ``speaker_latents.pt`` is present, reused for conditioning; otherwise the
    base XTTS-v2 model runs zero-shot from ``reference_wavs``. The loaded
    model is cached (one slot, keyed on ``checkpoint_dir``) across calls.
    """
    import torch
    import torchaudio

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    model = _get_model(checkpoint_dir)

    latents_file = (
        os.path.join(checkpoint_dir, "speaker_latents.pt") if checkpoint_dir else None
    )
    if latents_file and os.path.exists(latents_file):
        saved = torch.load(latents_file, map_location="cpu")
        gpt_cond_latent = saved["gpt_cond_latent"]
        speaker_embedding = saved["speaker_embedding"]
    else:
        gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(
            audio_path=reference_wavs
        )

    out = model.inference(
        text,
        language,
        gpt_cond_latent,
        speaker_embedding,
        temperature=params.get("temperature", 0.65),
    )
    wav = torch.tensor(out["wav"]).unsqueeze(0)
    torchaudio.save(output_path, wav, _SAMPLE_RATE)

    duration_secs = wav.shape[-1] / _SAMPLE_RATE
    return {"output_path": output_path, "duration_secs": round(duration_secs, 2)}


# ---------------------------------------------------------------------------
# Internal helpers touching torch/trainer (invoked only from the GPU path)
# ---------------------------------------------------------------------------


def _get_model(checkpoint_dir: Optional[str]):
    """Return a loaded ``Xtts`` model for ``checkpoint_dir`` (None = base model).

    One-slot cache: a hit returns the cached model; a miss drops the old model
    (freeing its VRAM) before loading the new one, so at most one synthesis
    model is resident. Single-worker executor upstream — no locking needed.
    """
    global _model_cache
    key = checkpoint_dir if checkpoint_dir else _BASE_MODEL_KEY
    if _model_cache is not None and _model_cache[0] == key:
        return _model_cache[1]

    release_cached_model()

    import torch
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager

    if checkpoint_dir:
        config = XttsConfig()
        config.load_json(os.path.join(checkpoint_dir, "config.json"))
        model = Xtts.init_from_config(config)
        model.load_checkpoint(
            config,
            checkpoint_dir=checkpoint_dir,
            vocab_path=os.path.join(checkpoint_dir, "vocab.json"),
            use_deepspeed=False,
        )
    else:
        manager = ModelManager()
        manager.download_model(_BASE_MODEL)
        base_dir = os.path.join(
            manager.output_prefix, _BASE_MODEL.replace("/", "--")
        )
        config = XttsConfig()
        config.load_json(os.path.join(base_dir, "config.json"))
        model = Xtts.init_from_config(config)
        model.load_checkpoint(config, checkpoint_dir=base_dir, use_deepspeed=False)

    if torch.cuda.is_available():
        model.cuda()

    _model_cache = (key, model)
    return model


def _avg_loss(keep_avg) -> Optional[float]:
    if keep_avg is None:
        return None
    try:
        return float(keep_avg["avg_loss"])
    except (KeyError, TypeError):
        return None


def _last_eval_loss(trainer_obj) -> Optional[float]:
    keep_avg = getattr(trainer_obj, "keep_avg_eval", None)
    val = _avg_loss(keep_avg)
    if val is not None:
        return val
    best = getattr(trainer_obj, "best_loss", None)
    return float(best) if isinstance(best, (int, float)) else None


def _find_best_checkpoint(trainer_obj, output_dir: str) -> str:
    """Locate the best checkpoint the trainer produced.

    Prefers the trainer's own ``best_model_path``; falls back to the newest
    ``best_model*.pth`` (then any ``*.pth``) under ``output_dir``.
    """
    best = getattr(trainer_obj, "best_model_path", None)
    if best and os.path.exists(best):
        return best

    import glob

    for pattern in ("**/best_model*.pth", "**/*.pth"):
        matches = glob.glob(os.path.join(output_dir, pattern), recursive=True)
        if matches:
            return max(matches, key=os.path.getmtime)
    raise FileNotFoundError(f"no checkpoint produced under {output_dir}")


def _save_speaker_latents(
    config_path: str,
    vocab_path: str,
    checkpoint_dir: str,
    train_csv: str,
    out_path: str,
) -> None:
    import csv

    import torch
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts

    with open(train_csv, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh, delimiter="|"))[1:]  # drop header
    latent_wavs = select_latent_wavs(rows, limit=5)

    config = XttsConfig()
    config.load_json(config_path)
    model = Xtts.init_from_config(config)
    model.load_checkpoint(
        config, checkpoint_dir=checkpoint_dir, vocab_path=vocab_path, use_deepspeed=False
    )
    if torch.cuda.is_available():
        model.cuda()

    gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(
        audio_path=latent_wavs
    )
    torch.save(
        {"gpt_cond_latent": gpt_cond_latent, "speaker_embedding": speaker_embedding},
        out_path,
    )
