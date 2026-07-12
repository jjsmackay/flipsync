# XTTS-v2 Integration (v1.5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In-app XTTS-v2 support: a fifth processing service (fine-tune + synthesise), orchestrator models/previews endpoints with a shared dataset-build step, and a Voice section on the dashboard.

**Architecture:** New `services/xtts` (port 8005) follows the vocal-separation service template (single-worker executor, engine-module test seam, OOM reporting) with transcription's rich-progress polling shape. The orchestrator gains a migration (`models` table, `jobs.progress_detail`), three job types (`dataset_build`, `finetune`, `preview`), two routers, and an XTTS service-client entry. The frontend gains a stacked "Voice" dashboard section.

**Tech Stack:** Python/FastAPI, `coqui-tts` (Idiap fork) + its `GPTTrainer`, SQLite raw SQL, React+TS+Tailwind v4.

**Normative contracts (read before each task):** `spec/api-contracts.md` §Models, §Previews, §"XTTS Service (port 8005)"; `spec/data-models.md` §models, §"Model status", §"Job types"; `spec/deployment.md` §"XTTS service"; design rationale in `docs/superpowers/specs/2026-07-12-xtts-v2-integration-design.md`.

## Global Constraints

- Error format everywhere: `{"error": "snake_case", "message": "...", "detail": {}}`. Orchestrator raises `AppError` (`services/orchestrator/errors.py`), never `HTTPException`.
- Services never touch the DB, never call other services; paths on `/data` are absolute.
- `GET /jobs/{job_id}` idempotent; POST /jobs returns 202; duplicate job_id → 409.
- Output WAVs 22.05 kHz mono 16-bit PCM (cleanup already guarantees this for cleaned segments).
- Python services: no ORM, no Celery; deps in `requirements.txt` (pins get a justifying comment).
- Test commands: orchestrator and per-service `uv run --with ...` invocations in root `CLAUDE.md` §Development environment. xtts tests: `cd services/xtts && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio --with soundfile --with numpy python -m pytest tests/ -v`.
- Workers do NOT commit; the orchestrating session commits per area after review.
- Frontend has NO test runner (do not introduce one in this wave); verify with `pnpm build` (tsc + vite).

---

### Task 1: XTTS service — API layer

**Files:**
- Create: `services/xtts/main.py`, `services/xtts/engine.py` (stub seam), `services/xtts/requirements.txt`, `services/xtts/tests/conftest.py`, `services/xtts/tests/test_api.py`
- Template: copy structure from `services/vocal-separation/main.py` (job store `_jobs: dict[str, dict]`, `_MAX_JOBS=256`, `_background_tasks` strong-ref set, `ThreadPoolExecutor(max_workers=1)` at main.py:38-57; 422/404 flat-error handlers at main.py:283-297; POST/GET handlers at main.py:326-371; `_is_cuda_oom`/`_empty_cuda_cache` at main.py:123-140 — copy these two verbatim).

**Interfaces:**
- Produces (consumed by orchestrator, Task 8/9): HTTP contract exactly per `spec/api-contracts.md` §"XTTS Service (port 8005)".
- Produces (consumed by Task 3): `engine.finetune(manifest_path: str, output_dir: str, params: dict, progress_cb: Callable[[dict], None]) -> dict` (returns the `result` payload: checkpoint_dir, model_path, config_path, vocab_path, speaker_latents_path, final_eval_loss); `engine.synthesise(text: str, language: str, reference_wavs: list[str], checkpoint_dir: str | None, output_path: str, params: dict) -> dict` (returns `{output_path, duration_secs}`); `engine.vram_available_gb() -> float`; `engine.FINETUNE_MIN_VRAM_GB = 12.0`. In this task `engine.py` contains only these signatures raising `NotImplementedError` (plus the constant) — the seam tests patch.
- Request models: Pydantic discriminated union on `type`: `FinetuneJob {job_id, type: Literal["finetune"], manifest_path, output_dir, params: FinetuneParams}` with `FinetuneParams {epochs:int=10, batch_size:int=3, grad_accum:int=1, learning_rate:float=5e-6, language:str, eval_split:float=0.1}`; `SynthesiseJob {job_id, type: Literal["synthesise"], text: str (1..2000), language: str, reference_wavs: list[str] (1..10), checkpoint_dir: str|None=None, output_path: str, params: SynthParams {temperature: float=0.65}}`.

