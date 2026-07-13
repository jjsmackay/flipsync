# Review UI

**Status:** DRAFT  
**Last updated:** 2026-07-13

---

## Purpose

The review UI is where users spend most of their time with FlipSync. Everything upstream — separation, diarisation, transcription — exists to produce a queue of segments the user can work through efficiently. The UI must make that work fast, low-friction, and keyboard-driven.

A user reviewing a full TV season may face 1,000–2,000 segments. The interaction model must support bulk operations for the obvious cases and fast single-segment review for the rest. Mouse works; keyboard should be faster.

---

## Pages

### 1. Project list (`/`)

Lists all projects with name, status, approved duration, and a progress bar toward the target duration. "New project" button. No other functionality on this page.

---

### 2. Project dashboard (`/projects/{id}`)

The entry point for a project, organised as a **guided stage flow**. Internal identifiers (statuses, job types) never render raw — every user-visible name goes through the label map in `src/utils/labels.ts` (e.g. `separation_running` → "Separating vocals", `diarisation` → "Speaker matching", `awaiting_reference` → "Needs speaker").

#### Stage model

Seven user-facing stages. The first six are always **Upload → Speaker → Separate → Match → Transcribe → Review**; the terminal stage is **Export** by default, or **Train** when the XTTS voice service is deployed (see *XTTS terminal stage* below). The pipeline proper is three distinct stages, each also a step row in the Pipeline section (see item 6). A pure function `deriveStage(project, xttsEnabled)` maps the polled project response to the single current stage, by precedence:

1. No sources → **Upload**
2. **No reference set → Speaker.** `reference_path` is the Speaker/pipeline divider. The "whose voice?" prompt is the only trigger for separation, and the upload-a-clip path sets a reference *before* separation runs — so a project with sources but no reference is always still choosing a voice: the prompt, the separation run that feeds the scan, the scout scan, or picking a candidate all live in Speaker. This keeps the strip monotonic (separation no longer flips to a pipeline stage ahead of the Speaker step).
3. Any `extract_audio`/`vocal_separation` job active, or any source in `uploaded`, `extracting`, `separation_pending`, `separation_running`, `extraction_failed`, `separation_failed` → **Separate**
4. Any `diarisation` job active, or any source in `diarisation_pending`, `diarisation_running`, `diarisation_failed` → **Match**
5. Any `transcription_bulk`/`transcription_segment` job active → **Transcribe.** Transcription auto-chains after matching (and is re-runnable), so this stage is current only while a transcription job actually runs; a failed transcription surfaces via the failed-job alerts while the project sits in Review.
6. `pending_count + maybe_count > 0` → **Review**
7. Nothing approved (`approved + auto_approved = 0`) but `below_threshold_count > 0` → **Review** (the lower-the-threshold guidance, not a misleading Export)
8. Otherwise → **Export** (or **Train** when XTTS is enabled)

Scout jobs belong to the Speaker stage, export jobs to Export, voice jobs to Train, and ephemeral `tuning_preview` jobs to no stage at all — none flips the strip to a pipeline stage. Stages are not strictly linear — adding a source mid-review (a reference already exists) returns the project to Separate; that is expected.

A companion `stepChip(project, step, xttsEnabled)` derives the Pipeline-section row chip for the three pipeline steps: **Failed** (red) when any source sits in that step's failed status, else Done/Running/Ready/Not run yet from the stage states.

##### XTTS terminal stage

When the XTTS service is not deployed the flow ends at **Export**. When it *is* deployed the intended end product is a trained voice model, so the terminal stage is **Train** instead — export stays available on the pipeline's Clean & package row but is no longer a stage. The frontend learns whether XTTS is present from `GET /capabilities` (`{ "xtts": bool }`, a point-in-time health probe of the profile-gated service), fetched once per dashboard load — deliberately not folded into the 3 s project poll so an absent service adds no probe latency. A resolved `true` is memoised for the session, so a momentary probe failure can't flip an enabled deployment back to Export. `stagesFor(xttsEnabled)` returns the ordered strip; `deriveStage`/`stageStates` take the same flag.

