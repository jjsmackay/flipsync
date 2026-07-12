"""Diarisation Service — FastAPI application.

Exposes:
  GET  /health
  POST /jobs
  GET  /jobs/{job_id}

Jobs are processed asynchronously in a background thread pool so that
blocking pyannote calls do not stall the event loop.
"""

import asyncio
import contextlib
import logging
import os
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

import uvicorn

import idle_unload
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# In-memory job store
_jobs: dict[str, dict] = {}

# Retain background job futures so they aren't garbage-collected mid-run.
_tasks: set[Future] = set()

# Thread pool for blocking pyannote calls (one worker keeps GPU usage sequential)
_executor = ThreadPoolExecutor(max_workers=1)

# Lazy-loaded models (populated by the lifespan preload, reused across jobs)
_pipeline = None
_embedding_model = None

# Readiness / startup state
_models_ready = False
_startup_error: Optional[str] = None

# Keep at most this many finished jobs in memory before evicting the oldest.
_MAX_FINISHED_JOBS = 100

# Idle VRAM unloader + its background watcher task. Enabled in the lifespan when
# IDLE_UNLOAD_SECS > 0; the pyannote pipeline + embedding model are released
# from VRAM after the configured idle period so they don't squat on GPU memory a
# later pipeline stage (transcription, in another container) needs.
_unloader: Optional[idle_unload.IdleUnloader] = None
_idle_watch_task: Optional[asyncio.Task] = None


def _is_loaded() -> bool:
    """True while either pyannote model is resident (and thus holding VRAM)."""
    return _pipeline is not None or _embedding_model is not None


def _unload_models() -> None:
    """Drop the pyannote pipeline + embedding model and return VRAM to the driver.

    Readiness (`_models_ready`) is intentionally left untouched — it means "loaded
    successfully at least once", so /health stays green and the orchestrator keeps
    submitting; the next job just reloads via ``_load_models`` on demand. Torch may
    be absent in unit tests, so the empty_cache is best-effort.
    """
    global _pipeline, _embedding_model
    import gc

    _pipeline = None
    _embedding_model = None
    gc.collect()
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:
        pass