**Behaviour to implement:**
- Startup: if `os.environ.get("XTTS_ACCEPT_CPML") != "1"` set module global `_startup_error = "XTTS_ACCEPT_CPML not set..."`; `/health` returns 503 flat error `cpml_not_accepted` in that case, else `{"status": "ok"}`. When accepted, set `os.environ["COQUI_TOS_AGREED"] = "1"` at startup.
- `POST /jobs`: 409 flat error `duplicate_job` on existing job_id; seed job dict `{job_id, type, status: "running", progress: {"phase": "preparing"} for finetune | None for synthesise, result: None, error: None, retry_with: None}`; dispatch `asyncio.create_task(_run_job_async(job, request))` offloading to the executor.
- Finetune worker `_run_finetune(job, req)`: VRAM preflight — `avail = engine.vram_available_gb()`; if `avail < engine.FINETUNE_MIN_VRAM_GB` → `status=failed`, `error=f"insufficient_vram: {engine.FINETUNE_MIN_VRAM_GB:g} GB required, {avail:.1f} GB available"`. Then `engine.finetune(..., progress_cb)` where progress_cb replaces `job["progress"]` with the dict (shape: `{phase, epoch, total_epochs, step, total_steps, train_loss, eval_loss, eta_secs}`). On `_is_cuda_oom(exc)`: `_empty_cuda_cache()`; if `req.params.batch_size > 1` → `error="cuda_oom"`, `retry_with={"batch_size": 1, "grad_accum": req.params.batch_size * req.params.grad_accum}`; else (already reduced) `error="cuda_oom"`, `retry_with=None` (terminal). Success → `status=complete`, `result=<engine dict>`, `progress={"phase":"packaging", ...last}` then complete.
- Synthesise worker: validate all `reference_wavs` exist (fail `reference_not_found` naming the path), create `output_path` parent dir, call `engine.synthesise`, `result={output_path, duration_secs}`. OOM here is terminal `cuda_oom` (no retry_with).
- `GET /jobs/{job_id}`: return full job dict (job_id, status, progress, result, error, retry_with); 404 flat error `job_not_found`.
- `__main__`: `uvicorn.run(app, host="0.0.0.0", port=8005)`.

**Tests** (patch `engine.finetune`/`engine.synthesise`/`engine.vram_available_gb` as module attributes BEFORE importing main — the vocal-separation seam, `services/vocal-separation/tests/test_api.py:22-27`):

- [ ] health ok when `XTTS_ACCEPT_CPML=1` (monkeypatch env); 503 `cpml_not_accepted` when unset
- [ ] POST finetune → 202 `{job_id}`; poll transitions to complete with result passthrough; progress dict visible mid-run (use an `engine.finetune` side_effect that calls progress_cb then blocks on an event)
- [ ] POST duplicate job_id → 409 `duplicate_job`
- [ ] VRAM preflight failure: `vram_available_gb` → 8.0 ⇒ failed, error starts `insufficient_vram`
- [ ] OOM at batch_size 3 ⇒ failed, `retry_with == {"batch_size": 1, "grad_accum": 3}` (simulate with `RuntimeError("CUDA out of memory")`)
- [ ] OOM at batch_size 1 ⇒ failed, `retry_with is None`
- [ ] POST synthesise → complete with `{output_path, duration_secs}`; missing reference wav ⇒ failed `reference_not_found`
- [ ] GET unknown job → 404 flat error; GET is idempotent (two polls identical)
- [ ] validation: bad `type` ⇒ 422 flat `validation_error`; empty `reference_wavs` ⇒ 422

Run: xtts test command from Global Constraints. Expected: all pass without torch/coqui installed.

### Task 2: XTTS service — dataset conversion module

**Files:**
- Create: `services/xtts/dataset.py`, `services/xtts/tests/test_dataset.py`

