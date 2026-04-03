# FlipSync Specification

This directory contains the full technical specification for FlipSync. It lives in the repo, versioned alongside the code. When a decision changes, update the spec first.

## Documents

| File | Contents |
|------|----------|
| `README.md` | This file. Orientation and index. |
| `overview.md` | What FlipSync is, goals, constraints, scope. |
| `architecture.md` | System design, services, data flow. |
| `pipeline.md` | Per-step processing detail. |
| `api-contracts.md` | Service interfaces and message schemas. |
| `data-models.md` | State schema, segment lifecycle, persistence. |
| `review-ui.md` | UI behaviour, keyboard model, component spec. |
| `deployment.md` | Docker, GPU, configuration. |
| `adr/` | Architecture Decision Records. |

## How to read this spec

Start with `overview.md`. It defines the goals and constraints that govern every other decision. If you're implementing a service, read `architecture.md` and `api-contracts.md`. If you're working on the frontend, read `review-ui.md` and `data-models.md`.

## Status

The spec is ahead of the code. Sections marked `[DRAFT]` are complete enough to build from but may still change. Sections marked `[OPEN]` contain unresolved decisions documented as explicit questions.

## Contributing

Open a PR against the spec before opening one against the code. API contracts and data models must be updated in the spec before the implementation diverges.