async def _idle_watch(idle_secs: float) -> None:
    """Poll the unloader and, when idle, run the free on the job executor.

    Running perform_unload on ``_executor`` (single worker, shared with jobs)
    guarantees the unload never overlaps a running diarisation. should_unload is
    checked first on the event loop so the executor task is only ever queued when
    the model is genuinely idle.
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
                        "Idle for %.0fs — released pyannote models from VRAM", idle_secs
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a watcher hiccup must not kill the loop
            logger.exception("idle-unload watcher error")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Preload pyannote models before the service reports ready.

    The spec allows up to a 5-minute startup while ~2 GB of models download.
    Loading here means a missing/invalid token or download failure is surfaced
    at startup (loudly logged) rather than mid-pipeline.
    """
    global _models_ready, _startup_error, _unloader, _idle_watch_task
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_executor, _load_models)
        _models_ready = True
        logger.info("Diarisation models preloaded; service is ready.")
    except Exception as exc:  # noqa: BLE001 — log loudly, keep container up for diagnosis
        _startup_error = str(exc)
        logger.error("FATAL: model preload failed — service not ready: %s", exc, exc_info=True)

    _unloader = None
    _idle_watch_task = None
    idle_secs = idle_unload.parse_idle_secs(os.environ.get("IDLE_UNLOAD_SECS"))
    if idle_secs > 0:
        _unloader = idle_unload.IdleUnloader(
            idle_secs=idle_secs, is_loaded=_is_loaded, unload=_unload_models
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


app = FastAPI(title="FlipSync Diarisation Service", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DiariseParams(BaseModel):
    min_segment_duration: float = 1.0
    min_speakers: int = 1
    max_speakers: int = 10
    # Scout-only. When set, pyannote is forced to this exact speaker count and
    # min/max are ignored. Left None for match mode and default scouts.
    num_speakers: Optional[int] = None
    # Scout-only. Bound the per-speaker curation pool so a long source is no
    # more overwhelming than a short one.
    pool_max_secs: float = 90.0
    pool_max_turns: int = 20


class JobRequest(BaseModel):
    job_id: str
    input_path: str
    reference_path: Optional[str] = None
    output_dir: str
    params: DiariseParams = DiariseParams()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_response(code: str, message: str, detail: dict | None = None, status_code: int = 400):
    return JSONResponse(
        status_code=status_code,
        content={"error": code, "message": message, "detail": detail or {}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return validation errors in the standard flat error format."""
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": "Request validation failed.",
            "detail": {"errors": jsonable_encoder(exc.errors())},
        },
    )


def _load_models():
    """Load pyannote pipeline and embedding model (once, at startup)."""
    global _pipeline, _embedding_model
    if _pipeline is None or _embedding_model is None:
        logger.info("Loading pyannote models…")
        from diariser import load_pipeline, load_embedding_model

        _pipeline = load_pipeline()
        _embedding_model = load_embedding_model()
        logger.info("pyannote models loaded")


def _evict_finished_jobs():
    """Bound the job store: drop the oldest finished jobs beyond the cap.

    dict preserves insertion order, so the earliest-submitted finished jobs
    are evicted first. Running jobs are always retained.
    """
    finished = [jid for jid, job in _jobs.items() if job["status"] in ("complete", "failed")]
    excess = len(finished) - _MAX_FINISHED_JOBS
    if excess <= 0:
        # Guard: a negative excess would slice from the END of the list
        # (finished[:-1] etc.), evicting almost every finished job.
        return
    for jid in finished[:excess]:
        _jobs.pop(jid, None)


def _run_job(job_id: str, request: JobRequest):
    """Blocking job runner — executes in thread pool.

    A null ``reference_path`` selects scout mode (reference-less diarisation
    yielding per-speaker montages); otherwise the existing match-mode
    diarisation runs.
    """
    from diariser import DiarisationError, run_diarisation, run_scout

    def _progress(pct: int):
        if job_id in _jobs:
            _jobs[job_id]["progress"] = pct

    try:
        _load_models()

        if request.reference_path is None:
            speakers = run_scout(
                pipeline=_pipeline,
                input_path=request.input_path,
                output_dir=request.output_dir,
                min_segment_duration=request.params.min_segment_duration,
                min_speakers=request.params.min_speakers,
                max_speakers=request.params.max_speakers,
                num_speakers=request.params.num_speakers,
                pool_max_secs=request.params.pool_max_secs,
                pool_max_turns=request.params.pool_max_turns,
                progress_callback=_progress,
            )
            _jobs[job_id].update(
                {
                    "status": "complete",
                    "progress": 100,
                    "speakers": speakers,
                    "error": None,
                    "message": None,
                }
            )
            logger.info("Scout job %s complete — %d speakers", job_id, len(speakers))
        else:
            segments, coverage_ratio = run_diarisation(
                pipeline=_pipeline,
                embedding_model=_embedding_model,
                input_path=request.input_path,
                reference_path=request.reference_path,
                output_dir=request.output_dir,
                min_segment_duration=request.params.min_segment_duration,
                min_speakers=request.params.min_speakers,
                max_speakers=request.params.max_speakers,
                progress_callback=_progress,
            )
            _jobs[job_id].update(
                {
                    "status": "complete",
                    "progress": 100,
                    "segments": segments,
                    "coverage_ratio": coverage_ratio,
                    "error": None,
                    "message": None,
                }
            )
            logger.info("Job %s complete — %d segments", job_id, len(segments))

    except DiarisationError as exc:
        logger.exception("Job %s failed (%s): %s", job_id, exc.error_code, exc)
        _jobs[job_id].update(
            {
                "status": "failed",
                "segments": None,
                "coverage_ratio": None,
                "speakers": None,
                "error": exc.error_code,
                "message": str(exc),
            }
        )
    except Exception as exc:
        logger.exception("Job %s failed: %s", job_id, exc)
        _jobs[job_id].update(
            {
                "status": "failed",
                "segments": None,
                "coverage_ratio": None,
                "speakers": None,
                "error": "diarisation_failed",
                "message": str(exc),
            }
        )
    finally:
        _evict_finished_jobs()
        # Restart the idle clock once the job is done, whatever the outcome.
        if _unloader is not None:
            _unloader.on_finish()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    if not _models_ready:
        return _error_response(
            "models_not_ready",
            _startup_error or "Models are still loading.",
            status_code=503,
        )
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def submit_job(req: JobRequest):
    job_id = req.job_id

    if job_id in _jobs:
        # Duplicate submit (e.g. an orchestrator retry after a timed-out but
        # delivered POST) must not restart or overwrite the original job.
        return _error_response(
            "job_exists",
            f"Job {job_id} already exists.",
            status_code=409,
        )

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "mode": "scout" if req.reference_path is None else "match",
        "progress": 0,
        "segments": None,
        "coverage_ratio": None,
        "speakers": None,
        "error": None,
        "message": None,
    }

    # Count from accept so a queued job blocks an idle unload before it starts.
    if _unloader is not None:
        _unloader.on_submit()

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor, _run_job, job_id, req)
    _tasks.add(future)
    future.add_done_callback(_tasks.discard)

    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in _jobs:
        return _error_response(
            "not_found",
            f"Job {job_id} not found.",
            status_code=404,
        )
    return _jobs[job_id]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
