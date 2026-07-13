# Pipeline tuning UI + distinct pipeline steps — design

**Date:** 2026-07-13
**Status:** Approved for planning
**Depends on:** migration 011 (`feature/pipeline-tuning-params`, merged) — all knobs already
persisted, validated, wired into service job payloads, and served in `GET /projects` `config`.

## Goal

Expose the migration-011 pipeline tuning knobs in the frontend, and rework the dashboard
flow so the pipeline reads as distinct steps — each with its own status, settings, and
run/re-run affordance. Add artefact listening throughout the stages and an A/B compare
modal for cleanup settings, so tuning is done by ear rather than blind.

## Knob inventory (backend complete, UI missing)

| Stage | Knobs | Bounds (server-validated) |
|---|---|---|
| Separate (Demucs) | `demucs_model`, `demucs_shifts` | `htdemucs`\|`mdx_extra`; 0–10 |
| Match (pyannote) | `diar_min_speakers`, `diar_max_speakers`, `diar_min_segment_duration` | 1–20; 1–20; >0–30 |
| Transcribe (whisper) | `whisper_beam_size`, `whisper_vad_filter` (+ existing `whisper_batch_size`, `whisper_compute_type`) | 1–10; bool |
| Clean (FFmpeg) | `target_lufs`, `highpass_hz`, `silence_threshold_db`, `silence_min_duration_secs` | −70…−5; 0–1000; −90…0; 0–10 |
| Train (XTTS) | `xtts_epochs`, `xtts_batch_size`, `xtts_grad_accum`, `xtts_learning_rate` | 1–200; 1–64; 1–64; >0–1 |
| Preview (XTTS) | `temperature` — **per-run only**, not project config | >0–2, default 0.65 |

All project-config knobs are accepted on `POST /projects` and `PATCH /projects/{id}`;
changes apply on the **next run** of that stage (no retro-apply). XTTS params are also
accepted per-run via `CreateModelRequest.params`.

## 1. Frontend types (`types/api.ts`)

- Add the 14 config fields to `ProjectConfig` and (optional) to `PatchProjectRequest`
  and `CreateProjectRequest`.
- Add `temperature?: number` to `CreatePreviewRequest`.
- Add `DEMUCS_MODELS = ['htdemucs', 'mdx_extra'] as const`.
- Client-side inputs clamp to the server bounds above (matching the existing
  `TranscribeSettingsPanel` clamp style); server 422 remains the backstop.

## 2. Stage machinery rework (`utils/stage.ts`)

`process` splits into three stages. New ordered strips:

```
base: upload → speaker → separate → match → transcribe → review → export
xtts: upload → speaker → separate → match → transcribe → review → train
```

`deriveStage` precedence (unchanged shape, split process branch):

1. no sources → `upload`
2. no `reference_path` → `speaker`
3. any source in `{uploaded, extracting, separation_pending, separation_running,
   separation_failed, extraction_failed}`, or an extract/separation job active → `separate`
4. any source in `{diarisation_pending, diarisation_running, diarisation_failed}`,
   or a diarisation job active → `match`
5. a transcription job active, or a transcription job failed with untranscribed
   segments owed → `transcribe`
6. existing logic unchanged: pending+maybe > 0 → `review`; all-below-threshold →
   `review`; else `train`/`export`.

`NON_PIPELINE_JOB_TYPES` and `stageStates` keep their current shape. Cleanup is **not**
a strip chip (it runs inside export/dataset-build).

**Strip navigation:** the three pipeline chips scroll/open to their step row in the
Process section (same pattern as the existing Train chip → Voice section).
`NextActionCard` gains `separate`/`match`/`transcribe` variants replacing the single
`process` variant — same content (job progress / start / continue buttons), headed by
the specific stage.

## 3. Process section stepper

The Process section becomes: sources table (unchanged, per-source kebab keeps
per-source reprocess) + four step rows:

```
① Separate vocals   {state}   [▶ vocals]  [Settings ▸] [Re-run]
② Match speaker     {state}               [Settings ▸] [Re-run]
③ Transcribe        {state}               [Settings ▸] [Run]
④ Clean & package   runs during export    [Settings ▸] [Compare…]
```

Each row:

- **State chip** derived from source statuses / active jobs: *Not run yet / Queued /
  Running (with progress) / Done (with counts) / Failed*.
- **Settings** — collapsed inline disclosure holding that stage's knobs, prefilled from
  `project.config`, own dirty-tracking + Save/Reset, PATCHes only its subset (existing
  panel pattern). If the step already ran, the saved message reads
  *“Saved — applies when this step re-runs”* with the Re-run button adjacent.
- **Run/Re-run:**
  - ① ② — reprocess **all sources** for that step via the existing reprocess endpoint;
    reuses the existing `would_invalidate_approvals` confirm dialog.
  - ③ — `runTranscription` (bulk re-run; also retriggerable from Review as today).
  - ④ — no run button; the row states its knobs apply at export/dataset build.
- **Listen:** ① gets a vocals-stem player per source (see §6). ② and ③ point at the
  review queue where segment audio already plays.

Row ③ **absorbs `TranscribeSettingsPanel`** (batch/precision join beam/VAD in one
disclosure). The standalone panel and its “Transcribe” subheading are removed.

