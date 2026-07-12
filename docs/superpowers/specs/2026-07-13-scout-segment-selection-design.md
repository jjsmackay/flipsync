# Scout segment selection + expected-speaker-count

Date: 2026-07-13
Status: approved, implementing
Branch: `feature/scout-segment-selection`

## Problem

The "find speakers" scout builds one opaque 30 s montage per candidate (turns
concatenated longest-first). When pyannote under-splits — merging two people
into one cluster — or a stray turn leaks in, the picked reference contains a
wrong-voice segment. The user cannot hear or exclude individual turns, and
cannot influence the speaker count. Only re-scouting (and hoping) is possible.

## Approach

Two levers, segment selection first:

1. **Curatable candidates.** Scout stops producing an opaque montage. Each
   candidate carries a **bounded pool of individual turn slices**. The
   reference is *computed* from that pool: longest-first up to 30 s, minus any
   turns the user excludes. Excluding a wrong-voice turn automatically promotes
   the next-longest clean turn into the 30 s window — backfill for free.
   Default state (nothing excluded) = today's montage, so the one-click path is
   unchanged; curation is opt-in.

2. **Expected speaker count (advanced).** An optional "how many people are in
   this source?" control. When set, pyannote is forced to that exact count
   (`num_speakers`); when blank, current 1–10 range. Direct fix for the merge
   case.

The pool is bounded (`pool_max_secs`, `pool_max_turns`) so a 1-hour source is
no more overwhelming than a 5-minute one — only the longest ~15 turns ever
matter, since only they can enter a 30 s reference.

## Changes

### Diarisation service (`diariser.py`, `main.py`)
- `run_scout` gains `num_speakers: int | None`, `pool_max_secs=90.0`,
  `pool_max_turns=20`. When `num_speakers` set, call pipeline with
  `num_speakers=N` (exact) instead of `min/max`.
- Per speaker, build a **pool**: whole turns, longest-first, until
  `pool_max_secs` or `pool_max_turns`. Slice each to `{output_dir}/{label}/{i}.wav`.
- Return per candidate `{speaker_label, total_secs, segment_count,
  pool:[{index,start,end,duration}]}`. Montage dropped.
- `DiariseParams` gains `num_speakers`, `pool_max_secs`, `pool_max_turns`.

### Data model (migration 007)
Rebuild `speaker_candidates`: drop `montage_path`, add `pool_json TEXT NOT NULL`
(the turn list). Candidates are transient (re-scout repopulates), so the
migration drops and recreates rather than migrating rows forward. Re-add the
project index.

### Orchestrator (`jobs.py`, `routers/reference.py`)
- Scout handler stores `pool_json`; passes `num_speakers` from the job params.
- Slice paths are **derived** (`reference_candidates/{scout_job_id}/{label}/{i}.wav`),
  not stored, so pool_json holds only `index/start/end/duration`.
- `POST /reference/scout` accepts `{source_id, expected_speaker_count?}`.
- `GET /reference/scout` serialises each candidate with its pool, each turn
  carrying a `sample_url`.
- `GET /reference/scout/samples/{label}/{index}` streams one turn slice.
- `POST /reference/scout/select` body `{speaker_label, excluded_indices:[]}`.
  Reference = pool − excluded, longest-first to 30 s cap, assembled via the
  stdlib `wave` module (no numpy/soundfile/ffmpeg dep), installed atomically.
  Existing ≥5 s gate runs on the assembled result. `reference_origin` records
  the excluded/included indices.

### Frontend (`SetReferencePanel.tsx`, `types/api.ts`, `api/client.ts`)
- `SpeakerCandidate.pool: PoolTurn[]` (drops candidate-level `sample_url`).
- Candidate card: summary + "Use this voice" (one-click default) + a
  "Choose segments" disclosure. Expanded: each pool turn shows duration, a play
  control (per-turn `sample_url`), and a "Not this speaker" exclude toggle.
  Turns in the 30 s window badge "in reference", the rest "backup". Live
  reference-length readout; "Use this voice" disabled under 5 s.
- Advanced drawer: "Expected number of speakers (optional)"; applied on the
  next scan. `startScout(projectId, sourceId, expectedCount?)`.
- `selectScoutSpeaker(projectId, label, excludedIndices)`.

### Defaults
`pool_max_secs=90`, `pool_max_turns=20`, reference cap `30`, min reference `5`,
`min_segment_duration=1.0` (unchanged, not exposed).

### Compatibility
Breaking change to the scout response and select payload. All internal and
unreleased — changed cleanly, not versioned. Existing scout tests are rewritten
to the pool shape.

## Testing
- Diarisation: `num_speakers` plumbing, pool caps (secs + turns), per-turn
  slicing + indices.
- Orchestrator: pool stored in `pool_json`, sample endpoint serves by index,
  select assembles from `pool − excluded` capped at 30 s, exclude→recompute,
  duration gate on assembled result, `expected_speaker_count` plumbing,
  migration 007 applied.
- Frontend: exclude toggle recomputes window/length, one-click default
  unchanged, expected-count re-scan.
