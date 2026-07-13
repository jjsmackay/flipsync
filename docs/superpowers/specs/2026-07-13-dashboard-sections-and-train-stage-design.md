# Dashboard sections rework + Train terminal stage — design

Date: 2026-07-13
Scope: `frontend/` (primary) + one small `services/orchestrator/` addition.

## Context

Five changes to the project dashboard (`ProjectDashboardPage`) and the stage
model:

1. Dismissed failed-job alerts reappear after a page reload.
2. Dashboard sections should be collapsible.
3. Review and Export should be reachable as buttons under the segments section.
4. Rename sections: "Videos" → "Sources", "Segments" → "Review".
5. With XTTS enabled, Export is no longer the last step — the terminal stage is
   Train.

XTTS is a deployment-time opt-in (`docker-compose` `profiles: ["xtts"]`). There
is no per-project "xtts enabled" flag today; the frontend shows the Voice
section whenever segments exist and only discovers XTTS is absent when a train
request returns `503 xtts_unavailable`. So (5) needs a real enabled signal.

## A. Failed-job dismissal persists across reload

`FailedJobsPanel` stores dismissed ids in component state only; the API keeps
returning the same `recent_failed_jobs`, so a reload re-shows them.

- Persist dismissed job ids to `localStorage` under `flipsync:dismissedFailedJobs`
  (JSON array of strings).
- Seed `dismissedIds` from storage on mount.
- On dismiss, add the id and write through.
- Prune: whenever `failedJobs` changes, drop stored ids no longer present in
  `recent_failed_jobs` and write back, so the list can't grow unbounded (a
  retried/resolved job stops being returned by the API).

This is UI dismissal state, not segment state — the CLAUDE.md "no localStorage
for segment state" rule does not apply.

## B. Collapsible sections

New reusable `frontend/src/components/ui/CollapsibleSection.tsx`, generalising
the existing inline Settings collapse (chevron `▸` + uppercase heading).

Props: `title`, `sectionKey` (persistence key), `defaultOpen`, `children`, and a
forwarded imperative handle exposing `{ open(): void; el: HTMLElement | null }`
so the parent can open + scroll to a section programmatically (gear → Settings,
"Go to training" → Voice).

Open state:
- On mount, read `localStorage["flipsync:section:" + sectionKey]` (`"open"` /
  `"closed"`). If present it wins; otherwise use `defaultOpen`.
- Toggling writes the explicit choice to `localStorage` (per section, global
  across projects — simplest; explicit user choice persists).

Smart default (the `defaultOpen` the parent passes) is computed from
`deriveStage(project, xttsEnabled)` at mount: expand the section matching the
current stage, collapse the rest.

| Stage | Expanded section |
|-------|------------------|
| upload / speaker / process | Sources |
| review | Review |
| export | Review |
| train | Voice |

Smart default applies at mount only (not reactive to later stage changes on the
same page); an explicit toggle persists and always wins. Applied to Sources,
Review, Voice, and Settings.

## C. Rename + restructure

- "Videos" → **Sources** (section title only).
- "Segments" section becomes the **Review** section. **Segments** is no longer a
  top-level section — it becomes a *subheading* inside Review, one tier down
  (the same `h3` subsection pattern the Voice section uses for
  Train/Models/Preview), wrapping the existing `StatsPanel`.

The `hasSources` / `hasSegments` gates are unchanged; `hasSegments` now gates the
whole Review section.

## D. Review section contents

The **Review** section (inside the `hasSegments` gate) contains, top to bottom:

1. **Segments** subheading (`h3`) → the existing `<StatsPanel …/>`.
2. A button row beneath it:
   - **Open review** — `Link` to `/projects/:id/review`.
   - **Export** — the existing `<ExportButton project=… />`.

The button row is additive; the stage strip's review chip and the guided card
keep their existing links.

## E. XTTS-enabled signal + Train terminal stage

### Backend: capabilities endpoint