## 4. Review, Voice, Create

- **Review section:** keeps only the review-flow knobs (`ProjectSettingsPanel` —
  match threshold + auto-approve). No pipeline knobs here.
- **TrainPanel:** the confirm dialog gains an *Advanced* disclosure —
  epochs / batch size / grad accum / learning rate, prefilled from `config.xtts_*`,
  sent as `CreateModelRequest.params` **only when changed from the config values**.
  Per-run only; does not PATCH the project.
- **PreviewPanel:** a temperature slider (0.05–2, step 0.05, default 0.65) in the shared
  header next to Conditioning; passed on every `createPreview`. Applies to both columns
  so A/B compares models, not sampling noise.
- **CreateProjectModal:** collapsed *Advanced* disclosure exposing all 14 knobs
  (grouped by stage), sent on `POST /projects`. Defaults prefilled; untouched fields
  may be omitted from the request (server defaults match).

## 5. Cleanup A/B compare modal

**Frontend** — reusable `CompareSettingsModal`, opened from row ④ *Compare…*:

- Target picker: any segment (searchable by the segment list API; defaults to the
  first approved/pending segment).
- Two param columns: **A** prefilled from saved config, **B** a draft copy; both editable.
- *Run comparison* → submits a tuning preview per column → renders an audio player
  under each when ready (near-instant: CPU FFmpeg on one segment).
- Per-column *Save these settings* → PATCHes the project with that column's values.
- Re-run freely after tweaking either column or switching segment.
- Poll/timeout handling copies the `PreviewPanel` pattern (3 s poll, bounded lifetime).

**Backend (orchestrator only — no service changes):**

- `POST /projects/{project_id}/tuning-preview`
  Body: `{"stage": "cleanup", "params": {target_lufs, highpass_hz, silence_threshold_db,
  silence_min_duration_secs}, "target": {"segment_id": "..."}}`
  → 202 `{enqueued_job: {...}, preview_id}`. **Stage-generic by design**: `stage` is an
  enum with `cleanup` as the only accepted value in this feature; `separation` is the
  planned follow-on. Unknown stage → 422. Params validated with the same bounds as
  the project fields.
- New `tuning_preview` job type: calls the cleanup service with the draft params for
  the single segment WAV (per-job params already supported), output to
  `projects/{project_id}/tuning_previews/{preview_id}.wav`. CPU job — not GPU-gated.
  Excluded from project-status recomputation (same treatment as voice jobs) and from
  pipeline stage derivation (`NON_PIPELINE_JOB_TYPES`).
- `GET /projects/{project_id}/tuning-preview/{preview_id}` → job status;
  `GET .../tuning-preview/{preview_id}/audio` → `FileResponse` WAV.
- **Scratch lifecycle:** `tuning_previews/` is ephemeral — best-effort sweep of files
  older than 24 h on project open/job submit; the directory is excluded from export
  and dataset build; results never touch segment tables.

## 6. Vocals stem endpoint

`GET /projects/{project_id}/sources/{source_id}/vocals` → `FileResponse` of
`vocals_path` (404 `audio_not_found` before separation completes). Full-file download
like segment audio — stems can be large, but this is a deliberate listen action.
Frontend: player behind a *▶ vocals* button per source on step row ① (fetch-on-click,
object URL, same error handling as segment audio).

## 7. Testing

- **stage.test.ts:** table tests for the split stages (each source status / job type →
  expected stage), strip composition for base/xtts.
- **Step rows:** component tests per row — state chip derivation, settings render config,
  dirty → PATCH called with only that row's subset, re-run wiring (reprocess params /
  `runTranscription`), invalidation confirm passthrough.
- **TrainPanel:** advanced fields prefill from config; `params` present in the request
  body only when changed.
- **PreviewPanel:** `temperature` in every create body; default 0.65.
- **CreateProjectModal:** advanced values land in the POST body; untouched → omitted.
- **CompareSettingsModal:** two submissions with respective params; save PATCHes the
  chosen column; poll/timeout behaviour.
- **Orchestrator:** tuning-preview endpoint validation (bounds, unknown stage, missing
  segment), job handler calls cleanup service with draft params and writes to the
  scratch path, audio serving, TTL sweep, status/stage exclusion. Vocals endpoint
  (200 after separation, 404 before).

## Out of scope (explicit)

- **Separation A/B** — follow-on. Requires window-processing support
  (`window_start_secs`/`window_secs`) in the vocal-separation service; the
  `tuning-preview` contract above already accommodates it.
- **Transcribe A/B** — dropped deliberately: single-segment comparisons can't judge
  statistical WER effects; scattered errors are cheaper to fix via the review queue's
  transcript editing; systematic failures are handled by flipping beam/VAD (now exposed)
  and re-running bulk/per-segment transcription (already built).
- **Match A/B** — no meaningful audible preview; params change segmentation structure.
- Any retro-apply of config changes to already-processed work.

## Spec updates on completion

Fold into `spec/review-ui.md` (dashboard stepper, strip stages, compare modal) and
`spec/api-contracts.md` (tuning-preview + vocals endpoints) when implementation lands,
per repo convention.
