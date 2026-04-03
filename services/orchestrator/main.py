"""FlipSync Orchestrator — FastAPI application entry point."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from jobs import recover_jobs
from routers import projects, sources, reference, pipeline, segments

logger = logging.getLogger(__name__)

# Processing service URLs from environment (stubbed in Wave 1)
SERVICE_URLS = {
    "vocal_separation": os.environ.get("VOCAL_SEPARATION_URL", "http://vocal-separation:8001"),
    "diarisation": os.environ.get("DIARISATION_URL", "http://diarisation:8002"),
    "transcription": os.environ.get("TRANSCRIPTION_URL", "http://transcription:8003"),
    "cleanup": os.environ.get("CLEANUP_URL", "http://cleanup:8004"),
}


async def _poll_service_health(name: str, url: str, timeout_secs: int = 300) -> None:
    """Poll GET /health until 200 or timeout. Log but don't fail startup."""
    deadline = asyncio.get_event_loop().time() + timeout_secs
    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await client.get(f"{url}/health", timeout=5)
                if resp.status_code == 200:
                    logger.info("Service %s is healthy at %s", name, url)
                    return
            except Exception:
                pass
            await asyncio.sleep(5)
    logger.warning("Service %s at %s did not become healthy within %ds", name, url, timeout_secs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Recover jobs that were in-flight before a restart
    await recover_jobs()

    # Start health polling for each processing service (non-blocking)
    for name, url in SERVICE_URLS.items():
        asyncio.create_task(_poll_service_health(name, url))

    yield


app = FastAPI(title="FlipSync Orchestrator", version="0.1.0", lifespan=lifespan)

# CORS — allow the frontend dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(projects.router)
app.include_router(sources.router)
app.include_router(reference.router)
app.include_router(pipeline.router)
app.include_router(segments.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
