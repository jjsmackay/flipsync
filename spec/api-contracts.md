# API Contracts

**Status:** DRAFT  
**Last updated:** 2026-04-03

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

---

#### `PATCH /projects/{project_id}`

Update project config. Only `name`, `match_threshold`, `target_duration_secs`, `whisper_model`, `language`, `auto_approve_enabled`, `auto_approve_match_threshold`, and `auto_approve_transcript_threshold` are patchable. Changing `match_threshold` or any auto-approve field triggers a synchronous re-evaluation of segment statuses by the orchestrator (not a queued job), applied in this order:

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

The orchestrator enqueues an `extract_audio` job immediately after writing the file to disk. Extraction runs FFmpeg as a subprocess within the orchestrator process (not a separate service). The client polls `GET /projects/{project_id}` for status updates. On success the source moves to `step1_pending`; on failure it moves to `extraction_failed`.

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

---

### Reference clip

#### `POST /projects/{project_id}/reference`

Upload or replace the reference clip. Replaces any existing reference. Does not automatically re-run diarisation.

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

### Pipeline control

#### `POST /projects/{project_id}/pipeline/start`

Start the full pipeline for all sources in `step1_pending` status. Enqueues jobs in sequence.

**Response 202:**
```json
{
  "enqueued_jobs": [
    { "id": "...", "type": "vocal_separation", "source_id": "..." },
    { "id": "...", "type": "vocal_separation", "source_id": "..." }
  ]
}
```

---

#### `POST /projects/{project_id}/sources/{source_id}/reprocess`

Re-run one or more pipeline steps for a single source.

**Request:**
```json
{
  "steps": ["step1"],
  "params": {
    "demucs_model": "mdx_extra"
  }
}
```

`steps` must be `["step1"]`, `["step2"]`, or `["step1", "step2"]`. Cannot re-run step 3 (transcription) via this endpoint — use the transcription endpoints below.

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
  "chunk_secs": null
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | UUID assigned by orchestrator |
| `input_path` | string | yes | Absolute path to input WAV |
| `output_path` | string | yes | Absolute path for output vocals WAV |
| `model` | string | yes | `htdemucs` (default, best quality) or `mdx_extra` (fallback for poor htdemucs output) |
| `chunk_secs` | int or null | no | If set, process audio in chunks of this duration (seconds) with 1-second overlap, then stitch. Used for OOM retry. If null, attempt whole-file processing. |

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

The orchestrator checks `retry_with_chunk_secs` on failure: if set, it automatically resubmits the job with `"chunk_secs": 60` added to the request body. If a chunked retry also fails, the orchestrator marks the source `step1_failed` and surfaces the error to the user.

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

**Speaker matching method:** The service extracts a single speaker embedding from the reference clip, plus one embedding per diarised segment. Each segment's `match_confidence` is the cosine similarity between its own embedding and the reference embedding. The per-speaker **average embedding** (computed from the same per-segment embeddings) is scored against the reference too and reported on every segment as `speaker_match_confidence` — a secondary cluster-level signal. Segments shorter than 1.0 s, or whose embedding extraction fails, fall back to the cluster score for `match_confidence`. See `spec/pipeline.md` §Phase 2 for detail.

**Segment WAV files:** The service creates the `output_dir` if it doesn't exist. Each segment is written as `{output_dir}/{segment_id}.wav` where `segment_id` is the full UUID (not truncated). The service generates UUIDs for segments — the orchestrator uses these as primary keys when writing to the database.

---

#### `GET /jobs/{job_id}`

On completion:
```json
{
  "job_id": "...",
  "status": "complete",
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

The orchestrator writes all segments to the database from this response. It copies `wav_path` to the `raw_path` column. Segments with `match_confidence` below `project.match_threshold` are written with status `below_threshold`; others with status `pending`. The orchestrator also updates the source's `coverage_ratio` column from the response.

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
  "batch_size": 16
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
    "silence_threshold_db": -50.0,
    "silence_min_duration_secs": 0.1,
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
| `silence_threshold_db` | float | -50.0 | Silence detection threshold for leading/trailing trim |
| `silence_min_duration_secs` | float | 0.1 | Minimum silence duration to trigger trim |
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

## Polling behaviour

The browser polls `GET /projects/{project_id}` every 3 seconds when any job is active. When no jobs are active, polling stops. The UI resumes polling when the user triggers a pipeline action.

The orchestrator polls processing services every 2 seconds per active job. This is an internal concern — the browser never polls services directly.

**Future:** WebSocket or SSE for push-based progress updates is a natural v2 improvement. The polling architecture is deliberately simple for v1 and the upgrade path is additive (new endpoint, same data shape).
