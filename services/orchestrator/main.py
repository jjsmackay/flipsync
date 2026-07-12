"""FlipSync Orchestrator — FastAPI application entry point."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from errors import AppError, app_error_handler
from jobs import recover_jobs, shutdown_runners
from routers import projects, sources, reference, pipeline, segments, models, previews
from service_client import SERVICE_URLS, check_health, close_client

logger = logging.getLogger(__name__)

DEFAULT_CORS_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000"


def cors_origins() -> list[str]:
    """Allowed CORS origins from CORS_ORIGINS (comma-separated), defaulting to
    the localhost/127.0.0.1 frontend dev servers (SC8)."""
    raw = os.environ.get("CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    return [o.strip() for o in raw.split(",") if o.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Recover jobs that were in-flight before a restart
    await recover_jobs()

    # Start health polling for each processing service (non-blocking). Keep a
    # reference to each task so it is not garbage-collected mid-flight.
    tasks: set[asyncio.Task] = set()
    for name in SERVICE_URLS:
        # xtts is an optional profile-gated service; a 300 s startup poll against
        # an absent container is just log noise. Its readiness is checked
        # on-demand by the Models/Previews endpoints via is_healthy.
        if name == "xtts":
            continue
        task = asyncio.create_task(check_health(name))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    app.state.background_tasks = tasks

    yield

    # Cancel any per-project job runner tasks still awaiting new work before
    # the event loop closes (an uncancelled pending task raises "Event loop is
    # closed" when garbage-collected after the loop is gone).
    await shutdown_runners()

    # Cleanup HTTP client on shutdown
    await close_client()


app = FastAPI(title="FlipSync Orchestrator", version="0.1.0", lifespan=lifespan)
app.add_exception_handler(AppError, app_error_handler)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return request-validation failures in the flat spec error format."""
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": "Request validation failed.",
            "detail": {"errors": jsonable_encoder(exc.errors())},
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Return unexpected errors in the flat spec error format instead of the
    default HTML/plain 500 body."""
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "An unexpected error occurred.",
            "detail": {},
        },
    )


# CORS — configurable via CORS_ORIGINS env, defaults to the frontend dev servers
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
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
app.include_router(models.router)
app.include_router(previews.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