**Interfaces:**
- Produces (consumed by `engine.finetune`): `load_manifest(manifest_path: str) -> list[dict]` (raises `ValueError` on missing/empty `segments`); `manifest_to_coqui_csv(manifest_path: str, out_dir: str, eval_split: float = 0.1) -> tuple[str, str]` returning `(train_csv, eval_csv)` paths.

**Behaviour:** Manifest per `spec/data-models.md` §models — export-manifest schema with absolute `audio_file` paths. CSV format is the Coqui `coqui` formatter: header `audio_file|text|speaker_name`, pipe-delimited, `speaker_name` fixed `"target"`. Text = manifest `text` with pipes/newlines replaced by spaces. Deterministic split: sort segments by `id`; every `round(1/eval_split)`-th row (index % 10 == 0 for 0.1) → eval, remainder → train; guarantee ≥1 eval and ≥1 train row (if the split would empty either, move one row). Write `metadata_train.csv` / `metadata_eval.csv` under `out_dir` (create dir).

- [ ] test: 12-segment manifest → train has 10-11 rows, eval ≥1, header correct, pipe-safe text, absolute paths preserved
- [ ] test: 2-segment manifest → 1 train + 1 eval
- [ ] test: empty/missing segments → ValueError
- [ ] Run xtts suite; all pass. (No ML deps needed — pure stdlib + csv.)

### Task 3: XTTS service — engine implementation

**Files:**
- Modify: `services/xtts/engine.py` (replace stubs)
- Create: `services/xtts/tests/test_engine.py` (only for pure helpers — ETA computation, checkpoint-dir packaging selection; everything touching TTS/torch stays untested here and is covered by the manual GPU smoke script)
- Create: `services/xtts/scripts/gpu_smoke.py` (manual: tiny fine-tune, 1 epoch, on a fixture WAV + synthesise round-trip; NOT in CI)

**Behaviour:** All `torch`/`TTS`/`trainer` imports lazy inside functions (pattern: `services/vocal-separation/separator.py:29,50`).
- `vram_available_gb()`: `torch.cuda.mem_get_info()[0] / 2**30`; return `0.0` if CUDA unavailable.
- `finetune(...)`: convert via `dataset.manifest_to_coqui_csv`; build `GPTArgs`/`GPTTrainerConfig` from the XTTS-v2 recipe (`recipes/ljspeech/xtts_v2/train_gpt_xtts.py` in the coqui-tts repo) with base model auto-download into the TTS cache (`~/.local/share/tts`); `formatter="coqui"`; epochs/batch/grad_accum/lr from params; progress via a `trainer` callback (or per-step hook) mapping to the progress dict — `eta_secs` = elapsed/steps_done × steps_remaining; after training copy best checkpoint → `output_dir/model.pth`, plus `config.json`, `vocab.json`; compute speaker conditioning latents from up to 5 longest training WAVs via the fine-tuned model's `get_conditioning_latents`, save `torch.save(..., output_dir/speaker_latents.pt)`; return result dict per Task 1 interface with `final_eval_loss` from the trainer's last eval.
- `synthesise(...)`: load base model via TTS API (or fine-tuned: `Xtts.init_from_config` + `load_checkpoint(checkpoint_dir)`); conditioning latents from `reference_wavs` (or reuse `speaker_latents.pt` when present in `checkpoint_dir`); `inference(...)` with temperature; write 22050 Hz mono WAV to `output_path`; return duration from sample count.

- [ ] Pure-helper tests pass; full xtts suite still green (engine imports must not require torch at import time — assert `import engine` works bare)
- [ ] `scripts/gpu_smoke.py` exists, documented usage in a module docstring (run on deploy host later)

### Task 4: Compose + deployment wiring

**Files:**
- Modify: `docker-compose.yml` (new `xtts` block; orchestrator `environment` + `XTTS_URL`)
- Create: `services/xtts/Dockerfile`
- Modify: `.env.example` if it exists (add `XTTS_ACCEPT_CPML=`)

