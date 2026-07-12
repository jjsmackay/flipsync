# CLAUDE.md — FlipSync Agent Configuration

## What this is

FlipSync extracts speaker-specific dialogue audio from video files and produces datasets for voice cloning. Self-hosted, GPU-required, Docker Compose deployment. Single speaker per project in v1.

## Spec location

All design documents live in `spec/`. Read order:

1. `spec/overview.md` — goals, constraints, scope
2. `spec/architecture.md` — system design, services, data flow, implementation guidance
3. `spec/data-models.md` — SQLite schema, state machines, enumerations
4. `spec/api-contracts.md` — all HTTP interfaces (browser→orchestrator, orchestrator→services)
5. `spec/pipeline.md` — per-step processing detail
6. `spec/review-ui.md` — frontend behaviour, keyboard model, component spec
7. `spec/deployment.md` — Docker, GPU, configuration
8. `spec/adr/ADR-001-sqlite.md` — SQLite decision record

**The spec is the source of truth.** If the code disagrees with the spec, the code is wrong. If the spec is ambiguous, ask — don't guess.

## Repo structure

```
flipsync/
├── spec/                     # Design documents (read-only reference)
├── services/
│   ├── orchestrator/         # FastAPI app — Python
│   ├── vocal-separation/     # Demucs service — Python
│   ├── diarisation/          # pyannote + cosine similarity — Python
│   ├── transcription/        # faster-whisper — Python
│   └── cleanup/              # FFmpeg cleanup — Python
├── frontend/                 # React + TypeScript SPA
├── docker-compose.yml
├── CLAUDE.md                 # This file
└── README.md
```

## Architecture invariants — DO NOT BREAK

1. **Services do not call each other.** All coordination flows through the orchestrator. Services expose HTTP APIs and process jobs. That's it.
2. **The browser never calls a processing service.** All browser traffic goes to the orchestrator on port 8000.
3. **SQLite is the source of truth for all state.** One `.db` file per project at `projects/{project_id}/project.db`. No JSON manifest files except `export/manifest.json` which is a derived output written at export time.
4. **Services do not read or write the database.** They receive inputs via HTTP request bodies and return outputs via HTTP response bodies. The orchestrator translates between service responses and database writes.
5. **Files on disk are the inter-service interface.** Vocal separation writes a WAV; diarisation reads that WAV. The orchestrator tells each service where to read/write via absolute paths on the shared `/data` volume.
6. **Jobs execute one at a time per project.** The in-memory job queue is FIFO. No parallel GPU jobs within a project.
7. **All processing services expose `GET /health` returning `{"status": "ok"}`.** The orchestrator checks this before submitting jobs.
8. **State transitions are enforced by the orchestrator.** See `spec/data-models.md` for the complete segment, source, and project state machines. Any transition not in those lists must be rejected with HTTP 409.
9. **Error responses use the standard format everywhere:** `{"error": "snake_case", "message": "Human-readable.", "detail": {}}`.

## Tech stack

| Component | Stack |
|-----------|-------|
| Orchestrator | Python, FastAPI, SQLite, `asyncio` for job runner |
| Vocal Separation | Python, Demucs (`htdemucs` / `mdx_extra`) |
| Diarisation | Python, pyannote.audio, scipy |
| Transcription | Python, faster-whisper |
| Cleanup | Python, FFmpeg (subprocess) |
| Frontend | React, TypeScript, Vite |

