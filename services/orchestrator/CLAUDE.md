# Orchestrator Service — Agent Scope

You own the orchestrator service (`services/orchestrator/`). Do not modify any other service directory or the frontend.

## Required reading before writing code

1. `spec/api-contracts.md` — full document, both Part 1 (browser→orchestrator) and Part 2 (orchestrator→services)
2. `spec/data-models.md` — full document (schema, state machines, enumerations, indexes)
3. `spec/architecture.md` — Orchestrator section and data flow

## What you own

- FastAPI application (`main.py`, all routers)
- SQLite database management (`db.py`, `migrations/`)
- Job queue (`jobs.py`, `status.py`)
- State machines (`state_machines.py`)
- Error handling (`errors.py`)
- All tests in `tests/`

## Key constraints

- **One SQLite database per project** at `projects/{project_id}/project.db`. Not a shared database. Use `PRAGMA journal_mode=WAL`.
- **No ORM.** Raw SQL only.
- **Error responses** use `AppError` from `errors.py` — never `HTTPException`. Format: `{"error": "snake_case", "message": "...", "detail": {}}`.
- **Streaming uploads.** Video files are 1–4 GB. Write chunks to disk via `aiofiles`. Never buffer in memory.
- **`extract_audio` and `export` run in-process** as FFmpeg subprocesses. They are not external service calls.
- **Job queue** is `asyncio`-based, one job at a time per project. No Celery, no RQ.
- **Recompute project status** after every job completion AND every user action (segment review, source deletion, bulk actions). Use `status.recompute_project_status()`.
- **Threshold changes are bidirectional.** `pending` ↔ `below_threshold`. Other statuses (approved, rejected, maybe, etc.) are never affected.
- **Validate state transitions** before any status change. Use `state_machines.validate_segment_transition()` and `state_machines.validate_source_transition()`. Return 409 on invalid transitions.

## Testing

Run tests with: `uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio python -m pytest tests/ -v`

All tests must pass before committing.
