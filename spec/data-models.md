# Data Models

**Status:** DRAFT  
**Last updated:** 2026-07-13

---

## Storage decision

**SQLite.** One database file per project at `projects/{project_id}/project.db`.

Rationale: the review UI needs queries the filesystem can't serve efficiently — "all segments with confidence > 0.85 and status pending", "total approved duration", "segments from episode 3 only". A flat JSON manifest works for export but not for interactive review. SQLite is a single file, survives Docker restarts, requires no separate service, and is trivially backed up by copying the file. It is the right tool at this scale.

The `manifest.json` file in the export directory is a **derived output**, not the source of truth. It is written at export time from the database. Processing services do not read or write JSON manifests — all data flows through the orchestrator, which reads service responses and writes to SQLite.

---

## Database creation and connection management

**Creation:** The orchestrator creates `project.db` inside the project's working directory when `POST /projects` is called. It runs all migration files against the new database immediately, producing a fully-formed schema.

**Connection management:** The orchestrator maintains one SQLite connection per project, opened on first access and kept open for the lifetime of the process. Since SQLite allows only one writer at a time, and the orchestrator is a single-process app, write contention is not a concern. Connections use `PRAGMA journal_mode=WAL` for concurrent read access during write operations (e.g. the browser reading segment state while the orchestrator writes transcription results).

**Multiple projects:** Each project has its own `.db` file. The orchestrator does not use a single shared database. When listing projects (`GET /projects`), the orchestrator reads a lightweight project index — either a single `index.db` at the `DATABASE_DIR` root, or by scanning project directories. The index approach is preferred for performance; the scan approach is acceptable for v1.

---

## Database schema

### `projects`

One row per project.

```sql
CREATE TABLE projects (
    id              TEXT PRIMARY KEY,       -- UUID
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL,          -- ISO 8601
    updated_at      TEXT NOT NULL,
    status          TEXT NOT NULL,          -- see Project status
    reference_path  TEXT,                   -- path to reference.wav
    reference_origin TEXT,                  -- JSON, provenance of the current reference; NULL if none set
    whisper_model   TEXT NOT NULL DEFAULT 'large-v2',
    language        TEXT,                   -- NULL = auto-detect
    match_threshold      REAL NOT NULL DEFAULT 0.75,
    target_lufs          REAL NOT NULL DEFAULT -23.0,
    target_duration_secs REAL NOT NULL DEFAULT 1800.0, -- progress bar target; default 30 minutes
    auto_approve_enabled             INTEGER NOT NULL DEFAULT 1,    -- boolean
    auto_approve_match_threshold     REAL NOT NULL DEFAULT 0.85,
    auto_approve_transcript_threshold REAL NOT NULL DEFAULT 0.90,
    whisper_batch_size    INTEGER NOT NULL DEFAULT 16,             -- segments transcribed concurrently on GPU (OOM lever)
    whisper_compute_type  TEXT NOT NULL DEFAULT 'default'          -- 'default' | 'float16' | 'int8_float16' | 'int8' (VRAM lever)
);
```

**`reference_origin` shape.** Records how the current reference was produced, for display on the dashboard (e.g. "Reference: SPEAKER_02 from s01e01"):

```json
{ "type": "uploaded" }
{ "type": "diarise_pick", "source_id": "…", "speaker_label": "SPEAKER_02" }
```

Set whenever `reference_path` is (re)established — by `POST /reference` (upload) or `POST /reference/scout/select` (diarise + pick). NULL until a reference is set.

**Auto-approve columns.** When `auto_approve_enabled` is set, segments whose `match_confidence` and `transcript_confidence` clear the two auto-approve thresholds (and that carry no flags or clipping warning) are moved from `pending` to `auto_approved` when transcription results land. See [Pipeline](pipeline.md) §Auto-approval for the full eligibility rule and [API Contracts](api-contracts.md) `PATCH /projects` for re-evaluation on threshold changes.

---

### `sources`

One row per uploaded video file.