**Content:** Dockerfile = vocal-separation pattern (`python:3.11-slim`, apt ffmpeg + libsndfile1, pip install requirements, `EXPOSE 8005`, `CMD ["python3", "main.py"]`). `requirements.txt`: `fastapi`, `uvicorn`, `coqui-tts` (comment: Idiap-maintained fork; pulls torch + trainer), `soundfile`. Compose block copies `vocal-separation` (docker-compose.yml:24-38): `./data:/data`, cache mount `${MODELS_ROOT:-/mnt/models/flipsync}/xtts:/root/.local/share/tts`, nvidia GPU reservation, `restart: unless-stopped`, `networks: [flipsync]`, plus `profiles: ["xtts"]` and `environment: [XTTS_ACCEPT_CPML=${XTTS_ACCEPT_CPML:-}]`. Match the existing image/build convention used by the other service blocks. Do NOT add xtts to orchestrator `depends_on` (profile-gated). Orchestrator env gains `XTTS_URL=http://xtts:8005`.

- [ ] `docker compose config` validates (without profile); `docker compose --profile xtts config` shows the service

### Task 5: Orchestrator — migration 003

**Files:**
- Create: `services/orchestrator/migrations/009_xtts_models.sql`

**Content:** `CREATE TABLE IF NOT EXISTS models (...)` — exact DDL from `spec/data-models.md` §models; `ALTER TABLE jobs ADD COLUMN progress_detail TEXT;`. Comment header like `002_add_exported_at.sql`.

- [ ] Test in `services/orchestrator/tests/test_projects.py` style: create project → `PRAGMA table_info(jobs)` includes `progress_detail`; `models` table exists. Run orchestrator suite; green.

### Task 6: Orchestrator — service client + readiness

**Files:**
- Modify: `services/orchestrator/service_client.py` (SERVICE_URLS at :20-25; add `"xtts": os.environ.get("XTTS_URL", "http://xtts:8005")`; new `async def is_healthy(service_name: str, timeout_secs: float = 3.0) -> bool` — single GET /health, True iff 200)
- Modify: `services/orchestrator/main.py:31-43` — skip `"xtts"` in the startup health-poll loop (optional profile service; a 300 s poll against an absent container is just log noise)

**Interfaces:** Produces `service_client.is_healthy` consumed by routers (Tasks 9-10) for the 503 `xtts_unavailable` guard.

- [ ] Unit test: `is_healthy` True on 200, False on connect error (patch httpx client). Suite green.

### Task 7: Orchestrator — dataset build extraction + `dataset_build` handler

**Files:**
- Modify: `services/orchestrator/jobs.py` — extract from `_handle_export` (:732-871): `_run_cleanup(project_id, project_row, segments, job_id, on_progress) -> list[dict]` (payload build + `_submit_with_retry("cleanup", ...)` + `poll_until_complete`, from :772-810) and `_apply_cleanup_results(conn, results) -> None` (:817-857). `_handle_export` calls the extracted helpers — behaviour unchanged (existing export tests are the regression net).
- Add: `_select_dataset_segments(conn, mode: str, min_confidence: float | None) -> tuple[list[sqlite3.Row], dict]` — `approved`: `status='approved'`; `auto`: `match_confidence >= ? AND status NOT IN ('rejected','auto_rejected')`; then training filters: drop `duration_secs` outside `[1.0, 11.0]`, drop rows whose `flags` JSON contains a `cleanup_error` entry; returns `(kept, dropped)` where dropped = `{"too_short": n, "too_long": n, "flagged": n}`.
- Add: `async def _handle_dataset_build(project_id, job_id, source_id, params)` — params `{model_id, mode, min_confidence}`. Re-select at run time; if kept duration < 300 s → fail job `insufficient dataset (...)` AND set model row `failed` with error. Clean only segments with `export_path IS NULL` via `_run_cleanup` + `_apply_cleanup_results` (progress 0-80). Re-read kept segments (some may have auto_rejected during cleanup — drop them). Write `projects/{project_id}/models/{model_id}/dataset.json`: export-manifest schema (`_write_manifest` :874-935 is the shape reference) but `audio_file` = ABSOLUTE `export_path`, plus `"selection": {"mode", "min_confidence", "dropped": {...}}`; `"speaker": "target"`. Update model row: `segment_count`, `dataset_duration_secs`, `dataset_manifest_path`. Complete job (progress 100).
- Register in `HANDLERS` (:980-987).

