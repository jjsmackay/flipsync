# Deployment

**Status:** Current  
**Last updated:** 2026-07-12

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

Single `docker-compose.yml` at the repo root. All services on one internal network. Two ports are published to the host: the frontend (`3000`) and the orchestrator (`8000`). The UI only needs the frontend port — it reaches the orchestrator via a same-origin `/api` path that the frontend container proxies internally, so the published orchestrator port is only for hitting the API directly.

Service images are pulled prebuilt from GitHub Container Registry (`ghcr.io/jjsmackay/flipsync/*`) rather than built locally. The published copy below mirrors the repo-root `docker-compose.yml`; that file is authoritative if the two ever diverge.

```yaml
services:
  orchestrator:
    image: ghcr.io/jjsmackay/flipsync/orchestrator:latest
    ports:
      - "${ORCHESTRATOR_PORT:-8000}:8000"
    volumes:
      - ${DATA_ROOT:-data}:/data
    environment:
      - DATA_DIR=/data
      - VOCAL_SEPARATION_URL=http://vocal-separation:8001
      - DIARISATION_URL=http://diarisation:8002
      - TRANSCRIPTION_URL=http://transcription:8003
      - CLEANUP_URL=http://cleanup:8004
      - CORS_ORIGINS=${CORS_ORIGINS:-http://localhost:3000,http://127.0.0.1:3000}
    depends_on:
      - vocal-separation
      - diarisation
      - transcription
      - cleanup
    restart: unless-stopped
    networks:
      - flipsync

  vocal-separation:
    image: ghcr.io/jjsmackay/flipsync/vocal-separation:latest
    volumes:
      - ${DATA_ROOT:-data}:/data
      - ${MODELS_ROOT:-/mnt/models/flipsync}/demucs:/root/.cache/torch
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    restart: unless-stopped
    networks:
      - flipsync

  diarisation:
    image: ghcr.io/jjsmackay/flipsync/diarisation:latest
    volumes:
      - ${DATA_ROOT:-data}:/data
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
    restart: unless-stopped
    networks:
      - flipsync

  transcription:
    image: ghcr.io/jjsmackay/flipsync/transcription:latest
    volumes:
      - ${DATA_ROOT:-data}:/data
      - ${MODELS_ROOT:-/mnt/models/flipsync}/whisper:/root/.cache/huggingface
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    restart: unless-stopped
    networks:
      - flipsync

  cleanup:
    image: ghcr.io/jjsmackay/flipsync/cleanup:latest
    volumes:
      - ${DATA_ROOT:-data}:/data
    restart: unless-stopped
    networks:
      - flipsync

  frontend:
    image: ghcr.io/jjsmackay/flipsync/frontend:latest
    ports:
      - "${FRONTEND_PORT:-3000}:3000"
    environment:
      - ORCHESTRATOR_PROXY_TARGET=http://orchestrator:8000
    depends_on:
      - orchestrator
    restart: unless-stopped
    networks:
      - flipsync

networks:
  flipsync:
    driver: bridge

volumes:
  data:
```

### Notes on this configuration

**Shared `/data` volume.** All services mount the same `/data` volume. This is how services read each other's output — the vocal separation service writes to `/data/projects/{id}/audio/vocals/`, and the diarisation service reads from the same path. No inter-service file transfer. No shared memory. Files on disk are the interface.

**Project data lives in a named volume by default.** `/data` is backed by the `data` named volume (`flipsync_data` once Compose prefixes the project name), which Docker stores under `/var/lib/docker/volumes/` — outside the compose working directory. That means it survives `docker compose down` and, critically, a deploy tool re-cloning or destroying its stack directory (Komodo, Portainer git stacks, etc.). No configuration is needed for this safety. Only `docker compose down -v` removes it (Komodo's "destroy" is a plain `down`, so it's safe there). To store project data on a host path instead — a browsable directory for local dev, or a specific disk — set `DATA_ROOT` to an absolute path (e.g. `/opt/flipsync-data`) or `./data`; it then becomes a bind mount. If you bind-mount under a deploy tool's git clone, a reclone can delete it, so prefer a named volume or an absolute path outside the clone.

