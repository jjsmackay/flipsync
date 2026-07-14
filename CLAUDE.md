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
3. **SQLite is the source of truth for all state.** One `.db` file per project at `projects/{project_id}/project.db`. No JSON manifest files except `export/manifest.json` (a derived output written at export time) and `models/{model_id}/dataset.json` (a derived output written at dataset-build time).
4. **Services do not read or write the database.** They receive inputs via HTTP request bodies and return outputs via HTTP response bodies. The orchestrator translates between service responses and database writes.
5. **Files on disk are the inter-service interface.** Vocal separation writes a WAV; diarisation reads that WAV. The orchestrator tells each service where to read/write via absolute paths on the shared `/data` volume.
6. **Jobs execute one at a time per project, and GPU jobs one at a time across the whole host.** The in-memory job queue is FIFO per project; a global GPU semaphore serialises GPU-bound jobs (vocal separation, diarisation, transcription, and voice fine-tune/preview for either engine — XTTS or GPT-SoVITS) across all projects. CPU jobs (extract_audio, export, dataset_build — the cleanup service is CPU-only) are not gated.
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
- **The reference gate lives in `_auto_enqueue_diarisation`.** Step 1 completing without a reference leaves the source at `diarisation_pending` (not a failure) and the project settles into `awaiting_reference`; `POST /pipeline/continue` resumes. A scout never touches its source's status.
- **Transcription results are cumulative.** Each poll returns ALL completed segments so far. Track which IDs you've already written to avoid duplicate writes. Dedup is keyed on the *parent* segment id — split results arrive as `children` under the parent id.
- **Bulk transcription can replace segment rows.** A `resegment: true` segment may come back as `children` — insert child rows and delete the parent in one transaction, then delete the parent WAV best-effort. Only untranscribed `pending`/`below_threshold` segments are eligible; never reviewed ones, never `transcription_segment` reruns.
- **`auto_approved` is system-assigned only.** Users can leave it (approve/reject/maybe/pending) but `PATCH /segments` must 409 any request to enter it. Applied when transcription results land; re-evaluated synchronously on `PATCH /projects` in order: demote ineligible `auto_approved` → `pending`, promote eligible `pending` → `auto_approved`, then the `pending` ↔ `below_threshold` swap. Export and `approved_duration_secs` include it; `approved_count` counts only `approved`.
- **`clipping_warning` is both a column and a status.** The column is a persistent fact. The status is a workflow state. Set both when export cleanup flags clipping. When user re-approves, status → `approved` but column stays `1`. Dataset-build cleanup is different: it NEVER mutates segment review status (or the column) — it only records `cleaned_path` (clipped audio is included in datasets; failed segments just drop out).
- **Voice jobs (`dataset_build`/`finetune`/`preview`) do not drive project status.** They are excluded from the active-jobs count in `status.recompute_project_status` so a running fine-tune leaves the project in `review`/`exported` and export stays available. They still show in `active_jobs` API responses and still block project deletion.
- **`flags` is a JSON array.** Use `json.dumps`/`json.loads`. Current flags: `cleanup_error: <msg>`, `short_transcript`, `transcription_error: <msg>` (per-segment transcription failure — transcript stays NULL so future runs retry; cleared on a later success).

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

**All waves complete (0–5) and merged to `main`.** The pipeline runs end-to-end in code: project CRUD → upload/extract → separation → diarisation/scout → transcription → review → export, plus XTTS train/preview. Frontend is the guided stage-flow dashboard (Sources / Pipeline stepper / Models). For detailed per-feature history, see git log and the auto-memory index (`MEMORY.md`).

**Recently landed:** simplify sweep, separation+alignment upgrades (`htdemucs_ft` default, selectable BS-RoFormer backend via `demucs_model`, optional torchaudio MMS word-alignment behind `align_words`), pipeline tuning UI (per-step settings + cleanup A/B compare), idle-VRAM unload (`IDLE_UNLOAD_SECS`).

