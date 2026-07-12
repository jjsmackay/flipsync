# Deployment

**Status:** Current  
**Last updated:** 2026-07-13

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
| `XTTS_ACCEPT_CPML` | only with `--profile xtts` | — | CPML licence acceptance; the XTTS service refuses to start without it (see §XTTS service) |

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
# Stop all services (containers only — project data and model caches survive)
docker compose down

# Stop and remove named volumes — THIS DELETES ALL PROJECT DATA on a default
# deployment. Project data lives in the `data` named volume (flipsync_data),
# which -v removes. Model caches are bind mounts under
# ${MODELS_ROOT:-/mnt/models/flipsync} and survive; a DATA_ROOT bind mount
# also survives. Do not run this unless you mean to wipe every project.
docker compose down -v

# Restart a single service
docker compose restart transcription

# View logs
docker compose logs -f orchestrator
docker compose logs -f vocal-separation
```

Project data lives in the `data` named volume by default (`flipsync_data` once Compose prefixes the project name). It survives `docker compose down` — but **not** `docker compose down -v`, which deletes the volume and every project in it. If `DATA_ROOT` is set, project data is a bind mount at that host path instead and `down -v` leaves it alone. Either way, back it up before doing anything destructive: copy the `DATA_ROOT` directory, or for the named volume `docker run --rm -v flipsync_data:/data -v "$PWD":/backup alpine tar czf /backup/flipsync-data.tar.gz -C /data .`

---

## Updating

```bash
git pull
docker compose pull
docker compose up -d
```

The orchestrator runs database migrations on startup. New migrations are applied automatically. No manual migration step required.

**Upgrading from the old `./data` bind-mount default.** Earlier releases bind-mounted `./data` by default; the default is now the `data` named volume. A deployment that relied on the old default will come up with an empty volume after upgrading and its projects will appear to vanish. Nothing has been deleted — the old data is still on disk at `./data`. Before upgrading (or before the next `docker compose up`), set `DATA_ROOT` to that directory — an absolute path is recommended — and the deployment keeps its existing projects.

If a release includes breaking changes to the data directory layout, the release notes will document a migration path. Breaking changes before v1.0 are possible; after v1.0 they will be rare and clearly signalled.

---

## GPU sharing between services

All three GPU services (vocal separation, diarisation, transcription) request the same GPU. Docker does not enforce exclusive GPU access — all three containers can hold the GPU simultaneously, and CUDA will context-switch between them.

The orchestrator serialises GPU-bound jobs (vocal separation, diarisation, transcription) host-wide: at most one GPU job *runs* across all projects at any time. CPU jobs (audio extraction, export) are not gated. See [Architecture](architecture.md) §Job queue.

That lock governs *execution*, not *residency*. Each service is a long-lived container that loads its model into VRAM on first use and keeps it there. So even though only one job computes at a time, an idle service still holds its model — Demucs, both pyannote models, and faster-whisper can all sit in VRAM at once. On a 6 GB card that combination alone can exhaust VRAM before the last stage (transcription) even allocates.

### Idle VRAM unloading

To stop idle upstream services squatting on GPU memory the current stage needs, **vocal separation** and **diarisation** release their models after a configurable idle period (`IDLE_UNLOAD_SECS`, default 60 s; set to 0 to disable and keep models warm). A watcher checks periodically and, when the service has been idle with no job in flight, frees the model and calls `torch.cuda.empty_cache()` so the VRAM returns to the driver for another container to use. The model reloads transparently on the next job — a few seconds for Demucs, ~5–15 s for pyannote. Back-to-back jobs within a stage keep the model warm; it only releases once the stage goes quiet.

The unload runs on each service's single-worker job executor, so it can never overlap a running job, and a job arriving during the idle window cancels a pending unload rather than losing its model mid-run. `/health` stays green throughout — readiness means "loaded successfully at least once", not "resident right now" — so the orchestrator keeps submitting normally.

Transcription is deliberately excluded: it's the last GPU stage, so nothing waits behind its model, and holding it avoids reload churn across a batch of segments.

---

## XTTS service (v1.5, opt-in)

The XTTS-v2 service is not started by default. It is gated behind a Compose profile and a licence-acceptance environment variable:

```bash
# .env
XTTS_ACCEPT_CPML=1   # accepts the Coqui Public Model Licence (non-commercial)

docker compose --profile xtts up -d
```

If `XTTS_ACCEPT_CPML` is unset the service still starts but reports unhealthy: `/health` returns 503 with error `cpml_not_accepted`, and the orchestrator will not submit jobs to it. Set the variable and restart the container to accept the licence. FlipSync distributes no XTTS weights; they download on first use into `${MODELS_ROOT:-/mnt/models/flipsync}/xtts`.

| Concern | Value |
|---------|-------|
| Internal port | 8005 (not exposed to host) |
| GPU | Same reservation pattern as vocal separation / diarisation |
| VRAM — preview (`synthesise`) | ~6 GB |
| VRAM — fine-tune | 16 GB recommended; 12 GB minimum (batch 1 + gradient accumulation, slower) |
| Model cache | `${MODELS_ROOT}/xtts:/root/.local/share/tts` |

The service runs a VRAM preflight before fine-tuning and fails fast with `insufficient_vram` rather than hitting a CUDA OOM mid-training. A fine-tune occupies the GPU for hours; the GPU-sharing caveats above apply with more force while one is running.

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

Inside the shared `/data` volume (the `data` named volume by default, or the `DATA_ROOT` bind mount):

```
/data/
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