**Interfaces:** Produces the dataset manifest consumed by the xtts service (Task 2 `load_manifest`), and model-row fields consumed by Task 8.

**Tests** (`tests/test_wave3_pipeline.py` mocking pattern, :209-247):
- [ ] approved-mode selection excludes pending/rejected; auto-mode includes high-confidence pending, excludes rejected and below-floor
- [ ] training filters drop <1 s, >11 s, cleanup_error-flagged; dropped counts correct in manifest
- [ ] dataset_build with all segments already cleaned makes NO cleanup service call (patch submit_job, assert not called)
- [ ] dataset_build cleans only missing; manifest has absolute paths + selection block; model row updated
- [ ] under-300 s at run time → job failed, model status `failed`
- [ ] export regression: existing export tests still green after extraction

### Task 8: Orchestrator — `finetune` handler + progress_detail

**Files:**
- Modify: `services/orchestrator/jobs.py` — add `_update_progress_detail(project_id, job_id, detail: dict)` beside `_update_progress` (:190-193) writing `progress_detail` JSON; add `async def _handle_finetune(project_id, job_id, source_id, params)`; register in `HANDLERS`.

**Behaviour:** params `{model_id, params: {epochs, batch_size, grad_accum, learning_rate}}`. Guard: model row must exist with `dataset_manifest_path` set and status `pending` (dataset_build succeeded) — else fail job `dataset build did not complete`. Set model `training`. Read `language` from projects row (NULL → `"en"`). Submit to xtts: `{job_id: <fresh uuid>, type: "finetune", manifest_path, output_dir: <models/{model_id} abs path>, params: {**hyperparams, language, eval_split: 0.1}}` via `_submit_with_retry("xtts", payload)`; `poll_until_complete("xtts", sid, interval_secs=10.0, on_progress=cb)` where cb maps the progress dict → `_update_progress` percent (`int(((epoch-1) + step/max(total_steps,1)) / max(total_epochs,1) * 100)`, clamp 0-99) and `_update_progress_detail` full dict. On failed with `retry_with`: mirror the vocal-sep OOM block (:338-356) — merge `retry_with` into params, FRESH job_id, resubmit once, re-poll; second failure terminal. On terminal failure: model → `failed` + error; fail job. On complete: model → `ready`, `checkpoint_dir`, `eval_loss` from `result.final_eval_loss`, `updated_at`; complete job.

- [ ] happy path: model ready, checkpoint_dir + eval_loss persisted, `jobs.progress_detail` JSON contains epoch/loss keys
- [ ] OOM retry: first poll failed `retry_with={batch_size:1,grad_accum:3}` → second submit payload has merged params + new job_id → success ⇒ model ready
- [ ] second OOM terminal ⇒ model failed, job failed
- [ ] `insufficient_vram` failure (no retry_with) ⇒ no resubmit, model failed
- [ ] dataset_build failed first ⇒ finetune job fails fast, model stays `failed`

### Task 9: Orchestrator — `preview` handler + conditioning

**Files:**
- Modify: `services/orchestrator/jobs.py` — add `_resolve_conditioning(conn, project_row, project_id, source: str | None, segment_count: int) -> tuple[str, list[str]]` returning `(resolved_source, abs_paths)`, raising `LookupError` when unavailable; add `async def _handle_preview(project_id, job_id, source_id, params)`; register in `HANDLERS`.

**Conditioning rules (spec api-contracts §Previews):** `reference_clip` → `projects.reference_path` (single-element list). `segments_raw` → top-`segment_count` by `match_confidence` where `status NOT IN ('rejected','auto_rejected') AND duration_secs BETWEEN 2 AND 12` → `raw_path`. `segments_cleaned` → same query + `export_path IS NOT NULL` → `export_path`. `source=None` → try cleaned, then raw, then reference.

**Handler:** params `{text, model_id, conditioning: {source, segment_count}}`. If `model_id`: read model row; must be `ready` → else fail job. Resolve conditioning; output `projects/{project_id}/previews/{job_id}.wav` (mkdir). Submit synthesise (`checkpoint_dir` from model row or None, `language` from project row or "en"), poll (default 2 s), complete/fail accordingly.