```sql
CREATE TABLE sources (
    id              TEXT PRIMARY KEY,       -- UUID
    project_id      TEXT NOT NULL REFERENCES projects(id),
    filename        TEXT NOT NULL,          -- original filename
    file_path       TEXT NOT NULL,          -- source/{id}.{ext}
    audio_path      TEXT,                   -- audio/raw/{id}.wav, set after extraction
    vocals_path     TEXT,                   -- audio/vocals/{id}.wav, set after step 1
    duration_secs   REAL,                   -- set after extraction
    status          TEXT NOT NULL,          -- see Source status
    separation_model     TEXT,                   -- Demucs model used
    separation_error     TEXT,                   -- error message if separation failed
    diarisation_error     TEXT,
    coverage_ratio  REAL,                   -- fraction of file attributed to target speaker
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```

**Orchestrator write behaviour for sources:**
- `audio_path` and `duration_secs` are set when the `extract_audio` job completes.
- `vocals_path` and `separation_model` are set when the `vocal_separation` job completes. `separation_model` records the Demucs model variant actually used (which may differ from the default if the user requested a specific model or the orchestrator retried with a fallback).
- `separation_error` is set when vocal separation fails; cleared when step 1 is re-run.
- `diarisation_error` is set when diarisation fails; cleared when step 2 is re-run.
- `coverage_ratio` is set from the diarisation service response when step 2 completes.

### `segments`

One row per diarised segment. This is the central table.

```sql
CREATE TABLE segments (
    id                      TEXT PRIMARY KEY,   -- UUID
    project_id              TEXT NOT NULL REFERENCES projects(id),
    source_id               TEXT NOT NULL REFERENCES sources(id),
    raw_path                TEXT NOT NULL,      -- segments/raw/{id}.wav
    export_path             TEXT,               -- export/{id}.wav, set after cleanup

    -- Diarisation
    start_secs              REAL NOT NULL,
    end_secs                REAL NOT NULL,
    duration_secs           REAL GENERATED ALWAYS AS (end_secs - start_secs) STORED,
    speaker_label           TEXT NOT NULL,      -- SPEAKER_00, SPEAKER_01, etc (local to source)

    -- Speaker matching
    match_confidence        REAL NOT NULL,      -- cosine similarity, 0.0-1.0
    speaker_match_confidence REAL,              -- cluster-level score, NULL on pre-006 rows

    -- Transcription
    transcript              TEXT,               -- NULL until transcribed
    transcript_edited       TEXT,               -- NULL unless user has edited
    transcript_confidence   REAL,               -- mean word probability, 0.0-1.0

    -- Review
    status                  TEXT NOT NULL DEFAULT 'pending',
    clipping_warning        INTEGER NOT NULL DEFAULT 0,       -- boolean, see note below
    flags                   TEXT,               -- JSON array of flag strings, see note below

    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
```

**Effective transcript:** the orchestrator always uses `COALESCE(transcript_edited, transcript)` when writing the export manifest. User edits take precedence; the original is preserved.

**Segment rows are not immutable across transcription.** Bulk transcription may replace an untranscribed `pending`/`below_threshold` segment with two or more sentence-aligned child rows (new UUIDs, inheriting `source_id`, `speaker_label`, and `match_confidence` from the parent; the parent row and WAV are deleted). Reviewed segments are never replaced. Eligibility is re-checked **at write time**, not just at job submission: when a `children` result lands, the parent must still be `pending`/`below_threshold` with `transcript` and `transcript_edited` NULL. If the user reviewed or edited it while the job ran, the parent is kept intact and the children's texts are joined into its `transcript` instead. See [Pipeline](pipeline.md) §Sentence-aligned re-segmentation.

**`speaker_match_confidence`** (migration 006) persists the cluster-level score the diarisation service reports per segment — the per-speaker average embedding scored against the reference. Secondary review signal alongside the per-segment `match_confidence`; NULL on segments diarised before the column existed. Returned by `GET /segments`.

**`clipping_warning` column vs `clipping_warning` status.** These are related but distinct. The `clipping_warning` INTEGER column is a persistent flag set by the cleanup service — it records "this segment's audio clips." The `clipping_warning` status is a review state that puts the segment back in the review queue. When the user re-approves a clipping segment (status → `approved`), the `clipping_warning` column remains `1` so the UI can still show the warning icon. The column is a fact about the audio; the status is a workflow state.