#### Page layout (top to bottom)

1. **Header** — project name, settings gear (shown only once segments exist; opens the Pipeline section and expands + scrolls to the Review row's Settings disclosure), theme toggle. No review-queue button; review is reached via the stage strip and action card.
2. **Stage strip** — the stages, each rendered as done (✓) / active (pulsing, jobs running) / needs-you (amber) / upcoming (dimmed). The Review chip links to the review queue once segments exist; the three pipeline chips (Separate/Match/Transcribe) open and scroll to the Pipeline section once sources exist; the Train chip (XTTS deployments) opens the Pipeline section and scrolls to the pipeline's Train row.
3. **Next-action card** — one card, fixed slot, reserved min-height (no layout shift on the 3 s poll); content cross-fades on stage change:
   - *Upload*: full drag-and-drop upload area.
   - *Speaker*: the Set reference panel (below) — a small state machine over the pre-reference phase.
   - *Separate / Match / Transcribe*: one shared body — active job progress with human labels (headed by the running step's name); or a **Start processing** button (uploaded-clip source queued, nothing running); or **Continue processing** (reference set at the gate); or a "processing stopped" notice pointing at the failed-job alerts.
   - *Review*: "N segments ready to review" with a **Start reviewing →** link and a secondary **Transcribe segments** button.
   - *Export*: export confirmation/summary and download (export flow below).
   - *Train* (XTTS deployments): a thin guide card with a **Go to training →** button that opens the Pipeline section and scrolls to the pipeline's Train row; the training controls themselves live there, not in the card.

   Sections 5–7 below are **collapsible**. Sources and Pipeline are always expanded by default (Pipeline hosts the whole journey including the Review, Clean & package, and Train rows); Models (trained models + preview) is expanded by default for Train. An explicit toggle is remembered (per section, in `localStorage`) and overrides the default.
4. **Failed-job alerts** — only when present, own slot, error messages with retry/dismiss (sourced from `recent_failed_jobs`). Dismissal persists across reloads (`localStorage`; the API keeps returning failed jobs, so an in-memory hide reappeared on refresh) and is pruned when a job stops being returned. Shown until dismissed or retried. Retry is job-type-aware:
   - `vocal_separation` / `diarisation` — retry goes through the same confirm flow as a manual reprocess: submit **without** `confirm`, and if the API returns 409 `would_invalidate_approvals`, surface the confirmation dialog. Never pre-confirm.
   - `scout_speakers` — retry re-runs the scout for the same source (`POST /reference/scout`), not a source reprocess.
   - `extract_audio` — no Retry button; extraction failure is terminal by design. The alert shows guidance instead: "Extraction failed — remove this video and re-upload it."
   - `transcription_segment` — retry calls the per-segment rerun endpoint when the failed job carries a segment id; if it doesn't, no Retry is shown (never a silent no-op).
5. **Sources** — hidden while the project has no sources. Filename, human status label, speaker coverage (— until diarisation completes), per-row ⋯ menu with **Re-run from vocal separation** (`steps: ["separation", "diarisation"]`) and **Re-run speaker matching** (`steps: ["diarisation"]`), plus an inline compact **+ Add video** upload button.
6. **Pipeline** — hidden while the project has no sources. The six-step stepper, mirroring the strip's ordering (Review sits between Transcribe and cleanup — cleanup only ever runs on the approved set). Each machine row: step number + title, a `stepChip` status chip, a collapsed **Settings** disclosure holding that stage's tuning knobs (see *Pipeline tuning* below), and a run affordance:
     1. **Separate vocals** — a ▶ vocals fetch-on-click player per post-separation source (`GET /sources/{id}/vocals`); **Re-run** reprocesses `["separation", "diarisation"]` across all eligible sources.
     2. **Match speaker** — **Re-run** reprocesses `["diarisation"]` across all eligible sources.
     3. **Transcribe** — **Run/Re-run** triggers bulk transcription; disabled while a transcription job runs.
     4. **Review** — the human step, a full row like the others. Chip counts work owed: *N to review* (blue, `pending + maybe`), *Done* (green, anything approved and nothing owed), else *Not run yet*. Body: the segment counts as compact chips (approved / auto-approved / pending / maybe / rejected / below threshold — zero counts dimmed), the approved-duration progress bar (includes auto-approved), and a **Settings** disclosure holding the review thresholds (match threshold, auto-approve toggle + its two thresholds — saving re-evaluates segment statuses synchronously). **Open review →** links to the review queue.
     5. **Clean & package** — chip reads *Runs during export* (no run button — its knobs apply at export/dataset-build time); **Compare…** opens the cleanup A/B modal. Body carries the **Export dataset** flow (the export confirmation/download state machine) plus the cleanup Settings disclosure — cleanup runs inside export, so the export action lives on this row.
     6. **Train** — XTTS deployments only, mirroring the strip's terminal chip. Chip: *Running* (blue) while a `dataset_build`/`finetune` job is active, *Done* (green) once any model is `ready`, *Ready* (amber) when Train is the current stage, else *Not run yet*. Body embeds the train panel itself: a **Train voice model** button that is disabled with a visible reason while approved audio is under the 300 s dataset minimum; a confirm step choosing the dataset mode (Reviewed vs Train without review with a confidence floor) plus a collapsed **Advanced** disclosure with the fine-tune hyperparameters (per-run overrides — reseeded from config on every confirm open, sent as `params` on `POST /models` only where they differ from config, never PATCHed); while a `dataset_build`/`finetune` job runs, a progress card (epoch/step/losses/ETA) replaces the affordance. Below that, the `xtts_*` Settings disclosure edits the persisted fine-tune defaults. The row action is **Models →**, linking to the Models section (models list + previews). There is no approved-duration bar here — that lives on the Review row; only the training-specific thresholds (300 s minimum, 30 min recommended) surface as inline text. Models state is owned by the dashboard and shared between this row and the Models section.

     A step re-run targets sources in a terminal status for that step (`complete` plus the step's failed statuses); mid-pipeline sources are skipped. If any source would invalidate approvals, ONE confirmation dialog covers all the 409ed sources — never pre-confirmed. Re-run buttons disable while any pipeline job is active.

   ##### Pipeline tuning

   Each step row's Settings disclosure edits that stage's knob subset (bounds mirror the API): Separate — `demucs_model`, `demucs_shifts`; Match — `diar_min_speakers`, `diar_max_speakers`, `diar_min_segment_duration`; Transcribe — `whisper_batch_size`, `whisper_compute_type`, `whisper_beam_size`, `whisper_vad_filter`; Clean — `target_lufs`, `highpass_hz`, `silence_threshold_db`, `silence_min_duration_secs`. Saving PATCHes exactly that subset; when the step has already run the saved message reads "Saved — applies when this step re-runs", with the Re-run button adjacent so the config → rerun link is explicit. The same knobs (plus the XTTS hyperparameters) are exposed at project creation behind a collapsed **Advanced** disclosure, sent on `POST /projects` only where changed from the defaults.

   ##### Cleanup A/B compare modal

   Opened from the Clean & package row. One segment picker (first 50 segments, transcript excerpt + duration labels); two editable param columns — **A** (seeded from saved config) and **B** (draft); **Run comparison** submits one `POST /tuning-preview` per column (`stage: "cleanup"`), each pane polling `GET /tuning-preview/{id}` every 3 s (bounded ~10 min) and rendering an audio player from `/tuning-preview/{id}/audio` on completion; a failed preview surfaces its job error in its pane. Per-column **Save these settings** PATCHes the project with that column's values. Results are ephemeral — nothing touches segment rows.
7. **Models** (v1.5, XTTS) — directly below Pipeline; rendered only when XTTS is deployed and segments exist (an empty project has nothing to train on; without the voice service there are no models or previews). Training itself lives on the pipeline's Train row; this section holds what training produces:
   - The trained model rows, directly under the section heading: shared status badge (Queued/Training/Ready/Failed/Cancelled), dataset mode, duration/segment count/eval loss, delete with confirm (blocked while training).
   - **Preview** — one text box + conditioning source + a shared **Temperature** slider (0.05–2, default 0.65, per-run — both columns use the one value so A/B compares models, not sampling noise), then side-by-side zero-shot (base model) and fine-tuned columns generating the same text for A/B listening. Polling for a generating preview is bounded (~10 min) and surfaces a timeout error pointing at the failed-job alerts.

   On XTTS deployments the guided flow's terminal **Train** card links to the pipeline's Train row (**Go to training →**) rather than embedding the controls; on non-XTTS deployments the flow ends at Export and the Models section is absent entirely.

(There is no top-level Review section: review settings, segment counts, approved duration, and the Open review link all live on the pipeline's Review row; the export flow lives on the Clean & package row. Project settings are therefore per-step disclosures throughout.)

The dashboard is the place for pipeline operations and error recovery. The review queue is for segment decisions only.

#### Terminology

| Internal | User sees |
|---|---|
| `uploaded` / `extracting` / `extraction_failed` | Uploaded / Extracting audio / Extraction failed |
| `separation_pending` / `separation_running` / `separation_failed` | Queued / Separating vocals / Vocal separation failed |
| `diarisation_pending` / `diarisation_running` / `diarisation_failed` | Waiting for speaker / Matching speaker / Speaker matching failed |
| `complete` (source) | Processed |
| `awaiting_reference` (project) | Needs speaker |
| `vocal_separation` / `diarisation` / `scout_speakers` (jobs) | Separating vocals / Matching speaker / Scanning for speakers |
| `transcription_bulk` / `export` (jobs) | Transcribing segments / Exporting dataset |
| `tuning_preview` (job) | Testing cleanup settings |

#### Set reference panel

Rendered inside the next-action card for the whole **Speaker** stage (no reference set). It is a state machine over the pre-reference phase, deriving its phase from source status and jobs — no client-side "which path did the user pick" flag, since the backend status alone determines the phase:

1. **Preparing** — sources still `uploaded` / `extracting`. "Getting your video ready" holding message.
2. **Prompt** — a source is `separation_pending` (audio extracted, nothing running). Heads **"Whose voice are we after?"** with two choices:
   - **Find speakers** → `POST /projects/{id}/pipeline/start`. Separation runs *under the Speaker stage* (no reference yet), so the strip stays on Speaker.
   - **Upload a clip** → file picker → `POST /projects/{id}/reference`. Sets a reference, so the strip advances to **Separate** (where **Start processing** runs separation straight through — diarisation is not gated when a reference exists).
3. **Separating** — a `vocal_separation` job is running (or a source is `separation_running`) with no reference. "Finding the speakers" progress. This is the find-speakers path mid-separation.
4. **Failed** — an `extraction_failed` / `separation_failed` source with no reference. Points at the failed-job alerts for retry.
5. **Scan and pick** — a source has a ready vocals stem (`diarisation_pending`). The panel **auto-scans** the first ready source: on mount it fetches `GET /projects/{id}/reference/scout`; if none has run (`no_scout`), it fires `POST /projects/{id}/reference/scout` once and polls for progress. On completion, a list of speaker cards sorted by talk time (`total_secs`) descending, each with stats (talk time, segment count) and a live **reference length** (the assembled reference the card will produce).
   - **Use this voice** on a card → `POST /projects/{id}/reference/scout/select` with the card's `excluded_indices`. Disabled while the live reference length is under 5 seconds. On success the reference is set, so the strip advances to **Match** (where **Continue processing** runs speaker matching).
   - Every card carries a **Preview** control that plays the assembled-reference montage the speaker would produce (`GET .../reference/scout/preview/{speaker_label}`, `preload="none"`) — audition a candidate without expanding. Its src carries the card's current `excluded_indices`, so it tracks curation; it is hidden only when every turn is excluded (nothing to play).
   - **Choose segments** on a card expands its curation pool: each pool turn shows its state (**In reference** / **Backup** / **Excluded**), duration, a play control (streams the turn's `sample_url`), and a **Not this speaker** toggle. Excluding a wrong-voice turn recomputes the reference live — the next-longest kept turn backfills toward the 30 s cap, and the Preview updates to match. Leaving a card untouched selects the default montage, unchanged from before.
   - An **Advanced** drawer carries **Expected number of speakers** (optional). Setting it and hitting **Scan again** re-runs the scout with `expected_speaker_count`, forcing pyannote to that exact count — the fix for a cluster that merged two people. A **Scan again** link re-runs the scout after a complete or failed scan.

There is no separate reference-upload tab or in-panel Continue button: the prompt is the only entry point, and continuing the pipeline lives in the pipeline stages (Separate/Match). Selecting a voice or uploading a clip advances the strip there rather than auto-continuing, so the user commits the pipeline run explicitly.

The canvas timeline component (see [Timeline component](#timeline-component)) is deliberately **not** reused here — it's built for segment-review density. The speaker picker is a simple list of cards with audio players.

---

### 3. Review queue (`/projects/{id}/review`)

The primary workspace. Full-page layout.

---

## Review queue layout

```
┌─────────────────────────────────────────────────────────────┐
│ Header: project name | approved Xm Ys / target | [Export]  │
├──────────────────┬──────────────────────────────────────────┤
│                  │                                          │
│   Segment list   │           Segment detail                 │
│   (left panel)   │           (right panel)                  │
│                  │                                          │
│  [filters/sort]  │  Waveform / spectrogram toggle           │
│                  │  Audio controls                          │
│  segment card    │  Transcript + edit                       │
│  segment card    │  Confidence scores                       │
│  segment card    │  Source + timestamp                      │
│  ...             │  Action buttons                          │
│                  │                                          │
└──────────────────┴──────────────────────────────────────────┘
```

The list and detail panels are always visible on desktop. Selecting a segment in the list loads it in the detail panel. On narrow screens, they stack vertically.

---

## Segment list panel

### Segment cards

Each card shows:

- Match confidence score (colour-coded: green ≥ 0.90, amber 0.75–0.89, red < 0.75)
- Duration in seconds
- First ~60 characters of transcript (or placeholder if not yet transcribed)
- Status indicator (dot: pending grey, approved green, auto-approved teal, rejected red, maybe amber)
- Source filename abbreviated (e.g. `s01e01`)

Cards are tightly packed. No waveform in the list — that lives in the detail panel.

### Filter bar

Above the list. Controls:

| Control | Options |
|---------|---------|
| Status | All / Pending / Maybe / Approved / Auto-approved / Rejected / Below threshold |
| Source | All / individual source files |
| Min confidence | Slider, 0.00–1.00, default 0.75 |
| Sort | Confidence ↓ (default) / Confidence ↑ / Uncertainty (most borderline first) / Duration ↓ / Duration ↑ / Source order |
| Min duration | Seconds input, default blank |

Filter state persists in the URL query string so the user can bookmark or share a filtered view.

### Pagination

50 segments per page. Page controls at bottom of list. The keyboard navigation wraps from the last segment on a page to the next page automatically.

---

## Segment detail panel

### Waveform

Canvas-rendered waveform of the segment audio. Coloured playhead that moves during playback. Click to seek.

Spectrogram toggle button replaces the waveform with a spectrogram view. State persists across segments during a session (if the user switched to spectrogram, the next segment also shows spectrogram).

### Audio controls

- Play / pause (Space)
- Restart from beginning (R)
- Playback speed: 0.75× / 1× / 1.25× / 1.5× (keyboard: `[` and `]`)

Audio plays automatically when a segment loads if auto-play is enabled (off by default, toggleable in the header).

### Transcript area

Displays the effective transcript (`transcript_edited` if present, else `transcript`). Transcript confidence shown as a secondary score beneath the text.

Clicking the transcript text activates inline editing. The edited value is saved to `transcript_edited` on blur or Enter. Escape discards the edit. An "undo edit" button appears when `transcript_edited` is set, which clears it and restores the original.

If the segment has not been transcribed, shows: `Transcript pending` in muted text.

### Confidence scores

Two scores displayed:

- **Speaker match:** `0.91` with label "Speaker match" and colour coding (same thresholds as the list card)
- **Transcript:** `0.88` with label "Transcript confidence" (only shown once transcription is complete)

Both scores link to a tooltip explaining what they mean and how they're calculated. When the segment carries a `speaker_match_confidence` (the cluster-level score — see [Data Models](data-models.md)), it is shown as a secondary line ("Cluster score: 0.42") beneath the speaker match. Nothing bigger — it's a secondary signal.

### Source info

- Filename: `s01e01.mkv`
- Timestamp: `02:22:11 – 02:22:16` (HH:MM:SS format)
- Duration: `4.6s`

### Flags

If the segment has `flags` set (JSON array from the database), display them as small informational badges below the source info. Current flags:
- `short_transcript` — "Short segment: transcript confidence may be unreliable"
- `cleanup_error: ...` — shown only on auto-rejected segments; the cleanup error message

Flags are informational, not actionable. They help the user make review decisions.

### Action buttons

Three primary actions, always visible:

```
[ Approve ]   [ Maybe ]   [ Reject ]
```

Keyboard: `A` approve, `M` maybe, `X` reject.

After any action, focus moves automatically to the next segment in the list. The previous segment's card updates its status indicator in place without re-rendering the list.

If the segment's status is `auto_approved`, a teal "Auto-approved" chip is shown above the action buttons with a tooltip: "Approved automatically — speaker match and transcript confidence both cleared the project's auto-approve thresholds. Approve to confirm, or override." The `A` key (and Approve button) confirms it to `approved`; Maybe/Reject demote it as usual.

If the segment's status is `rejected`, the three primary actions are replaced with a single "Un-reject" button (restores the segment to `pending`) — button only, no keyboard shortcut, to avoid a second misclick undoing the first.

If the segment has a `clipping_warning` (the boolean column, not the status), the Approve button shows a warning icon and a tooltip: "This segment was flagged for clipping during cleanup. It may contain audio distortion." This warning persists even after re-approval — it's a fact about the audio, not a workflow state. The `clipping_warning` status puts the segment back in the review queue; the `clipping_warning` column keeps the icon visible regardless of status.

---

## Keyboard model

All keys active when the detail panel has focus (i.e. a segment is loaded). No modifier keys required for primary actions.

| Key | Action |
|-----|--------|
| `Space` | Play / pause |
| `R` | Restart playback |
| `A` | Approve and advance |
| `M` | Maybe and advance |
| `X` | Reject and advance |
| `J` | Next segment (without acting) |
| `K` | Previous segment |
| `E` | Focus transcript edit field |
| `Escape` | Blur transcript edit / cancel |
| `[` | Decrease playback speed |
| `]` | Increase playback speed |
| `?` | Show keyboard shortcut overlay |

"Advance" means: move to the next segment in the current filtered list, loading it in the detail panel.

When the transcript edit field is focused, all keys except `Escape` and `Enter` pass through to the text input. `Enter` saves and returns focus to the panel. `Escape` cancels and returns focus.

---

## Bulk operations

Accessible from a "Bulk actions" button above the segment list. Opens an inline panel (not a modal) with:

**Preset operations:**
- Confirm all auto-approved (auto-approved → approved)
- Approve all pending with confidence ≥ 0.90
- Approve all pending with confidence ≥ 0.85
- Reject all pending under 1.5 seconds
- Reject all pending under 2.0 seconds
- Move all maybe → pending (reset deferred decisions)

**Custom operation:**

```
Action:      [ Approve ▾ ]
Status:      [ Pending  ▾ ]   [ Maybe ▾ ]
Confidence:  ≥ [ 0.80 ]
Duration:    ≥ [ 2.0 ] seconds
Source:      [ All sources ▾ ]

[ Preview: affects 412 segments ]   [ Apply ]
```

The preview count updates live as the user adjusts filters. It calls `GET /segments` with a `count_only=true` parameter (returns just the total, no segment data). The Apply button calls `POST /segments/bulk`.

The preview must count what the action will actually touch: the bulk endpoint intersects the filter with the statuses the action may transition from (per the segment transition rules — e.g. `approve` never touches `rejected` segments), so the UI intersects the selected status filter with the chosen action's allowed set **before** requesting the count. When the intersection is empty, Apply is disabled with a hint (e.g. "Approve doesn't apply to rejected segments") rather than promising zero-effect work.

After a bulk operation, the segment list refreshes and the summary stats in the header update.

---

## Timeline component

A horizontal timeline strip beneath the filter bar, spanning the full width of the list panel. Renders all segments for the current source file (or all sources if "All" is selected) as coloured bars on a time axis.

Colour coding matches segment status: green approved, teal auto-approved, red rejected, amber maybe, grey pending, light grey below threshold.

Clicking a bar in the timeline selects that segment. The timeline is for navigation and orientation — seeing where approved segments cluster, identifying gaps — not for editing.

At full season scale (10+ hours of source audio, 1,500+ segments), the timeline renders using a canvas element. Segments narrower than 2px at the current scale are rendered as single-pixel marks. Zoom controls (scroll wheel or pinch) adjust the visible range.

This is the component identified in the brainstorm as a candidate for a future Rust/WASM implementation. The v1 implementation is TypeScript/React/canvas. The interface between the timeline component and the rest of the UI is a defined prop contract so the implementation can be replaced without touching the surrounding page.

**Timeline component props:**

```typescript
interface TimelineProps {
  segments: TimelineSegment[];       // id, start_secs, end_secs, status
  totalDuration: number;             // seconds
  selectedSegmentId: string | null;
  onSegmentSelect: (id: string) => void;
  visibleRange?: [number, number];   // seconds, optional zoom
}
```

---

## Export flow

The "Export" button in the header is always visible. Its state:

- **Greyed out:** No approved segments yet
- **Active:** One or more approved segments; shows approved count and duration
- **Running:** Export job in progress; shows spinner and progress
- **Complete:** Shows "Download" link

Export includes segments in `approved` and `auto_approved` status. Clicking Export (when active) shows a confirmation panel:

```
Export dataset

1,145 segments (743 approved · 402 auto-approved) · 1h 20m 21s of audio

  Segments with clipping warnings: 3
  Segments without transcripts: 0

This will clean and normalise all approved segments.
The previous export (if any) will be replaced.

[ Cancel ]   [ Export ]
```

The clipping warning count is a yellow caution; if non-zero it links to a filtered view of those segments. The user can choose to review and reject them before exporting, or proceed.

After export completes, the confirmation panel is replaced by a download button.

---

## Empty and edge states

**No segments in queue:** "No segments match the current filters. Try widening the confidence threshold or changing the status filter."

**All segments reviewed:** "You've reviewed all segments in this filter. X approved, Y rejected, Z in Maybe." with a link to view the maybe pile.

**Low coverage warning (shown on dashboard, surfaced in queue header):** "Some source files have low target speaker coverage. Check the dashboard for details. Your dataset may be thinner than expected."

**Transcription still running:** Segments show "Transcript pending" in the detail panel. A banner at the top of the queue: "Transcription in progress — X segments remaining."

**No transcript on an approved segment at export time:** Logged as a warning in the export confirmation. The manifest will exclude that segment unless the user adds a transcript manually.

---

## What the UI does not do (v1)

- Video playback (the timeline and audio player are audio-only)
- Waveform scrubbing with word-level transcript alignment
- Side-by-side comparison of original vs generated audio (v1.5)
- Drag-to-trim segment boundaries
- Keyboard-accessible bulk operations (bulk panel is mouse/click only in v1)
