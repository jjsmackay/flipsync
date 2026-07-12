"""Vocal Separation Service — Demucs-backed FastAPI app.

Exposes:
  GET  /health
  POST /jobs
  GET  /jobs/{job_id}
"""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

import separator as sep

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

# In-memory job store: job_id → job dict
_jobs: dict[str, dict] = {}

# Cap on retained jobs. Terminal (complete/failed) jobs are evicted oldest-first
# once this is exceeded so the store doesn't grow unbounded over the lifetime of
# a long-running (`restart: unless-stopped`) container. Running jobs are never
# evicted.
_MAX_JOBS = 256

# Strong references to in-flight background tasks. Without this, asyncio only
# holds a weak reference and a task can be garbage-collected mid-run, leaving a
# job stuck "running" forever.
_background_tasks: set[asyncio.Task] = set()

# Thread pool for blocking Demucs calls (keeps asyncio event loop free)
_executor = ThreadPoolExecutor(max_workers=1)

# Set to a human-readable reason if model preload fails at startup. While set,
# /health reports unhealthy so the orchestrator won't submit jobs that would
# all fail anyway.
_preload_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Preload the default model on startup.

    A preload failure is recorded and surfaced via /health rather than being
    swallowed — the previous behaviour let the service report healthy while
    every job failed.
    """
    global _preload_error
    preload = os.environ.get("PRELOAD_MODELS", "htdemucs").split(",")
    preload = [m.strip() for m in preload if m.strip()]
    if preload:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(_executor, sep.preload_models, preload)
        except Exception as exc:  # noqa: BLE001 — record any preload failure
            _preload_error = str(exc)
            logger.critical(
                "Model preload FAILED (%s) — /health will report unhealthy",
                exc,
                exc_info=True,
            )
    yield


app = FastAPI(title="FlipSync Vocal Separation Service", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class JobRequest(BaseModel):
    job_id: str
    input_path: str
    output_path: str
    model: str = "htdemucs"
    chunk_secs: Optional[int] = None

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str) -> str:
        if v not in sep.VALID_MODELS:
            raise ValueError(f"model must be one of {sorted(sep.VALID_MODELS)}")
        return v


# ---------------------------------------------------------------------------
# Background processing
# ---------------------------------------------------------------------------


def _set_progress(job_id: str, progress: int) -> None:
    if job_id in _jobs:
        _jobs[job_id]["progress"] = progress


def _is_cuda_oom(exc: BaseException) -> bool:
    """True if ``exc`` represents a CUDA out-of-memory condition.

    Covers ``torch.cuda.OutOfMemoryError`` and plain ``RuntimeError``s whose
    message indicates an allocator failure — cuBLAS/cuDNN allocations surface
    OOM as a generic ``RuntimeError`` ("CUDA out of memory", "CUBLAS_STATUS_
    ALLOC_FAILED"), not as ``OutOfMemoryError``, so those must reach the
    chunked-retry path too rather than being reported as a generic error.
    """
    try:
        import torch

        if isinstance(exc, torch.cuda.OutOfMemoryError):
            return True
    except Exception:  # torch missing (tests) or no cuda submodule
        pass
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        return "out of memory" in msg or "alloc_failed" in msg
    return False


def _empty_cuda_cache() -> None:
    """Best-effort CUDA cache clear; a no-op when torch/CUDA is unavailable."""
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:
        pass


def _mark_oom_failed(job_id: str) -> None:
    """Mark a job failed after chunking has already been attempted.

    ``retry_with_chunk_secs`` is set to null: the audio has already been
    processed in chunks and failed, so asking the orchestrator to resubmit with
    the same chunk size would be guaranteed to fail again. Null tells the
    orchestrator to stop retrying and mark the source separation_failed.
    """
    _jobs[job_id].update(
        {
            "status": "failed",
            "error": "cuda_oom",
            "message": "CUDA ran out of memory even with chunked processing.",
            "retry_with_chunk_secs": None,
            "progress": 0,
        }
    )


def _run_separation(job: dict) -> None:
    """Blocking separation call — runs in a thread pool executor."""
    job_id = job["job_id"]
    input_path = job["input_path"]
    output_path = job["dest_path"]
    model_name = job["model"]
    chunk_secs = job["chunk_secs"]
    already_chunked = chunk_secs is not None

    def progress_cb(p: int) -> None:
        _set_progress(job_id, p)

    try:
        if not already_chunked:
            # Attempt whole-file processing first.
            try:
                logger.info("Job %s: whole-file separation started", job_id)
                sep.separate(
                    input_path=input_path,
                    output_path=output_path,
                    model_name=model_name,
                    chunk_secs=None,
                    progress_callback=progress_cb,
                )
            except Exception as exc:
                if not _is_cuda_oom(exc):
                    raise
                logger.warning(
                    "Job %s: CUDA OOM on whole-file; retrying with chunk_secs=60",
                    job_id,
                )
                _empty_cuda_cache()
                # Internal chunked retry.
                try:
                    sep.separate(
                        input_path=input_path,
                        output_path=output_path,
                        model_name=model_name,
                        chunk_secs=60,
                        progress_callback=progress_cb,
                    )
                except Exception as exc2:
                    if not _is_cuda_oom(exc2):
                        raise
                    logger.error(
                        "Job %s: CUDA OOM even with chunking; marking failed", job_id
                    )
                    _empty_cuda_cache()
                    _mark_oom_failed(job_id)
                    return
        else:
            # chunk_secs was provided — the orchestrator asked for a chunked run.
            logger.info(
                "Job %s: chunked separation started (chunk_secs=%d)", job_id, chunk_secs
            )
            try:
                sep.separate(
                    input_path=input_path,
                    output_path=output_path,
                    model_name=model_name,
                    chunk_secs=chunk_secs,
                    progress_callback=progress_cb,
                )
            except Exception as exc:
                if not _is_cuda_oom(exc):
                    raise
                logger.error(
                    "Job %s: CUDA OOM on chunked attempt; marking failed", job_id
                )
                _empty_cuda_cache()
                _mark_oom_failed(job_id)
                return

        _jobs[job_id].update(
            {
                "status": "complete",
                "progress": 100,
                "output_path": output_path,
                "error": None,
                "message": None,
                "retry_with_chunk_secs": None,
            }
        )
        logger.info("Job %s: complete", job_id)

    except Exception as exc:
        logger.exception("Job %s: unexpected error", job_id)
        _jobs[job_id].update(
            {
                "status": "failed",
                "error": "processing_error",
                "message": str(exc),
                "retry_with_chunk_secs": None,
                "progress": 0,
            }
        )


async def _run_job_async(job: dict) -> None:
    """Kick off separation in the thread pool so the event loop stays free."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_executor, _run_separation, job)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return validation errors in the standard flat error format.

    FastAPI's default wraps them in ``{"detail": [...]}``; the contract mandates
    ``{"error", "message", "detail"}`` across all services.
    """
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": "Request validation failed.",
            "detail": {"errors": jsonable_encoder(exc.errors())},
        },
    )


@app.get("/health")
async def health():
    if _preload_error is not None:
        return JSONResponse(
            status_code=503,
            content={
                "error": "model_preload_failed",
                "message": f"Model preload failed: {_preload_error}",
                "detail": {},
            },
        )
    return {"status": "ok"}


def _evict_old_jobs() -> None:
    """Evict oldest terminal jobs when the store exceeds ``_MAX_JOBS``."""
    excess = len(_jobs) - _MAX_JOBS
    if excess <= 0:
        return
    removable = [
        jid for jid, j in _jobs.items() if j["status"] in ("complete", "failed")
    ]
    for jid in removable[:excess]:
        del _jobs[jid]


@app.post("/jobs", status_code=202)
async def submit_job(req: JobRequest):
    if req.job_id in _jobs:
        # Duplicate submit (e.g. an orchestrator retry after a timed-out but
        # delivered POST) must not restart or overwrite the original job.
        return JSONResponse(
            status_code=409,
            content={
                "error": "job_exists",
                "message": f"Job {req.job_id} already exists.",
                "detail": {},
            },
        )

    job: dict = {
        "job_id": req.job_id,
        "status": "running",
        "progress": 0,
        "output_path": None,
        "error": None,
        "message": None,
        "retry_with_chunk_secs": None,
        # Internal fields (not exposed in poll response)
        "input_path": req.input_path,
        "dest_path": req.output_path,
        "model": req.model,
        "chunk_secs": req.chunk_secs,
    }
    _jobs[req.job_id] = job
    _evict_old_jobs()
    task = asyncio.create_task(_run_job_async(job))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"job_id": req.job_id}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in _jobs:
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_found",
                "message": f"Job {job_id} not found.",
                "detail": {},
            },
        )
    job = _jobs[job_id]
    # Return only the public fields
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": job["progress"],
        "output_path": job.get("output_path"),
        "error": job.get("error"),
        "message": job.get("message"),
        "retry_with_chunk_secs": job.get("retry_with_chunk_secs"),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
