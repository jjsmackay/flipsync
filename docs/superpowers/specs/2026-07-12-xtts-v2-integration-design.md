# XTTS-v2 Integration (v1.5) — Design

**Status:** APPROVED (design), pending implementation plan
**Date:** 2026-07-12
**Normative spec:** `spec/architecture.md` §v1.5, `spec/api-contracts.md` §Models/§Previews/§XTTS Service, `spec/data-models.md` §models

---

## Context

FlipSync v1 produces XTTS-v2-ready datasets. v1.5 closes the loop in-app: hear the cloned voice (zero-shot preview), train a fine-tuned model, and compare the two by ear. This matches the roadmap line in `overview.md`.

The original spec assumed "the official Coqui streaming server image". Coqui shut down in early 2024; that image is unmaintained. This design uses the community-maintained **`coqui-tts` fork (Idiap)**, which ships both XTTS-v2 inference and the `GPTTrainer` fine-tuning recipe.

## Decisions

**D1 — One service, two job types.** A fifth processing service `services/xtts` (port 8005) with the standard contract (`GET /health`, `POST /jobs`, `GET /jobs/{job_id}`) and two job types: `finetune` and `synthesise`. Training and inference share the same ~7 GB torch/TTS image and model cache, and contend for the same GPU anyway — the FIFO job queue serialises them. A split train/synth service pair was considered and rejected (doubles image weight, buys nothing). The service is single-worker so jobs serialise at the service level too.

**D2 — Dataset build extracted from export.** "Dataset build" becomes a shared internal step: take a segment selection → run cleanup on segments lacking cleaned audio → write a dataset manifest. Export = dataset build (approved segments) + tar.gz archive. Fine-tune = dataset build + `GPTTrainer`. One code path, two consumers. The export archive includes exactly the files listed in its manifest, so cleaned audio from non-export dataset builds sitting in `export/` never leaks into an archive.

**D3 — Early fine-tune (dataset modes).** `POST /models` takes a dataset selector: `approved` (default — reviewed segments) or `auto` (segments at/above a `min_confidence`, default 0.85, regardless of review status). Auto trades misattributed speakers and unchecked transcripts for speed; the UI labels it accordingly. The `models` row records mode, confidence floor, and selection stats so auto-trained and reviewed-trained models are comparable artefacts.

**D4 — Stage-selectable conditioning for previews.** Zero-shot (and fine-tuned) synthesis needs reference WAVs for speaker conditioning latents. `POST /previews` selects the source stage: `reference_clip` (available from day zero), `segments_raw` (top-N diarised segments by match confidence, post step 2), or `segments_cleaned` (best quality, after a dataset build). The vocal-separation stage is deliberately not an option — vocal stems are whole-file, not speaker-specific. Default: best available stage.

**D5 — CPML gating.** The service ships as an opt-in Compose profile (`xtts`), never bundles weights, and refuses to start unless `XTTS_ACCEPT_CPML=1` (mapped to `COQUI_TOS_AGREED`). Weights download on first use into `${MODELS_ROOT}/xtts`. FlipSync stays Apache 2.0; CPML acceptance is between the operator and the model licence.

**D6 — VRAM tiers and preflight.** This is an open project; the design can't assume a 24 GB card. Preview/inference needs ~6 GB; fine-tuning documents a 16 GB recommendation (12 GB minimum with batch 1 + gradient accumulation). The service runs a VRAM preflight at fine-tune start and fails fast with an `insufficient_vram` error stating required vs available, instead of a raw CUDA OOM three epochs in.

**D7 — OOM retry mirrors Demucs.** Mid-training OOM returns `retry_with: {batch_size, grad_accum}`; the orchestrator resubmits once with the reduced settings. A second OOM is terminal.

## Flow

**Fine-tune:** `POST /projects/{id}/models` → orchestrator creates a `models` row (`pending`) and enqueues two chained jobs: `dataset_build` (cleanup via port 8004 for uncleaned segments, then in-process manifest write to `models/{model_id}/dataset.json`) and `finetune` (port 8005). The service converts the manifest to a Coqui formatter CSV, holds out 10% for eval, trains the GPT component, and writes the checkpoint bundle (`model.pth`, `config.json`, `vocab.json`, cached speaker latents) to `models/{model_id}/`. Dataset filters (all modes): duration outside 1–11 s dropped (GPT trainer sample cap), `cleanup_error`-flagged dropped; drop counts reported, so a thin dataset is visible, not silent (C3). Hard floor: 5 minutes of selected audio → else 409; UI warns below the 30-minute target.

**Preview:** `POST /projects/{id}/previews` `{text, model_id|null, conditioning}` → `preview` job → `synthesise` on port 8005 with resolved reference WAV paths and optional checkpoint dir → WAV at `previews/{job_id}.wav`, streamed via `GET /previews/{id}/audio`. Preview id = job id; no new table.

**Progress:** fine-tune polls at 10 s (not 2 s). Service returns `{phase, epoch, total_epochs, step, total_steps, train_loss, eval_loss, eta_secs}`; orchestrator maps to a percentage for `jobs.progress` and persists the object to a new `jobs.progress_detail` JSON column so the dashboard survives refreshes.

## Data model

New `models` table (id, status `pending → training → ready | failed | cancelled`, dataset_mode, min_confidence, segment_count, dataset_duration_secs, dataset_manifest_path, checkpoint_dir, params JSON, eval_loss, error, timestamps). New job types: `dataset_build`, `finetune`, `preview`. Additive v1.5 migration: `models` table + `jobs.progress_detail`.

## Frontend

New "Voice" tab on the project dashboard: train button gated on the existing approved-duration progress bar (auto mode available behind an explicit "train without review" toggle), training progress card (epoch/loss/ETA), model list with delete, and a preview panel — one text box, conditioning-source dropdown, generate against zero-shot and/or any ready model, players side by side. Ears-only comparison, no scoring.

## Testing

Unit: manifest→formatter conversion, train/eval split, dataset filters, VRAM preflight, conditioning-source resolution. API: job lifecycle with the trainer/TTS mocked; 409s (insufficient dataset, conditioning unavailable, model not ready); OOM retry path. GPU smoke: a tiny-epoch real fine-tune on the CI fixture as a manual script, not in CI.

## Risks / notes

- **Auto-mode transcript quality.** Wrong transcripts hurt GPT training more than noisy audio. Mitigated by the confidence floor and UI labelling; not eliminated.
- **Training time.** Hours on a 3090 for a 30-minute dataset at 10 epochs; longer on smaller cards with grad accumulation. ETA in progress makes this visible.
- **Cross-project GPU contention** exists in v1 already (queue is per-project); a multi-hour fine-tune magnifies it. Single-worker xtts service serialises its own jobs; a global GPU lock is out of scope for v1.5.
- **CPML** commercial review remains the operator's responsibility; resolved for the project by D5.
