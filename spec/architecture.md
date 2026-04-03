# Architecture

**Status:** DRAFT  
**Last updated:** 2026-04-03

---

## Principles

**Modular, not monolithic.** Four services, each responsible for one processing step. They share no code and communicate through the orchestrator. Adding or replacing a service doesn't touch the others.

**The UI orchestrates; services execute.** The frontend never calls a processing service directly. All commands go through the FastAPI orchestrator, which owns the job queue and state transitions.

**Output of each step is a first-class artifact.** Intermediate files (vocals stem, segment WAVs, cleaned WAVs) are stored on disk and addressable. Segment metadata (timestamps, confidence scores, transcripts, review state) lives in SQLite. Any step can be re-run without re-running steps that came before it.

**Processing is async; the browser polls.** Jobs are queued server-side. The browser requests current state and sends actions; it doesn't hold open connections waiting for results.

---

## System diagram

```
┌─────────────────────────────────────────────────────┐
│                     Browser                         │
│              React + TypeScript SPA                 │
└────────────────────┬────────────────────────────────┘
                     │ HTTP (polling + actions)
┌────────────────────▼────────────────────────────────┐
│                  Orchestrator                       │
│               FastAPI + job queue                   │
│           State store (SQLite)                      │
└──┬───────────────┬───────────────┬──────────────┬───┘
   │               │               │              │
   ▼               ▼               ▼              ▼
┌──────┐     ┌──────────┐    ┌──────────┐   ┌──────────┐
│Vocal │     │Diarise + │    │Transcribe│   │ Cleanup  │
│ Sep  │     │  Match   │    │          │   │  + Norm  │
│Demucs│     │pyannote  │    │  faster- │   │  FFmpeg  │
│      │     │+ cosine  │    │  whisper │   │          │
└──────┘     └──────────┘    └──────────┘   └──────────┘
```

All services and the orchestrator run in the same Docker network. Services expose HTTP APIs on internal ports. Only the orchestrator port is exposed to the host.

---

## Services

### Orchestrator

**Technology:** FastAPI (Python)  
**Responsibilities:**
- Accepts file uploads (streamed to disk, not buffered) and stores them in the project working directory
- Manages the job queue: enqueues, tracks, and retries processing jobs
- Calls processing services in sequence and stores their output in SQLite
- Serves current project state to the browser
- Accepts review actions from the browser (approve, reject, maybe, edit transcript)
- Handles export: packages approved segments and writes `manifest.json` from the database

**Does not:**
- Perform any audio or ML processing (except FFmpeg audio extraction, which runs as a subprocess)
- Hold open streaming connections to the browser (polling only, v1)

**Port:** 8000 (host-exposed)

#### Implementation guidance

**CORS:** The orchestrator must add CORS middleware allowing the frontend origin (`http://localhost:3000`). Allow all methods and headers from this origin.

**File uploads:** Source video files are 1–4 GB. Use Starlette's streaming upload (write chunks to disk as they arrive). Do not buffer the entire file in memory. Set no application-level size limit — the filesystem is the constraint.

**Job queue:** The job queue is an in-memory queue backed by the `jobs` table in SQLite. On startup, the orchestrator loads any `queued` or `running` jobs from the database and resumes them. The queue is not a separate library — it is a simple FIFO list managed by the orchestrator. Jobs are executed one at a time per project to avoid GPU contention. The `jobs` table is the persistence layer; the in-memory queue is the execution layer.

**Polling loop:** The orchestrator polls each active processing service every 2 seconds using `asyncio.create_task` background tasks spawned when a job is submitted. Each polling task runs until the job completes or fails, then updates the database and dequeues the next job. FastAPI's async lifecycle (`@app.on_event("startup")` or lifespan context) initialises the job runner. No external scheduler library is needed.

**Service readiness:** On startup, the orchestrator should poll `GET /health` on each processing service until it returns 200 before accepting pipeline jobs. Use a generous timeout (up to 5 minutes) for first-run model downloads. If a service is unreachable when a job is submitted, the job should be queued and retried when the service becomes available.

