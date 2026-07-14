"""Stdout-line → progress-dict parsing for GPT-SoVITS training.

Pure logic, no subprocess handling here (the driver that owns the subprocess
loop lands in the training/Dockerfile task). Formats pinned in
research-gpt-sovits.md §5:

- s2 (SoVITS) training prints clean, line-oriented ``logging`` output — this
  module's ``SovitsProgressTracker`` folds those lines into the shared
  ``{phase, epoch, total_epochs, step, total_steps, train_loss, eta_secs}``
  progress dict (spec §3/§6).
- s1 (GPT) training has NO parseable stdout (a redrawing Lightning tqdm bar).
  Its progress is inferred by the driver polling the checkpoint directory
  instead — ``latest_gpt_checkpoint_epoch`` is the pure helper for that.
- Prep stages print no usable per-line progress at all (research §5); the
  driver counts processed files against the `.list` line count, no parser
  needed here.
"""

from __future__ import annotations

import ast
import re
from typing import Optional

# Phase constants (spec §3's pipeline table / §6's phase-aware percent bands).
PHASE_PREPARING = "preparing"
PHASE_TRAINING_SOVITS = "training_sovits"
PHASE_TRAINING_GPT = "training_gpt"
PHASE_PACKAGING = "packaging"

_LOSS_KEYS = ("loss_disc", "loss_gen", "loss_fm", "loss_mel", "kl_ssl", "loss_kl")

_EPOCH_PERCENT_RE = re.compile(r"Train Epoch:\s*(\d+)\s*\[(\d+)%\]")
_LOSS_LINE_RE = re.compile(r"INFO:[^:]*:(\[.*\])\s*$")
_EPOCH_BOUNDARY_RE = re.compile(r"====>\s*Epoch:\s*(\d+)")
_CKPT_SAVE_RE = re.compile(r"saving ckpt (.+)_e(\d+):Success\.")
_START_SENTINEL_RE = re.compile(r"start training from epoch\s+(\d+)")
_GPT_CKPT_RE = re.compile(r".+-e(\d+)\.ckpt$")


# ---------------------------------------------------------------------------
# ETA helper — same linear-extrapolation convention as the xtts template's
# engine.compute_eta_secs. Kept here (not in engine.py) since it operates on
# the same step/total_steps counters this module parses; the subprocess
# driver supplies elapsed wall-clock time.
# ---------------------------------------------------------------------------


def compute_eta_secs(elapsed_secs: float, steps_done: int, total_steps: int) -> float:
    """Linear ETA: elapsed / steps_done x steps_remaining.

    Returns 0.0 before any step completes (no rate to extrapolate from) and at
    completion (no steps remaining).
    """
    if steps_done <= 0 or total_steps <= 0:
        return 0.0
    remaining = max(total_steps - steps_done, 0)
    return (elapsed_secs / steps_done) * remaining


# ---------------------------------------------------------------------------
# Individual line parsers (each pure, each independently testable)
# ---------------------------------------------------------------------------


def parse_epoch_percent_line(line: str) -> Optional[tuple[int, int]]:
    """``INFO:{exp_name}:Train Epoch: 3 [45%]`` -> ``(epoch, percent)``."""
    m = _EPOCH_PERCENT_RE.search(line)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def parse_loss_line(line: str) -> Optional[dict]:
    """``INFO:{exp_name}:[loss_disc, loss_gen, loss_fm, loss_mel, kl_ssl,
    loss_kl, global_step, lr]`` (a Python list repr) -> a dict of named
    fields. Returns ``None`` for any non-matching line, including the
    epoch/percent line above (also ``INFO:``-prefixed, but not a list).
    """
    m = _LOSS_LINE_RE.match(line)
    if not m:
        return None
    try:
        values = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return None
    if not isinstance(values, list) or len(values) != 8:
        return None
    result = dict(zip(_LOSS_KEYS, values[:6]))
    result["global_step"] = values[6]
    result["lr"] = values[7]
    return result