**`flags` column.** JSON array of string tags for machine-generated annotations that don't warrant their own column. Current usage:
- `"cleanup_error"` — FFmpeg failed on this segment during cleanup; the segment was auto-rejected. The error message is stored in the `flags` array as `"cleanup_error: <message>"`.
- `"short_transcript"` — segment is under 2 seconds; transcript confidence score may be unreliable.
- `"transcription_error: <message>"` — the transcription service reported a per-segment error. `transcript` is left NULL (never written as an empty string), so the segment stays eligible for future transcription runs; the flag is cleared when a later transcription succeeds.

Future flags can be added without schema changes. The UI should display flags as informational badges on the segment detail panel. Flags are set by the orchestrator based on service responses; services do not write to the database directly.

---

### `jobs`

One row per processing job. Jobs are the unit of work the orchestrator queues and tracks.

```sql
CREATE TABLE jobs (
    id              TEXT PRIMARY KEY,       -- UUID
    project_id      TEXT NOT NULL REFERENCES projects(id),
    source_id       TEXT,                   -- NULL for project-wide jobs (e.g. bulk transcription)
    type            TEXT NOT NULL,          -- see Job types
    status          TEXT NOT NULL,          -- queued | running | complete | failed | cancelled
    params          TEXT,                   -- JSON, job-specific parameters
    error           TEXT,                   -- error message if failed
    progress        INTEGER,                -- 0-100, updated during execution
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    completed_at    TEXT
);
```

**v1.5 addition:** a `progress_detail TEXT` column (JSON, nullable) is added by migration — rich progress for long-running jobs (fine-tune epoch/loss/ETA). `progress` remains the 0–100 integer.

---

### `models` (v1.5)

One row per XTTS-v2 fine-tuned model.

```sql
CREATE TABLE models (
    id                    TEXT PRIMARY KEY,   -- UUID
    project_id            TEXT NOT NULL REFERENCES projects(id),
    status                TEXT NOT NULL,      -- see Model status
    dataset_mode          TEXT NOT NULL,      -- approved | auto
    min_confidence        REAL,               -- auto mode only; NULL for approved
    segment_count         INTEGER,            -- set after dataset build
    dataset_duration_secs REAL,               -- set after dataset build
    dataset_manifest_path TEXT,               -- models/{id}/dataset.json
    checkpoint_dir        TEXT,               -- models/{id}/, set when ready
    params                TEXT,               -- JSON hyperparameters
    eval_loss             REAL,               -- final eval loss, set when ready
    error                 TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
```

**Dataset manifest** (`models/{id}/dataset.json`): same schema as the export manifest, with two differences — `audio_file` values are absolute paths (the inter-service interface), and a `selection` block records `{ mode, min_confidence, dropped: { too_short, too_long, flagged } }` so every model documents exactly what it was trained on.

**Dataset build** is a shared internal step: select segments (by review status or confidence floor), run cleanup on any lacking cleaned audio, write the manifest. Export reuses it (mode `approved`) and then archives; the archive contains exactly the files listed in its manifest.

---

### `speaker_candidates`

Transient store for the most recent reference-scouting result for a project — the speakers found by a `scout_speakers` job, waiting to be picked (or re-picked) as the reference. Exists so a scout result is queryable state rather than a JSON file, per the SQLite-source-of-truth rule.

```sql
CREATE TABLE speaker_candidates (
    id             TEXT PRIMARY KEY,       -- UUID
    project_id     TEXT NOT NULL REFERENCES projects(id),
    scout_job_id   TEXT NOT NULL REFERENCES jobs(id),
    source_id      TEXT NOT NULL REFERENCES sources(id),
    speaker_label  TEXT NOT NULL,          -- SPEAKER_00 etc, local to the scouted source
    pool_json      TEXT NOT NULL,          -- JSON list of {index,start,end,duration} — the curation pool
    total_secs     REAL NOT NULL,          -- total talk time for this speaker in the scouted source
    segment_count  INTEGER NOT NULL,       -- number of segments attributed to this speaker
    created_at     TEXT NOT NULL
);
```

