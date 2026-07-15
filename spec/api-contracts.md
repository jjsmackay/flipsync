# API Contracts

**Status:** DRAFT  
**Last updated:** 2026-07-13

---

## Conventions

All APIs are JSON over HTTP. All timestamps are ISO 8601 UTC strings. All IDs are UUIDs unless noted.

**Error response format** (all endpoints, all services):

```json
{
  "error": "short_snake_case_code",
  "message": "Human-readable description.",
  "detail": {}
}
```

`detail` is optional and service-specific. HTTP status codes follow standard semantics: 400 bad request, 404 not found, 409 conflict (invalid state transition), 422 validation error, 500 internal error.

**Internal service calls** are made by the orchestrator only. Services do not call each other. The browser never calls a processing service directly.

**CORS.** The orchestrator must enable CORS middleware for the frontend origin (`http://localhost:3000` in development). Allow all methods and headers from this origin.

**File uploads.** Source video files can be 1–4 GB. The orchestrator must configure Starlette/FastAPI to accept large multipart uploads — stream to disk rather than buffering in memory. Set `max_upload_size` to at least 10 GB or disable the limit entirely (the filesystem is the constraint, not the app).

---

## Part 1 — Browser → Orchestrator

Base URL: `http://localhost:8000`

---

### Capabilities

#### `GET /capabilities`

Deployment-level feature flags and server-owned tables for the frontend. Fetched once per dashboard load (not on the project poll).

```json
{
  "xtts": true,
  "voice_training": true,
  "engines": [
    { "id": "xtts",       "name": "XTTS-v2",    "healthy": true, "languages": ["en", "..."] },
    { "id": "gpt_sovits", "name": "GPT-SoVITS", "healthy": true, "languages": ["en", "zh", "ja", "ko", "yue"] }
  ],
  "bulk_action_sources": {
    "approve": ["auto_approved", "clipping_warning", "maybe", "pending"],
    "reject": ["approved", "auto_approved", "clipping_warning", "maybe", "pending"],
    "maybe": ["approved", "auto_approved", "pending"],
    "pending": ["auto_approved", "maybe", "rejected"]
  }
}
```

`engines` lists every voice fine-tune engine the deployment knows about, with a point-in-time health probe per engine (`healthy` is `true` iff that profile-gated service responds `200` to `/health`, probed concurrently). The frontend builds the Train-stage engine picker from this array (picker shown only when more than one engine is healthy).

`voice_training` is derived: `true` iff **any** engine is healthy. The dashboard uses it to decide whether the terminal stage is **Train** (present) or **Export** (absent) — this replaces `xtts` for that decision, so a GPT-SoVITS-only deployment still offers the Train stage.

`xtts` is the original single-engine health flag, retained for backward compatibility; it always equals the `healthy` value of the `xtts` entry in `engines`. Always `200`.

`bulk_action_sources` serves the orchestrator's bulk transition table (the statuses each bulk action may move FROM — the same table `POST /segments/bulk` enforces). The frontend uses it for live bulk-preview counts so they cannot drift from what Apply affects; it keeps a baked-in copy only as a fallback for older orchestrators.

---

### Projects

#### `GET /projects`

List all projects.

**Response 200:**
```json
{
  "projects": [
    {
      "id": "550e8400-...",
      "name": "Downton Abbey — Carson",
      "status": "review",
      "created_at": "2026-04-01T10:00:00Z",
      "updated_at": "2026-04-03T14:32:00Z",
      "stats": {
        "approved_count": 743,
        "approved_duration_secs": 4821.3,
        "pending_count": 891
      }
    }
  ]
}
```

---

#### `POST /projects`

Create a new project.

**Request:**
```json
{
  "name": "Downton Abbey — Carson",
  "whisper_model": "large-v2",
  "language": null,
  "match_threshold": 0.75,
  "target_duration_secs": 1800
}
```

**Response 201:**
```json
{
  "id": "550e8400-...",
  "name": "Downton Abbey — Carson",
  "status": "new"
}
```

---

#### `GET /projects/{project_id}`

Full project state, including per-source status and summary stats.

**Response 200:**
```json
{
  "id": "550e8400-...",
  "name": "Downton Abbey — Carson",
  "status": "review",
  "reference_path": "reference.wav",
  "reference_origin": { "type": "diarise_pick", "source_id": "...", "speaker_label": "SPEAKER_02" },
  "config": {
    "whisper_model": "large-v2",
    "language": null,
    "match_threshold": 0.75,
    "target_duration_secs": 1800,
    "auto_approve_enabled": true,
    "auto_approve_match_threshold": 0.85,
    "auto_approve_transcript_threshold": 0.90
  },
  "stats": {
    "total_segments": 1842,
    "approved_count": 743,
    "auto_approved_count": 402,
    "approved_duration_secs": 4821.3,
    "pending_count": 489,
    "maybe_count": 47,
    "rejected_count": 161,
    "below_threshold_count": 312,
    "source_coverage": [
      {
        "source_id": "...",
        "filename": "s01e01.mkv",
        "status": "complete",
        "coverage_ratio": 0.21,
        "low_coverage_warning": false,
        "error": null
      }
    ]
  },
  "active_jobs": [
    {
      "id": "...",
      "type": "transcription_bulk",
      "status": "running",
      "progress": 62
    }
  ],
  "recent_failed_jobs": [
    {
      "id": "...",
      "type": "vocal_separation",
      "source_id": "...",
      "error": "cuda_oom",
      "completed_at": "2026-04-03T14:05:00Z"
    }
  ]
}
```

`reference_path` and `reference_origin` are both `null` until a reference is set by either producer (upload or diarise + pick).

---

#### `PATCH /projects/{project_id}`

Update project config. Patchable: `name`, `match_threshold`, `target_duration_secs`, `whisper_model`, `language`, `auto_approve_enabled`, `auto_approve_match_threshold`, `auto_approve_transcript_threshold`, `whisper_batch_size` (1–64), `whisper_compute_type` (`default` | `float16` | `int8_float16` | `int8`), plus the pipeline tuning knobs: `demucs_model` (`htdemucs` | `mdx_extra`), `demucs_shifts` (0–10), `diar_min_speakers`/`diar_max_speakers` (1–20), `diar_min_segment_duration` (>0, ≤30), `whisper_beam_size` (1–10), `whisper_vad_filter` (bool), `target_lufs` (−70…−5), `highpass_hz` (0–1000), `do_trim_silence` (bool), `silence_threshold_db` (−90…0), `silence_min_duration_secs` (0–10), `silence_pad_start_secs` (0–2), `silence_pad_end_secs` (0–2), and the fine-tune hyperparameters `xtts_epochs` (1–200), `xtts_batch_size` (1–64), `xtts_grad_accum` (1–64), `xtts_learning_rate` (>0, ≤1). All tuning knobs are accepted on `POST /projects` too, with the same bounds. Out-of-range values return 422. Changing a tuning knob applies to the next run of that stage; it does not reprocess existing work. Changing `match_threshold` or any auto-approve field triggers a synchronous re-evaluation of segment statuses by the orchestrator (not a queued job), applied in this order:

1. **Auto-approve demotion:** segments with status `auto_approved` that no longer meet the auto-approve eligibility rule (see [Pipeline](pipeline.md) §Auto-approval) are moved to `pending`.
2. **Auto-approve promotion:** segments with status `pending` that meet the eligibility rule are moved to `auto_approved`.
3. **Display threshold swap:**
   - Segments with `match_confidence >= new_threshold` and status `below_threshold` are moved to `pending`.
   - Segments with `match_confidence < new_threshold` and status `pending` are moved to `below_threshold`.

Segments in any other status (`approved`, `rejected`, `maybe`, `clipping_warning`, `auto_rejected`) are not affected by re-evaluation — user decisions are preserved. Auto-approve eligibility requires `match_confidence >= max(match_threshold, auto_approve_match_threshold)`, so step 1 also catches raises of the display threshold.

**Request:**
```json
{
  "match_threshold": 0.70,
  "auto_approve_transcript_threshold": 0.92
}
```

**Response 200:** Updated project object (same shape as GET).

---

#### `DELETE /projects/{project_id}`

Delete a project and all its data.

**Request:**
```json
{
  "confirm": true
}
```

`confirm` is required. Without it, returns **422**. Deletes the project's working directory (all source files, audio, segments, export) and its SQLite database. If the project is in the index, removes that entry too. This is irreversible.

