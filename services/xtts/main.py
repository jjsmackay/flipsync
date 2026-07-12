"""XTTS-v2 Service — fine-tune + synthesise FastAPI app (port 8005).

Exposes:
  GET  /health
  POST /jobs           (finetune | synthesise, discriminated on ``type``)
  GET  /jobs/{job_id}

Follows the vocal-separation template (single-worker executor, in-memory job
store, engine-module test seam) with transcription's rich-progress polling
shape: ``GET /jobs/{job_id}`` returns the full job dict including a ``progress``
object for fine-tune jobs.

Licensing: XTTS-v2 ships under the Coqui Public Model License (CPML). The
operator must acknowledge it by setting ``XTTS_ACCEPT_CPML=1``; until then the
service reports unhealthy and refuses jobs.
"""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Optional, Union

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import engine

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

# Thread pool for blocking fine-tune / synthesise calls (keeps the event loop
# free). Single worker: one GPU job at a time, matching the pipeline invariant.
_executor = ThreadPoolExecutor(max_workers=1)

# Set at startup to a human-readable reason if the CPML has not been accepted.
# While set, /health reports 503 so the orchestrator won't submit jobs.
_startup_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gate startup on CPML acceptance.

    XTTS-v2 is licensed under the Coqui Public Model License. We require an
    explicit opt-in (``XTTS_ACCEPT_CPML=1``) rather than silently agreeing on
    the operator's behalf; when accepted we set ``COQUI_TOS_AGREED=1`` so the
    TTS library's non-interactive download path proceeds.
    """
    global _startup_error
    if os.environ.get("XTTS_ACCEPT_CPML") != "1":
        _startup_error = (
            "XTTS_ACCEPT_CPML not set. XTTS-v2 is licensed under the Coqui "
            "Public Model License (CPML); set XTTS_ACCEPT_CPML=1 to accept it "
            "and enable the service."
        )
        logger.critical("%s /health will report unhealthy.", _startup_error)
    else:
        os.environ["COQUI_TOS_AGREED"] = "1"
    yield


app = FastAPI(title="FlipSync XTTS Service", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Pydantic models — discriminated union on ``type``
# ---------------------------------------------------------------------------


class FinetuneParams(BaseModel):
    epochs: int = 10
    batch_size: int = 3
    grad_accum: int = 1
    learning_rate: float = 5e-6
    language: str
    eval_split: float = 0.1


class SynthParams(BaseModel):
    temperature: float = 0.65


class FinetuneJob(BaseModel):
    job_id: str
    type: Literal["finetune"]
    manifest_path: str
    output_dir: str
    params: FinetuneParams


class SynthesiseJob(BaseModel):
    job_id: str
    type: Literal["synthesise"]
    text: str = Field(min_length=1, max_length=2000)
    language: str
    reference_wavs: list[str] = Field(min_length=1, max_length=10)
    checkpoint_dir: Optional[str] = None
    output_path: str
    params: SynthParams = Field(default_factory=SynthParams)


JobRequest = Annotated[
    Union[FinetuneJob, SynthesiseJob], Field(discriminator="type")
]


# ---------------------------------------------------------------------------
# OOM detection (copied verbatim from vocal-separation/main.py)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Background processing
# ---------------------------------------------------------------------------


def _run_finetune(job: dict, req: FinetuneJob) -> None:
    """Blocking fine-tune call — runs in the thread pool executor."""
    job_id = job["job_id"]

    # VRAM pre-flight: fail fast with a clear message rather than OOM-ing deep
    # into training on an under-provisioned GPU.
    avail = engine.vram_available_gb()
    if avail < engine.FINETUNE_MIN_VRAM_GB:
        logger.warning(
            "Job %s: insufficient VRAM (%.1f < %g GB)",
            job_id,
            avail,
            engine.FINETUNE_MIN_VRAM_GB,
        )
        job.update(
            {
                "status": "failed",
                "error": (
                    f"insufficient_vram: {engine.FINETUNE_MIN_VRAM_GB:g} GB "
                    f"required, {avail:.1f} GB available"
                ),
            }
        )
        return

    def progress_cb(detail: dict) -> None:
        job["progress"] = detail

    try:
        logger.info("Job %s: fine-tune started", job_id)
        result = engine.finetune(
            manifest_path=req.manifest_path,
            output_dir=req.output_dir,
            params={
                "epochs": req.params.epochs,
                "batch_size": req.params.batch_size,
                "grad_accum": req.params.grad_accum,
                "learning_rate": req.params.learning_rate,
                "language": req.params.language,
                "eval_split": req.params.eval_split,
            },
            progress_cb=progress_cb,
        )
        last = job["progress"] if isinstance(job.get("progress"), dict) else {}
        job["progress"] = {**last, "phase": "packaging"}
        job["result"] = result
        job["status"] = "complete"
        logger.info("Job %s: fine-tune complete", job_id)

    except Exception as exc:  # noqa: BLE001
        if _is_cuda_oom(exc):
            _empty_cuda_cache()
            if req.params.batch_size > 1:
                # Report a smaller configuration for the orchestrator to
                # resubmit — mirrors vocal separation's retry_with_chunk_secs.
                logger.warning(
                    "Job %s: CUDA OOM at batch_size=%d; reporting retry_with",
                    job_id,
                    req.params.batch_size,
                )
                # "status" is written LAST: the orchestrator acts the moment it
                # polls a terminal status, so error/retry_with must already be
                # in place when it does.
                job.update(
                    {
                        "error": "cuda_oom",
                        "retry_with": {
                            "batch_size": 1,
                            "grad_accum": req.params.batch_size * req.params.grad_accum,
                        },
                        "status": "failed",
                    }
                )
            else:
                # Already at the minimum batch size — retrying is futile.
                logger.error("Job %s: CUDA OOM at batch_size=1; terminal", job_id)
                job.update(
                    {"error": "cuda_oom", "retry_with": None, "status": "failed"}
                )
        else:
            logger.exception("Job %s: fine-tune failed", job_id)
            job.update({"error": str(exc), "status": "failed"})


def _run_synthesise(job: dict, req: SynthesiseJob) -> None:
    """Blocking synthesise call — runs in the thread pool executor."""
    job_id = job["job_id"]

    for wav in req.reference_wavs:
        if not os.path.exists(wav):
            logger.warning("Job %s: reference wav not found: %s", job_id, wav)
            job.update(
                {"status": "failed", "error": f"reference_not_found: {wav}"}
            )
            return

    try:
        os.makedirs(os.path.dirname(req.output_path), exist_ok=True)
        logger.info("Job %s: synthesis started", job_id)
        result = engine.synthesise(
            text=req.text,
            language=req.language,
            reference_wavs=req.reference_wavs,
            checkpoint_dir=req.checkpoint_dir,
            output_path=req.output_path,
            params={"temperature": req.params.temperature},
        )
        job["result"] = result
        job["status"] = "complete"
        logger.info("Job %s: synthesis complete", job_id)

    except Exception as exc:  # noqa: BLE001
        if _is_cuda_oom(exc):
            # Synthesis is a single forward pass; there is no smaller config to
            # retry with, so OOM is terminal.
            _empty_cuda_cache()
            logger.error("Job %s: CUDA OOM during synthesis; terminal", job_id)
            job.update({"status": "failed", "error": "cuda_oom", "retry_with": None})
        else:
            logger.exception("Job %s: synthesis failed", job_id)
            job.update({"status": "failed", "error": str(exc)})


async def _run_job_async(job: dict, req: JobRequest) -> None:
    """Offload the blocking job to the thread pool so the event loop stays free."""
    loop = asyncio.get_running_loop()
    if req.type == "finetune":
        await loop.run_in_executor(_executor, _run_finetune, job, req)
    else:
        await loop.run_in_executor(_executor, _run_synthesise, job, req)


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
    if _startup_error is not None:
        return JSONResponse(
            status_code=503,
            content={
                "error": "cpml_not_accepted",
                "message": _startup_error,
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
    if _startup_error is not None:
        return JSONResponse(
            status_code=503,
            content={
                "error": "cpml_not_accepted",
                "message": _startup_error,
                "detail": {},
            },
        )
    if req.job_id in _jobs:
        return JSONResponse(
            status_code=409,
            content={
                "error": "duplicate_job",
                "message": f"Job {req.job_id} already exists.",
                "detail": {},
            },
        )

    job: dict = {
        "job_id": req.job_id,
        "type": req.type,
        "status": "running",
        "progress": {"phase": "preparing"} if req.type == "finetune" else None,
        "result": None,
        "error": None,
        "retry_with": None,
    }
    _jobs[req.job_id] = job
    _evict_old_jobs()

    task = asyncio.create_task(_run_job_async(job, req))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"job_id": req.job_id}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in _jobs:
        return JSONResponse(
            status_code=404,
            content={
                "error": "job_not_found",
                "message": f"Job {job_id} not found.",
                "detail": {},
            },
        )
    job = _jobs[job_id]
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": job.get("progress"),
        "result": job.get("result"),
        "error": job.get("error"),
        "retry_with": job.get("retry_with"),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8005)