`pool_json` is the bounded curation pool for the candidate: a JSON array of `{index, start, end, duration}` turn descriptors, longest-first. The turn slice WAVs themselves live at `reference_candidates/{scout_job_id}/{speaker_label}/{index}.wav` — paths are **derived** from `scout_job_id` + `speaker_label` + `index`, not stored. `total_secs`/`segment_count` cover the speaker's full talk time in the source, not just the pool.

**Rows are replaced on a new scout.** Running a fresh `scout_speakers` job for a project deletes all existing `speaker_candidates` rows for that `project_id` and inserts the new set. **Rows are kept after a pick.** Selecting a candidate as the reference (`POST /reference/scout/select`) does not delete `speaker_candidates` rows — the user can re-pick a different speaker from the same scout without re-scouting.

---

## Enumerations

### Project status

| Value | Meaning |
|-------|---------|
| `new` | Created, no files uploaded yet |
| `ready` | Files uploaded, pipeline not started |
| `processing` | One or more jobs running |
| `awaiting_reference` | Step 1 complete on at least one source, waiting for the user to set a reference before step 2 |
| `review` | All pipeline steps complete, awaiting user review |
| `exporting` | Export job running |
| `exported` | Export complete, archive available |

State transitions:

```
new                 -> ready              (first source file uploaded)
ready               -> processing         (pipeline started)
processing          -> review             (all jobs complete, no failures)
processing          -> ready              (all jobs complete, some failed — user action needed)
processing          -> awaiting_reference (jobs drained; ≥1 source at diarisation_pending; reference_path IS NULL)
awaiting_reference  -> processing          (user sets a reference and triggers pipeline/continue, OR a scout job is enqueued)
review              -> processing         (user re-runs a step or triggers transcription)
review              -> exporting          (user triggers export)
exporting           -> exported           (export job completes)
exported            -> processing         (user re-runs a step)
exported            -> review             (user changes approvals without re-processing)
exported            -> exporting          (user re-exports with different approvals)
```

The orchestrator recomputes project status after each job completion or user action, evaluated in order — first match wins:

1. `processing` — any job is `queued` or `running` (a running `scout_speakers` job counts, so scouting simply shows as `processing`).
2. `review` — all sources are `complete`.
3. `awaiting_reference` — no active jobs, `reference_path IS NULL`, and at least one source is at `diarisation_pending`.
4. `ready` — no active jobs and one or more sources ended in a failed state (some failed, user action needed).

---

### Source status

| Value | Meaning |
|-------|---------|
| `uploaded` | File received, audio not yet extracted |
| `extracting` | FFmpeg audio extraction running |
| `extraction_failed` | FFmpeg failed; user action required |
| `separation_pending` | Audio extracted, step 1 not started |
| `separation_running` | Demucs running |
| `separation_failed` | Demucs failed |
| `diarisation_pending` | Step 1 complete, step 2 not started |
| `diarisation_running` | pyannote running |
| `diarisation_failed` | pyannote failed |
| `complete` | Both steps done; segments written to database |

State transitions:

```
uploaded          -> extracting        (upload handler enqueues extraction job)
extracting        -> separation_pending     (extraction succeeds)
extracting        -> extraction_failed (extraction fails)
separation_pending     -> separation_running     (vocal separation job starts)
separation_running     -> diarisation_pending     (step 1 succeeds)
separation_running     -> separation_failed      (step 1 fails, including after OOM retry)
diarisation_pending     -> diarisation_running     (diarisation job starts)
diarisation_running     -> complete          (step 2 succeeds)
diarisation_running     -> diarisation_failed      (step 2 fails)
complete          -> separation_pending     (user re-runs step 1; deletes all segments for this source)
complete          -> diarisation_pending     (user re-runs step 2; deletes segments for this source)
separation_failed      -> separation_pending     (user retries step 1)
diarisation_failed      -> diarisation_pending     (user retries step 2)
```

`extraction_failed` has no forward transition. The user must delete the source and re-upload. This is intentional — a corrupt file won't become less corrupt on retry.

Reprocessing a source that already sits at the target pending status (`separation_pending` → re-run separation, `diarisation_pending` → re-run diarisation) is a re-enqueue, not a state transition, and is accepted without transition validation. This covers jobs that failed before their handler ran (e.g. a service-readiness timeout), which leave the source at its pending status.