**Response 200:**
```json
{
  "deleted": true
}
```

**Response 409** if any jobs are currently running:
```json
{
  "error": "jobs_active",
  "message": "Cannot delete project while jobs are running. Cancel or wait for completion."
}
```

---

### Sources

#### `POST /projects/{project_id}/sources`

Upload a source video file. Multipart form data. The file is streamed to disk at `source/{id}.{ext}`.

**Request:** `multipart/form-data`, field `file`. Files up to 10 GB are accepted.

**Response 202:**
```json
{
  "id": "...",
  "filename": "s01e01.mkv",
  "status": "extracting"
}
```

The orchestrator enqueues an `extract_audio` job immediately after writing the file to disk. Extraction runs FFmpeg as a subprocess within the orchestrator process (not a separate service). The client polls `GET /projects/{project_id}` for status updates. On success the source moves to `separation_pending`; on failure it moves to `extraction_failed`.

---

#### `DELETE /projects/{project_id}/sources/{source_id}`

Remove a source file and all its segments from the project. Requires confirmation if the source has approved segments.

**Request:**
```json
{
  "confirm": true
}
```

**Response 200:**
```json
{
  "deleted_segment_count": 47,
  "deleted_approved_count": 12
}
```

#### `GET /projects/{project_id}/sources/{source_id}/vocals`

Stream the source's separated vocals stem as `audio/wav` — the full file, no Range support (a deliberate listen action; stems can be large). Available once vocal separation has completed for the source.

