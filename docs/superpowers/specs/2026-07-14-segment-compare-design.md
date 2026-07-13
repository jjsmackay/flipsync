# Segment vs Model-Output Comparison — Design

**Date:** 2026-07-14
**Status:** Approved

## Goal

Judge clone fidelity: pick a real approved segment, synthesise its exact
transcript through a trained XTTS model, and A/B listen — real voice vs cloned
voice saying the same words. Lives in the Models section. Cross-model
comparison and sampling-knob sweeps are explicit later extensions, not MVP.

## Approach

A compare is **a preview with provenance**: an ordinary `preview` job whose
params carry a `segment_id`. No new resource, no new job type. The pairing
survives reloads because `GET /previews` returns the `segment_id` and the
segment audio endpoint already exists.

## API changes (orchestrator)

### 1. `POST /projects/{id}/previews` — optional `segment_id`

- `CreatePreviewRequest` gains `segment_id: Optional[str] = None`.
- When `segment_id` is set, `text` is **not required and is ignored** — the
  orchestrator reads the segment's transcript from the DB at enqueue time, so
  the clone is guaranteed to say exactly what the original says.
- Validation (409 `segment_not_comparable`): segment must exist in the
  project and have a non-empty transcript.
- Request validation (422): neither `text` nor `segment_id` provided.
- `segment_id` is stored in the job params. The resolved transcript is stored
  as `text` in params (so history shows what was synthesised).

### 2. `GET /projects/{id}/previews` — surface `segment_id`

Each preview in the list response includes `segment_id` (null for plain
previews). Frontend uses it to re-pair original/clone across reloads.

### 3. Conditioning exclusion

`_resolve_conditioning` gains `exclude_segment_id: str | None = None`. When
set, the `segments_raw` / `segments_cleaned` pools filter that segment out
(`AND seg.id != ?`) so the model is never conditioned on the audio it is
being judged against. `reference_clip` conditioning is unaffected. The
preview job runner threads the param through.

### 4. `GET /projects/{id}/segments` — optional `q`

Case-insensitive `LIKE` match on `transcript`, composed with all existing
filters/sort/pagination. Powers the picker's text search server-side
(approved segments can exceed a page).

## Frontend (Models section)

New **Compare panel** beside the existing Preview panel:

- **Segment picker:** debounced text-search list over
  `status=approved,auto_approved` segments (uses new `q` param), showing
  transcript + duration. Selecting one shows its transcript read-only and
  enables the original-audio player (existing segment audio endpoint).
- **Model picker + sampling knobs:** same controls as the Preview panel —
  ready models only; Advanced toggle reuses the existing sampling-knob
  components. (This is what makes the knob-sweep extension free later.)
- **Generate:** creates a preview with `segment_id` and default conditioning;
  polls via the existing previews polling machinery.
- **Result:** an A/B pair — two labelled players, **Original** and **Clone**,
  stacked.
- **History:** recent previews carrying a `segment_id` list below as past
  compares, re-paired with their segment (segment deleted → show transcript
  from params, original player disabled).

## Error handling

- Standard flat error format throughout.
- XTTS service unhealthy → existing 503 path unchanged.
- Segment missing/no transcript → 409 `segment_not_comparable`.
- Conditioning pool empty after exclusion → existing 409
  `conditioning_unavailable` path (the exclusion can only shrink the pool by
  one).

## Testing

- **Orchestrator API tests:** segment_id validation paths (missing segment,
  empty transcript, text derivation into params, segment_id in list
  response), `q` filter (match, no-match, composition with status filter),
  and text/segment_id both-absent 422.
- **Unit test:** `_resolve_conditioning` excludes the target segment from
  both raw and cleaned pools; reference clip unaffected.
- **Frontend component test:** picker → generate → A/B render flow with
  mocked API; history re-pairing.

## Out of scope (MVP)

- Multi-model side-by-side (several previews sharing a `segment_id`,
  different `model_id`).
- Multi-take sampling sweeps (several previews sharing a `segment_id`,
  different knobs).

Both are supported by the data model as designed; UI only.