**Database connections:** Each project has its own SQLite file. The orchestrator opens connections per-project, using `PRAGMA journal_mode=WAL` for concurrent reads during writes. Connections can be kept open for the process lifetime or pooled — the choice is an implementation detail. A project listing endpoint should use a lightweight index (see [Data Models](data-models.md) for options).

---

### Service 1 — Vocal Separation

**Technology:** Python, Demucs  
**Input:** Audio file path (extracted from video by FFmpeg pre-step)  
**Output:** Vocals stem WAV file path  
**Port:** 8001 (internal only)

The orchestrator calls FFmpeg directly (subprocess) to strip audio from video before passing to this service. FFmpeg is not a separate service — it's a tool called by the orchestrator and the cleanup service.

Demucs model variant is configurable. Default: `htdemucs`. The service accepts a model parameter so the orchestrator can retry with a different variant if output quality is poor.

GPU memory limits mean very long audio files may need chunking. v1 attempts whole-file processing first. If Demucs OOMs, the service returns an error with a suggested chunk duration, and the orchestrator retries with chunking enabled. Stitching is handled inside the service before returning the output path.

---

### Service 2 — Diarisation + Speaker Matching

**Technology:** Python, pyannote.audio, scipy (cosine similarity)  
**Input:** Vocals stem WAV path, reference clip path  
**Output:** Segment WAVs on disk + segment metadata returned via HTTP to the orchestrator (see [API Contracts](api-contracts.md))  
**Port:** 8002 (internal only)

Two-phase operation:
1. Diarise the full vocals audio into a speaker timeline (speaker labels are anonymous at this stage: `SPEAKER_00`, `SPEAKER_01`, etc.)
2. Compare each speaker's aggregate embedding against the reference clip embedding using cosine similarity; assign a match confidence score to each segment

The service returns all speakers' segments, not just the matched target. The orchestrator filters by confidence threshold for the initial review queue, but stores everything. This allows the threshold to be adjusted without reprocessing.

Pyannote requires a HuggingFace token to download models on first run. The token is passed via environment variable. After first run, models are cached in a Docker volume and the token is no longer needed.

---

### Service 3 — Transcription

**Technology:** Python, faster-whisper  
**Input:** List of segment audio file paths  
**Output:** Per-segment transcript text and word-level confidence scores  
**Port:** 8003 (internal only)

Transcribes all matched segments upfront, not just high-confidence ones. The transcript and its confidence score are both review signals — a segment where the speaker match is borderline but the transcription is clean and coherent is worth keeping.

Model size is configurable. Default: `large-v2`. Smaller models run faster on limited hardware. The orchestrator passes the model size as a parameter.

Language can be specified or left as auto-detect. Auto-detect works well for single-language sources; specify explicitly for non-English or mixed-language content.

---

### Service 4 — Cleanup and Normalisation

**Technology:** Python, FFmpeg (subprocess)  
**Input:** List of approved segment audio file paths  
**Output:** Cleaned WAV files in the export directory  
**Port:** 8004 (internal only)

Runs only on approved segments, immediately before export. Applies in order:

1. Loudness normalisation (EBU R128, target -23 LUFS)
2. Silence trimming (leading and trailing, configurable threshold)
3. Noise floor detection and gentle high-pass filter (>80 Hz)
4. Clipping detection — flag segments that clip rather than silently distort them

Flagged (clipping) segments are returned to the review queue with a warning rather than silently excluded from the export. The user decides whether to reject them or accept the risk.

v1 uses sensible fixed defaults. Parameters will be exposed as project-level config in a later version.

---

## Data flow

