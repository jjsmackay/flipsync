"""GPT-SoVITS Service — fine-tune + synthesise FastAPI app (port 8006).

Exposes:
  GET  /health
  POST /jobs           (finetune | synthesise, discriminated on ``type``)
  GET  /jobs/{job_id}

Structurally mirrors ``services/xtts/main.py`` (single-worker executor,
in-memory job store, engine-module test seam, rich-progress polling shape).
Two deliberate contract differences from xtts:

- No CPML-style licence acceptance gate. GPT-SoVITS's pretrained weights are
  MIT-licensed and public (spec §8) — ``/health`` is unconditionally healthy.
- ``SynthesiseJob.reference_wavs`` has no minimum length. A fine-tuned
  GPT-SoVITS model supplies its own stored ``reference.wav``/``reference.txt``
  from the model bundle (spec §4), so the orchestrator submits synthesise
  jobs for this engine with an empty list; xtts's ``min_length=1`` would
  wrongly 422 that request.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from pydantic import BaseModel, ConfigDict, Field

import engine
import idle_unload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

# Idle VRAM unloader + its background watcher task. Populated in the lifespan
# when IDLE_UNLOAD_SECS > 0; left None (disabled) otherwise. When enabled, the
# cached synthesis model is released from VRAM after the configured idle
# period so it doesn't squat on GPU memory another stage/service needs.
_unloader: Optional[idle_unload.IdleUnloader] = None
_idle_watch_task: Optional[asyncio.Task] = None


async def _idle_watch(idle_secs: float) -> None:
    """Poll the unloader and, when idle, run the free on the job executor.

    Running perform_unload on ``_executor`` (a single worker, shared with
    jobs) guarantees the unload never overlaps a running fine-tune/synthesis.
    """
    interval = max(1.0, min(15.0, idle_secs))
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(interval)
        try:
            if _unloader is not None and _unloader.should_unload():
                did = await loop.run_in_executor(_executor, _unloader.perform_unload)
                if did:
                    logger.info(
                        "Idle for %.0fs — released synthesis model from VRAM",
                        idle_secs,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a watcher hiccup must not kill the loop
            logger.exception("idle-unload watcher error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _unloader, _idle_watch_task
    _unloader = None
    _idle_watch_task = None
    idle_secs = idle_unload.parse_idle_secs(os.environ.get("IDLE_UNLOAD_SECS"))
    if idle_secs > 0:
        _unloader = idle_unload.IdleUnloader(
            idle_secs=idle_secs,
            is_loaded=engine.is_model_loaded,
            unload=engine.release_cached_model,
        )
        _idle_watch_task = asyncio.create_task(_idle_watch(idle_secs))
        logger.info("Idle VRAM unload enabled (IDLE_UNLOAD_SECS=%.0f)", idle_secs)
    else:
        logger.info("Idle VRAM unload disabled (IDLE_UNLOAD_SECS<=0)")

    try:
        yield
    finally:
        if _idle_watch_task is not None:
            _idle_watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _idle_watch_task
        _unloader = None
        _idle_watch_task = None


app = FastAPI(title="FlipSync GPT-SoVITS Service", lifespan=lifespan)

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


# ---------------------------------------------------------------------------
# Pydantic models — discriminated union on ``type``
# ---------------------------------------------------------------------------


class FinetuneParams(BaseModel):
    # Permissive: unknown keys (future knobs, or ones another engine's Train
    # panel sent) are ignored rather than rejected — the orchestrator's
    # per-engine `params` bag is deliberately schema-free (spec §5.4).
    model_config = ConfigDict(extra="ignore")

    sovits_epochs: int = 8
    gpt_epochs: int = 15
    batch_size: int = 4


class SynthParams(BaseModel):
    # GPT-SoVITS TTS_infer_pack sampling defaults (research §4 — TTS.run's
    # documented input dict), not xtts's — the two engines' sane defaults
    # differ even though the field names line up.
    model_config = ConfigDict(extra="ignore")

    temperature: float = 1.0
    speed: float = 1.0
    repetition_penalty: float = 1.35
    top_k: int = 15
    top_p: float = 1.0


class FinetuneJob(BaseModel):
    job_id: str
    type: Literal["finetune"]
    manifest_path: str
    output_dir: str
    params: FinetuneParams = Field(default_factory=FinetuneParams)


class SynthesiseJob(BaseModel):
    job_id: str
    type: Literal["synthesise"]
    text: str = Field(min_length=1, max_length=2000)
    language: str
    # No min_length: unlike xtts, GPT-SoVITS needs no FlipSync-side
    # conditioning audio when synthesising from a trained model (see module
    # docstring) — the orchestrator sends [] for this engine.
    reference_wavs: list[str] = Field(default_factory=list, max_length=10)
    checkpoint_dir: Optional[str] = None
    output_path: str
    params: SynthParams = Field(default_factory=SynthParams)


JobRequest = Annotated[
    Union[FinetuneJob, SynthesiseJob], Field(discriminator="type")
]


# ---------------------------------------------------------------------------
# OOM detection (copied verbatim from the xtts/vocal-separation template)
# ---------------------------------------------------------------------------


def _is_cuda_oom(exc: BaseException) -> bool:
    """True if ``exc`` represents a CUDA out-of-memory condition.

    Covers ``torch.cuda.OutOfMemoryError`` and plain ``RuntimeError``s whose
    message indicates an allocator failure — cuBLAS/cuDNN allocations surface
    OOM as a generic ``RuntimeError`` ("CUDA out of memory", "CUBLAS_STATUS_
    ALLOC_FAILED"), not as ``OutOfMemoryError``, so those must reach the
    retry path too rather than being reported as a generic error.
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

    # Free any cached synthesis model first — training needs the VRAM, and a
    # lingering preview model would also skew the pre-flight reading below.
    engine.release_cached_model()

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
            params=req.params.model_dump(),
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
                            "batch_size": max(1, req.params.batch_size // 2),
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
            params=req.params.model_dump(),
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
    try:
        if req.type == "finetune":
            await loop.run_in_executor(_executor, _run_finetune, job, req)
        else:
            await loop.run_in_executor(_executor, _run_synthesise, job, req)
    finally:
        if _unloader is not None:
            _unloader.on_finish()


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
        "type": req.type,
        "status": "running",
        "progress": {"phase": "preparing"} if req.type == "finetune" else None,
        "result": None,
        "error": None,
        "retry_with": None,
    }
    _jobs[req.job_id] = job
    _evict_old_jobs()

    # Counted from submit (not job start) so a queued job blocks idle-unload.
    if _unloader is not None:
        _unloader.on_submit()

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
    uvicorn.run(app, host="0.0.0.0", port=8006)
