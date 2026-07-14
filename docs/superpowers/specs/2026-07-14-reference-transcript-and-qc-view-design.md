# Reference transcript + before/after-clean QC view — design

**Date:** 2026-07-14
**Branch base:** `main`
**Status:** approved-pending user review

## Goals

Two independent UI-facing features:

1. **Surface the reference speaker transcription.** The reference clip is never
   transcribed today (no data, no DB column). Auto-transcribe it and display the
   result read-only in the UI. Motivated by the GPT-SoVITS engine, which requires
   a `reference.txt` transcript of `reference.wav`.
2. **Before/after-clean playback of approved audio.** A dedicated page that lets
   you A/B the raw vs cleaned rendering of every segment that will ship
   (the export set), to QC cleanup quality.

Decisions locked with the user:

- Reference transcript: **auto-transcribe on set + manual re-run**, **read-only**.
- QC "after clean" audio: **on-demand cleanup preview** (reuse `tuning_preview`).
- QC scope: **full export set** (`approved` + `auto_approved` + `clipping_warning`).
- QC location: **dedicated page**.

## Feature A — Reference transcript

### Data model

Migration `013_reference_transcript.sql`:

```sql
ALTER TABLE projects ADD COLUMN reference_transcript TEXT;
```

`NULL` = not yet transcribed / no reference. No separate status column — an
in-flight `reference_transcribe` job in `active_jobs` signals "transcribing".

### New job type `reference_transcribe`

Registered in **both** registries (import-time assertion requires parity):

- `job_types.py`: `"reference_transcribe": JobSpec(service="transcription", gpu=True)`
- `jobs.py` `HANDLERS`: `"reference_transcribe": _handle_reference_transcribe`

It is also added to `STATUS_EXEMPT_JOB_TYPES` (`job_types.py`) so a short
reference transcribe does **not** perturb `recompute_project_status` — the
project stays in whatever stage it's in — while still appearing in `active_jobs`
so the UI can show a spinner.

`_handle_reference_transcribe(project_id, job_id, source_id, params)`:

1. Load the project row; if `reference_path` is NULL → fail the job
   (`no_reference`).
2. Build a transcription payload with a single synthetic segment:
   `{id: "reference", wav_path: "{data_prefix}/projects/{id}/reference.wav"}`,
   `batch_size: 1`, plus the project's whisper config
   (`whisper_model`, `language`, `whisper_compute_type`, `whisper_beam_size`,
   `whisper_vad_filter`, `align_words`). Mirrors `_handle_transcription_segment`.
3. `_submit_with_retry("transcription", payload)`; `poll_until_complete`.
4. Read `completed_segments[0]["transcript"]`; `UPDATE projects SET
   reference_transcript=? WHERE id=?`; `_complete_job`.
5. On per-entry `error` or empty result → fail the job with a clear message;
   leave `reference_transcript` unchanged (NULL).

Does not read or write segment rows.

### Triggers

- **Auto:** `_finalise_reference` (fires for both upload and scan-pick) sets
  `reference_transcript = NULL` (clear any stale transcript, since the clip
  changed) and enqueues `reference_transcribe` after status recompute.
- **Manual re-run:** `POST /projects/{id}/reference/transcribe` in
  `routers/reference.py`:
  - 409 `no_reference` if `reference_path` is NULL.
  - 503 `transcription_unavailable` if the transcription service is unhealthy
    (mirrors the tuning-preview health check).
  - else enqueue and return 202 `{enqueued_job: {id, type}}`.

### Surface

- `_project_detail` (`routers/projects.py`) includes `reference_transcript` in the
  project body.
- Frontend `ProjectDetail` type gains `reference_transcript?: string | null`.
- `ReferenceCard` gains a read-only transcript block beneath the audio player:
  - transcript present → render the text (read-only) + a "Re-transcribe" button.
  - a `reference_transcribe` job present in `active_jobs` → "Transcribing…".
  - neither → a "Transcribe" button.
  - Buttons call new client fn `transcribeReference(projectId)`.

## Feature B — before/after-clean QC view

### Backend

**No backend change.** Reuses the existing `tuning_preview` job and endpoints
(`POST/GET /projects/{id}/tuning-preview[/{id}[/audio]]`) unchanged.

### Route + navigation

- New route `/projects/:projectId/qc` in `App.tsx` → `QcPage` (mirrors the
  review-queue route — a full-page project-scoped view).
- A `<Link>` to it from `ProjectDashboardPage` (in the Review area).

### Page behaviour (`QcPage`)

1. Fetch the project (`getProject`) for its saved cleanup config
   (`config.target_lufs`, `highpass_hz`, `silence_threshold_db`,
   `silence_min_duration_secs`).
2. Fetch segments with `status = EXPORTABLE_STATUSES_CSV`
   (`approved,auto_approved,clipping_warning` — already a constant in
   `constants.ts`), paginated.
3. Segment list on the left; selecting one opens a detail pane with a single
   audio player and a **Raw ⇄ Clean toggle**:
   - **Raw** = `getSegmentAudioUrl(projectId, segmentId)` (existing raw endpoint).
   - **Clean** = lazily `createTuningPreview({stage:'cleanup', params:<project
     cleanup config>, target:{segment_id}})` → poll `getTuningPreview` →
     `getTuningPreviewAudioUrl` → blob → object URL. Reuses the create→poll→blob
     pattern from `CompareSettingsModal`'s `ResultPane`.
   - Rendered clean previews are cached per-segment in component state so
     re-selecting a segment is instant (no re-render).

Because the preview uses the project's **saved** cleanup config (no draft
overrides), "after clean" is exactly what export / dataset-build will produce.

## Error handling

- Transcription or cleanup service down → 503 surfaced inline; transcript stays
  NULL; QC clean toggle shows the error.
- Reference replaced → old transcript cleared and re-transcribed.
- Cleanup yields silence (`auto_rejected`) → the `tuning_preview` job fails; QC
  shows "cleanup produced silence" for that segment.
- Empty reference transcript (silence) → stored as returned; UI renders whatever
  came back.

## Testing

**Backend (orchestrator):**

- `_handle_reference_transcribe` against a mocked transcription service (writes
  `reference_transcript`).
- `_finalise_reference` clears the old transcript and enqueues the job.
- `POST /reference/transcribe`: 202 happy path, 409 no reference, 503 service
  down.
- `reference_transcript` present in `GET /projects/{id}`.
- Follows existing `tests/test_rf2_transcription.py` patterns.

**Frontend:**

- `QcPage`: lists the export set, raw/clean toggle triggers a preview and swaps
  the source.
- `ReferenceCard`: the three transcript states render correctly.

## Pre-implementation check

Confirm `tests/test_rf2_transcription.py` and the reference-from-video code do
not already persist a reference transcript before adding the column — the
exploration found none, but verify first.
