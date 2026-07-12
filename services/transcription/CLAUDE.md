# Transcription Service — Agent Scope

You own the transcription service (`services/transcription/`). Do not modify any other service directory, the orchestrator, or the frontend.

## Required reading before writing code

1. `spec/api-contracts.md` §Transcription Service (port 8003)
2. `spec/pipeline.md` §Step 3 — Transcription

## Your API

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Return `{"status": "ok"}` when ready |
| `POST /jobs` | Accept a job with segment list, return 202 |
| `GET /jobs/{job_id}` | Return current state with cumulative `completed_segments` |

## Key constraints

- **You do not touch the database.** Receive segment WAV paths via HTTP. Return transcripts via HTTP.
- **You do not call other services.**
- **`GET /jobs/{job_id}` must be idempotent.** Return current state, don't mutate on read.
- **`completed_segments` is cumulative.** Each poll returns ALL segments completed so far, not just new ones since last poll. The orchestrator deduplicates.
- **faster-whisper models:** Accept `model` param (tiny, base, small, medium, large-v2, large-v3). Default `large-v2`.
- **Language:** Accept `language` param (ISO 639-1 code or null for auto-detect).
- **Batch size:** Accept `batch_size` param (default 16). Number of segments to transcribe concurrently on GPU.
- **Compute type:** Accept `compute_type` param (`default` | `float16` | `int8_float16` | `int8`, default `default`). `default` derives per device (float16 GPU, int8 CPU); others pass through to CTranslate2 to trade precision for VRAM. `load_model` caches on `(model_size, compute_type)` and reloads when either changes.
- **No automatic OOM retry.** If the service OOMs, the job fails. The user re-triggers with a smaller batch_size.
- **Per-segment output:** Return `id`, `transcript` (string), and `transcript_confidence` (mean word probability, 0.0–1.0).
- **Sentence-aligned re-segmentation:** When a segment has `resegment: true` (with required `start_secs`), split the word sequence per `spec/pipeline.md` §Sentence-aligned re-segmentation and slice child WAVs from the parent (stdlib `wave`, same directory, full-UUID filenames). The completed entry becomes `{id, children: [{id, wav_path, start_secs, end_secs, transcript, transcript_confidence}]}` keyed by the parent id, with **absolute** child timestamps (`start_secs` + in-file offsets). Single utterance or no words → return the unsplit shape. `resegment` false/absent → exactly the old behaviour. Logic lives in `resegment.py`.
- **Error responses** use: `{"error": "snake_case", "message": "Human-readable.", "detail": {}}`.
