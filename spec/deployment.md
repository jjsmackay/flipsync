# Deployment

**Status:** DRAFT  
**Last updated:** 2026-04-03

---

## Requirements

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| GPU | Nvidia, 6 GB VRAM | Nvidia, 10+ GB VRAM |
| CUDA | 11.8 | 12.x |
| RAM | 16 GB | 32 GB |
| Disk | 50 GB free | 200 GB free |
| OS | Linux (Ubuntu 22.04+) | Linux (Ubuntu 22.04+) |
| Docker | 24.0+ | latest |
| Nvidia Container Runtime | required | required |

**GPU is mandatory.** Demucs and pyannote will not run acceptably on CPU. Whisper will run on CPU but is impractically slow for bulk transcription. There is no CPU-only fallback mode in v1.

**Disk space** scales with source material. A single TV season of 720p MKV files is typically 20–40 GB. Intermediate audio files (raw, vocals, segment WAVs) add roughly 15–20% on top of the source size. Budget generously.

---

## Docker Compose

Single `docker-compose.yml` at the repo root. All services on one internal network. Only the orchestrator port is exposed to the host.

```yaml
version: "3.9"

services:

  orchestrator:
    build: ./services/orchestrator
    ports:
      - "8000:8000"
    volumes:
      - ./data:/data
    environment:
      - DATA_DIR=/data
      - VOCAL_SEPARATION_URL=http://vocal-separation:8001
      - DIARISATION_URL=http://diarisation:8002
      - TRANSCRIPTION_URL=http://transcription:8003
      - CLEANUP_URL=http://cleanup:8004
      - CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
    depends_on:
      - vocal-separation
      - diarisation
      - transcription
      - cleanup
    networks:
      - flipsync

  vocal-separation:
    build: ./services/vocal-separation
    volumes:
      - ./data:/data
      - ${MODELS_ROOT:-/mnt/models/flipsync}/demucs:/root/.cache/torch
    environment:
      - DEMUCS_MODEL=htdemucs
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    networks:
      - flipsync

  diarisation:
    build: ./services/diarisation
    volumes:
      - ./data:/data
      - ${MODELS_ROOT:-/mnt/models/flipsync}/pyannote:/root/.cache/torch
    environment:
      - HF_TOKEN=${HF_TOKEN}
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    networks:
      - flipsync

  transcription:
    build: ./services/transcription
    volumes:
      - ./data:/data
      - ${MODELS_ROOT:-/mnt/models/flipsync}/whisper:/root/.cache/huggingface
    environment:
      - WHISPER_MODEL=large-v2
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    networks:
      - flipsync

  cleanup:
    build: ./services/cleanup
    volumes:
      - ./data:/data
    networks:
      - flipsync

  frontend:
    build: ./frontend
    ports:
      - "3000:3000"
    environment:
      - VITE_API_URL=http://localhost:8000
    networks:
      - flipsync

networks:
  flipsync:
    driver: bridge
```

### Notes on this configuration

**Shared `/data` volume.** All services mount the same `./data` directory on the host. This is how services read each other's output — the vocal separation service writes to `/data/projects/{id}/audio/vocals/`, and the diarisation service reads from the same path. No inter-service file transfer. No shared memory. Files on disk are the interface.

**Model caches are bind mounts under `${MODELS_ROOT:-/mnt/models/flipsync}/`**, not named Docker volumes. This host reserves a dedicated disk at `/mnt/models` for model caches across services (Ollama, whisperx-asr, etc.) — using the same mount keeps FlipSync's ~5 GB of model downloads off the (often much smaller) disk backing Docker's default volume storage, and keeps them visible/manageable directly from the host filesystem. `MODELS_ROOT` is an optional `.env` override for hosts without that mount (e.g. local dev — point it at a plain directory under `./data`). Models are downloaded once on first run and cached permanently; `docker compose down` doesn't touch them.

**The cleanup service has no GPU reservation.** FFmpeg runs on CPU. This is intentional and correct.

**The frontend is a separate container** serving the built React app. In development, the Vite dev server runs instead. In production, it serves the built static files via a lightweight HTTP server (e.g. nginx or `serve`).

---

## Environment variables

Create a `.env` file at the repo root before first run. This file is gitignored.

```bash
# Required
HF_TOKEN=hf_...          # HuggingFace token for pyannote model download

# Optional
MODELS_ROOT=/mnt/models/flipsync   # host dir for model caches; default shown
```

Model selection (Demucs and Whisper) is not configured via environment variables — the orchestrator passes the model name in each job request, per `api-contracts.md`.