**Model caches are bind mounts under `${MODELS_ROOT:-/mnt/models/flipsync}/`**, not named Docker volumes. This host reserves a dedicated disk at `/mnt/models` for model caches across services (Ollama, whisperx-asr, etc.) — using the same mount keeps FlipSync's ~5 GB of model downloads off the (often much smaller) disk backing Docker's default volume storage, and keeps them visible/manageable directly from the host filesystem. `MODELS_ROOT` is an optional `.env` override for hosts without that mount (e.g. local dev — point it at a plain directory under `./data`). Models are downloaded once on first run and cached permanently; `docker compose down` doesn't touch them.

**The cleanup service has no GPU reservation.** FFmpeg runs on CPU. This is intentional and correct.

**The frontend is a separate container** serving the built React app with Caddy. Caddy serves the static assets and proxies `/api/*` to the orchestrator (`ORCHESTRATOR_PROXY_TARGET`), so the browser only ever talks to the frontend origin — the orchestrator's published port and CORS are not needed for normal UI use. Caddy streams request bodies with no size cap by default, so large source uploads (multi-GB video) pass straight through to the orchestrator rather than being buffered or capped at this hop.

**Uploads through an external reverse proxy.** Source files are large (1–4 GB). Any reverse proxy you put in front of the frontend (on a separate host, ingress, etc.) must allow large request bodies and long-running uploads — otherwise it will reset the connection mid-upload regardless of what FlipSync does. For nginx that means `client_max_body_size` raised to your largest expected source (e.g. `5G`), generous `proxy_*_timeout` values, and `proxy_request_buffering off` so the body streams instead of spooling to a temp file. Equivalent settings apply to Caddy, Traefik, or any other edge proxy. This is deployment-specific and lives in your proxy config, not in the repo.

---

## Environment variables

Create a `.env` file at the repo root before first run: `cp .env.example .env`. This file is gitignored. `.env.example` is the canonical, commented reference — it documents every variable and the exact HuggingFace steps. Summary:

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `HF_TOKEN` | yes | — | HuggingFace token for pyannote model download (diarisation only) |
| `DATA_ROOT` | no | `data` (named volume) | Override project-data storage with a host bind path (absolute, or `./data`). Unset = named volume (see note above) |
| `MODELS_ROOT` | no | `/mnt/models/flipsync` | Host dir for the demucs/pyannote/whisper caches |
| `ORCHESTRATOR_PORT` | no | `8000` | Host port for the orchestrator API |
| `FRONTEND_PORT` | no | `3000` | Host port for the UI |
| `CORS_ORIGINS` | no | `http://localhost:3000,http://127.0.0.1:3000` | Browser origins allowed to call the orchestrator directly |

Model selection (Demucs and Whisper) is not configured via environment variables — the orchestrator passes the model name in each job request, per `api-contracts.md`.

**Orchestrator CORS.** `CORS_ORIGINS` only matters if something calls the orchestrator's published port from a browser directly. The UI itself calls the same-origin `/api` proxy, so it needs no CORS entry.

**HuggingFace token.** Only needed on first run to download pyannote's models. After they're cached under `${MODELS_ROOT}/pyannote`, the token is no longer used for downloads, but it must still be present in the environment or pyannote's library fails to initialise — a pyannote limitation, not a FlipSync one. The token's account must first accept the access conditions on all three gated repos the pipeline pulls:

- `pyannote/speaker-diarization-3.1`
- `pyannote/segmentation-3.0`
- `pyannote/embedding`

This is a one-time step. See `.env.example` for the click-through walkthrough.

> On the Komodo-deployed stack, set these in the stack's Environment field, not a hand-edited `.env` on the host — Komodo overwrites `.env` on deploy.

---

## First run

```bash
# 1. Clone the repo
git clone https://github.com/jjsmackay/flipsync.git
cd flipsync

# 2. Create .env
cp .env.example .env
# Edit .env and add your HF_TOKEN

# 3. Pull images and start
docker compose up -d

# 4. Open the app
# http://localhost:3000
```

On first run:
- Docker pulls the prebuilt service images from `ghcr.io/jjsmackay/flipsync/*` (a few minutes depending on network)
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
docker compose pull
docker compose up -d
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
