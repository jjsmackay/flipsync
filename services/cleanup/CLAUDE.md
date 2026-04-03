# Cleanup Service — Agent Scope

You own the cleanup service (`services/cleanup/`). Do not modify any other service directory, the orchestrator, or the frontend.

## Required reading before writing code

1. `spec/api-contracts.md` §Cleanup Service (port 8004)
2. `spec/pipeline.md` §Step 4 — Cleanup + Normalisation

## Your API

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Return `{"status": "ok"}` when ready |
| `POST /jobs` | Accept a job with segment list and params, return 202 |
| `GET /jobs/{job_id}` | Return current state with per-segment results on completion |

## Key constraints

- **You do not touch the database.** Receive input/output paths via HTTP. Return results via HTTP.
- **You do not call other services.**
- **`GET /jobs/{job_id}` must be idempotent.** Return current state, don't mutate on read.
- **No GPU.** FFmpeg runs on CPU. This service has no GPU reservation.
- **Processing order per segment:** (1) two-pass loudness normalisation (EBU R128), (2) silence trimming (leading/trailing), (3) high-pass filter (>80 Hz), (4) clipping detection.
- **Per-segment error handling.** Do not abort the job on individual segment failures. Process all segments. Each result includes `error` (null on success), `output_path`, `clipping_warning` (bool), `auto_rejected` (bool — true if silent after trim).
- **Job-level `error`** is only set if the entire job fails (e.g., FFmpeg binary not found).
- **Params** include: `target_lufs`, `true_peak_dbtp`, `lra`, `highpass_hz`, `silence_threshold_db`, `silence_min_duration_secs`, `clipping_threshold_db`, `clipping_min_consecutive_samples`, `output_sample_rate` (22050), `output_channels` (1).
- **Create output directories if they don't exist.**
- **Error responses** use: `{"error": "snake_case", "message": "Human-readable.", "detail": {}}`.
