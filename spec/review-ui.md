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

Five user-facing stages: **Upload → Speaker → Process → Review → Export**. A pure function `deriveStage(project)` maps the polled project response to the single current stage, by precedence:

1. No sources → **Upload**
2. **No reference set → Speaker.** `reference_path` is the Speaker/Process divider. The "whose voice?" prompt is the only trigger for separation, and the upload-a-clip path sets a reference *before* separation runs — so a project with sources but no reference is always still choosing a voice: the prompt, the separation run that feeds the scan, the scout scan, or picking a candidate all live in Speaker. This keeps the strip monotonic (separation no longer flips to Process ahead of the Speaker step).
3. Reference set, active jobs other than `export` and `scout_speakers` → **Process**
4. Reference set, any source in `uploaded`, `extracting`, `separation_pending`, `separation_running`, `diarisation_pending`, `diarisation_running`, or a failed state (`extraction_failed`, `separation_failed`, `diarisation_failed`) → **Process**
5. `pending_count + maybe_count > 0` → **Review**
6. Otherwise → **Export**

Scout jobs belong to the Speaker stage and export jobs to the Export stage, so neither flips the strip to Process. Stages are not strictly linear — adding a source mid-review (a reference already exists) returns the project to Process; that is expected.

#### Page layout (top to bottom)

1. **Header** — project name, settings gear (expands and scrolls to the Settings section), theme toggle. No review-queue button; review is reached via the stage strip and action card.
2. **Stage strip** — the five stages, each rendered as done (✓) / active (pulsing, jobs running) / needs-you (amber) / upcoming (dimmed). The Review chip links to the review queue once segments exist.
3. **Next-action card** — one card, fixed slot, reserved min-height (no layout shift on the 3 s poll); content cross-fades on stage change:
   - *Upload*: full drag-and-drop upload area.
   - *Speaker*: the Set reference panel (below) — a small state machine over the pre-reference phase.
   - *Process*: active job progress with human labels; or a **Start processing** button (uploaded-clip source queued, nothing running); or **Continue processing** (reference set at the gate); or a "processing stopped" notice pointing at the failed-job alerts.
   - *Review*: "N segments ready to review" with a **Start reviewing →** link and a secondary **Transcribe segments** button.
   - *Export*: export confirmation/summary and download (export flow below).
4. **Failed-job alerts** — only when present, own slot, error messages with retry/dismiss (sourced from `recent_failed_jobs`). Shown until dismissed or retried. Retry is job-type-aware:
   - `vocal_separation` / `diarisation` — retry goes through the same confirm flow as a manual reprocess: submit **without** `confirm`, and if the API returns 409 `would_invalidate_approvals`, surface the confirmation dialog. Never pre-confirm.
   - `scout_speakers` — retry re-runs the scout for the same source (`POST /reference/scout`), not a source reprocess.
   - `extract_audio` — no Retry button; extraction failure is terminal by design. The alert shows guidance instead: "Extraction failed — remove this video and re-upload it."
   - `transcription_segment` — retry calls the per-segment rerun endpoint when the failed job carries a segment id; if it doesn't, no Retry is shown (never a silent no-op).
5. **Videos** (sources) — filename, human status label, speaker coverage (— until diarisation completes), per-row ⋯ menu with **Re-run from vocal separation** (`steps: ["separation", "diarisation"]`) and **Re-run speaker matching** (`steps: ["diarisation"]`), plus an inline compact **+ Add video** upload button. Hidden while the project has no sources.
6. **Segments** (stats) — compact grid: approved / auto-approved / pending / maybe / rejected / below threshold counts, approved duration vs target (duration includes auto-approved). Hidden until segments exist.
7. **Settings** — collapsed disclosure: match threshold, auto-approve toggle and its two thresholds (`auto_approve_match_threshold`, `auto_approve_transcript_threshold`). Saving calls `PATCH /projects` and refreshes stats — threshold changes re-evaluate segment statuses synchronously, so counts move immediately.

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

#### Set reference panel

Rendered inside the next-action card for the whole **Speaker** stage (no reference set). It is a state machine over the pre-reference phase, deriving its phase from source status and jobs — no client-side "which path did the user pick" flag, since the backend status alone determines the phase:

1. **Preparing** — sources still `uploaded` / `extracting`. "Getting your video ready" holding message.
2. **Prompt** — a source is `separation_pending` (audio extracted, nothing running). Heads **"Whose voice are we after?"** with two choices:
   - **Find speakers** → `POST /projects/{id}/pipeline/start`. Separation runs *under the Speaker stage* (no reference yet), so the strip stays on Speaker.
   - **Upload a clip** → file picker → `POST /projects/{id}/reference`. Sets a reference, so the strip advances to **Process** (where **Start processing** runs separation straight through — diarisation is not gated when a reference exists).
3. **Separating** — a `vocal_separation` job is running (or a source is `separation_running`) with no reference. "Finding the speakers" progress. This is the find-speakers path mid-separation.
4. **Failed** — an `extraction_failed` / `separation_failed` source with no reference. Points at the failed-job alerts for retry.
5. **Scan and pick** — a source has a ready vocals stem (`diarisation_pending`). The panel **auto-scans** the first ready source: on mount it fetches `GET /projects/{id}/reference/scout`; if none has run (`no_scout`), it fires `POST /projects/{id}/reference/scout` once and polls for progress. On completion, a list of speaker cards sorted by talk time (`total_secs`) descending, each with stats (talk time, segment count) and a live **reference length** (the assembled reference the card will produce).
   - **Use this voice** on a card → `POST /projects/{id}/reference/scout/select` with the card's `excluded_indices`. Disabled while the live reference length is under 5 seconds. On success the reference is set, so the strip advances to **Process** (where **Continue processing** runs speaker matching).
   - Every card carries a **Preview** control that plays the assembled-reference montage the speaker would produce (`GET .../reference/scout/preview/{speaker_label}`, `preload="none"`) — audition a candidate without expanding. Its src carries the card's current `excluded_indices`, so it tracks curation; it is hidden only when every turn is excluded (nothing to play).
   - **Choose segments** on a card expands its curation pool: each pool turn shows its state (**In reference** / **Backup** / **Excluded**), duration, a play control (streams the turn's `sample_url`), and a **Not this speaker** toggle. Excluding a wrong-voice turn recomputes the reference live — the next-longest kept turn backfills toward the 30 s cap, and the Preview updates to match. Leaving a card untouched selects the default montage, unchanged from before.
   - An **Advanced** drawer carries **Expected number of speakers** (optional). Setting it and hitting **Scan again** re-runs the scout with `expected_speaker_count`, forcing pyannote to that exact count — the fix for a cluster that merged two people. A **Scan again** link re-runs the scout after a complete or failed scan.

There is no separate reference-upload tab or in-panel Continue button: the prompt is the only entry point, and continuing the pipeline lives in the Process stage. Selecting a voice or uploading a clip advances the strip to Process rather than auto-continuing, so the user commits the pipeline run explicitly.

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
