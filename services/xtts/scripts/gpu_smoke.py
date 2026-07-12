"""Manual GPU smoke test for the XTTS engine — NOT run in CI.

What it does
------------
1. Builds a tiny one-segment dataset manifest from a fixture WAV.
2. Runs ``engine.finetune`` for a single epoch on the real GPU (downloads the
   XTTS-v2 base checkpoint into the TTS cache on first run — several GB).
3. Runs ``engine.synthesise`` twice: zero-shot from the fixture WAV, then from
   the just-produced fine-tuned checkpoint, writing two output WAVs.

Honest caveats
--------------
- **Requires** a CUDA GPU with >= ~12 GB free VRAM, ``coqui-tts`` + ``torch``
  installed, and ``XTTS_ACCEPT_CPML=1`` (Coqui Public Model License).
- One epoch on one clip does **not** produce a usable voice — this verifies the
  code path (config build, trainer wiring, checkpoint packaging, latent save,
  inference round-trip), not output quality.
- The progress-callback field mapping (``on_train_step_end`` hook attribute
  names on Coqui's ``Trainer``) is written against the recipe API from the
  library docs and has NOT been run against a live trainer here; if attribute
  names differ in the installed version, adjust ``engine._on_train_step`` /
  ``engine._find_best_checkpoint`` / ``engine._last_eval_loss`` accordingly.
  This is the single most likely thing to need a tweak on first real run.
- First run is slow (model download + compile). Budget 10-20 min.

Usage
-----
    XTTS_ACCEPT_CPML=1 python3 scripts/gpu_smoke.py /path/to/fixture.wav [workdir]

``fixture.wav`` should be a few seconds of clean 22.05 kHz mono speech. If no
workdir is given, a temp directory is used.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

# Run from the service root so ``import engine`` / ``import dataset`` resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402


def _build_manifest(wav_path: str, out_dir: str) -> str:
    manifest_path = os.path.join(out_dir, "dataset.json")
    manifest = {
        "version": "1",
        "project_id": "smoke",
        "speaker": "target",
        "segments": [
            {
                "id": "0001",
                "audio_file": os.path.abspath(wav_path),
                "text": "This is a smoke test of the fine-tuning pipeline.",
            },
            {
                "id": "0002",
                "audio_file": os.path.abspath(wav_path),
                "text": "A second line so the split yields both a train and eval row.",
            },
        ],
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return manifest_path


def main() -> int:
    if os.environ.get("XTTS_ACCEPT_CPML") != "1":
        print("Set XTTS_ACCEPT_CPML=1 to accept the Coqui Public Model License.")
        return 2
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    wav_path = sys.argv[1]
    workdir = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="xtts_smoke_")
    os.makedirs(workdir, exist_ok=True)

    print(f"VRAM available: {engine.vram_available_gb():.1f} GB")

    manifest_path = _build_manifest(wav_path, workdir)
    model_dir = os.path.join(workdir, "model")

    print("Fine-tuning (1 epoch)...")
    result = engine.finetune(
        manifest_path=manifest_path,
        output_dir=model_dir,
        params={
            "epochs": 1,
            "batch_size": 1,
            "grad_accum": 1,
            "learning_rate": 5e-6,
            "language": "en",
            "eval_split": 0.5,
        },
        progress_cb=lambda d: print("  progress:", d),
    )
    print("Fine-tune result:", json.dumps(result, indent=2))

    zero_shot_out = os.path.join(workdir, "zero_shot.wav")
    print("Synthesising zero-shot...")
    print(
        engine.synthesise(
            text="Hello from the zero shot base model.",
            language="en",
            reference_wavs=[os.path.abspath(wav_path)],
            checkpoint_dir=None,
            output_path=zero_shot_out,
            params={"temperature": 0.65},
        )
    )

    ft_out = os.path.join(workdir, "fine_tuned.wav")
    print("Synthesising from fine-tuned checkpoint...")
    print(
        engine.synthesise(
            text="Hello from the fine tuned voice.",
            language="en",
            reference_wavs=[os.path.abspath(wav_path)],
            checkpoint_dir=result["checkpoint_dir"],
            output_path=ft_out,
            params={"temperature": 0.65},
        )
    )

    print(f"Done. Outputs in {workdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
