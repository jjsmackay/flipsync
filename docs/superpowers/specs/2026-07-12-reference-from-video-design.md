# Reference acquisition: derive a reference from the source video

**Status:** Design — approved for planning
**Date:** 2026-07-12
**Author:** Jono (with Claude)

## Problem

Today a project can only obtain its target-speaker reference by uploading a
separate audio file (`POST /projects/{id}/reference`). The reference is
consumed by step 2 (diarisation) for cosine-similarity speaker matching. For a
user whose only material *is* the source videos, "go find a clean clip of this
person" is friction — the clean audio they need already exists inside the
pipeline as the step 1 vocals stem.

This design lets a user derive the reference from their own source material,
with **diarise + pick** ("here are the voices we found — click yours") as the
default path.

## Scope

**In scope — two reference producers:**

1. **Upload** — existing endpoint, unchanged. Retained for pristine external
   clips and headless/scripted runs.
2. **Diarise + pick** — new default. Scout one source with a reference-less
   diarisation pass, present the detected speakers with playable samples, and
   build the reference from the chosen speaker.

**Out of scope — documented as future work:**

- **Clip-scrub** — hand-cutting a time range from a source's vocals stem. Once
  diarise + pick exists, clip-scrub is only a manual override for when
  clustering gets it wrong, and it carries the most unique UI (full-stem
  streaming + a dual-thumb scrubber). Deferred until clustering is shown to
  misbehave in practice. See [Future work](#future-work).

## Key concept: one reference artifact, several producers

The reference is a single artifact — `reference.wav` plus a provenance record —
and each producer converges on it. Everything downstream of the reference
(step 2 matching) is **unchanged** regardless of which producer was used. This
is what keeps multiple acquisition methods cheap: the back end has one
consumer, not three.

```
Upload ─────────────┐
                    ├──▶ projects/{id}/reference.wav ──▶ step 2 matching (unchanged)
Diarise + pick ─────┘
(Clip-scrub) ── future ┘
```

## The pipeline gate

The vocals stem — the clean single-speaker audio a good embedding needs —
exists only after step 1. So diarise + pick cannot run until step 1 has
produced a stem. This forces a pause in the pipeline for projects that have no
reference yet. We model that pause as an explicit, method-agnostic
**"Set reference" stage**.

### Behaviour

- **Reference already set** when `POST /projects/{id}/pipeline/start` is called
  (uploaded, or picked on a prior run) → the pipeline runs step 1 → step 2 →
  … straight through, exactly as today. **No gate.**
- **No reference** at start → the orchestrator enqueues step 1
  (`vocal_separation`) for every `step1_pending` source and **does not chain
  step 2**. When those jobs drain, the project rests in the new
  `awaiting_reference` state. The user sets a reference by any available
  producer, then `POST /projects/{id}/pipeline/continue` enqueues step 2 for
  all `step2_pending` sources and the pipeline proceeds to completion.

The upload producer is always available; diarise + pick becomes available for a
source once that source has a vocals stem (`vocals_path` set).

## State machine changes

### Project status

Add one value: **`awaiting_reference`** — "step 1 complete on at least one
source, waiting for the user to set a reference before step 2".

New transitions:

```
processing         -> awaiting_reference  (jobs drained; ≥1 source at step2_pending; reference_path IS NULL)
awaiting_reference -> processing           (user sets a reference and triggers pipeline/continue, OR a scout job is enqueued)
```

Recompute precedence (extends the existing "recompute after each job completion
or user action" rule). Evaluate in order; first match wins:

1. `processing` — any job is `queued` or `running` (a running `scout_speakers`
   job counts, so scouting simply shows as `processing`).
2. `review` — all sources are `complete`.
3. `awaiting_reference` — no active jobs, `reference_path IS NULL`, and at least
   one source is at `step2_pending`.
4. `ready` — no active jobs and one or more sources ended in a failed state
   (existing "some failed, user action needed" path).

### Source status

**No new source states.** `step2_pending` (step 1 complete, step 2 not started)
is the natural pause point and is reused as-is. A scout pass reads a source's
vocals stem but does not change that source's status.

## Data model changes

### `projects` — add `reference_origin`

```sql
reference_origin TEXT,   -- JSON, provenance of the current reference; NULL if none set
```

Shape:

```json
{ "type": "uploaded" }
{ "type": "diarise_pick", "source_id": "…", "speaker_label": "SPEAKER_02" }
```

(A future clip-scrub producer would add `"type": "clip_scrub"` with
`source_id`, `start_secs`, `end_secs`.) The UI uses this to show what the
current reference is, e.g. "Reference: SPEAKER_02 from s01e01".

### New table `speaker_candidates`

Transient store for the most recent scout result of a project. Honours the
"SQLite is the source of truth" invariant rather than writing a JSON file to
disk.

```sql
CREATE TABLE speaker_candidates (
    id             TEXT PRIMARY KEY,       -- UUID
    project_id     TEXT NOT NULL REFERENCES projects(id),
    scout_job_id   TEXT NOT NULL REFERENCES jobs(id),
    source_id      TEXT NOT NULL REFERENCES sources(id),
    speaker_label  TEXT NOT NULL,          -- SPEAKER_00 etc, local to the scouted source
    montage_path   TEXT NOT NULL,          -- reference_candidates/{scout_job_id}/{speaker_label}.wav
    total_secs     REAL NOT NULL,          -- total talk time for this speaker in the scouted source
    segment_count  INTEGER NOT NULL,       -- number of segments attributed to this speaker
    created_at     TEXT NOT NULL
);
```

Rows for a project are **replaced** whenever a new scout runs (delete existing
rows for the `project_id`, insert the new set). Rows are kept after a reference
is picked so the user can re-pick without re-scouting.

### Job types

Add **`scout_speakers`** — a reference-less diarisation pass over one source
that yields speaker candidates. Runs through the standard HTTP submit/poll
pattern against the diarisation service. `source_id` is set; `params` records
the scouted source and diarisation parameters.

## Diarisation service changes

The service gains a **reference-less scout mode**, selected when
`reference_path` is `null` in the job request.

### `POST /jobs` — scout mode

```json
{
  "job_id": "…",
  "input_path": "/data/projects/{id}/audio/vocals/{source_id}.wav",
  "reference_path": null,
  "output_dir": "/data/projects/{id}/reference_candidates/{job_id}/",
  "params": {
    "min_segment_duration": 1.0,
    "min_speakers": 1,
    "max_speakers": 10,
    "montage_max_secs": 30.0
  }
}
```

When `reference_path` is `null`, the service:

1. Runs pyannote diarisation to produce anonymous speaker clusters
   (`SPEAKER_00`…), exactly as in match mode but **without** the reference
   embedding / cosine-similarity step.
2. For each speaker, writes a **montage WAV** to `output_dir` at
   `{speaker_label}.wav` — that speaker's segments concatenated up to
   `montage_max_secs` (longest segments first, so the sample is representative
   and the clip is usable as a reference). This single file serves *both* as
   the picker's playback sample *and*, on selection, as the `reference.wav`
   itself.
3. Does **not** write per-segment WAVs and does **not** compute
   `match_confidence`.

### `GET /jobs/{job_id}` — scout mode completion

```json
{
  "job_id": "…",
  "status": "complete",
  "mode": "scout",
  "speakers": [
    {
      "speaker_label": "SPEAKER_00",
      "montage_path": "/data/projects/{id}/reference_candidates/{job_id}/SPEAKER_00.wav",
      "total_secs": 412.6,
      "segment_count": 173
    },
    {
      "speaker_label": "SPEAKER_01",
      "montage_path": "/data/projects/{id}/reference_candidates/{job_id}/SPEAKER_01.wav",
      "total_secs": 88.2,
      "segment_count": 41
    }
  ],
  "error": null
}
```

Match-mode requests and responses are unchanged. The `mode` field
(`"scout"` | `"match"`) is present so the orchestrator can assert the response
shape it expects.

## Orchestrator API changes

All new endpoints live under the reference sub-resource. Errors use the
standard flat format `{"error", "message", "detail"}`.

### `POST /projects/{project_id}/reference/scout`

Enqueue a scout pass on one source.

**Request:** `{ "source_id": "…" }`

**Response 202:** `{ "job_id": "…", "type": "scout_speakers" }`

**Response 422 `vocals_not_ready`** if the source has no vocals stem
(`vocals_path` is null — step 1 has not completed for it).

### `GET /projects/{project_id}/reference/scout`

Return the status of the latest scout job for the project and, once complete,
its speaker candidates (read from `speaker_candidates`). The frontend polls
this.

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
      "sample_url": "/api/projects/{id}/reference/scout/samples/SPEAKER_00" },
    { "speaker_label": "SPEAKER_01", "total_secs": 88.2, "segment_count": 41,
      "sample_url": "/api/projects/{id}/reference/scout/samples/SPEAKER_01" }
  ]
}
```

**Response 404 `no_scout`** if no scout has been run for the project.

### `GET /projects/{project_id}/reference/scout/samples/{speaker_label}`

Stream the montage WAV for a candidate speaker so the browser can play it.
Full file, no Range support (matches the segment-audio streaming convention).
Reads `montage_path` from `speaker_candidates` for the latest scout.

**Response 404 `unknown_speaker`** if the label is not in the current
candidate set.

### `POST /projects/{project_id}/reference/scout/select`

Adopt a candidate speaker as the reference.

**Request:** `{ "speaker_label": "SPEAKER_02" }`

**Behaviour:** copy the candidate's `montage_path` → `projects/{id}/reference.wav`,
set `projects.reference_path`, and set `projects.reference_origin` to
`{"type": "diarise_pick", "source_id": "…", "speaker_label": "SPEAKER_02"}`.
Does **not** auto-run step 2 (mirrors the upload endpoint's "does not
automatically re-run diarisation" behaviour).

**Response 200:** `{ "reference_path": "reference.wav", "duration_secs": 27.9 }`

**Response 404 `unknown_speaker`** if the label is not in the current
candidate set.

**Response 422 `reference_too_short`** if the candidate's montage is under the
5-second minimum (same floor the upload endpoint enforces). A speaker with so
little talk time is not a plausible target; the UI should also disable
**Use this voice** on cards whose `total_secs` is under 5. `detail` carries
`{ "duration_secs": …, "minimum_secs": 5.0 }`.

### `POST /projects/{project_id}/reference` (upload) — unchanged

Also sets `reference_origin` to `{"type": "uploaded"}`. No other change.

### `POST /projects/{project_id}/pipeline/start` — behaviour change

- If `reference_path` is set: unchanged (step 1 → step 2 chained per source).
- If `reference_path` is null: enqueue only step 1 (`vocal_separation`) for all
  `step1_pending` sources; do not chain step 2. The project reaches
  `awaiting_reference` once those jobs drain.

Response shape unchanged (`{ "enqueued_jobs": [...] }`).

### `POST /projects/{project_id}/pipeline/continue` — new

Enqueue step 2 (`diarisation`, match mode) for every source at `step2_pending`.

**Response 202:** `{ "enqueued_jobs": [...] }` (same shape as start).

**Response 409 `no_reference`** if `reference_path` is still null — the gate has
not been satisfied.

## Frontend changes

When the project is in `awaiting_reference`, the dashboard shows a **Set
reference** panel with two tabs:

- **Upload** — the existing reference upload control. On success, the Continue
  action is enabled.
- **Find speakers** (default tab):
  1. A source dropdown listing sources that have a vocals stem ready
     (default: the first such source).
  2. A **Scan for speakers** button → `POST …/reference/scout`, then poll
     `GET …/reference/scout` with progress.
  3. On completion, a list of **speaker cards**, each with a play control
     (streams `sample_url`) and stats (talk time from `total_secs`, segment
     count). Sort by `total_secs` descending — the target speaker is usually
     the most talkative.
  4. **Use this voice** on a card → `POST …/reference/scout/select`. On
     success the panel shows the chosen reference and enables **Continue**.

**Continue** → `POST …/pipeline/continue`, then resume normal polling
(3 s while jobs active, per the existing rule). The panel also displays the
current `reference_origin` if one is already set (e.g. re-picking).

The canvas timeline component is deliberately **not** reused here — it is built
for segment-review density. The speaker picker is a simple list of cards with
audio players.

## Testing

**Orchestrator (unit):**

- Gate logic: `pipeline/start` with `reference_path` null enqueues step 1 only
  and does not chain step 2; with a reference set, chains as today.
- Project status recompute: `processing → awaiting_reference` on drain with a
  `step2_pending` source and no reference; precedence vs `review`/`ready`.
- `awaiting_reference → processing` on `pipeline/continue`.
- `scout/select` copies the montage to `reference.wav` and sets
  `reference_path` + `reference_origin`.
- `pipeline/continue` returns 409 `no_reference` when reference still null.
- `speaker_candidates` rows are replaced on a fresh scout.

**Orchestrator (API):** request validation and response shape for each new
endpoint, including `vocals_not_ready`, `no_scout`, `unknown_speaker`,
`no_reference`.

**Diarisation service:**

- Scout mode (`reference_path: null`) returns `mode: "scout"` with a `speakers`
  array, writes montage WAVs, and writes no per-segment WAVs.
- Montage duration is capped at `montage_max_secs` and is a valid WAV.
- Match mode behaviour is unchanged (regression).

**Frontend:**

- Set reference panel renders in `awaiting_reference`; both tabs present.
- Find speakers: scan → poll → cards render sorted by talk time; select →
  Continue enabled.
- Upload tab still satisfies the gate and enables Continue.

## Files likely touched

- `spec/*` — fold this design into the canonical spec (data-models, api-contracts,
  pipeline, review-ui, architecture) once implemented.
- `services/orchestrator/` — new reference/scout endpoints, `pipeline/continue`,
  gate branch in `pipeline/start`, status recompute, `scout_speakers` job type,
  `speaker_candidates` table + migration, `reference_origin` column.
- `services/diarisation/` — reference-less scout mode, montage writer.
- `frontend/` — Set reference panel (Upload + Find speakers tabs), scout
  polling, speaker cards, Continue action.

## Future work

- **Clip-scrub producer:** `POST /projects/{id}/reference/extract`
  `{ source_id, start_secs, end_secs }` cutting a range from the vocals stem via
  FFmpeg, a `GET …/sources/{sid}/vocals` streaming endpoint, and a dual-thumb
  range-slider UI. Add `"type": "clip_scrub"` to `reference_origin`. Build only
  if diarisation clustering proves unreliable enough to need a manual override.
- **Per-segment embeddings:** the existing "future refinement" note in the
  diarisation contract (per-segment rather than per-speaker-average scoring)
  is orthogonal to this design and unaffected by it.
