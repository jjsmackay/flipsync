"""FlipSync Orchestrator — FastAPI application entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from errors import AppError, app_error_handler
from jobs import recover_jobs
from routers import projects, sources, reference, pipeline, segments
from service_client import SERVICE_URLS, check_health, close_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Recover jobs that were in-flight before a restart
    await recover_jobs()

    # Start health polling for each processing service (non-blocking)
    for name in SERVICE_URLS:
        asyncio.create_task(check_health(name))

    yield

    # Cleanup HTTP client on shutdown
    await close_client()


app = FastAPI(title="FlipSync Orchestrator", version="0.1.0", lifespan=lifespan)
app.add_exception_handler(AppError, app_error_handler)

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
