# Selectable dark mode (default: system)

Date: 2026-07-13
Scope: `frontend/` only

## Goal

Add a user-selectable theme (Light / Dark / System) to the FlipSync frontend, defaulting to System, applied consistently across all pages and components.

## Mechanism

Tailwind v4 has no `darkMode` config key — `dark:` variants normally follow the OS media query only. To support a manual override, add to `frontend/src/index.css`:

```css
@import "tailwindcss";
@custom-variant dark (&:where(.dark, .dark *));
```

`dark:` variants then activate whenever `<html>` carries class `dark`. The app controls that class directly instead of relying purely on the media query.

## Theme state

- `ThemeProvider` (React context, `frontend/src/hooks/useTheme.tsx` or similar) holds `mode: 'light' | 'dark' | 'system'`.
- Persisted to `localStorage['flipsync-theme']`. Default `'system'` if unset.
  - This is a UI preference, not segment/review data — does not conflict with the project's "stateless browser, no localStorage for segment state" rule (`frontend/CLAUDE.md`), which is scoped to review/segment state coming from the orchestrator API.
- Resolves `mode` to an effective `light` | `dark`:
  - `'light'` → light
  - `'dark'` → dark
  - `'system'` → read `matchMedia('(prefers-color-scheme: dark)').matches`
- Applies/removes the `dark` class on `document.documentElement` whenever the resolved theme changes.
- When `mode === 'system'`, subscribes to the `matchMedia` change event and re-resolves live while the tab is open (no reload needed if the OS theme changes).
- Inline script in `frontend/index.html`, executed before the React app mounts, reads `localStorage['flipsync-theme']` (and `matchMedia` if `system`/unset) and sets the `dark` class synchronously. Prevents a flash of the wrong theme on initial load.

## Toggle component

- New `ThemeToggle` component (`frontend/src/components/ui/ThemeToggle.tsx`).
- Single icon button: sun (light) / moon (dark) / monitor (system) icon reflecting current mode.
- Click cycles Light → Dark → System → Light.
- `title` attribute set to current mode for a native tooltip.
- Placed in the existing header markup of all three pages that currently render their own header row:
  - `ProjectListPage.tsx`
  - `ProjectDashboardPage.tsx`
  - `ReviewQueuePage.tsx`
- No shared layout/header component is introduced — none exists today, and adding one is out of scope for this change.

## Styling rollout

Full-app rollout. Add `dark:` variants across all pages and shared components, including:

- Page shells and headers (all three pages)
- `components/project/*` (CreateProjectModal, JobsPanel, PipelineControls, SourcesTable, StatsPanel, UploadArea)
- `components/review/*` (AudioControls, BulkOperations, FilterBar, KeyboardHelp, SegmentCard, SegmentDetail, Timeline, WaveformCanvas)
- `components/ui/*` (ConfidenceBadge, ProgressBar, StatusBadge) and the new `ThemeToggle`
- `components/export/ExportButton.tsx`

General colour mapping (adjust per-component for contrast/accent needs):

| Light | Dark |
|---|---|
| `bg-white` / `bg-gray-50` | `bg-gray-900` / `bg-gray-800` |
| `text-gray-900` | `text-gray-100` |
| `text-gray-500` / `text-gray-400` | `text-gray-400` / `text-gray-500` |
| `border-gray-200` | `border-gray-700` |

Existing accent colours (blue/green/red for status, actions, stats) get dark-mode-appropriate shade adjustments where used against dark backgrounds (e.g. `bg-blue-600` buttons stay legible as-is; text-on-background combos like `text-green-600` on `bg-gray-900` get checked for contrast and adjusted if needed, e.g. `dark:text-green-400`).

`Timeline.tsx` and `WaveformCanvas.tsx` draw via `<canvas>`, which can't use Tailwind classes. Their draw functions must read colours from the resolved theme (via the theme context/hook) rather than hardcoded hex values, so canvas content matches the surrounding theme.

## Out of scope

- No backend/orchestrator changes.
- No per-project or per-user theme setting stored server-side — this is a local browser preference only.
- No new automated test framework. Manual verification only (see below).

## Testing / verification

Manual, via the `run` skill / dev server:

- Toggle cycles Light → Dark → System correctly and persists across reload.
- With mode = System, changing the OS theme while the tab is open updates the UI live.
- No flash of incorrect theme on initial page load (hard refresh in each mode).
- All three pages, all modals (CreateProjectModal), and the review queue (including Timeline/WaveformCanvas) render correctly in both Light and Dark.
- Existing component tests, if any target these files, aren't broken by the class-name changes (no test hardcodes light-only class names as behavioural assertions).