New `GET /capabilities` on the orchestrator returning `{ "xtts": <bool> }`,
where the value is `service_client.is_healthy("xtts")` (existing 3 s
point-in-time probe). No auth (consistent with the rest of the app).

Rationale: fetched once per dashboard load, deliberately *not* folded into the
3 s `GET /projects/{id}` poll — a down or absent XTTS would otherwise add probe
latency to every poll.

### Frontend: consume it

- `getCapabilities()` in `api/client.ts` → `{ xtts: boolean }`.
- Dashboard fetches it once on mount into `xttsEnabled` (default `false` until
  resolved). Memoise a resolved `true` at module scope so a later transient
  probe failure doesn't flip an enabled deployment back to Export.

### stage.ts

- Add `'train'` to `Stage`; `STAGE_LABELS.train = 'Train'`.
- Replace the single `STAGES` array with `stagesFor(xttsEnabled)`:
  - disabled → `['upload','speaker','process','review','export']`
  - enabled  → `['upload','speaker','process','review','train']`
- `deriveStage(project, xttsEnabled = false)`: only the final
  `return 'export'` changes to `return xttsEnabled ? 'train' : 'export'`. The
  two review-gating branches are untouched. Default arg keeps existing callers
  and tests valid.
- `stageStates(project, xttsEnabled = false)` uses `stagesFor(xttsEnabled)`.

### StageStrip

- Takes `xttsEnabled` and `onGoToVoice` props.
- Renders `stagesFor(xttsEnabled)`.
- The `train` chip is clickable and calls `onGoToVoice` (open + scroll the Voice
  section) — mirroring how the `review` chip links to the review queue.

### NextActionCard

- Takes `xttsEnabled` and `onGoToVoice`; uses `deriveStage(project, xttsEnabled)`.
- New `TrainStage` rendered when `stage === 'train'`: a thin guide card
  ("Ready to train your voice" / approved-audio blurb) with a **Go to training →**
  button that calls `onGoToVoice`. It does *not* duplicate `TrainPanel`; the real
  controls stay in the Voice section. This is the deliberate reversal of the
  earlier "Voice stays out of the guided card" decision, per this change.
- `ExportStage` is unreachable when XTTS is enabled (no `export` stage); it stays
  for the disabled case. Export remains reachable via the Review-section button
  (D) in both cases.

### ProjectDashboardPage wiring

- Fetch capabilities → `xttsEnabled`.
- Hold `CollapsibleSection` refs for Voice and Settings.
- `onGoToVoice` = `voiceRef.open()` + `voiceRef.el.scrollIntoView(...)`.
- Gear button reuses the same pattern against the Settings ref.
- Pass `xttsEnabled` + `onGoToVoice` to `StageStrip` and `NextActionCard`.

## Error handling

- `getCapabilities()` failure → treat as `xtts: false` (degrade to Export
  terminal stage); no error surfaced. Train still guarded server-side by the
  `503 xtts_unavailable` path already handled in `TrainPanel`.
- `localStorage` access wrapped in try/catch (private-mode / disabled storage) —
  fall back to in-memory behaviour (current behaviour) on failure.

## Testing

- `stage.test.ts`: `deriveStage(project, true)` returns `train` at the terminal;
  `stagesFor(true|false)`; `stageStates` with the train set.
- `CollapsibleSection` test: default-open honoured, localStorage override wins,
  toggle writes through, imperative `open()`.
- `FailedJobsPanel` test: seeds from localStorage, persists dismissal across
  remount, prunes ids absent from `failedJobs`.
- Orchestrator: `GET /capabilities` returns `{ "xtts": bool }` (mock
  `is_healthy` both ways).
- `pnpm build` + `tsc` clean; orchestrator suite green.

## Out of scope

- Marking Train "done" when a ready model exists (train stays the terminal
  needs-you/active chip). Possible follow-up.
- Any change to the XTTS service or the train/preview APIs themselves.
