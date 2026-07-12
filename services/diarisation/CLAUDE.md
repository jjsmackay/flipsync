# Diarisation Service — Agent Scope

You own the diarisation service (`services/diarisation/`). Do not modify any other service directory, the orchestrator, or the frontend.

## Required reading before writing code

1. `spec/api-contracts.md` §Diarisation Service (port 8002)
2. `spec/pipeline.md` §Step 2 — Diarisation + Speaker Matching

## Your API

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Return `{"status": "ok"}` when ready |
| `POST /jobs` | Accept a job, return 202 with `{"job_id": "..."}` |
| `GET /jobs/{job_id}` | Return current job state with segments on completion |

## Key constraints

- **You do not touch the database.** Receive paths via HTTP. Return segment metadata via HTTP. The orchestrator writes to SQLite.
- **You do not call other services.**
- **`GET /jobs/{job_id}` must be idempotent.** Return current state, don't mutate on read.
- **Create `output_dir` if it doesn't exist.**
- **Speaker matching method:** Extract a single embedding from the reference clip, plus one embedding per segment. Each segment's `match_confidence` is the cosine similarity of its own embedding vs the reference. The per-speaker average embedding (from those same per-segment embeddings) scored against the reference is reported on every segment as `speaker_match_confidence` (secondary cluster signal). Segments under 1.0 s, or whose embedding extraction fails, fall back to the cluster score for `match_confidence` — a per-segment extraction failure never fails the job.
- **Segment WAV files:** Write each segment as `{output_dir}/{segment_id}.wav` where `segment_id` is a full UUID you generate. The orchestrator uses these IDs as primary keys.
- **Return ALL speakers' segments**, not just the matched target. The orchestrator filters by threshold.
- **On completion**, return `segments` array (id, start_secs, end_secs, speaker_label, match_confidence, speaker_match_confidence, wav_path) and `coverage_ratio`.
- **pyannote requires `HF_TOKEN`** env var. Models download on first run (~2 GB). Use generous startup timeout.
- **Error responses** use: `{"error": "snake_case", "message": "Human-readable.", "detail": {}}`.