```
User uploads video files + reference clip
          │
          ▼
Orchestrator: FFmpeg extracts audio track → working_dir/audio/raw/
          │
          ▼
Service 1 (Demucs): vocals stem → working_dir/audio/vocals/
          │
          ▼
Service 2 (pyannote): speaker timeline + confidence scores
          │ → working_dir/segments/raw/{segment_id}.wav
          │ → orchestrator writes segment rows to SQLite
          ▼
Service 3 (Whisper): transcripts returned to orchestrator
          │ → orchestrator writes transcript + confidence to SQLite
          ▼
Review UI: user approves / rejects / edits (all state in SQLite)
          │
          ▼
Service 4 (FFmpeg): cleanup on approved segments only
          │ → working_dir/export/{segment_id}.wav
          ▼
Orchestrator: writes manifest.json (XTTS-v2 format) from database → export/
          │
          ▼
User downloads export archive
```

---

## Project structure (working directory)

Each FlipSync project gets an isolated working directory. All paths below are relative to `projects/{project_id}/`.

```
projects/
└── {project_id}/
    ├── project.db            # SQLite database — source of truth for all state
    ├── source/               # Original uploaded video files (read-only after upload)
    ├── audio/
    │   ├── raw/              # FFmpeg-extracted audio, one file per source
    │   └── vocals/           # Demucs vocals stem, one file per source
    ├── segments/
    │   └── raw/              # Sliced segment WAVs from diarisation
    ├── export/               # Cleaned WAVs + manifest.json, written at export time
    └── reference.wav         # Uploaded reference clip for speaker matching
```

---

## Mono repo structure

```
flipsync/
├── spec/                     # This specification
├── services/
│   ├── orchestrator/         # FastAPI app
│   ├── vocal-separation/     # Demucs service
│   ├── diarisation/          # pyannote + cosine similarity service
│   ├── transcription/        # faster-whisper service
│   └── cleanup/              # FFmpeg cleanup service
├── frontend/                 # React + TypeScript SPA
├── docker-compose.yml
├── CLAUDE.md                 # Claude Code agent configuration
└── README.md
```

---

## Branching strategy

Gitflow pattern:

```
main
└── integrate/orchestrator        # Orchestrator + service integration
    ├── feature/job-queue
    └── feature/export
└── integrate/vocal-separation
    └── feature/demucs-chunking
└── integrate/diarisation
    └── feature/cosine-matching
└── integrate/transcription
└── integrate/cleanup
└── integrate/frontend
    ├── feature/review-ui
    └── feature/timeline-component
```

`main` receives merges from integration branches only. Integration branches receive merges from feature branches. Feature branches are where agent and contributor work happens.

---

## Multi-agent development

Claude Code subagent pattern for parallel development:

1. Coordinator agent reads the spec and builds a dependency graph of tasks
2. Independent tasks (services with no cross-service dependencies at a given stage) are assigned to specialist agents
3. Agents work on their integration branch; each feature gets its own branch
4. Wave-based execution: services that depend on an upstream contract wait for that contract to be committed to the spec before work begins

The `CLAUDE.md` file at the repo root configures agent context: which service they own, which spec documents are relevant, which contracts they must not break.

---

## v1.5 addition: XTTS-v2 service

When XTTS-v2 synthesis is added in v1.5, it runs as a fifth Docker service using the official Coqui streaming server image. It attaches to the same Docker network. The orchestrator gains two new endpoints: one to trigger zero-shot inference from a set of approved segments, one to trigger a full fine-tuning job. No other services change.

CPML licence implications for the project's open source positioning are an open question to be resolved before v1.5 planning. See `overview.md` — open questions.

---

## Roadmap / out of scope

Items explicitly out of scope for v1 that the architecture does not need to accommodate:

- Multi-speaker projects (single speaker per project in v1; v2 adds multiple target speakers per project)
- Video playback in the review UI (the `source/` directory layout is forward-compatible with this; no changes needed)
- Scene-based acoustic slicing for Demucs (whole-file processing in v1; chunking on OOM is a reliability measure, not an acoustic optimisation)
- Hosted / cloud deployment (self-hosted only; no design decisions should assume a multi-tenant context)