---

### Segment status

| Value | Meaning |
|-------|---------|
| `pending` | Not yet reviewed |
| `approved` | User approved; included in export |
| `auto_approved` | System approved on confidence; included in export; user can demote |
| `rejected` | User rejected; excluded from export |
| `maybe` | User deferred decision |
| `below_threshold` | Below display threshold; not shown by default |
| `clipping_warning` | Cleanup flagged clipping; returned to review |
| `auto_rejected` | Silent after trim; excluded automatically |

State transitions:

```
pending          -> approved
pending          -> auto_approved    (system: transcription results land, or auto-approve re-evaluation)
pending          -> rejected
pending          -> maybe
pending          -> below_threshold  (when user raises match threshold)
maybe            -> approved
maybe            -> rejected
maybe            -> pending          (bulk reset: move all maybe → pending)
approved         -> rejected         (user changes mind)
approved         -> maybe            (user defers decision)
approved         -> clipping_warning (after cleanup step flags it)
auto_approved    -> approved         (user confirms)
auto_approved    -> rejected         (user overrides)
auto_approved    -> maybe            (user defers)
auto_approved    -> pending          (system: auto-approve re-evaluation; or bulk reset)
auto_approved    -> clipping_warning (after cleanup step flags it)
below_threshold  -> pending          (when user lowers match threshold)
clipping_warning -> approved         (user accepts the risk)
clipping_warning -> rejected
rejected         -> pending          (user un-rejects; misclick recovery)
```

`auto_approved` behaves like `approved` for export and duration statistics, but is visually distinct in the UI and freely demotable. Only the system moves segments *into* `auto_approved`, and only from `pending`; the `PATCH /segments` endpoint rejects user requests to set it (409).

`auto_rejected` is a terminal state with no exit. These segments were silent after trimming — they contain no usable audio. If the user re-runs step 2 for the source, all segments (including `auto_rejected`) for that source are deleted and new segments are created from scratch.

`rejected` can be returned to `pending` by the user (undo). This exists purely as misclick recovery — reject is a single keystroke in the review flow, and without an undo the only way back is reprocessing the entire source. `auto_rejected` remains terminal: it records a fact about the audio (silent after trimming), not a reviewer decision, so there is nothing to "undo."

The orchestrator rejects any transition not in this list.

---

### Model status (v1.5)

| Value | Meaning |
|-------|---------|
| `pending` | Row created; dataset build queued or running |
| `training` | `finetune` job running on the XTTS service |
| `ready` | Checkpoint written; usable for previews |
| `failed` | Dataset build or training failed; `error` holds the message |
| `cancelled` | User cancelled the job |

Transitions: `pending → training | failed | cancelled`; `training → ready | failed | cancelled`. Deleting a model removes the row and its checkpoint directory; deletion is rejected (409) while `pending` or `training`.

---

### Job types

| Value | Triggered by | Execution |
|-------|-------------|-----------|
| `extract_audio` | File upload | In-process FFmpeg subprocess (no external service) |
| `vocal_separation` | User starts pipeline or re-runs step 1 | External service (port 8001) |
| `scout_speakers` | User requests speaker scan for reference acquisition | External service (port 8002), reference-less diarisation over one source; `source_id` set |
| `diarisation` | Step 1 complete or user re-runs step 2 | External service (port 8002) |
| `transcription_bulk` | Step 2 complete or user triggers | External service (port 8003) |
| `transcription_segment` | User re-transcribes a single segment | External service (port 8003) |
| `export` | User triggers export | External service (port 8004) for cleanup, then in-process for manifest + archive |
| `dataset_build` (v1.5) | Fine-tune trigger | External service (port 8004) for segments lacking cleaned audio, then in-process manifest write |
| `finetune` (v1.5) | User triggers fine-tune | External service (port 8005) |
| `preview` (v1.5) | User requests preview synthesis | External service (port 8005) |

`extract_audio` and the manifest/archive phase of `export` run as FFmpeg subprocesses inside the orchestrator. They follow the same job lifecycle (create row, update progress, mark complete/failed) but do not involve polling an external service — the orchestrator runs the subprocess in a background `asyncio` task and updates the job row directly on completion.

