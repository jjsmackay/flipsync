# Dashboard UX rework — guided stage flow

**Date:** 2026-07-13
**Status:** Approved
**Scope:** Frontend only (`frontend/`), plus the dashboard section of `spec/review-ui.md`. No API or orchestrator changes. The review queue page is untouched this round.

## Problem

The project dashboard is a flat stack of sections (Jobs, Stats, Set reference, Settings, Pipeline, Sources, Upload) in an order that doesn't match how the tool is used. Internal jargon leaks into the UI: source badges render raw statuses (`step1_pending`, `step2_running`), reprocess buttons say "Step 1+2" / "Step 2", and the primary control is "Start pipeline". Nothing explains what the review queue is or when to use it — it's an unexplained button in the top-right. Sections mount and unmount as the 3-second poll changes state, causing layout jumps.

## Design

### 1. Stage model

The dashboard is organised around five user-facing stages:

**Upload → Speaker → Process → Review → Export**

A pure function `deriveStage(project: ProjectDetail): Stage` maps the existing poll response to one current stage, by precedence:

1. No sources → **Upload**
2. Active jobs (any type except export), or any source still to be processed (`uploaded`, `extracting`, `step1_pending`, `step1_running`, `step2_running`) or in a failed processing state (`extraction_failed`, `step1_failed`, `step2_failed`) → **Process**. Queued-but-not-started shows the "Start processing" button; failed shows the failure alongside the failed-job alerts.
3. A source at `step2_pending` with no active jobs (the reference gate) → **Speaker**
4. Segments awaiting review (`pending` + `maybe` + `clipping_warning` counts > 0) → **Review**
5. Otherwise → **Export** (covers `ready`, `exporting`, `exported`)

Each stage chip in the strip renders one of four states: **done** (✓), **active** (spinner — jobs running), **needs-you** (amber — user input required), **upcoming** (dimmed). The strip is informational plus light navigation: the Review chip links to the review queue once segments exist. No API changes — everything needed is already in `GET /projects/{id}`.

Stages are not strictly linear (users can add sources after reviewing, or reprocess). The precedence rules above always resolve to a single current stage; earlier stages show done when their work is complete, and re-entering an earlier stage (e.g. new upload while reviewing) is expected behaviour.

### 2. Stage strip + next-action card

The strip sits directly under the project title. Below it, one **next-action card** always occupies the same slot with a fixed minimum height. Its content follows the current stage:

| Stage | Card content |
|---|---|
| Upload | Upload dropzone with "Upload a video to get started" |
| Speaker | The existing Set-reference panel (upload a clip, or scan a source and pick a speaker) |
| Process | Active job progress — job label, source filename, progress bar (absorbs today's Jobs panel active-job display). If processing hasn't started and a source is queued, the card shows the "Start processing" button. |
| Review | "N segments ready to review — listen and approve the good ones." with a **Start reviewing →** primary button. Secondary: "Transcribe segments" when untranscribed segments remain. |
| Export | Export confirmation/summary and the existing export flow (reuses `ExportButton`) |

The top-right "Review queue →" header button is removed. Review is reached through its stage card and the Review chip in the strip.

### 3. Terminology

All internal names get a user-facing label map (single source: a `labels.ts` module):

| Internal | User sees |
|---|---|
| step1 / `vocal_separation` | Vocal separation ("Separating vocals…" when active) |
| step2 / `diarisation` | Speaker matching ("Matching speaker…" when active) |
| `uploaded` | Uploaded |
| `extracting` | Extracting audio |
| `step1_pending` | Queued |
| `step1_running` | Separating vocals |
| `step1_failed` | Vocal separation failed |
| `step2_pending` | Waiting for speaker |
| `step2_running` | Matching speaker |
| `step2_failed` | Speaker matching failed |
| `extraction_failed` | Extraction failed |
| `complete` (source) | Processed |
| "Step 1+2" reprocess button | Re-run from vocal separation |
| "Step 2" reprocess button | Re-run speaker matching |
| "Start pipeline" | Start processing |
| "Run transcription" | Transcribe segments |
| `awaiting_reference` (project badge) | needs speaker |
| `transcription_bulk` job | Transcribing segments |

Reprocess actions move into a per-source-row overflow menu (⋯) in the Sources table. Segment review statuses (approved / rejected / maybe / auto-approved / below threshold / clipping) are already plain enough and are out of scope.

### 4. Page layout

Top to bottom:

1. **Header** — project name, settings gear, theme toggle
2. **Stage strip**
3. **Next-action card**
4. **Failed-job alerts** — only when present; retry/dismiss as today
5. **Sources** — table with human status labels, per-row progress when running, ⋯ reprocess menu, and an inline "+ Add video" control. The large dropzone only dominates when the project has no sources (it *is* the next-action card then).
6. **Stats** — compact single row (segment counts, approved duration vs target)
7. **Settings** — collapsed disclosure at the bottom (thresholds, auto-approve banding); also reachable via the header gear (scrolls/expands)

The standalone Pipeline section disappears — starting or continuing processing belongs to the next-action card.

### 5. Polish

- The next-action card slot has a reserved minimum height; content swaps cross-fade. Sections never mount/unmount on poll — they render empty-state or hide via height, keeping layout stable.
- Progress bars update in place (stable React keys per job id).
- Failed-job alerts are the only conditionally-present block, and they sit in their own slot so appearing doesn't reflow the card.

### 6. Components

| Component | Change |
|---|---|
| `StageStrip` | New. Renders five chips from `deriveStage` + per-stage done/active/needs-you/upcoming state. |
| `NextActionCard` | New. Hosts stage-dependent content; composes `UploadArea`, `SetReferencePanel`, job progress, review CTA, `ExportButton`. |
| `deriveStage` | New pure function in `src/utils/stage.ts`. |
| `labels.ts` | New label map module in `src/utils/`. |
| `ProjectDashboardPage` | Restructured to the layout above. |
| `SourcesTable` | Human labels, per-row progress, ⋯ reprocess menu, inline add-video. |
| `StatusBadge` | Uses the label map; styles keyed on internal status as today. |
| `JobsPanel` | Reduced to failed-job alerts; active-job rendering moves into `NextActionCard`. |
| `PipelineControls` | Removed; actions fold into `NextActionCard`. |
| `StatsPanel` | Compacted to a single row. |
| `ProjectListPage` | Project status badges use human labels. |

### 7. Spec update

Rewrite the dashboard section of `spec/review-ui.md` to match this design, including the stage model, precedence rules, and terminology table. The spec remains the source of truth.

### 8. Testing

- Unit tests for `deriveStage` — one per precedence branch, plus the reference-gate vs active-jobs ordering case.
- Component tests for `NextActionCard` — renders the right content per stage.
- Label map test — every source/project status and job type has a human label (no raw snake_case falls through).
- Existing `SetReferencePanel` tests keep passing inside the new card.

## Error handling

Unchanged patterns: API errors surface as inline red text within the card/section that triggered them; the reprocess "would invalidate approvals" confirm dialog is kept as-is, launched from the new ⋯ menu.