def parse_epoch_boundary_line(line: str) -> Optional[int]:
    """``====> Epoch: 3`` -> ``3``. The reliable epoch-complete signal."""
    m = _EPOCH_BOUNDARY_RE.search(line)
    return int(m.group(1)) if m else None


def parse_ckpt_save_line(line: str) -> Optional[dict]:
    """``saving ckpt {name}_e{epoch}:Success.`` -> ``{"name", "epoch"}``."""
    m = _CKPT_SAVE_RE.search(line)
    if not m:
        return None
    return {"name": m.group(1), "epoch": int(m.group(2))}


def parse_start_sentinel(line: str) -> Optional[int]:
    """``start training from epoch %s`` -> the starting epoch number."""
    m = _START_SENTINEL_RE.search(line)
    return int(m.group(1)) if m else None


def is_training_done_line(line: str) -> bool:
    """``training done`` -> the s2 end-of-run sentinel."""
    return line.strip() == "training done"


# ---------------------------------------------------------------------------
# Stateful s2 (SoVITS) progress tracker
# ---------------------------------------------------------------------------


class SovitsProgressTracker:
    """Folds s2 stdout lines into the shared finetune progress dict.

    The intra-epoch percent line gives no absolute step count, only a percent
    — so ``step``/``total_steps`` here are a 0-100 pseudo-step pair rather
    than real batch indices; the orchestrator's phase-percent math only cares
    about the ratio, and the real batch count is buried in a non-line-
    oriented tqdm bar not worth parsing (research §5).
    """

    def __init__(self, total_epochs: int):
        self._state = {
            "phase": PHASE_TRAINING_SOVITS,
            "epoch": 0,
            "total_epochs": total_epochs,
            "step": 0,
            "total_steps": 100,
            "train_loss": None,
            "eta_secs": None,
        }

    def state(self) -> dict:
        return dict(self._state)

    def feed(self, line: str) -> dict:
        """Update state from one stdout line; return the new state dict.

        Unrecognised lines (including the checkpoint-save notice, which
        carries no progress information) leave the state unchanged.
        """
        epoch_percent = parse_epoch_percent_line(line)
        if epoch_percent is not None:
            epoch, percent = epoch_percent
            self._state["epoch"] = epoch
            self._state["step"] = percent
            self._state["total_steps"] = 100
            return self.state()

        loss = parse_loss_line(line)
        if loss is not None:
            self._state["train_loss"] = loss["loss_gen"]
            return self.state()

        boundary_epoch = parse_epoch_boundary_line(line)
        if boundary_epoch is not None:
            self._state["epoch"] = boundary_epoch
            self._state["step"] = 100
            self._state["total_steps"] = 100
            return self.state()

        start_epoch = parse_start_sentinel(line)
        if start_epoch is not None:
            self._state["epoch"] = start_epoch
            return self.state()

        if is_training_done_line(line):
            self._state["epoch"] = self._state["total_epochs"]
            self._state["step"] = 100
            self._state["total_steps"] = 100
            return self.state()

        # Checkpoint-save notices and any other junk line: no progress signal.
        return self.state()


# ---------------------------------------------------------------------------
# s1 (GPT) checkpoint-epoch helper — no parseable stdout, infer from files.
# ---------------------------------------------------------------------------


def latest_gpt_checkpoint_epoch(filenames: list[str], total_epochs: int) -> Optional[int]:
    """Latest completed GPT (s1) epoch from a checkpoint dir listing.

    Filenames follow ``{exp_name}-e{epoch}.ckpt`` (research §2). Returns
    ``None`` if no filename matches. Clamped to ``total_epochs`` so a stale
    checkpoint left over from a previous, longer run can't report progress
    beyond what this run is configured for.
    """
    epochs = []
    for name in filenames:
        m = _GPT_CKPT_RE.match(name)
        if m:
            epochs.append(int(m.group(1)))
    if not epochs:
        return None
    return min(max(epochs), total_epochs)