- [ ] conditioning resolution: each source + fallback order + LookupError cases (no reference, no segments)
- [ ] zero-shot preview happy path: synthesise payload has `checkpoint_dir: None`, output under previews/
- [ ] fine-tuned preview: checkpoint_dir from ready model; non-ready model ⇒ job failed

### Task 10: Orchestrator — models router

**Files:**
- Create: `services/orchestrator/routers/models.py` (`APIRouter(prefix="/projects/{project_id}/models")`)
- Modify: `services/orchestrator/main.py:16` (import) and `:93-97` (`app.include_router`)

**Endpoints (contract: spec api-contracts §Models):**
- `POST ""` 202 — body `{dataset?: {mode, min_confidence}, params?: {...}}` (Pydantic, defaults per spec). Guards in order: `require_project`; `await service_client.is_healthy("xtts")` else `AppError(503, "xtts_unavailable", ...)`; any model `pending|training` → `AppError(409, "finetune_in_progress", ...)`; `_select_dataset_segments` duration < 300 → `AppError(409, "insufficient_dataset", ..., {"selected_duration_secs": x, "required_secs": 300})`. Insert model row (uuid, `pending`, mode, min_confidence only for auto, params JSON, timestamps). `enqueue(project_id, "dataset_build", params={model_id, mode, min_confidence})` then `enqueue(project_id, "finetune", params={model_id, params})` (FIFO ⇒ ordering). Return `{"model": {...}, "enqueued_jobs": [{id, type} × 2]}`.
- `GET ""` 200 — `{"models": [...]}` all columns, `params` JSON-parsed, ordered `created_at DESC`.
- `DELETE "/{model_id}"` — 404 `model_not_found`; 409 `model_training` if `pending|training`; `shutil.rmtree(checkpoint dir, ignore_errors=True)` + delete row → 204.

- [ ] POST defaults → mode approved, 202 shape with both jobs; auto mode persists min_confidence
- [ ] 409 insufficient_dataset (detail keys), 409 finetune_in_progress, 503 when is_healthy False (patch it)
- [ ] GET list shape; DELETE happy + 409 while training + 404

### Task 11: Orchestrator — previews router + job-summary progress_detail

**Files:**
- Create: `services/orchestrator/routers/previews.py` (`APIRouter(prefix="/projects/{project_id}/previews")`)
- Modify: `services/orchestrator/main.py` (register)
- Modify: `services/orchestrator/routers/projects.py` `_project_detail` job-summary build + `services/orchestrator/routers/pipeline.py` jobs list: include `progress_detail` (JSON-parsed, None-safe) in job dicts.

**Endpoints (contract: spec api-contracts §Previews):**
- `POST ""` 202 — body `{text: str (1..500), model_id: str|None, conditioning?: {source?: Literal[...], segment_count: int = 5}}`. Guards: `require_project`; text length via Pydantic (422); `is_healthy("xtts")` else 503; `model_id` set and model not `ready` → `AppError(409, "model_not_ready", ...)` (404-as-409 acceptable per spec text); conditioning availability check by calling `_resolve_conditioning` (LookupError → `AppError(409, "conditioning_unavailable", ...)`). `enqueue(project_id, "preview", params={text, model_id, conditioning: {source, segment_count}})`. Return `{"enqueued_job": {"id": job_id, "type": "preview"}}`.
- `GET ""` 200 — jobs `WHERE type='preview' ORDER BY created_at DESC LIMIT ?` (default 20) → `{"previews": [{id, status, text, model_id, conditioning, created_at}]}` (fields from params JSON).
- `GET "/{preview_id}/audio"` — job must exist, type preview, status complete AND file exists else `AppError(404, "preview_not_ready", ...)`; `FileResponse(path, media_type="audio/wav")` (copy the segments audio endpoint pattern in `routers/segments.py`).