**Outstanding — real-GPU E2E verification (not yet confirmed on hardware):**
- Diarisation + steps 2–5 end-to-end, including scout mode.
- RoFormer separation run + `htdemucs_ft` load under torch 2.5.1; torchaudio MMS alignment run.
- torch-cu12 / ctranslate2 system-CUDA coexistence in the transcription image (highest risk — a bad build breaks transcription for everyone). Confirm the torch/torchaudio pin against the CUDA 12.9.2 base.
- Wire the RoFormer (`ROFORMER_MODEL_DIR`) and MMS (`TORCH_HOME`) weight caches into `docker-compose.yml` under `MODELS_ROOT` — documented but not yet wired (re-download on recreate until then).
- Visual check of the stepper dashboard on the deploy host.
- **GPT-SoVITS engine (whole stack unverified on hardware):** image build in CI (pruned dep set: gradio/pydantic pins, jieba_fast source build, NLTK data package names); real prep→s2→s1 runs under torch 2.5.1/cu124 (stdout format vs parser, DDP spawn, pin `FINETUNE_MIN_VRAM_GB` from the real run — 8.0 is provisional); first-use ~1.2 GB weight download into `${MODELS_ROOT}/gpt-sovits` surviving recreate; synthesis of a fine-tuned bundle (v2Pro `.pth` header path, sv-model symlink, output WAV audibly correct); idle unload actually returning VRAM; process-group SIGKILL reaping DDP children on mid-train failure; `scripts/gpu_smoke.py` end-to-end in the container; **and a real preview through the orchestrator/UI, not just gpu_smoke** (create gpt_sovits model → train → ready → preview → compare/download/delete).

## Docker notes

- All services share the same `/data` volume — that's how files flow between them. By default it's the `data` named volume (`flipsync_data` after Compose adds the project prefix; survives `compose down` and a deploy tool re-cloning its stack dir). Set `DATA_ROOT` to a host path (absolute, or `./data` for local dev) to bind-mount instead; if bind-mounting under a managed git clone, use an absolute path outside the clone so a reclone/destroy can't wipe project data.
- Model caches are bind mounts under `${MODELS_ROOT:-/mnt/models/flipsync}/` (`demucs`, `pyannote`, `whisper`, `xtts`, `gpt-sovits`) — the dedicated model-storage disk used by other GPU stacks on the deploy host, not named Docker volumes. `MODELS_ROOT` is an optional `.env` override for hosts without that disk. They survive `docker compose down`. The `gpt-sovits` cache (`GPT_SOVITS_PRETRAINED_DIR`) is wired in `docker-compose.yml` from day one; its ~1.2 GB of v2Pro pretrained weights download from the public `lj1995/GPT-SoVITS` HF repo (no token) on first job. Two older caches are **not yet wired into `docker-compose.yml`** and re-download on container recreate until they are: the RoFormer weights (`ROFORMER_MODEL_DIR`, default `${MODELS_ROOT}/audio-separator`) and the wav2vec2/MMS alignment weights (`TORCH_HOME`). Add matching bind mounts under `MODELS_ROOT` when deploying either feature.
- Only the orchestrator (8000) and frontend (3000) expose ports to the host.
- The cleanup service has no GPU reservation. FFmpeg runs on CPU.
- `HF_TOKEN` env var is required for pyannote model download on first run. After that, cached.
- `IDLE_UNLOAD_SECS` (default 60; 0 disables) controls idle VRAM unloading in the two upstream GPU services (vocal-separation, diarisation) and the gpt-sovits synthesis-model cache: after that many idle seconds they release their model from VRAM so it doesn't squat on GPU memory the next stage needs. Models reload transparently on the next job. Transcription is excluded (last GPU stage). See `spec/deployment.md` §Idle VRAM unloading.
- The voice engines are profile-gated and off by default: `--profile xtts` (needs `XTTS_ACCEPT_CPML=1`) and `--profile gpt-sovits` (MIT weights — no acceptance env, no token). The Train stage appears when at least one engine is healthy.
