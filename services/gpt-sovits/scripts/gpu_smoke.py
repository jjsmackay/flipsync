"""Manual GPU smoke test for the GPT-SoVITS engine — NOT run in CI.

What it does
------------
1. Builds a tiny two-segment dataset manifest from a fixture WAV (the
   committed ``tests/fixtures/smoke.wav`` by default).
2. Downloads any missing pretrained weights into GPT_SOVITS_PRETRAINED_DIR
   (~1.2 GB on a cold cache), then runs ``engine.finetune`` — the real
   4-stage prep + s2 + s1 subprocess pipeline — with 1 epoch each.
3. Verifies all five bundle files landed in the output dir.
4. Runs ``engine.synthesise`` against the trained bundle, writing a preview
   WAV, and prints timings + VRAM at each step.

Honest caveats
--------------
- **Requires** a CUDA GPU with >= ~8 GB free VRAM and the full image
  environment (vendored repo + torch + upstream deps) — in practice run it
  inside the gpt-sovits container:
      docker compose --profile gpt-sovits run --rm gpt-sovits \
          python3 scripts/gpu_smoke.py
- One epoch on one ~4.5-second clip (comfortably inside the 3-10 s reference
  band packaging enforces) does **not** produce a usable voice — this
  verifies the code path (env/config contracts against the real vendored
  scripts, checkpoint discovery, bundle packaging, inference round-trip),
  not output quality.
- First run is slow (pretrained download + BERT/HuBERT warm-up). Budget
  10-20 min on a cold cache.

Usage
-----
    python3 scripts/gpu_smoke.py [--wav tests/fixtures/smoke.wav] [--workdir DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

# Run from the service root so ``import engine`` / ``import dataset`` resolve.
_SERVICE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SERVICE_ROOT)

import engine  # noqa: E402


def _build_manifest(wav_path: str, out_dir: str) -> str:
    # Copy the fixture to two distinct filenames: upstream prep keys its
    # per-clip outputs by wav basename, so two rows pointing at the same file
    # would collapse into a single training sample.
    import shutil

    seg_a = os.path.abspath(os.path.join(out_dir, "smoke-seg-a.wav"))
    seg_b = os.path.abspath(os.path.join(out_dir, "smoke-seg-b.wav"))
    shutil.copyfile(wav_path, seg_a)
    shutil.copyfile(wav_path, seg_b)

    manifest_path = os.path.join(out_dir, "dataset.json")
    manifest = {
        "version": "1",
        "project_id": "smoke",
        "speaker": "target",
        "segments": [
            {
                "id": "0001",
                "audio_file": seg_a,
                "text": "This is a smoke test of the fine tuning pipeline.",
                "duration_secs": 4.5,
            },
            {
                "id": "0002",
                "audio_file": seg_b,
                "text": "A second line so the dataset has more than one row.",
                "duration_secs": 4.5,
            },
        ],
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="End-to-end GPT-SoVITS train + synthesise smoke on a real GPU."
    )
    parser.add_argument(
        "--wav",
        default=os.path.join(_SERVICE_ROOT, "tests", "fixtures", "smoke.wav"),
        help="short speech WAV to train on (default: committed fixture)",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="working directory (default: a fresh temp dir)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.wav):
        print(f"fixture WAV not found: {args.wav}")
        return 2

    workdir = args.workdir or tempfile.mkdtemp(prefix="gpt_sovits_smoke_")
    os.makedirs(workdir, exist_ok=True)

    print(f"Vendored repo:   {engine._repo_dir()}")
    print(f"Pretrained dir:  {engine._pretrained_dir()}")
    missing = engine.missing_pretrained_files(engine._pretrained_dir())
    print(f"Missing weights: {len(missing)} of {len(engine.PRETRAINED_FILES)}")
    print(f"VRAM available:  {engine.vram_available_gb():.1f} GB")

    manifest_path = _build_manifest(args.wav, workdir)
    model_dir = os.path.join(workdir, "model")

    print("Fine-tuning (1 sovits epoch + 1 gpt epoch)...")
    t0 = time.monotonic()
    result = engine.finetune(
        manifest_path=manifest_path,
        output_dir=model_dir,
        params={"sovits_epochs": 1, "gpt_epochs": 1, "batch_size": 1},
        progress_cb=lambda d: print("  progress:", d),
    )
    print(f"Fine-tune took {time.monotonic() - t0:.0f}s")
    print("Fine-tune result:", json.dumps(result, indent=2))

    missing_bundle = [
        name
        for name in ("gpt.ckpt", "sovits.pth", "config.json", "reference.wav", "reference.txt")
        if not os.path.isfile(os.path.join(model_dir, name))
    ]
    if missing_bundle:
        print(f"FAIL: bundle incomplete, missing {missing_bundle}")
        return 1
    print("Bundle complete (all five files present).")
    print(f"VRAM after training: {engine.vram_available_gb():.1f} GB free")

    preview = os.path.join(workdir, "preview.wav")
    print("Synthesising from the trained bundle...")
    t0 = time.monotonic()
    out = engine.synthesise(
        text="Hello from the fine tuned GPT SoVITS voice.",
        language="en",
        reference_wavs=[],
        checkpoint_dir=model_dir,
        output_path=preview,
        params={},
    )
    print(f"Synthesis took {time.monotonic() - t0:.0f}s")
    print("Synthesise result:", out)
    print(f"VRAM after synthesis: {engine.vram_available_gb():.1f} GB free")

    print(f"Done. Outputs in {workdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
