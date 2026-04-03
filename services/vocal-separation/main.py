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

import torch
import uvicorn
from fastapi import FastAPI
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

# Thread pool for blocking Demucs calls (keeps asyncio event loop free)
_executor = ThreadPoolExecutor(max_workers=1)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Preload the default model on startup."""
    preload = os.environ.get("PRELOAD_MODELS", "htdemucs").split(",")
    preload = [m.strip() for m in preload if m.strip()]
    if preload:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_executor, sep.preload_models, preload)
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


def _run_separation(job: dict) -> None:
    """Blocking separation call — runs in a thread pool executor."""
    job_id = job["job_id"]
    input_path = job["input_path"]
    output_path = job["output_path"]
    model_name = job["model"]
    chunk_secs = job["chunk_secs"]
    already_chunked = chunk_secs is not None

    def progress_cb(p: int) -> None:
        _set_progress(job_id, p)

    try:
        if not already_chunked:
            # Attempt whole-file processing first
            try:
                logger.info("Job %s: whole-file separation started", job_id)
                sep.separate(
                    input_path=input_path,
                    output_path=output_path,
                    model_name=model_name,
                    chunk_secs=None,
                    progress_callback=progress_cb,
                )
            except torch.cuda.OutOfMemoryError:
                logger.warning(
                    "Job %s: CUDA OOM on whole-file; retrying with chunk_secs=60",
                    job_id,
                )
                torch.cuda.empty_cache()
                # Retry with chunking
                try:
                    sep.separate(
                        input_path=input_path,
                        output_path=output_path,
                        model_name=model_name,
                        chunk_secs=60,
                        progress_callback=progress_cb,
                    )
                except torch.cuda.OutOfMemoryError:
                    logger.error(
                        "Job %s: CUDA OOM even with chunking; marking failed", job_id
                    )
                    torch.cuda.empty_cache()
                    _jobs[job_id].update(
                        {
                            "status": "failed",
                            "error": "cuda_oom",
                            "retry_with_chunk_secs": 60,
                            "progress": 0,
                        }
                    )
                    return
        else:
            # chunk_secs was provided — this is already the chunked retry attempt
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
            except torch.cuda.OutOfMemoryError:
                logger.error(
                    "Job %s: CUDA OOM on pre-chunked attempt; marking failed", job_id
                )
                torch.cuda.empty_cache()
                _jobs[job_id].update(
                    {
                        "status": "failed",
                        "error": "cuda_oom",
                        "retry_with_chunk_secs": 60,
                        "progress": 0,
                    }
                )
                return

        _jobs[job_id].update(
            {
                "status": "complete",
                "progress": 100,
                "output_path": output_path,
                "error": None,
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
                "progress": 0,
            }
        )


async def _run_job_async(job: dict) -> None:
    """Kick off separation in the thread pool so the event loop stays free."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _run_separation, job)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def submit_job(req: JobRequest):
    job: dict = {
        "job_id": req.job_id,
        "status": "running",
        "progress": 0,
        "output_path": None,
        "error": None,
        "retry_with_chunk_secs": None,
        # Internal fields (not exposed in poll response)
        "input_path": req.input_path,
        "model": req.model,
        "chunk_secs": req.chunk_secs,
    }
    _jobs[req.job_id] = job
    asyncio.create_task(_run_job_async(job))
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
        "retry_with_chunk_secs": job.get("retry_with_chunk_secs"),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
