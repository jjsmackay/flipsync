# Vocal Separation Service — Agent Scope

You own the vocal separation service (`services/vocal-separation/`). Do not modify any other service directory, the orchestrator, or the frontend.

## Required reading before writing code

1. `spec/api-contracts.md` §Vocal Separation Service (port 8001)
2. `spec/pipeline.md` §Step 1 — Vocal Separation

## Your API

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Return `{"status": "ok"}` when ready |
| `POST /jobs` | Accept a job, return 202 with `{"job_id": "..."}` |
| `GET /jobs/{job_id}` | Return current job state (polled every 2s by orchestrator) |

## Key constraints

- **You do not touch the database.** Receive input/output paths via HTTP. Return results via HTTP. The orchestrator handles all persistence.
- **You do not call other services.** You receive a job, process it, return a result.
- **`GET /jobs/{job_id}` must be idempotent.** Return current state, don't mutate on read.
- **Create output directories if they don't exist.**
- **Demucs models:** Default `htdemucs`. Accept `model` param to use `mdx_extra` as fallback.
- **OOM handling:** Attempt whole-file processing first. On CUDA OOM, return `{"status": "failed", "error": "cuda_oom", "retry_with_chunk_secs": 60}`. The orchestrator will resubmit with `chunk_secs` set.
- **Chunked processing:** When `chunk_secs` is set, process audio in chunks with 1-second overlap, then stitch before writing output.
- **Error responses** use: `{"error": "snake_case", "message": "Human-readable.", "detail": {}}`.