**Errors:** 404 `not_found` (unknown source), 404 `audio_not_found` (separation hasn't produced a stem yet, or the WAV is missing on disk).

---

### Reference clip

The reference is a single artifact — `reference.wav` plus a provenance record (`reference_origin`) — with two producers: **upload** (below) and **diarise + pick** (a reference-less scout pass over a source, then select a detected speaker). Both converge on the same `reference_path`; everything downstream (step 2 matching) is unchanged regardless of which producer was used.

#### `POST /projects/{project_id}/reference`

Upload or replace the reference clip. Replaces any existing reference. Does not automatically re-run diarisation. Sets `reference_origin` to `{"type": "uploaded"}`.

**Request:** `multipart/form-data`, field `file`. Must be an audio file, minimum 5 seconds.

**Response 200:**
```json
{
  "reference_path": "reference.wav",
  "duration_secs": 18.4
}
```

**Response 422** if clip is under 5 seconds:
```json
{
  "error": "reference_too_short",
  "message": "Reference clip must be at least 5 seconds. Uploaded clip is 3.1 seconds.",
  "detail": { "duration_secs": 3.1, "minimum_secs": 5.0 }
}
```

---

#### `GET /projects/{project_id}/reference/audio`

Stream the project's current reference clip (`reference.wav`) as `audio/wav` — the full file, no `Range` support, matching the segment-audio streaming convention (`GET /segments/{segment_id}/audio`).

**Response 200:** `audio/wav`, `Content-Length` header set.

**Response 404 `no_reference`** if the project has no reference set (`reference_path` is null).

**Response 404 `audio_not_found`** if `reference_path` is set but the WAV is missing on disk.

---

#### `POST /projects/{project_id}/reference/scout`

Enqueue a reference-less diarisation pass ("scout") over one source, to surface its speakers as reference candidates. Available once the source has a vocals stem (`vocals_path` set, i.e. step 1 complete).

**Request:**
```json
{ "source_id": "…", "expected_speaker_count": 2 }
```

`expected_speaker_count` is optional. When provided, it is forwarded to the diarisation service as `num_speakers`, forcing pyannote to that exact speaker count (the fix for a cluster that has merged two people). Omit it to use the default 1–10 range.

**Response 202:**
```json
{ "job_id": "…", "type": "scout_speakers" }
```

**Response 422 `vocals_not_ready`** if the source has no vocals stem (step 1 has not completed for it).

**Response 422 `invalid_speaker_count`** if `expected_speaker_count` is provided and less than 1.

**Response 404** if `source_id` does not exist.

---

#### `GET /projects/{project_id}/reference/scout`

Return the status of the latest scout job for the project and, once complete, its speaker candidates (read from `speaker_candidates`). The frontend polls this.

**Response 200 (running):**
```json
{ "status": "running", "progress": 40, "source_id": "…", "speakers": [] }
```

**Response 200 (complete):**
```json
{
  "status": "complete",
  "source_id": "…",
  "speakers": [
    { "speaker_label": "SPEAKER_00", "total_secs": 412.6, "segment_count": 173,
      "pool": [
        { "index": 0, "start": 63.2, "end": 88.0, "duration": 24.8,
          "sample_url": "/projects/{id}/reference/scout/samples/SPEAKER_00/0" },
        { "index": 1, "start": 12.0, "end": 30.5, "duration": 18.5,
          "sample_url": "/projects/{id}/reference/scout/samples/SPEAKER_00/1" }
      ] },
    { "speaker_label": "SPEAKER_01", "total_secs": 88.2, "segment_count": 41,
      "pool": [
        { "index": 0, "start": 5.0, "end": 11.0, "duration": 6.0,
          "sample_url": "/projects/{id}/reference/scout/samples/SPEAKER_01/0" }
      ] }
  ]
}
```

`speakers` is sorted by `total_secs` descending — the target speaker is usually the most talkative. Each candidate's `pool` is its bounded curation set (longest-first); the reference is assembled from these turns minus any the user excludes on select.

`sample_url` is orchestrator-relative (like `audio_url` on segments). Clients resolve it against their API base — the frontend prefixes its same-origin `/api` proxy path.

**Response 200 (failed):** if the latest scout failed (or was cancelled), the response still carries any candidates from an earlier successful scan, so the UI can report the failure while keeping the previous speakers pickable:
```json
{ "status": "failed", "error": "…", "source_id": "…", "speakers": [ /* prior candidates, [] if none */ ] }
```

**Response 404 `no_scout`** if no scout has been run for the project.

---

#### `GET /projects/{project_id}/reference/scout/samples/{speaker_label}/{index}`

Stream one pool turn WAV for a candidate speaker so the browser can play it. Resolves to `reference_candidates/{scout_job_id}/{speaker_label}/{index}.wav` for the latest scout. Full file, no `Range` header support, matching the segment-audio streaming convention (`GET /segments/{segment_id}/audio`).

**Response 200:** `audio/wav`, `Content-Length` header set.

**Response 404 `unknown_speaker`** if the label is not in the current candidate set.

**Response 404 `unknown_segment`** if `index` is not a turn in that candidate's pool.

---

#### `GET /projects/{project_id}/reference/scout/preview/{speaker_label}`

Stream the assembled-reference montage a candidate speaker would produce, so the browser can audition it with one control — no expanding to individual pool turns. The orchestrator assembles it in memory from the candidate's pool turns minus any in the repeatable `exclude` query parameter (e.g. `?exclude=0&exclude=3`) — longest-first up to the 30 s cap, concatenated via the stdlib `wave` module — using the **same** assembly as `scout/select`, so the preview matches the eventual reference exactly. Nothing is written to disk and the reference is not touched. Full file, no `Range` support.

**Response 200:** `audio/wav`, `Content-Length` header set.

**Response 404 `unknown_speaker`** if the label is not in the current candidate set.

**Response 422 `reference_too_short`** if every pool turn is excluded (nothing left to assemble).

---

#### `POST /projects/{project_id}/reference/scout/select`

Adopt a candidate speaker as the reference. The orchestrator assembles `reference.wav` from the candidate's pool turns minus any in `excluded_indices` — longest-first up to a 30 s cap, concatenated via the stdlib `wave` module — sets `reference_path`, and sets `reference_origin` to `{"type": "diarise_pick", "source_id": "…", "speaker_label": "…", "excluded_indices": […], "included_indices": […]}`. Does **not** auto-run step 2 — mirrors the upload endpoint's "does not automatically re-run diarisation" behaviour.

**Request:**
```json
{ "speaker_label": "SPEAKER_02", "excluded_indices": [1] }
```

`excluded_indices` is optional (default `[]`). An empty list assembles the full montage (longest-first up to the cap) — identical to the previous behaviour. Listing indices leaves those wrong-voice turns out; the next-longest kept turns backfill the reference toward the cap.

**Response 200:**
```json
{
  "reference_path": "reference.wav",
  "duration_secs": 27.9
}
```

**Response 404 `unknown_speaker`** if the label is not in the current candidate set.

**Response 422 `reference_too_short`** if the assembled reference is under the 5-second minimum — either because too many turns were excluded (none left) or the kept turns total under the floor (same floor the upload endpoint enforces). The UI should also disable **Use this voice** while the live reference length is under 5 s:
```json
{
  "error": "reference_too_short",
  "message": "Candidate clip must be at least 5 seconds. SPEAKER_02 has 3.1 seconds of talk time.",
  "detail": { "duration_secs": 3.1, "minimum_secs": 5.0 }
}
```

---

### Pipeline control

#### `POST /projects/{project_id}/pipeline/start`

Start the pipeline for all sources in `separation_pending` status.

- **If `reference_path` is set:** unchanged from before — step 1 → step 2 is chained per source, exactly as today.
- **If `reference_path` is null:** enqueues step 1 (`vocal_separation`) only, for all `separation_pending` sources. Step 2 is not chained. The project reaches `awaiting_reference` once those jobs drain. The user sets a reference (upload or diarise + pick) and calls `pipeline/continue` to proceed.

**Response 202:** (shape unchanged regardless of branch)
```json
{
  "enqueued_jobs": [
    { "id": "...", "type": "vocal_separation", "source_id": "..." },
    { "id": "...", "type": "vocal_separation", "source_id": "..." }
  ]
}
```

---

#### `POST /projects/{project_id}/pipeline/continue`

Enqueue step 2 (`diarisation`, match mode) for every source at `diarisation_pending`. This is how a project leaves `awaiting_reference` once a reference has been set.

**Response 202:**
```json
{
  "enqueued_jobs": [
    { "id": "...", "type": "diarisation", "source_id": "..." }
  ]
}
```

**Response 409 `no_reference`** if `reference_path` is still null — the gate has not been satisfied.

**Response 409 `no_pending_sources`** if no source is at `diarisation_pending`.

---

#### `POST /projects/{project_id}/sources/{source_id}/reprocess`

Re-run one or more pipeline steps for a single source.

**Request:**
```json
{
  "steps": ["separation"],
  "params": {
    "demucs_model": "mdx_extra"
  }
}
```

`steps` must be `["separation"]`, `["diarisation"]`, or `["separation", "diarisation"]`. Cannot re-run step 3 (transcription) via this endpoint — use the transcription endpoints below.

**Response 409** if the source has approved segments that would be invalidated:
```json
{
  "error": "would_invalidate_approvals",
  "message": "Re-running step 2 will discard 23 approved segments from this source.",
  "detail": { "approved_count": 23 }
}
```

Client must re-send with `"confirm": true` to proceed.

**Response 202:** Enqueued job list (same shape as pipeline start).

---

### Transcription

#### `POST /projects/{project_id}/transcription/run`

Transcribe all pending/maybe segments that have not yet been transcribed. Untranscribed `pending` segments are submitted with `resegment: true` and may be replaced by sentence-aligned children (see [Pipeline](pipeline.md) §Sentence-aligned re-segmentation); `maybe` segments are transcribed without re-segmentation.

**Response 202:**
```json
{
  "enqueued_job": { "id": "...", "type": "transcription_bulk", "segment_count": 1089 }
}
```

---

#### `POST /projects/{project_id}/segments/{segment_id}/transcription/rerun`

Re-transcribe a single segment. Overwrites `transcript` and `transcript_confidence`. Preserves `transcript_edited`.

**Response 202:**
```json
{
  "enqueued_job": { "id": "...", "type": "transcription_segment" }
}
```

---

### Segments

#### `GET /projects/{project_id}/segments`

Paginated segment list with filtering and sorting.

**Query parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `status` | string or comma-list | `pending,maybe` | Filter by status |
| `source_id` | UUID | — | Filter to one source |
| `q` | string | — | Case-insensitive substring match against the effective transcript (`transcript_edited` if set, else `transcript`). `%` and `_` are literal characters, not `LIKE` wildcards. |
| `min_confidence` | float | — | Filter by match confidence |
| `max_confidence` | float | — | Filter by match confidence |
| `min_duration` | float | — | Filter by duration in seconds |
| `max_duration` | float | — | Filter by duration in seconds |
| `sort` | string | `match_confidence` | `match_confidence`, `duration`, `start_secs`, `transcript_confidence`, `uncertainty` |
| `order` | string | `desc` (`asc` for `uncertainty`) | `asc` or `desc` |
| `page` | int | 1 | 1-based |
| `per_page` | int | 50 | Max 200 |
| `count_only` | bool | false | Return total count only, no segment data; used by bulk action preview |

`status` accepts any segment status, including `auto_approved`. `sort=uncertainty` orders by `ABS(match_confidence - match_threshold)` — with the default `asc` order, the most borderline segments come first, which is the recommended review order once auto-approval has skimmed the confident top band.

When `count_only=true`, returns immediately without fetching segment data:

```json
{ "total": 412 }
```

**Response 200** (standard):
```json
{
  "segments": [
    {
      "id": "7f3c2a1b-...",
      "source_id": "...",
      "source_filename": "s01e01.mkv",
      "start_secs": 142.31,
      "end_secs": 146.88,
      "duration_secs": 4.57,
      "match_confidence": 0.91,
      "speaker_match_confidence": 0.88,
      "transcript": "Well, I'm sure I don't know what you mean.",
      "transcript_edited": null,
      "transcript_confidence": 0.88,
      "status": "pending",
      "clipping_warning": false,
      "audio_url": "/projects/{project_id}/segments/{segment_id}/audio"
    }
  ],
  "pagination": {
    "page": 1,
    "per_page": 50,
    "total": 891,
    "pages": 18
  }
}
```

`speaker_match_confidence` is the cluster-level score persisted from diarisation (see the Diarisation Service response below) — `null` on segments diarised before migration 006. The single-segment representation returned by `PATCH /segments` includes it too.

---

#### `GET /projects/{project_id}/segments/{segment_id}/audio`

Stream the raw segment WAV file. Returns the full file in a single response — no `Range` header support in v1. For a typical 5-second segment at 44.1 kHz stereo, this is ~1.7 MB.

**Response 200:** `audio/wav`, `Content-Length` header set.  
**Response 404:** Segment not found or WAV not yet written.

---

#### `PATCH /projects/{project_id}/segments/{segment_id}`

Update a segment's review state or transcript.

Patchable fields: `status`, `transcript_edited`.

`status` may be set to any value the transition rules allow from the segment's current status — except `auto_approved`, which only the system assigns. A request to set `auto_approved` returns **409** `invalid_transition` regardless of current status.

**Request:**
```json
{
  "status": "approved"
}
```

**Response 409** for invalid status transitions:
```json
{
  "error": "invalid_transition",
  "message": "Cannot transition from 'rejected' to 'approved'.",
  "detail": { "from": "rejected", "to": "approved" }
}
```

**Response 200:** Updated segment object.

---

#### `POST /projects/{project_id}/segments/{segment_id}/boundaries`

Re-cut a segment's audio with new time boundaries (the review panel's trim/extend control). The raw slice is re-cut from the source's retained separated-vocals WAV, so extending a boundary recovers real neighbouring audio (a word the diariser clipped) and trimming drops bleed from an adjacent speaker.

**Request** (both fields optional; absolute seconds — omit an edge to leave it unchanged; at least one required):
```json
{
  "start_secs": 1.5,
  "end_secs": 6.0
}
```

Boundaries are clamped to `[0, vocals_duration]`. The re-cut segment must be at least 0.1 s long. Side effects: `duration_secs` recomputes; the cleaned cache (`cleaned_path` + file) is cleared so the next dataset build re-cleans; any prior export is invalidated; a `boundary_edited` flag is added (the transcript is left as-is — re-transcribe if the words changed).

**Response 200:** Updated segment object.
**Response 409** `vocals_unavailable` if the source's separated audio is missing. **422** `no_change` (no fields) / `invalid_boundaries` (too short). **404** if the segment does not exist.

---

#### `POST /projects/{project_id}/segments/stitch`

Concatenate 2+ segments into a single clip and replace them with one stitched segment. The segments' raw WAVs are joined in the order given (cross-source allowed; each is normalised so differing rates/channels join cleanly). Consecutive clips are joined with a short equal-power crossfade (~10 ms) so a boundary landing mid-waveform doesn't click. Use when diarisation split one utterance, or to assemble specific lines into one clip.