All Python services use standard HTTP frameworks (FastAPI or Flask — agent's choice for processing services, FastAPI for orchestrator). No external job queue library (no Celery, no RQ). No ORM — raw SQL for SQLite.

## Branching

```
main
└── integrate/orchestrator
│   ├── feature/project-crud
│   ├── feature/job-queue
│   ├── feature/pipeline-control
│   └── feature/export
└── integrate/vocal-separation
└── integrate/diarisation
└── integrate/transcription
└── integrate/cleanup
└── integrate/frontend
    ├── feature/project-dashboard
    ├── feature/review-queue
    └── feature/timeline
```

Work on feature branches. Merge to integration branches. Only integration branches merge to `main`. Never commit directly to `main`.

## Execution waves

Services have dependencies. Build in this order:

### Wave 0 — Scaffolding
- Docker Compose file with all services defined (can use placeholder images)
- Repo structure with empty service directories and Dockerfiles
- Spec files copied to `spec/`

### Wave 1 — Orchestrator core (blocking for everything else)
- Project CRUD (`POST/GET/PATCH/DELETE /projects`)
- Source upload with FFmpeg extraction (`POST /projects/{id}/sources`)
- Reference clip upload (`POST /projects/{id}/reference`)
- SQLite database creation and migrations
- Job queue: in-memory FIFO backed by `jobs` table, `asyncio` background tasks
- Service health polling on startup
- CORS middleware
- Streaming file uploads (no memory buffering)

**Read:** `spec/api-contracts.md` Part 1 (Projects, Sources, Reference), `spec/data-models.md` (full), `spec/architecture.md` (Orchestrator section)

### Wave 2 — Processing services (parallel, no cross-dependencies)

Each service is independent. They can be built in parallel after Wave 1 establishes the job submission/polling pattern.

**Vocal Separation agent:**
- `GET /health`, `POST /jobs`, `GET /jobs/{job_id}`
- Demucs whole-file processing, OOM catch → retry with chunking, chunk stitching
- Read: `spec/api-contracts.md` §Vocal Separation Service, `spec/pipeline.md` §Step 1

**Diarisation agent:**
- `GET /health`, `POST /jobs`, `GET /jobs/{job_id}`
- pyannote diarisation, speaker embedding extraction, average-embedding cosine similarity
- Segment WAV slicing with UUID filenames, output_dir creation
- Read: `spec/api-contracts.md` §Diarisation Service, `spec/pipeline.md` §Step 2

**Transcription agent:**
- `GET /health`, `POST /jobs`, `GET /jobs/{job_id}`
- faster-whisper batch transcription, cumulative `completed_segments` in poll response
- Read: `spec/api-contracts.md` §Transcription Service, `spec/pipeline.md` §Step 3

**Cleanup agent:**
- `GET /health`, `POST /jobs`, `GET /jobs/{job_id}`
- FFmpeg two-pass loudness normalisation, silence trim, high-pass, clipping detection
- Per-segment error handling (continue on failure, report per-segment errors)
- Read: `spec/api-contracts.md` §Cleanup Service, `spec/pipeline.md` §Step 4

### Wave 3 — Orchestrator integration + pipeline control
- Wire orchestrator to call processing services (submit job, poll, handle response)
- Pipeline start endpoint (`POST /projects/{id}/pipeline/start`)
- Reprocess endpoint (`POST /projects/{id}/sources/{sid}/reprocess`)
- Transcription trigger endpoints
- OOM retry logic for vocal separation (check `retry_with_chunk_secs`, resubmit with `chunk_secs`)
- Threshold re-evaluation on `PATCH /projects` (inline SQL, bidirectional `pending` ↔ `below_threshold`)
- Incremental transcription writes (deduplicate cumulative `completed_segments`)
- Source status transitions, project status recomputation

**Read:** `spec/api-contracts.md` (full), `spec/data-models.md` §State transitions

### Wave 4 — Segments API + Export
- Segment listing with filters/sort/pagination (`GET /projects/{id}/segments`)
- Segment audio streaming (`GET /projects/{id}/segments/{sid}/audio`) — full file, no Range support
- Segment review actions (`PATCH /projects/{id}/segments/{sid}`) — enforce transition rules
- Bulk actions (`POST /projects/{id}/segments/bulk`) — respect transition rules per segment
- Export trigger → cleanup service → manifest.json from DB → tar.gz archive
- Export download endpoint

**Read:** `spec/api-contracts.md` §Segments + §Export, `spec/data-models.md` §Segment status

### Wave 5 — Frontend
- Project list page
- Project dashboard (source status, stats, job progress, failed jobs, pipeline controls)
- Review queue (segment list + detail panel, filter bar, audio player, transcript editing)
- Keyboard navigation (A/M/X/J/K/Space/R/E)
- Bulk operations panel with live preview count
- Timeline component (canvas-rendered, zoom, segment selection)
- Export flow (confirmation panel, progress, download)
- Polling: 3s interval when jobs active, stop when idle

**Read:** `spec/review-ui.md` (full), `spec/api-contracts.md` Part 1

## Common pitfalls

### Orchestrator agent
- **Database per project, not per app.** Each project has its own `project.db`. Don't create a single shared database. Use `PRAGMA journal_mode=WAL`.
- **Extraction is a job, not synchronous.** `POST /sources` writes the file and enqueues an `extract_audio` job. The handler returns 202 immediately. Extraction runs in a background asyncio task.
- **`extract_audio` and `export` run in-process.** These are FFmpeg subprocesses inside the orchestrator, not external service calls. They still use job rows for status tracking but don't involve HTTP polling.
- **Streaming uploads.** Video files are 1–4 GB. Write chunks to disk as they arrive. Do not buffer in memory.
- **CORS.** Frontend is on :3000, orchestrator on :8000. Add middleware.
- **Threshold changes are bidirectional.** Lowering threshold: `below_threshold` → `pending`. Raising threshold: `pending` → `below_threshold`. Other statuses are untouched.
- **Transcription results are cumulative.** Each poll returns ALL completed segments so far. Track which IDs you've already written to avoid duplicate writes.
- **`clipping_warning` is both a column and a status.** The column is a persistent fact. The status is a workflow state. Set both when cleanup flags clipping. When user re-approves, status → `approved` but column stays `1`.
- **`flags` is a JSON array.** Use `json.dumps`/`json.loads`. Current flags: `cleanup_error: <msg>`, `short_transcript`.

### Processing service agents
- **You do not touch the database.** Receive paths and params via HTTP. Return results via HTTP. The orchestrator handles persistence.
- **You do not call other services.** You receive a job, process it, return a result.
- **`GET /jobs/{job_id}` must be idempotent.** The orchestrator polls this every 2 seconds. Return current state, don't mutate on read.
- **Create output directories if they don't exist.** Don't assume the orchestrator pre-creates them.
- **Use full UUIDs for segment filenames.** Not truncated.

### Frontend agent
- **Stateless browser.** All state comes from the orchestrator API. No localStorage for segment state.
- **Poll `GET /projects/{id}` every 3s when jobs active.** Stop when `active_jobs` is empty. Resume on user pipeline action.
- **Filter state in URL query string.** Bookmarkable filtered views.
- **Keyboard shortcuts only active when detail panel has focus.** When transcript edit is focused, keys pass through to the input except Escape and Enter.
- **Audio: full file download per segment, not streaming.** No Range header support. Segments are small (~1-2 MB).

## Testing approach

Each service should have:
1. Unit tests for core logic (Demucs wrapper, pyannote wrapper, FFmpeg command builder, etc.)
2. Integration tests against the HTTP API (submit job, poll to completion, verify output)
3. A small test fixture (short audio file, ~5-10 seconds) committed to the repo for CI

The orchestrator needs:
1. Unit tests for state machine transitions (segment, source, project)
2. Unit tests for job queue logic
3. Integration tests against a mock service (or the real service with test fixtures)
4. API tests for every endpoint (request validation, response shape, error cases)

Frontend needs:
1. Component tests for the review queue (keyboard navigation, status transitions)
2. API mock tests for polling behaviour

## Development environment

- **Python:** `python3` only (no `python` or `pip` binary). Use `uv` for running/installing: `uv run --with <deps> python -m <cmd>`
- **Node:** Available via nvm. Package manager is `pnpm` (enabled via `corepack enable pnpm`).
- **Docker:** Available in rootless mode. `docker compose up -d` from repo root pulls prebuilt GHCR images (no local build step).
- **Orchestrator tests:** `cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio python -m pytest tests/ -v`
- **Vocal separation tests:** `cd services/vocal-separation && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio --with anyio --with soundfile --with numpy python -m pytest tests/ -v`
- **Diarisation tests:** `cd services/diarisation && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio --with soundfile --with numpy --with scipy python -m pytest tests/ -v`
- **Transcription tests:** `cd services/transcription && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio python -m pytest tests/ -v`
- **Cleanup tests:** `cd services/cleanup && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio --with numpy --with soundfile python -m pytest tests/ -v`

## Error handling pattern

All services use a flat error response: `{"error": "snake_case", "message": "...", "detail": {}}`.
The orchestrator uses `AppError` from `errors.py` — never `HTTPException` (which wraps in `{"detail": {...}}`).
Processing services should return the same flat format directly.

## Current status

- **Wave 0 (scaffolding):** Complete.
- **Wave 1 (orchestrator core):** Complete.
- **Wave 2 (processing services):** Complete. All four services (vocal-separation, diarisation, transcription, cleanup) implemented with tests.
- **Wave 3 (orchestrator integration):** Complete. Service client, pipeline orchestration, polling, OOM retry, threshold re-evaluation all wired.
- **Wave 4 (Segments API + Export):** Complete. Segment listing with filters/sort/pagination, audio streaming, PATCH review actions with 409 transition enforcement, bulk actions, export job (cleanup → manifest.json → tar.gz) and download.
- **Wave 5 (frontend):** Complete. Project list, dashboard, review queue, export flow, keyboard nav, timeline. Merged to main.
- **Review-fix hardening:** Complete. The 2026-07-12 whole-project review fixes (`integrate/review-fixes`) are merged to main.
- **Deploy hardening:** Complete and merged to main — prebuilt GHCR images, configurable orchestrator/frontend ports, same-origin `/api` proxy (no CORS needed for the UI), model-cache bind mounts under `MODELS_ROOT`, pyannote.audio 4.0.7 upgrade, HF_TOKEN env docs.
- **Next:** All waves and hardening merged. Outstanding: full end-to-end verification on real GPU hardware (diarisation + steps 2–5 not yet confirmed E2E).

## Docker notes

- All services share the same `/data` volume — that's how files flow between them. By default it's the `data` named volume (`flipsync_data` after Compose adds the project prefix; survives `compose down` and a deploy tool re-cloning its stack dir). Set `DATA_ROOT` to a host path (absolute, or `./data` for local dev) to bind-mount instead; if bind-mounting under a managed git clone, use an absolute path outside the clone so a reclone/destroy can't wipe project data.
- Model caches are bind mounts under `${MODELS_ROOT:-/mnt/models/flipsync}/` (`demucs`, `pyannote`, `whisper`) — the dedicated model-storage disk used by other GPU stacks on the deploy host, not named Docker volumes. `MODELS_ROOT` is an optional `.env` override for hosts without that disk. They survive `docker compose down`.
- Only the orchestrator (8000) and frontend (3000) expose ports to the host.
- The cleanup service has no GPU reservation. FFmpeg runs on CPU.
- `HF_TOKEN` env var is required for pyannote model download on first run. After that, cached.
