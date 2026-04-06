# Frontend — Agent Scope

You own the frontend (`frontend/`). Do not modify any service directory or the orchestrator.

## Required reading before writing code

1. `spec/review-ui.md` — full document (pages, components, keyboard model, export flow)
2. `spec/api-contracts.md` Part 1 — browser→orchestrator endpoints

## Tech stack

- React + TypeScript
- Vite (dev server + build)
- pnpm (package manager)

## Key constraints

- **Stateless browser.** All state comes from the orchestrator API at `http://localhost:8000`. No localStorage for segment state.
- **Poll `GET /projects/{id}` every 3s when jobs active.** Stop when `active_jobs` is empty. Resume on user pipeline action.
- **Filter state in URL query string.** Bookmarkable filtered views.
- **Keyboard shortcuts only active when detail panel has focus.** When transcript edit is focused, keys pass through to the input except Escape and Enter.
- **Audio: full file download per segment, not streaming.** No Range header support. Segments are small (~1–2 MB).
- **Error responses from the API** are shaped: `{"error": "snake_case", "message": "Human-readable.", "detail": {}}`.

## Shared utilities

- `src/utils/format.ts` — `formatDuration` (h/m/s) and `formatDurationCoarse` (h/m). Use these instead of local helpers.

## Commands

- `pnpm install` — install dependencies
- `pnpm dev` — start dev server on :3000
- `pnpm build` — production build
- `pnpm preview` — preview production build