**Request** (`segment_ids` length ≥ 2, distinct, ordered):
```json
{ "segment_ids": ["id-a", "id-b"] }
```

The merged segment: `status: "pending"` with a `stitched` flag; transcript = the inputs' effective transcripts space-joined in order; `match_confidence` = the minimum of the inputs; synthetic `start_secs`/`end_secs` (first segment's start + total duration, since the audio is no longer a contiguous slice of one source). The originals' rows and WAVs are removed and any prior export is invalidated.

**Response 200:** The new merged segment object.
**Response 404** if any `segment_id` is missing. **422** `duplicate_segment` (ids not distinct) or fewer than 2 ids. **409** `audio_unavailable` if a segment's raw WAV is missing on disk.

---

#### `POST /projects/{project_id}/segments/bulk`

Apply an action to multiple segments at once.

**Request:**
```json
{
  "action": "approve",
  "filter": {
    "status": "pending",
    "min_confidence": 0.90,
    "min_duration": 2.0
  }
}
```

`action` must be `approve`, `reject`, `maybe`, or `pending`. Each action respects the segment status transition rules — it only affects segments whose current status allows that transition. For example, `approve` only affects segments in `pending`, `maybe`, `auto_approved`, or `clipping_warning` status; `pending` only affects segments in `maybe`, `auto_approved`, or `rejected` status (the `below_threshold` → `pending` transition is handled by the threshold re-evaluation in `PATCH /projects`, not by bulk actions). Segments in ineligible statuses are silently skipped. The filter uses the same parameters as `GET /segments`. A bulk `approve` with `filter: {"status": "auto_approved"}` is the "confirm all auto-approved" operation.

**Response 200:**
```json
{
  "affected_count": 284
}
```

---

### Export

#### `POST /projects/{project_id}/export`

Trigger export. Runs cleanup on all approved segments, then writes `manifest.json` and packages the archive.

**Response 409** if no approved segments:
```json
{
  "error": "no_approved_segments",
  "message": "There are no approved segments to export."
}
```

**Response 202:**
```json
{
  "enqueued_job": { "id": "...", "type": "export", "segment_count": 743 }
}
```

---

#### `GET /projects/{project_id}/export/download`

Download the export archive as a `.tar.gz` file.

**Response 404** if export has not completed.  
**Response 200:** `application/gzip` stream, filename `{project_name}_export.tar.gz`.

---

### Models (v1.5)

#### `POST /projects/{project_id}/models`

Trigger a voice fine-tune. Runs a dataset build, then submits a `finetune` job to the selected engine's service.

**Request (all fields optional; defaults shown):**
```json
{
  "engine": "xtts",
  "dataset": { "mode": "approved", "min_confidence": null },
  "params": { "epochs": 10, "batch_size": 3, "grad_accum": 1, "learning_rate": 5e-6 }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `engine` | string | `xtts` | Fine-tune engine: `xtts` \| `gpt_sovits`. Persisted on the model row and echoed in listings |
| `dataset.mode` | string | `approved` | `approved`: segments with status `approved`. `auto`: segments with `match_confidence >= min_confidence` and status not `rejected`/`auto_rejected` (review not required) |
| `dataset.min_confidence` | float | 0.85 | `auto` mode only: match-confidence floor. Ignored in `approved` mode |
| `params.epochs` | int | 10 | XTTS: training epochs |
| `params.batch_size` | int | 3 | Per-step batch size (both engines) |
| `params.grad_accum` | int | 1 | XTTS: gradient accumulation steps |
| `params.learning_rate` | float | 5e-6 | XTTS: learning rate |
| `params.sovits_epochs` | int | service default | GPT-SoVITS: SoVITS (s2) training epochs |
| `params.gpt_epochs` | int | service default | GPT-SoVITS: GPT (s1) training epochs |

`params` is a permissive bag: keys not used by the chosen engine are ignored by that engine. XTTS resolves omitted params against the project's `xtts_*` config columns; GPT-SoVITS forwards only explicitly-sent params and the service fills the rest from its own defaults (no project columns in v1).

Dataset filters apply in both modes: segments outside 1–11 seconds and segments flagged `cleanup_error` are excluded. Drop counts are recorded in the dataset manifest so a thin dataset is visible, not silent.

**Response 409** `insufficient_dataset` if the selected segments total under 300 seconds. `detail`: `{ "selected_duration_secs": 214.7, "required_secs": 300 }`.
**Response 409** `finetune_in_progress` if a model for this project is `pending` or `training`.
**Response 503** `xtts_unavailable` if `engine` is `xtts` and the XTTS service is not deployed or unhealthy.
**Response 503** `engine_unavailable` if `engine` is `gpt_sovits` and that service is not deployed or unhealthy.

**Response 202:**
```json
{
  "model": { "id": "...", "status": "pending", "dataset_mode": "approved" },
  "enqueued_jobs": [
    { "id": "...", "type": "dataset_build" },
    { "id": "...", "type": "finetune" }
  ]
}
```

---

#### `GET /projects/{project_id}/models`

List models for a project.

**Response 200:** `{ "models": [ ... ] }` — full rows as defined in `data-models.md` §models.

---

#### `DELETE /projects/{project_id}/models/{model_id}`

Delete a model row and its checkpoint directory.

**Response 409** `model_training` if the model is `pending` or `training` — cancel the job first.
**Response 204** on success.

---

#### `GET /projects/{project_id}/models/{model_id}/download`

Download the trained checkpoint bundle as an uncompressed tar archive, for use with an external runtime. The archive contains the checkpoint files from `models/{model_id}/`; the file list is **per-engine**:

**XTTS-v2** (e.g. for a Wyoming/Home Assistant TTS server):
- `model.pth`, `config.json`, `vocab.json` — mandatory; sufficient for `Xtts.load_checkpoint(checkpoint_dir=…)`
- `speaker_latents.pt` — included when present (cached `{gpt_cond_latent, speaker_embedding}` conditioning)

**GPT-SoVITS** (all five mandatory — the engine needs a conditioning reference *and its transcript* at inference):
- `gpt.ckpt` (fine-tuned GPT/s1 weights), `sovits.pth` (fine-tuned SoVITS/s2 weights)
- `config.json` (engine, version, sample rate, relative file paths, vendored commit, resolved hyperparams)
- `reference.wav` + `reference.txt` (conditioning reference clip, 3–10 s, selected from the training set at packaging time)

The tar is streamed in chunks (no temp file, no full-file buffering), so a multi-GB `model.pth` transfers without a memory or disk spike. It is uncompressed because model weights do not gzip meaningfully.

**Response 200:** `Content-Type: application/x-tar`, `Content-Disposition: attachment; filename="{model_id}.tar"`, streamed tar body.
**Response 409** `model_not_ready` if the model status is not `ready` (still training/failed), with `detail.status`.
**Response 404** `model_not_found` if the model row does not exist; `model_bundle_not_found` if a mandatory checkpoint file is missing on disk.

---

### Previews (v1.5)

#### `POST /projects/{project_id}/previews`

Synthesise a speech preview. `model_id: null` uses the XTTS base model (zero-shot); base previews are XTTS-only. A non-null `model_id` routes to the model's engine service: XTTS models get orchestrator-resolved conditioning as below, GPT-SoVITS models synthesise with the reference stored in their bundle (`conditioning` is ignored, and there is no base/untrained GPT-SoVITS preview).

Either `text` or `segment_id` is required. `segment_id` drives the A/B compare flow: when set, `text` is ignored and the orchestrator synthesises the segment's effective transcript (`transcript_edited` if set, else `transcript`) instead, so the clone says exactly what the original recording says. The target segment is also excluded from the `segments_raw`/`segments_cleaned` conditioning pools (it would be a trivial one-shot copy of the very line being compared).

**Request (free text):**
```json
{
  "text": "This is what the cloned voice sounds like.",
  "model_id": null,
  "conditioning": { "source": "segments_cleaned", "segment_count": 5 },
  "temperature": 0.65,
  "speed": 1.0,
  "repetition_penalty": 10.0,
  "top_k": 50,
  "top_p": 0.85
}
```

**Request (segment compare):**
```json
{
  "segment_id": "7f3c2a1b-...",
  "model_id": null,
  "conditioning": { "source": "segments_cleaned", "segment_count": 5 }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `text` | string | — | 1–500 characters. Required unless `segment_id` is set. |
| `segment_id` | UUID or null | null | Compare against this segment: synthesises its effective transcript and excludes it from segment conditioning pools. Ignores `text` when set. Required unless `text` is set. |
| `model_id` | string or null | null | Fine-tuned model to synthesise with; null = base model zero-shot |
| `conditioning.source` | string | best available | `reference_clip`, `segments_raw`, `segments_cleaned`, or `custom`. Default resolves to the best available stage (cleaned > raw > reference) |
| `conditioning.segment_count` | int | 5 | Segment sources only: top-N segments by match confidence, duration 2–12 s |
| `conditioning.clip_id` | string or null | null | For `source: "custom"` only — the id from `POST .../previews/conditioning`. Conditions XTTS on a one-off uploaded clip, inference-only, without touching the diarisation reference. |
| `temperature` | float | 0.65 | XTTS sampling temperature (>0, ≤2). Higher = more varied delivery. |
| `speed` | float | 1.0 | Speaking-rate multiplier (0.25–2). Real rate control, not resampling. |
| `repetition_penalty` | float | 10.0 | 1–20. Raise to kill stutters, repeated syllables, and trailing silences. |
| `top_k` | int | 50 | 1–100. Sample from only the k most likely tokens. |
| `top_p` | float | 0.85 | Nucleus sampling cutoff (>0, ≤1). |
| `enable_text_splitting` | bool | true | Split long text into per-sentence prosody contours. |

All sampling knobs are per-run only — never stored on the project, and the tabled defaults are **XTTS values applied per-engine**: the orchestrator persists only the knobs the request explicitly sent, then fills the rest per the model's engine (XTTS previews get the values above; GPT-SoVITS previews leave omitted knobs to the service's own defaults — XTTS-scale values like `repetition_penalty: 10.0` are never forced onto it, and these knobs are never sent for GPT-SoVITS). `num_beams`/`length_penalty` are deliberately not exposed: coqui XTTS's inference path is not beam-aware (`num_beams` > 1 crashes with a tensor reshape error), and `length_penalty` only applies under beam search.

