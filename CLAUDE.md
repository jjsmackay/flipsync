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
6. **Jobs execute one at a time per project, and GPU jobs one at a time across the whole host.** The in-memory job queue is FIFO per project; a global GPU semaphore serialises GPU-bound jobs (vocal separation, diarisation, transcription, XTTS fine-tune and preview) across all projects. CPU jobs (extract_audio, export, dataset_build — the cleanup service is CPU-only) are not gated.
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

- **Wave 0 (scaffolding):** Complete.
- **Wave 1 (orchestrator core):** Complete.
- **Wave 2 (processing services):** Complete. All four services (vocal-separation, diarisation, transcription, cleanup) implemented with tests.
- **Wave 3 (orchestrator integration):** Complete. Service client, pipeline orchestration, polling, OOM retry, threshold re-evaluation all wired.
- **Wave 4 (Segments API + Export):** Complete. Segment listing with filters/sort/pagination, audio streaming, PATCH review actions with 409 transition enforcement, bulk actions, export job (cleanup → manifest.json → tar.gz) and download.
- **Wave 5 (frontend):** Complete. Project list, dashboard, review queue, export flow, keyboard nav, timeline. Merged to main.
- **Review-fix hardening:** Complete. The 2026-07-12 whole-project review fixes (`integrate/review-fixes`) are merged to main.
- **Deploy hardening:** Complete and merged to main — prebuilt GHCR images, configurable orchestrator/frontend ports, same-origin `/api` proxy (no CORS needed for the UI), model-cache bind mounts under `MODELS_ROOT`, pyannote.audio 4.0.7 upgrade, HF_TOKEN env docs.
- **Conceptual fixes wave 1:** Complete, merged to main — sentence-aligned re-segmentation (transcription service splits untranscribed segments into 1–15 s sentence children; orchestrator replaces parent rows transactionally) and auto-approve banding (`auto_approved` status, per-project thresholds, PATCH re-evaluation, uncertainty sort, dashboard settings panel).
- **Reference from video (diarise + pick):** Complete, merged to main. Pipeline start no longer requires a reference; projects gate at `awaiting_reference` after step 1, and the user either uploads a reference or scans a source for speakers and picks one (scout mode in the diarisation service, `speaker_candidates` table, `reference/scout` endpoints, `pipeline/continue`, Set reference panel in the dashboard). Spec folded into `spec/`.
- **Semantic rename + dashboard UX rework:** Complete, merged to main. `step1`/`step2` renamed to `separation`/`diarisation` everywhere (source statuses, source columns via migration 005, reprocess `steps` param, spec docs). Dashboard rebuilt as a guided stage flow: `deriveStage` + StageStrip + NextActionCard (`frontend/src/utils/stage.ts`, `labels.ts`), user-facing labels only (no raw statuses), reprocess via per-source ⋯ menu, settings collapsed. PipelineControls and JobsPanel replaced (failed jobs → FailedJobsPanel). Spec updated in `spec/review-ui.md` §Project dashboard.
- **Review follow-ups:** Complete, merged to main — per-segment match scoring in the diarisation service, rejected→pending undo, and the global GPU semaphore (host-wide serialisation of GPU jobs).
- **Review fixes wave 2:** Latest wave (2026-07-13, `feature/rf2-*` branches). Transcription: write-time eligibility re-check before resegment splits, per-segment `transcription_error` flags (transcript stays NULL for retry), boundary clamping, duration preload. Export/submit: staged re-export (previous export preserved until success), `clipping_warning` in the export set, idempotent submit via `job_exists` 409 on all services, pre-GPU-lock service-readiness wait (`SERVICE_READY_TIMEOUT_SECS`, default 1800). Reference: idempotent `pipeline/continue`, shared segment-deletion helper with a diarisation no-op gate, shared reference finalisation, scout re-scan/montage-leak/tiebreak fixes, `speaker_match_confidence` persisted (migration 006) and served. Frontend: job-type-aware retry, resilient scout polling, action-aware bulk preview, audio fetch error state, exact work-queue match, cluster score display. Docs/spec synced.
- **Review fixes wave 3:** In progress (`feature/review-fixes-wave3`, unmerged). P1: approving a segment now requires a transcript (PATCH 409 `no_transcript`; bulk approve reports `skipped_no_transcript`; export fails loudly on approved-but-untranscribed rather than silently dropping them from the manifest/archive). P2: `deriveStage` routes an all-below-threshold project to Review (with a "lower the threshold" card + settings shortcut) instead of a misleading Export. P3: per-segment "Re-transcribe" wired into segment detail (was dead code); whisper `batch_size` + `compute_type` exposed as project config (migration 008) and surfaced in the settings panel — OOM/VRAM levers plumbed orchestrator→transcription service. TDD'd across all three services.
- **Idle VRAM unload:** Merged + deployed by the user (idle-timer model release in vocal-separation + diarisation; `IDLE_UNLOAD_SECS`, default 60). Vocal-separation and diarisation release their models from VRAM after `IDLE_UNLOAD_SECS` idle (default 60; 0 disables), so idle upstream services stop squatting on GPU memory the current stage needs — fixes transcription OOM on constrained single-GPU hosts. Idle-timer watcher runs the free on each service's single-worker executor; reload-on-demand on next job. TDD'd (unit + live wiring tests green). GPU E2E on the deploy host still owed.
- **Simplify sweep:** Complete (2026-07-13, `feature/simplify-sweep`). Whole-codebase reuse/simplification/efficiency/altitude pass. Orchestrator: shared helpers (`audio.py` duration probe, `_progress_cb`, `_fail_model`, keyed-flag helpers, manifest builders, `enqueue_bulk_transcription`), `job_types.py` registry (GPU/voice/service facts derive from one table, import-time HANDLERS check), project status fully derived (`compute_project_status` takes active job types; no router-side status writes), `APPROVED/EXPORTABLE_STATUSES` in `state_machines.py`, `GET /capabilities` serves `bulk_action_sources`. Services: vocal-sep attempts-loop + O(n) stitch, diarisation `_diarise_turns`, cleanup bounded parallelism + vectorised clipping. Frontend: `utils/errors.ts`/format helpers, `pipelineJobs()` in stage.ts, exportable-status constants (ExportButton counts fixed), waveform offscreen cache, poll re-render dedup, parallel timeline fetch, served bulk table adopted.
- **Separation + alignment upgrades:** Complete (2026-07-13, `feature/sep-align-upgrades`, squash-merged). `htdemucs_ft` is now the default separation model. A BS-RoFormer backend (via the `audio-separator` package) is selectable through the same `demucs_model` config — weights cache under `ROFORMER_MODEL_DIR`, and it honours idle-VRAM unload like the Demucs models. Optional wav2vec2 forced-alignment pass (`align_words` project config, migration 012) refines whisper word timestamps before sentence-aligned re-segmentation: off by default, timestamps-only (word text, `transcript_confidence`, and auto-approval unaffected), and a no-op unless the segment is being re-segmented. Backend is the torchaudio-native MMS forced aligner (`torchaudio.pipelines.MMS_FA`), not whisperx (dep-solve rejected whisperx's pyannote/Lightning weight). Frontend settings expose the separation-model selector and the align toggle. TDD'd; all runnable suites green.
- **Pipeline tuning UI + distinct steps:** Complete on this branch (2026-07-13). Backend: migration-011 knobs were already plumbed; added `GET /sources/{id}/vocals` (stem streaming) and the stage-generic `POST/GET /tuning-preview` endpoints + `tuning_preview` CPU job (ephemeral cleanup render of one segment to `tuning_previews/`, 24 h TTL sweep, status-exempt via `STATUS_EXEMPT_JOB_TYPES`). Frontend: stage strip split to Upload→Speaker→Separate→Match→Transcribe→Review→Export|Train (`deriveStage` rework + `stepChip`); Process section gained a four-row stepper — per-step Settings disclosures PATCHing their own knob subset ("applies when this step re-runs"), re-run-all-eligible-sources with a single invalidation confirm, per-source vocals players, and a cleanup A/B compare modal (two param columns → two tuning previews → save the winner). Knob metadata centralised in `frontend/src/utils/tuning.ts` + shared `KnobFields`/`StageSettingsPanel`; TrainPanel per-run XTTS params, PreviewPanel shared temperature slider, CreateProjectModal Advanced disclosure. TranscribeSettingsPanel absorbed into the Transcribe row. Design doc: `docs/superpowers/specs/2026-07-13-pipeline-tuning-ui-design.md`. Deferred follow-on: separation window A/B (needs service-side window params).
- **Next:** Outstanding: full end-to-end verification on real GPU hardware (diarisation + steps 2–5, including scout mode, not yet confirmed E2E). For the separation+alignment upgrades specifically: RoFormer separation run + `htdemucs_ft` (now default) load under torch 2.5.1; torchaudio MMS alignment run; the torch-cu12 / ctranslate2 system-CUDA coexistence check in the transcription image (highest risk — a bad build breaks transcription for everyone, not just align users); confirm the torch/torchaudio pin against the CUDA 12.9.2 base; and wire the `audio-separator` (RoFormer) and `TORCH_HOME` (MMS) weight caches into `docker-compose.yml` under `MODELS_ROOT` (documented but not yet wired — they re-download on recreate until then). Visual check of the new stepper dashboard on the deploy host owed.

## Docker notes

- All services share the same `/data` volume — that's how files flow between them. By default it's the `data` named volume (`flipsync_data` after Compose adds the project prefix; survives `compose down` and a deploy tool re-cloning its stack dir). Set `DATA_ROOT` to a host path (absolute, or `./data` for local dev) to bind-mount instead; if bind-mounting under a managed git clone, use an absolute path outside the clone so a reclone/destroy can't wipe project data.
- Model caches are bind mounts under `${MODELS_ROOT:-/mnt/models/flipsync}/` (`demucs`, `pyannote`, `whisper`) — the dedicated model-storage disk used by other GPU stacks on the deploy host, not named Docker volumes. `MODELS_ROOT` is an optional `.env` override for hosts without that disk. They survive `docker compose down`. Two newer caches are **not yet wired into `docker-compose.yml`** and re-download on container recreate until they are: the RoFormer weights (`ROFORMER_MODEL_DIR`, default `${MODELS_ROOT}/audio-separator`) and the wav2vec2/MMS alignment weights (`TORCH_HOME`). Add matching bind mounts under `MODELS_ROOT` when deploying either feature.
- Only the orchestrator (8000) and frontend (3000) expose ports to the host.
- The cleanup service has no GPU reservation. FFmpeg runs on CPU.
- `HF_TOKEN` env var is required for pyannote model download on first run. After that, cached.
- `IDLE_UNLOAD_SECS` (default 60; 0 disables) controls idle VRAM unloading in the two upstream GPU services (vocal-separation, diarisation): after that many idle seconds they release their model from VRAM so it doesn't squat on GPU memory the next stage needs. Models reload transparently on the next job. Transcription is excluded (last GPU stage). See `spec/deployment.md` §Idle VRAM unloading.
