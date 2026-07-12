# Overview

**Status:** DRAFT  
**Last updated:** 2026-04-03

---

## What FlipSync is

FlipSync extracts clean, speaker-specific dialogue audio from video files and produces datasets ready for voice cloning. You give it a video collection and identify your target speaker — either by uploading a short reference clip, or by picking the speaker from the voices FlipSync finds in your own footage. It runs a pipeline of vocal separation, diarisation, speaker matching, transcription, and audio cleanup. You review the segments in a browser UI, approve what's usable, and export a labelled WAV archive with a training manifest.

The pipeline is modular. Any step can be re-run in isolation if the output isn't good enough. The UI shows you confidence scores and transcriptions so you can make informed approval decisions, not just listen to clips in the dark.

The immediate use case is extracting a TV character's dialogue across multiple seasons to clone their voice for a home assistant. The tool is general enough to serve anyone who needs a speaker-specific audio corpus.

---

## Goals

**G1 — Accessible to non-technical users.**  
No command line required. A user who can run Docker and drag files into a browser should be able to produce a usable dataset.

**G2 — Iterative, not linear.**  
Processing steps produce reviewable intermediate output. Any step can be re-run with different parameters. Approvals survive re-processing.

**G3 — Stateless browser, stateful server.**  
All processing state lives server-side. The browser is a view layer. Progress survives restarts, closed tabs, and network interruptions.

**G4 — Self-hosted, GPU-first.**  
Designed for a local machine with an Nvidia GPU. Not cloud-dependent. GPU is a hard requirement for vocal separation and diarisation; transcription benefits from it.

**G5 — Output is immediately usable.**  
The export format is XTTS-v2 compatible without post-processing. A user should be able to take the export directly into a training run.

**G6 — Open and extensible.**  
Apache 2.0. The spec is public and in the repo. API contracts are explicit so contributors can work on services independently.

---

## Non-goals (v1)

These are not in scope for v1 and should not influence v1 design decisions.

- Multi-speaker extraction in a single project
- Video playback in the review UI
- In-app XTTS-v2 synthesis or fine-tuning
- Cloud or hosted deployment
- A public community speaker profile library

They are documented in the roadmap section of `architecture.md`.

---

## Constraints

**C1 — Nvidia GPU required.**  
Demucs and pyannote require CUDA. The deployment target is a machine with an Nvidia card and the Nvidia Container Runtime installed. CPU-only fallback is out of scope.

**C2 — Long audio files.**  
A full TV season can be 10+ hours of audio across 10–13 files. The pipeline must handle this without requiring the user to babysit it. GPU memory limits on Demucs are a known risk — see [Open Questions](#open-questions).

**C3 — Low target speaker coverage.**  
For minor characters, the target speaker may appear in only a fraction of the audio. The pipeline and UI must degrade gracefully: surface low-coverage warnings, don't silently produce a thin dataset.

**C4 — Source material quality varies.**  
TV audio includes music, effects, overlapping dialogue, and varying recording quality across seasons and productions. The pipeline cannot assume clean input.

**C5 — No internet required at runtime.**  
Once deployed, FlipSync must operate fully offline. All models are local. No external API calls during processing.

---

## Licensing

FlipSync is Apache 2.0.

Dependencies:

| Dependency | License | Notes |
|------------|---------|-------|
| Demucs | MIT | No restrictions |
| pyannote.audio | MIT | No restrictions |
| faster-whisper | MIT | No restrictions |
| FFmpeg | LGPL/GPL | Called as a subprocess; no licence conflict with Apache 2.0 |
| XTTS-v2 | CPML | Coqui Public Model Licence; review before any commercial use |

XTTS-v2 is a v1.5+ dependency. Its CPML licence requires review before FlipSync enables it in any context that could be considered commercial. This is documented as an open question until v1.5 planning begins.

---

## Ethics and positioning

FlipSync is neutral infrastructure. It automates steps that were previously manual or required specialist tooling, but it doesn't unlock any capability that didn't already exist.

The README leads with the voice banking use case: extracting your own voice before a degenerative illness takes it. This is a genuine, meaningful application and the right tone for the project's public face.

The documentation assumes the user has rights to their source material. FlipSync does not and will not police how users source video files.

---

## Open questions

| ID | Question | Blocking |
|----|----------|---------|
| OQ-01 | What are the practical GPU memory limits for Demucs on 40–60 minute audio files? Auto-chunking on OOM is implemented in the spec, but real-world chunk size tuning needs testing. | Deployment, pipeline |
| OQ-02 | Does pyannote support iterative speaker fingerprint refinement natively, or does cosine similarity re-matching need to be implemented separately? | Pipeline step 2 |
| OQ-04 | When does the repo go public? Lean towards early with a clear "building in the open" README. | Project |

**Resolved questions are moved to the relevant ADR.** See `adr/`. OQ-03 (database vs filesystem) resolved: SQLite. See ADR-001.

---

## Roadmap summary

| Version | Scope |
|---------|-------|
| v1 | Single speaker, full pipeline, review UI, export |
| v1.5 | XTTS-v2 integration: zero-shot preview, fine-tuning trigger, quality comparison |
| v2 | Multi-speaker extraction, scene-based Demucs slicing, community speaker profiles |
| v3+ | Hosted / cloud version (TBD) |