The orchestrator resolves the conditioning source to absolute WAV paths (XTTS previews only). The vocal-separation stage is not an option: vocal stems are whole-file, not speaker-specific.

**Response 409** `conditioning_unavailable` if the requested source has no audio yet (e.g. `segments_raw` before diarisation has run, or a `custom` `clip_id` that is missing/expired). XTTS previews only — GPT-SoVITS models carry their own bundled reference, so this check is skipped for them.
**Response 409** `model_not_ready` if `model_id` refers to a model that is not `ready`.

---

#### `POST /projects/{project_id}/previews/conditioning`

Upload a one-off clip to condition XTTS synthesis on — inference-only, distinct from the project reference clip (which gates diarisation). Multipart (`file`), streamed to disk. Returns a `clip_id` to pass as `conditioning.clip_id` with `source: "custom"`. Clips are inference scratch under `projects/{id}/conditioning/` and are best-effort swept after 24 h.

**Request:** `multipart/form-data` with a `file` field (audio, ≥ 2 s).

**Response 201:** `{ "clip_id": "…", "duration_secs": 4.2 }`
**Response 422** `conditioning_too_short` if under 2 seconds.

---

#### `POST /projects/{project_id}/previews/conditioning/from-segment`

Promote an existing segment's audio (e.g. a stitched clip of expressive lines) to a custom conditioning clip — no download/reupload, and the project reference is untouched. Copies the segment's raw WAV into the conditioning scratch dir.

**Request:** `{ "segment_id": "…" }`
**Response 201:** `{ "clip_id": "…", "duration_secs": 4.2 }`
**Response 404** if the segment doesn't exist. **409** `audio_unavailable` if its WAV is missing. **422** `conditioning_too_short` if under 2 seconds.

---

#### `GET /projects/{project_id}/previews/conditioning`

List the project's custom conditioning clips (newest first) so the preview UI can offer previously uploaded/promoted clips.

**Response 200:** `{ "clips": [{ "clip_id": "…", "duration_secs": 4.2 }] }`

---

_The remaining responses below pertain to `POST .../previews`._

**Response 409** `segment_not_comparable` if `segment_id` doesn't exist or has no transcript.
**Response 422** if neither `text` nor `segment_id` is given.
**Response 503** `xtts_unavailable` if the resolved engine is XTTS (base preview, or an xtts model) and the XTTS service is not deployed or unhealthy; `engine_unavailable` for an unhealthy GPT-SoVITS model preview.

**Response 202:** `{ "enqueued_job": { "id": "...", "type": "preview" } }` — the preview id is the job id.

---

#### `GET /projects/{project_id}/previews`

List recent previews (derived from `preview` jobs).

**Query params:** `limit` (default 20).

**Response 200:**
```json
{
  "previews": [
    {
      "id": "...",
      "status": "complete",
      "text": "...",
      "segment_id": null,
      "model_id": null,
      "conditioning": { "source": "segments_cleaned", "segment_count": 5 },
      "sampling": { "temperature": 0.65, "speed": 1.0, "top_k": 50, "top_p": 0.85, "repetition_penalty": 10.0 },
      "created_at": "2026-07-12T04:00:00Z"
    }
  ]
}
```

`segment_id` is the segment this preview was compared against, or `null` for a plain free-text preview. `sampling` records the knobs this take was rendered with (for compare/preview history provenance); fields are `null` for previews created before this metadata was recorded.

---

#### `GET /projects/{project_id}/previews/{preview_id}/audio`

**Response 404** until the preview job completes.
**Response 200:** `audio/wav`, full file (no Range support, consistent with segment audio).

---

#### `DELETE /projects/{project_id}/previews/{preview_id}`

Delete a preview (comparison or free-text) — removes the `preview` job row and best-effort deletes its WAV.

**Response 204** on success.
**Response 404** `preview_not_found` if the id isn't a preview job for this project.
**Response 409** `preview_running` if the preview is still `queued` or `running`.

---

### Tuning previews

Ephemeral A/B renders of stage settings on a sample — never written to segment tables. Results live in `projects/{project_id}/tuning_previews/` (best-effort swept after 24 h on submit; excluded from export and dataset builds). Stage-generic by design: `cleanup` is the only stage in v1; `separation` (window-preview) is the planned follow-on.

#### `POST /projects/{project_id}/tuning-preview`

Render ONE segment through the cleanup service with draft params.

**Request:**
```json
{
  "stage": "cleanup",
  "params": {
    "target_lufs": -19.0,
    "highpass_hz": 120,
    "silence_threshold_db": -50.0,
    "silence_min_duration_secs": 0.1,
    "silence_pad_start_secs": 0.05,
    "silence_pad_end_secs": 0.2
  },
  "target": { "segment_id": "..." }
}
```

Param bounds match the project-config fields (out-of-range → 422; unknown `stage` → 422). The job merges the draft knobs over the project's saved cleanup params (fixed keys like `true_peak_dbtp` come from the standard cleanup payload). `tuning_preview` is a CPU job — not GPU-gated — and is excluded from project-status recomputation, so an A/B never flips the project to `processing`.

**Response 202:** `{"enqueued_job": {"id": "...", "type": "tuning_preview"}}`
**Errors:** 404 `not_found` (unknown segment), 503 `cleanup_unavailable`.

#### `GET /projects/{project_id}/tuning-preview/{preview_id}`

Poll the preview job. **Response 200:** `{"id": "...", "status": "queued|running|complete|failed", "error": null}`. 404 `not_found` for ids that aren't tuning previews.

#### `GET /projects/{project_id}/tuning-preview/{preview_id}/audio`

**Response 404** (`preview_not_ready`) until the job completes; **200** `audio/wav` after.

---

### Jobs

#### `GET /projects/{project_id}/jobs`

List recent jobs for a project.

**Query params:** `status` (filter), `limit` (default 20).

**Response 200:**
```json
{
  "jobs": [
    {
      "id": "...",
      "type": "transcription_bulk",
      "status": "running",
      "progress": 62,
      "source_id": null,
      "created_at": "2026-04-03T14:00:00Z",
      "started_at": "2026-04-03T14:00:05Z",
      "completed_at": null,
      "error": null
    }
  ]
}
```

---

## Part 2 — Orchestrator → Processing Services

These endpoints are called by the orchestrator only. Services bind on their internal ports and are not accessible from the host.

All processing endpoints are async: they accept a job, return `202 Accepted`, and the orchestrator polls for completion.