- [ ] POST happy (zero-shot) 202; 409 conditioning_unavailable when no reference/segments; 409 model_not_ready; 503 unhealthy; 422 text too long
- [ ] GET list maps params correctly; audio 404 before complete, 200 audio/wav after (write a stub wav + mark job complete directly in DB)
- [ ] project detail + jobs list include progress_detail once set

### Task 12: Frontend — types + API client

**Files:**
- Modify: `frontend/src/types/api.ts` — add `ModelStatus`, `Model`, `CreateModelRequest`, `PreviewConditioning {source?: 'reference_clip'|'segments_raw'|'segments_cleaned'; segment_count?: number}`, `Preview`, `CreatePreviewRequest`; add `progress_detail?: TrainingProgress | null` to `JobSummary` with `TrainingProgress {phase; epoch; total_epochs; step; total_steps; train_loss; eval_loss; eta_secs}`.
- Modify: `frontend/src/api/client.ts` — new sections: `createModel(projectId, body): Promise<{model: Model; enqueued_jobs: EnqueuedJob[]}>`; `getModels(projectId): Promise<{models: Model[]}>`; `deleteModel(projectId, modelId): Promise<void>`; `createPreview(projectId, body): Promise<{enqueued_job: EnqueuedJob}>`; `getPreviews(projectId): Promise<{previews: Preview[]}>`; `getPreviewAudioUrl(projectId, previewId): string` (URL-builder pattern, client.ts:198).

- [ ] `pnpm build` green (types compile, no unused errors)

### Task 13: Frontend — Voice section

**Files:**
- Create: `frontend/src/components/voice/VoiceSection.tsx` (container), `frontend/src/components/voice/TrainPanel.tsx`, `frontend/src/components/voice/ModelsList.tsx`, `frontend/src/components/voice/PreviewPanel.tsx`
- Modify: `frontend/src/pages/ProjectDashboardPage.tsx` — insert `<Section title="Voice"><VoiceSection project={project} refetch={refetch} /></Section>` at :165 (after Upload)
- Modify: `frontend/src/components/project/JobsPanel.tsx:12-19` — `JOB_LABELS` += `dataset_build: 'Dataset build'`, `finetune: 'Fine-tune'`, `preview: 'Preview synthesis'`

**Behaviour (design doc §Frontend; ExportButton.tsx state-machine is the interaction template):**
- `TrainPanel`: shows approved-duration vs target (reuse numbers like `StatsPanel`); Train button → confirm panel: mode radio (Reviewed / "Train without review" auto-mode with confidence number input default 0.85, labelled as trading quality for speed), warning when below 1800 s target, blocked under 300 s; POST `createModel`, surface `ApiError.error` codes (`insufficient_dataset`, `finetune_in_progress`, `xtts_unavailable`) as inline messages; on success call `refetch`.
- Training progress card: find active job `type==='finetune'` in `project.active_jobs`; render `progress_detail` (epoch x/y, step, train/eval loss, ETA via `formatDuration`) + `ProgressBar`; dataset_build job renders its label + bar. Polling is automatic (jobs active ⇒ 3 s poll).
- `ModelsList`: `getModels` on mount + after refetch; rows: status badge, mode (+confidence), duration/segments, eval_loss, created; delete button (confirm; disabled while training).
- `PreviewPanel`: textarea (500 max, counter); conditioning source select (Auto/reference/raw/cleaned); model select (Zero-shot + ready models); Generate → `createPreview`, poll `getPreviews` every 3 s until that id completes (local `setInterval`, cleared on unmount) then fetch blob from `getPreviewAudioUrl` → object URL → `<audio controls>`; keep last generated per column: two-column layout "Zero-shot" / "Fine-tuned" for side-by-side ear comparison; errors inline via `ApiError`.
- Styling: Tailwind, existing conventions (cards `rounded-lg border p-4`, section headers as in dashboard).

- [ ] `pnpm build` green; manual smoke via `pnpm dev` deferred to the live-deploy pass

### Task 14: Full verification

- [ ] All five Python suites green (commands in root `CLAUDE.md` + xtts command above)
- [ ] `pnpm build` green
- [ ] `docker compose config` + `--profile xtts` variant green
- [ ] Verifier pass over the whole diff against spec contracts; fix findings; re-run affected suites