---

## Derived values

These are computed by the orchestrator on request; not stored.

**Project summary stats** (returned with project state):

```json
{
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
      "coverage_ratio": 0.21,
      "low_coverage_warning": false
    }
  ]
}
```

`low_coverage_warning` is `true` when `coverage_ratio < 0.15` (i.e. the target speaker accounts for less than 15% of the source file's total audio). This threshold is hardcoded in v1.

**Counts are per-status:** `approved_count` counts status `approved` only; `auto_approved_count` counts status `auto_approved`. **`approved_duration_secs` covers both** — it measures what the export would contain, and drives the progress bar. UI copy that reports "approved" totals should show the breakdown (e.g. "1,145 approved · 402 of those auto").

**Approved duration** drives the progress indicator in the project header. XTTS-v2 needs roughly 30 minutes of clean audio for a fine-tune. The UI surfaces this as a progress bar towards a configurable target (default 1800 seconds).

---

## Export manifest format

Written to `export/manifest.json` at export time. XTTS-v2 compatible.

```json
{
  "version": "1",
  "project_id": "550e8400-e29b-41d4-a716-446655440000",
  "exported_at": "2026-04-03T14:32:00Z",
  "speaker": "target",
  "segments": [
    {
      "id": "7f3c2a1b-...",
      "audio_file": "7f3c2a1b.wav",
      "text": "The transcript text for this segment.",
      "source": "s01e01.mkv",
      "start_secs": 142.31,
      "end_secs": 146.88,
      "duration_secs": 4.57,
      "match_confidence": 0.91,
      "transcript_confidence": 0.88
    }
  ],
  "stats": {
    "segment_count": 743,
    "total_duration_secs": 4821.3
  }
}
```

The `text` field uses `COALESCE(transcript_edited, transcript)`. Segments with no transcript (edge case: transcription failed for a segment the user approved anyway) are excluded from the export with a warning logged.

---

## Indexes

```sql
CREATE INDEX idx_segments_project     ON segments(project_id);
CREATE INDEX idx_segments_source      ON segments(source_id);
CREATE INDEX idx_segments_status      ON segments(status);
CREATE INDEX idx_segments_confidence  ON segments(match_confidence);
CREATE INDEX idx_jobs_project_status  ON jobs(project_id, status);
CREATE INDEX idx_sources_project      ON sources(project_id);
CREATE INDEX idx_speaker_candidates_project ON speaker_candidates(project_id);
```

These cover the queries the review UI needs: filter by status, sort by confidence, filter by source, aggregate approved duration.

---

## Migration strategy

Schema changes use sequential numbered migration files in `services/orchestrator/migrations/`. The orchestrator runs pending migrations on startup. SQLite's `ALTER TABLE` support is limited; additive changes (new columns with defaults) are preferred. Destructive changes require a new table and data copy.

For v1, migrations are add-only. Breaking schema changes before v1.0 are acceptable with a documented upgrade path in the release notes.

Migration log:

| # | File | What it does |
|---|------|--------------|
| 001 | `001_initial_schema.sql` | Initial schema: `projects`, `sources`, `segments`, `jobs`, indexes |
| 002 | `002_add_exported_at.sql` | `projects.exported_at` — stale/invalidated-export detection |
| 003 | `003_auto_approve.sql` | Auto-approve configuration on `projects` (`auto_approve_enabled` + the two thresholds) |
| 004 | `004_reference_from_video.sql` | `projects.reference_origin` + the `speaker_candidates` table (reference acquisition from source video) |
| 005 | `005_semantic_source_statuses.sql` | Renames positional step1/step2 column names and source statuses to `separation`/`diarisation` |
| 006 | `006_speaker_match_confidence.sql` | `segments.speaker_match_confidence REAL NULL` — persists the cluster-level score the diarisation service already reports |
| 007 | `007_scout_pool.sql` | Rebuilds `speaker_candidates`: replaces `montage_path` with `pool_json` (curatable per-turn scout pool). Candidate rows are transient, so the table is dropped and recreated |
| 008 | `008_xtts_models.sql` | v1.5: adds the `models` table and the `jobs.progress_detail` column. Both additive |