**Duplicate submissions.** `POST /jobs` on every service returns **409** `{"error": "job_exists", ...}` when the `job_id` is already known, rather than re-running or overwriting the job. The orchestrator treats that 409 as *already submitted* — the usual cause is a retry after a submit whose response timed out but whose request was accepted — and proceeds straight to polling. This makes submit-with-retry idempotent end to end.

---

### Vocal Separation Service (port 8001)

#### `GET /health`

**Response 200:** `{ "status": "ok" }`  
Returns 200 when the service is ready to accept jobs. The orchestrator should retry on connection refused during startup.

#### `POST /jobs`

**Request:**
```json
{
  "job_id": "...",
  "input_path": "/data/projects/{project_id}/audio/raw/{source_id}.wav",
  "output_path": "/data/projects/{project_id}/audio/vocals/{source_id}.wav",
  "model": "htdemucs",
  "chunk_secs": null,
  "shifts": 0
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | UUID assigned by orchestrator |
| `input_path` | string | yes | Absolute path to input WAV |
| `output_path` | string | yes | Absolute path for output vocals WAV |
| `model` | string | yes | `htdemucs` (Demucs v4), `htdemucs_ft` (fine-tuned, default), `mdx_extra`, or `bs_roformer` (RoFormer via audio-separator). RoFormer weights download on first use to `ROFORMER_MODEL_DIR` (defaults under `MODELS_ROOT`). |
| `chunk_secs` | int or null | no | If set, process audio in chunks of this duration (seconds) with 1-second overlap, then stitch. Used for OOM retry. If null, attempt whole-file processing. Applies to Demucs backends only; RoFormer manages its own segmentation. |
| `shifts` | int | no | Demucs test-time augmentation passes (0–10, default 0). Higher = cleaner separation at N+1× the runtime. Sourced from project `demucs_shifts`. Ignored for RoFormer. |

**Response 202:**
```json
{ "job_id": "..." }
```

---

#### `GET /jobs/{job_id}`

**Response 200:**
```json
{
  "job_id": "...",
  "status": "running",
  "progress": 34,
  "output_path": null,
  "error": null,
  "retry_with_chunk_secs": null
}
```

When complete:
```json
{
  "job_id": "...",
  "status": "complete",
  "progress": 100,
  "output_path": "/data/projects/{project_id}/audio/vocals/{source_id}.wav",
  "error": null,
  "retry_with_chunk_secs": null
}
```

On OOM:
```json
{
  "job_id": "...",
  "status": "failed",
  "error": "cuda_oom",
  "retry_with_chunk_secs": 60
}
```

The orchestrator checks `retry_with_chunk_secs` on failure: if set, it automatically resubmits the job with `"chunk_secs": 60` added to the request body. If a chunked retry also fails, the orchestrator marks the source `separation_failed` and surfaces the error to the user.

---

### Diarisation Service (port 8002)

#### `GET /health`

**Response 200:** `{ "status": "ok" }`  
Returns 200 when the service is ready. Note: on first run, the service downloads pyannote models (~2 GB) before becoming ready. The orchestrator should use a generous startup timeout (up to 5 minutes).

#### `POST /jobs`

**Request:**
```json
{
  "job_id": "...",
  "input_path": "/data/projects/{project_id}/audio/vocals/{source_id}.wav",
  "reference_path": "/data/projects/{project_id}/reference.wav",
  "output_dir": "/data/projects/{project_id}/segments/raw/",
  "params": {
    "min_segment_duration": 1.0,
    "min_speakers": 1,
    "max_speakers": 10
  }
}
```

**Response 202:** `{ "job_id": "..." }`

**Scout mode.** Selected by setting `reference_path` to `null` — a reference-less diarisation pass used to surface speaker candidates for reference acquisition (see the reference-scout endpoints in Part 1). Request shape:

```json
{
  "job_id": "...",
  "input_path": "/data/projects/{project_id}/audio/vocals/{source_id}.wav",
  "reference_path": null,
  "output_dir": "/data/projects/{project_id}/reference_candidates/{job_id}/",
  "params": {
    "min_segment_duration": 1.0,
    "min_speakers": 1,
    "max_speakers": 10,
    "num_speakers": 2,
    "pool_max_secs": 90.0,
    "pool_max_turns": 20
  }
}
```

In scout mode the service runs diarisation to produce anonymous speaker clusters exactly as in match mode, but skips the reference embedding / cosine-similarity step entirely. `num_speakers` (optional) forces pyannote to that exact count when set (ignoring `min`/`max`). For each speaker it writes a **bounded pool of individual turn WAVs** to `output_dir/{speaker_label}/{index}.wav` — the speaker's turns taken whole, longest-first, until the pool reaches `pool_max_secs` (default 90.0) or `pool_max_turns` (default 20). No `match_confidence` or `speaker_match_confidence` is computed and no montage is written; the reference is assembled downstream by the orchestrator from the pool.

**Speaker matching method (match mode):** The service extracts a single speaker embedding from the reference clip, plus one embedding per diarised segment. Each segment's `match_confidence` is the cosine similarity between its own embedding and the reference embedding. The per-speaker **average embedding** (computed from the same per-segment embeddings) is scored against the reference too and reported on every segment as `speaker_match_confidence` — a secondary cluster-level signal. Segments shorter than 1.0 s, or whose embedding extraction fails, fall back to the cluster score for `match_confidence`. See `spec/pipeline.md` §Phase 2 for detail.

**Segment WAV files:** The service creates the `output_dir` if it doesn't exist. Each segment is written as `{output_dir}/{segment_id}.wav` where `segment_id` is the full UUID (not truncated). The service generates UUIDs for segments — the orchestrator uses these as primary keys when writing to the database.

---

#### `GET /jobs/{job_id}`

On completion (match mode):
```json
{
  "job_id": "...",
  "status": "complete",
  "mode": "match",
  "segments": [
    {
      "id": "7f3c2a1b-4d5e-6f7a-8b9c-0d1e2f3a4b5c",
      "start_secs": 142.31,
      "end_secs": 146.88,
      "speaker_label": "SPEAKER_00",
      "match_confidence": 0.91,
      "speaker_match_confidence": 0.88,
      "wav_path": "/data/projects/{project_id}/segments/raw/7f3c2a1b-4d5e-6f7a-8b9c-0d1e2f3a4b5c.wav"
    }
  ],
  "coverage_ratio": 0.21,
  "error": null
}
```

`match_confidence` is the segment's own embedding scored against the reference; `speaker_match_confidence` is the cluster-level (per-speaker average embedding) score for the speaker that segment belongs to — a secondary signal, identical across all segments of the same speaker label. For segments shorter than 1.0 s or whose embedding extraction failed, `match_confidence` equals `speaker_match_confidence` (cluster fallback).

The orchestrator writes all segments to the database from this response. It copies `wav_path` to the `raw_path` column and persists both scores (`match_confidence` and `speaker_match_confidence`). Segments with `match_confidence` below `project.match_threshold` are written with status `below_threshold`; others with status `pending`. The orchestrator also updates the source's `coverage_ratio` column from the response.

On completion (scout mode):
```json
{
  "job_id": "...",
  "status": "complete",
  "mode": "scout",
  "speakers": [
    {
      "speaker_label": "SPEAKER_00",
      "total_secs": 412.6,
      "segment_count": 173,
      "pool": [
        { "index": 0, "start": 63.2, "end": 88.0, "duration": 24.8 },
        { "index": 1, "start": 12.0, "end": 30.5, "duration": 18.5 }
      ]
    },
    {
      "speaker_label": "SPEAKER_01",
      "total_secs": 88.2,
      "segment_count": 41,
      "pool": [
        { "index": 0, "start": 5.0, "end": 11.0, "duration": 6.0 }
      ]
    }
  ],
  "error": null
}
```

The `mode` field (`"scout"` | `"match"`) is present on every completion response so the orchestrator can assert the shape it expects. On a scout completion, the orchestrator replaces the project's `speaker_candidates` rows with one row per speaker in the response.

---

### Transcription Service (port 8003)

#### `GET /health`

**Response 200:** `{ "status": "ok" }`

#### `POST /jobs`

**Request:**
```json
{
  "job_id": "...",
  "segments": [
    {
      "id": "7f3c2a1b-...",
      "wav_path": "/data/projects/{project_id}/segments/raw/7f3c2a1b.wav",
      "start_secs": 142.31,
      "resegment": true
    }
  ],
  "model": "large-v2",
  "language": null,
  "batch_size": 16,
  "compute_type": "default",
  "beam_size": 5,
  "vad_filter": false,
  "align": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | UUID assigned by orchestrator |
| `segments` | array | yes | List of segment IDs and WAV paths to transcribe |
| `segments[].start_secs` | float | when `resegment` is true | Absolute start of the segment within its source, used to compute absolute child timestamps |
| `segments[].resegment` | bool | no (default false) | Whether the service may split this segment into sentence-aligned children. See [Pipeline](pipeline.md) §Sentence-aligned re-segmentation. |
| `model` | string | yes | faster-whisper model size: `tiny`, `base`, `small`, `medium`, `large-v2` (default), `large-v3` |
| `language` | string or null | no | ISO 639-1 language code (e.g. `en`, `fr`, `ja`). If null, faster-whisper auto-detects per segment. |
| `batch_size` | int | no | Number of segments to transcribe concurrently on GPU. Default 16. Reduce if GPU OOMs during transcription. |
| `compute_type` | string | no | CTranslate2 precision: `default` (float16 on GPU, int8 on CPU), `float16`, `int8_float16`, or `int8`. Default `default`. A lighter type cuts VRAM on a constrained GPU. |
| `beam_size` | int | no | faster-whisper beam width. Default 5. Sourced from project `whisper_beam_size`. |
| `vad_filter` | bool | no | Drop non-speech with faster-whisper's VAD before decoding. Default false. Sourced from project `whisper_vad_filter`. |
| `align` | bool | no | Run a wav2vec2 forced-alignment pass over each segment's words before re-segmentation, sharpening word start/end times. Default false. Sourced from project `align_words`. No effect unless `resegment` is also true for a given segment — see [Pipeline](pipeline.md) §Optional word alignment. |

Transcription always runs with word-level timestamps enabled; they are consumed internally for re-segmentation and not returned per word.

**Response 202:** `{ "job_id": "...", "segment_count": 1089 }`

**OOM handling:** Unlike vocal separation, the transcription service does not have automatic OOM retry. If the service OOMs, the job fails. The orchestrator surfaces the error and the user can re-trigger transcription with a smaller `batch_size` (stored as a job parameter). The orchestrator does not automatically retry with a lower batch size — this is a manual recovery in v1.

---

#### `GET /jobs/{job_id}`

Progress updates return partial results as they complete:

```json
{
  "job_id": "...",
  "status": "running",
  "progress": 62,
  "completed_segments": [
    {
      "id": "7f3c2a1b-...",
      "transcript": "Well, I'm sure I don't know what you mean.",
      "transcript_confidence": 0.88
    },
    {
      "id": "9a1d4c2e-...",
      "children": [
        {
          "id": "3e8f1b6a-...",
          "wav_path": "/data/projects/{project_id}/segments/raw/3e8f1b6a-....wav",
          "start_secs": 201.40,
          "end_secs": 205.92,
          "transcript": "I told you not to come back here.",
          "transcript_confidence": 0.93
        },
        {
          "id": "c47a9d05-...",
          "wav_path": "/data/projects/{project_id}/segments/raw/c47a9d05-....wav",
          "start_secs": 205.92,
          "end_secs": 209.31,
          "transcript": "And yet here you are.",
          "transcript_confidence": 0.91
        }
      ]
    }
  ],
  "error": null
}
```

Each entry in `completed_segments` takes one of two shapes:

- **Unsplit** (the common case, and the only case when `resegment` was false): `{id, transcript, transcript_confidence}`.
- **Split** (`resegment` was true and the segment produced 2+ utterances): `{id, children: [...]}` where `id` is the parent segment and each child carries a service-generated UUID, the child WAV path, **absolute** `start_secs`/`end_secs`, and its own transcript and confidence. The parent has no top-level transcript.

`completed_segments` is **cumulative** — each poll returns all segments completed so far, not just new ones since the last poll. Entries are keyed by the parent segment `id`; the orchestrator must track which parent IDs it has already written to the database and skip duplicates. This design is simpler for the service (no cursor state) and idempotent for the orchestrator.

The orchestrator writes completed segments to the database as they arrive, rather than waiting for the full job to finish. This lets the review UI show transcriptions incrementally.

---

### Cleanup Service (port 8004)

#### `GET /health`

**Response 200:** `{ "status": "ok" }`

#### `POST /jobs`

**Request:**
```json
{
  "job_id": "...",
  "segments": [
    {
      "id": "7f3c2a1b-...",
      "input_path": "/data/projects/{project_id}/segments/raw/7f3c2a1b.wav",
      "output_path": "/data/projects/{project_id}/export/7f3c2a1b.wav"
    }
  ],
  "params": {
    "target_lufs": -23.0,
    "true_peak_dbtp": -2.0,
    "lra": 7.0,
    "highpass_hz": 80,
    "do_trim_silence": true,
    "silence_threshold_db": -50.0,
    "silence_min_duration_secs": 0.1,
    "silence_pad_start_secs": 0.05,
    "silence_pad_end_secs": 0.2,
    "clipping_threshold_db": -0.1,
    "clipping_min_consecutive_samples": 3,
    "output_sample_rate": 22050,
    "output_channels": 1
  }
}
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `target_lufs` | float | -23.0 | EBU R128 loudness target |
| `true_peak_dbtp` | float | -2.0 | True peak ceiling |
| `lra` | float | 7.0 | Loudness range target |
| `highpass_hz` | int | 80 | High-pass filter cutoff frequency |
| `do_trim_silence` | bool | true | Whether to trim leading/trailing silence. False keeps the diariser's boundaries intact. |
| `silence_threshold_db` | float | -50.0 | Silence detection threshold for leading/trailing trim |
| `silence_min_duration_secs` | float | 0.1 | Minimum silence duration to trigger trim |
| `silence_pad_start_secs` | float | 0.05 | Silence re-added to the head after trimming (clean attack). 0 disables. |
| `silence_pad_end_secs` | float | 0.2 | Silence re-added to the tail after trimming (clean decay). 0 disables. |
| `clipping_threshold_db` | float | -0.1 | dBFS threshold for clipping detection |
| `clipping_min_consecutive_samples` | int | 3 | Number of consecutive samples at/above threshold to flag clipping |
| `output_sample_rate` | int | 22050 | Output sample rate (Hz) |
| `output_channels` | int | 1 | Output channel count |

**Response 202:** `{ "job_id": "..." }`

---

#### `GET /jobs/{job_id}`

On completion:
```json
{
  "job_id": "...",
  "status": "complete",
  "results": [
    {
      "id": "7f3c2a1b-...",
      "output_path": "/data/projects/{project_id}/export/7f3c2a1b.wav",
      "clipping_warning": false,
      "auto_rejected": false,
      "error": null
    },
    {
      "id": "9a1d4e2c-...",
      "output_path": null,
      "clipping_warning": false,
      "auto_rejected": true,
      "error": null
    },
    {
      "id": "b3e5f7a9-...",
      "output_path": null,
      "clipping_warning": false,
      "auto_rejected": false,
      "error": "ffmpeg_error: exit code 1, invalid data found when processing input"
    }
  ],
  "error": null
}
```

**Per-segment error handling:** The cleanup service processes all segments and does not abort the job on individual segment failures. Each segment result includes an `error` field. If a segment fails FFmpeg processing, `error` is set, `output_path` is null, and `auto_rejected` is false. The orchestrator marks such segments as `auto_rejected` with the error message stored in the segment's `flags` field. The job-level `error` is only set if the entire job fails (e.g. FFmpeg binary not found).

The orchestrator updates segment statuses from this response: `auto_rejected` segments (silent after trim or FFmpeg failure) move to `auto_rejected` status; `clipping_warning` segments move to `clipping_warning` and are returned to the review queue.

---

### XTTS Service (port 8005) — v1.5

#### `GET /health`

**Response 200:** `{ "status": "ok" }`

**Response 503** `cpml_not_accepted` if `XTTS_ACCEPT_CPML` is not set. The service still starts and serves this from `/health` (and from `POST /jobs`) until the licence is accepted and the container restarted — a live, diagnosable 503 rather than a crash loop. The orchestrator treats the failing health check as an unhealthy service, will not submit jobs, and the Models/Previews endpoints return 503 `xtts_unavailable`.

#### `POST /jobs`

Two job types, discriminated by `type`.

**Request (`finetune`):**
```json
{
  "job_id": "...",
  "type": "finetune",
  "manifest_path": "/data/projects/{project_id}/models/{model_id}/dataset.json",
  "output_dir": "/data/projects/{project_id}/models/{model_id}",
  "params": {
    "epochs": 10,
    "batch_size": 3,
    "grad_accum": 1,
    "learning_rate": 5e-6,
    "language": "en",
    "eval_split": 0.1
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | UUID assigned by orchestrator |
| `manifest_path` | string | yes | Dataset manifest. Same schema as the export manifest, except `audio_file` values are absolute paths |
| `output_dir` | string | yes | Directory for the checkpoint bundle; created if missing |
| `params.language` | string | yes | From project settings |
| `params.eval_split` | float | no | Fraction of segments held out for eval (default 0.1) |

The service converts the manifest to a Coqui formatter CSV, splits train/eval, and runs the XTTS-v2 `GPTTrainer` recipe. Remaining params as documented on `POST /projects/{id}/models`.

**Request (`synthesise`):**
```json
{
  "job_id": "...",
  "type": "synthesise",
  "text": "This is what the cloned voice sounds like.",
  "language": "en",
  "reference_wavs": ["/data/projects/{project_id}/segments/raw/7f3c2a1b-1234-5678-9abc-def012345678.wav"],
  "checkpoint_dir": null,
  "output_path": "/data/projects/{project_id}/previews/{job_id}.wav",
  "params": { "temperature": 0.65, "speed": 1.0, "repetition_penalty": 10.0, "top_k": 50, "top_p": 0.85 }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `reference_wavs` | array | yes | 1–10 WAV paths for speaker conditioning latents |
| `checkpoint_dir` | string or null | no | Fine-tuned checkpoint bundle; null = base model (zero-shot) |
| `output_path` | string | yes | Absolute path for the output WAV; parent dir created if missing |
| `params.temperature` | float | no | Sampling temperature (default 0.65) |
| `params.speed` | float | no | Speaking-rate multiplier (default 1.0) |
| `params.repetition_penalty` | float | no | Default 10.0 |
| `params.top_k` | int | no | Default 50 |
| `params.top_p` | float | no | Default 0.85 |

The service always synthesises with `enable_text_splitting=True`: preview text can run to 500 characters, past XTTS's per-language sentence limit, and each split sentence gets its own prosody contour.

**Response 202:** `{ "job_id": "..." }`

---

#### `GET /jobs/{job_id}`

**Running (`finetune`):**
```json
{
  "job_id": "...",
  "status": "running",
  "progress": {
    "phase": "training",
    "epoch": 3,
    "total_epochs": 10,
    "step": 412,
    "total_steps": 1380,
    "train_loss": 2.84,
    "eval_loss": 3.01,
    "eta_secs": 5400
  },
  "result": null,
  "error": null
}
```

`phase` is one of `preparing` (model download, manifest→CSV conversion), `training`, `packaging`.

**Complete (`finetune`):**
```json
{
  "job_id": "...",
  "status": "complete",
  "result": {
    "checkpoint_dir": "/data/projects/{project_id}/models/{model_id}",
    "model_path": ".../model.pth",
    "config_path": ".../config.json",
    "vocab_path": ".../vocab.json",
    "speaker_latents_path": ".../speaker_latents.pt",
    "final_eval_loss": 2.71
  },
  "error": null
}
```

**Complete (`synthesise`):** `result` is `{ "output_path": "...", "duration_secs": 4.2 }`.

**Failure modes (`finetune`):**

- **VRAM preflight failure** (before training starts): `status: "failed"`, `error: "insufficient_vram: 12 GB required, 8 GB available"`. Not retryable.
- **CUDA OOM during training:** `status: "failed"`, `error: "cuda_oom"`, plus an advisory `"retry_with": { "batch_size": 1, "grad_accum": 3 }`. The orchestrator fails the model and job loudly and does **not** auto-resubmit — silently shrinking the operator's training config is worse than a clear failure that says to reduce the batch size and retry. `retry_with` is surfaced as guidance only.

---

### GPT-SoVITS Service (port 8006) — GPT-SoVITS engine

Same job dialect as the XTTS service, with per-engine differences noted here. No licence gate: the models are MIT-licensed and public, so `/health` is a plain probe (no `cpml_not_accepted` analogue) and no HF token is needed. Pretrained base weights download from the public `lj1995/GPT-SoVITS` HF repo on first use into `GPT_SOVITS_PRETRAINED_DIR`; the service is healthy before weights arrive, and a mid-job download failure fails that job with a clear error.

#### `GET /health`

**Response 200:** `{ "status": "ok" }`

#### `POST /jobs`

Two job types, discriminated by `type`.

**Request (`finetune`):** same shape as XTTS — `{ job_id, type, manifest_path, output_dir, params }`. Differences:

| Field | Difference vs XTTS |
|-------|--------------------|
| `params` | GPT-SoVITS knobs: `sovits_epochs`, `gpt_epochs`, `batch_size`. All optional — the orchestrator forwards only explicitly-sent keys and the service fills its own defaults. No `language`/`eval_split` injection (v1 trains `en` only; there is no eval pass) |

The service converts the manifest to the upstream `.list` format, runs the vendored prep stages (text/BERT, HuBERT, speaker-verification, semantic tokens) and the two training stages (SoVITS s2, then GPT s1) as subprocesses, then packages the bundle — including selecting a 3–10 s training segment (measured from the decoded audio, trimmed if over-band) as the inference conditioning reference. A dataset with no usable reference fails the job with `reference_unavailable: ...` rather than packaging a bundle that cannot synthesise.

**Request (`synthesise`):** same shape as XTTS. Differences:

| Field | Difference vs XTTS |
|-------|--------------------|
| `reference_wavs` | May be empty (the orchestrator sends `[]`): a fine-tuned bundle carries its own `reference.wav`/`reference.txt` |
| `checkpoint_dir` | Required in practice — there is no base-model (zero-shot) path; `null` is rejected |
| `params` | `temperature`, `speed`, `repetition_penalty`, `top_k`, `top_p` accepted with GPT-SoVITS-scale defaults (e.g. `repetition_penalty` 1.35 — upstream caps at 2.0, so XTTS-scale values must not be forced here) |

**Response 202:** `{ "job_id": "..." }`

#### `GET /jobs/{job_id}`

Same envelope as XTTS. `progress.phase` is one of `preparing` (dataset conversion + prep stages), `training_sovits`, `training_gpt`, `packaging` — two independently counted training sub-stages (`epoch`/`total_epochs` are per-sub-stage; the orchestrator's percent mapping weights the phases 0–5 / 5–50 / 50–95 / 95–99).

**Complete (`finetune`):** `result` is
```json
{
  "checkpoint_dir": "/data/projects/{project_id}/models/{model_id}",
  "gpt_path": ".../gpt.ckpt",
  "sovits_path": ".../sovits.pth",
  "config_path": ".../config.json",
  "reference_wav_path": ".../reference.wav",
  "reference_text_path": ".../reference.txt",
  "final_eval_loss": null
}
```
(`final_eval_loss` is always `null` — GPT-SoVITS training runs no eval pass; the orchestrator stores NULL.)

**Complete (`synthesise`):** `result` is `{ "output_path": "...", "duration_secs": 4.2 }` — as XTTS.

**Failure modes (`finetune`):** same contract as XTTS — `insufficient_vram: ...` preflight (via `FINETUNE_MIN_VRAM_GB`, default 8.0), and `cuda_oom` with an advisory `retry_with` suggestion (fail-loud: the orchestrator surfaces it but never auto-resubmits). Subprocess stage failures surface the stage name and a stderr tail in the error string.

---

## Polling behaviour

The browser polls `GET /projects/{project_id}` every 3 seconds when any job is active. When no jobs are active, polling stops. The UI resumes polling when the user triggers a pipeline action.

The orchestrator polls processing services every 2 seconds per active job. This is an internal concern — the browser never polls services directly.

**v1.5 exception:** `finetune` jobs run for hours and are polled every 10 seconds. The orchestrator maps the service's progress object to a 0–100 value for `jobs.progress` and persists the full object to `jobs.progress_detail` so the dashboard survives refreshes.

**Future:** WebSocket or SSE for push-based progress updates is a natural v2 improvement. The polling architecture is deliberately simple for v1 and the upgrade path is additive (new endpoint, same data shape).