**Orchestrator CORS.** The orchestrator reads `CORS_ORIGINS` — a comma-separated list of allowed browser origins. It defaults to `http://localhost:3000,http://127.0.0.1:3000` (the frontend dev and container origins), so it only needs setting when the frontend is served from a different host or port. Override it via the orchestrator's `environment:` block in `docker-compose.yml` rather than `.env`.

The HuggingFace token is only needed on first run to download pyannote's speaker diarisation and embedding models. After the models are cached in `/mnt/models/flipsync/pyannote`, the token is no longer used. It must still be present in the environment or pyannote's library will fail to initialise — this is a pyannote limitation, not a FlipSync one.

**To obtain a HuggingFace token:**
1. Create an account at huggingface.co
2. Go to Settings → Access Tokens → New token (read access is sufficient)
3. Accept the model licence for `pyannote/speaker-diarization-3.1` at huggingface.co/pyannote/speaker-diarization-3.1
4. Accept the model licence for `pyannote/segmentation-3.0`

Both model licences must be accepted under the same account as the token. This is a one-time step.

---

## First run

```bash
# 1. Clone the repo
git clone https://github.com/your-org/flipsync.git
cd flipsync

# 2. Create .env
cp .env.example .env
# Edit .env and add your HF_TOKEN

# 3. Build and start
docker compose up --build

# 4. Open the app
# http://localhost:3000
```

On first run:
- Docker builds all service images (5–10 minutes depending on hardware and network)
- pyannote downloads its models on first job run (~2 GB, cached to `/mnt/models/flipsync/pyannote`)
- Demucs downloads its model on first job run (~80 MB, cached to `/mnt/models/flipsync/demucs`)
- Whisper downloads its model on first job run (large-v2 is ~3 GB, cached to `/mnt/models/flipsync/whisper`)

Total first-run model download: ~5 GB. Subsequent starts are fast.

---

## Stopping and restarting

```bash
# Stop all services (preserves volumes and data)
docker compose down

# Stop and remove volumes
# Model caches (/mnt/models/flipsync/*) and project data (./data) are bind
# mounts, not named volumes, so this does NOT delete them — only stops
# containers. To actually wipe caches, rm -rf the mount paths directly.
docker compose down -v

# Restart a single service
docker compose restart transcription

# View logs
docker compose logs -f orchestrator
docker compose logs -f vocal-separation
```

Project data lives in `./data/` on the host. This directory is a bind mount, so it survives `docker compose down`. Back it up before doing anything destructive.

---

## Updating

```bash
git pull
docker compose up --build
```

The orchestrator runs database migrations on startup. New migrations are applied automatically. No manual migration step required.

If a release includes breaking changes to the data directory layout, the release notes will document a migration path. Breaking changes before v1.0 are possible; after v1.0 they will be rare and clearly signalled.

---

## GPU sharing between services

All three GPU services (vocal separation, diarisation, transcription) request the same GPU. Docker does not enforce exclusive GPU access — all three containers can hold the GPU simultaneously, and CUDA will context-switch between them.

In practice this is fine for sequential pipeline execution: when vocal separation is running, diarisation and transcription are idle. The risk is if a user triggers a transcription job while vocal separation is still running on a large file — both will compete for VRAM.

v1 does not enforce GPU job serialisation at the orchestrator level. If VRAM contention causes OOM errors in practice, the orchestrator will add a GPU job queue (one GPU job at a time) in a patch release. This is a known risk, not an oversight.

---

## Development setup

For local development without rebuilding containers on every change:

```yaml
# docker-compose.override.yml (gitignored)
services:
  orchestrator:
    volumes:
      - ./services/orchestrator:/app
    command: uvicorn main:app --reload --host 0.0.0.0 --port 8000

  frontend:
    volumes:
      - ./frontend:/app
    command: npm run dev
```

The override file mounts source directories and enables hot reload. Processing services do not need hot reload — they change less frequently and rebuilding them is acceptable.

---

## Data directory layout

The `./data/` directory on the host:

```
data/
└── projects/
    └── {project_id}/
        ├── project.db           # SQLite database
        ├── reference.wav
        ├── source/
        ├── audio/
        │   ├── raw/
        │   └── vocals/
        ├── segments/
        │   └── raw/
        └── export/
```

Each project is fully self-contained in its directory. To back up a project, copy its directory. To delete a project, delete its directory and the database row (the UI provides a delete action that does both).

---

## Verify GPU access

Before starting a processing job, verify the GPU is accessible inside the containers:

```bash
docker compose run --rm vocal-separation nvidia-smi
```

Expected output: your GPU listed with driver version and CUDA version. If this fails, the Nvidia Container Runtime is not installed or not configured correctly. See the Nvidia Container Toolkit installation guide for your OS.
