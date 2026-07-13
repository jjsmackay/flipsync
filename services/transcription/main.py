"""Transcription Service — faster-whisper implementation."""

import asyncio
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, model_validator

from transcriber import VALID_MODELS, load_model, process_batch

app = FastAPI(title="FlipSync Transcription Service")

# In-memory job store: job_id -> job state dict
_jobs: dict[str, dict] = {}
# Lock per job to protect concurrent mutation during background processing
_job_locks: dict[str, asyncio.Lock] = {}
# Strong references to in-flight background tasks so they are not garbage
# collected mid-run (asyncio only holds weak refs to bare create_task results).
_background_tasks: set[asyncio.Task] = set()

# Bound the job store so a long-running service does not leak memory. Once the
# cap is reached, the oldest terminal (complete/failed) jobs are evicted.
MAX_JOBS = 500


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class SegmentRef(BaseModel):
    id: str
    wav_path: str
    # Absolute start of the segment within its source; required when
    # resegment is true so child timestamps can be made absolute.
    start_secs: Optional[float] = None
    # Whether the service may split this segment into sentence-aligned
    # children (spec/pipeline.md §Sentence-aligned re-segmentation).
    resegment: bool = False

    @model_validator(mode="after")
    def _start_required_for_resegment(self) -> "SegmentRef":
        if self.resegment and self.start_secs is None:
            raise ValueError("start_secs is required when resegment is true.")
        return self


VALID_COMPUTE_TYPES = {"default", "float16", "int8_float16", "int8"}


class JobRequest(BaseModel):
    job_id: str
    segments: list[SegmentRef]
    model: str = "large-v2"
    language: Optional[str] = None
    batch_size: int = 16
    # 'default' keeps the device-derived precision (float16 GPU / int8 CPU); the
    # others let the orchestrator trade precision for VRAM on a constrained GPU.
    compute_type: str = "default"
    # Whisper decoding knobs. beam_size 5 and vad_filter off are faster-whisper's
    # own defaults, so an unset job behaves exactly as before.
    beam_size: int = 5
    vad_filter: bool = False
    # Optional forced-alignment pass (project's align_words config). Off by
    # default so an unset job behaves exactly as before; it only takes effect
    # for segments that are also being re-segmented (see transcriber.py).
    align: bool = False

    @field_validator("beam_size")
    @classmethod
    def _validate_beam_size(cls, v: int) -> int:
        if v < 1:
            raise ValueError("beam_size must be a positive integer.")
        return v

    @field_validator("model")
    @classmethod
    def _validate_model(cls, v: str) -> str:
        if v not in VALID_MODELS:
            raise ValueError(
                f"Invalid model '{v}'. Must be one of: {sorted(VALID_MODELS)}."
            )
        return v

    @field_validator("batch_size")
    @classmethod
    def _validate_batch_size(cls, v: int) -> int:
        if v < 1:
            raise ValueError("batch_size must be a positive integer.")
        return v

    @field_validator("compute_type")
    @classmethod
    def _validate_compute_type(cls, v: str) -> str:
        if v not in VALID_COMPUTE_TYPES:
            raise ValueError(
                f"Invalid compute_type '{v}'. Must be one of: {sorted(VALID_COMPUTE_TYPES)}."
            )
        return v


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return validation errors in the flat service error format."""
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": "Request validation failed.",
            "detail": {"errors": jsonable_encoder(exc.errors())},
        },
    )


# ---------------------------------------------------------------------------
# Job store maintenance
# ---------------------------------------------------------------------------

def _evict_if_full() -> None:
    """Evict oldest terminal jobs when the store is at capacity."""
    while len(_jobs) >= MAX_JOBS:
        evicted = False
        for jid, job in list(_jobs.items()):
            if job.get("status") in ("complete", "failed"):
                _jobs.pop(jid, None)
                _job_locks.pop(jid, None)
                evicted = True
                break
        if not evicted:
            # All jobs still running; do not evict live state.
            break


# ---------------------------------------------------------------------------
# Background transcription task
# ---------------------------------------------------------------------------

async def _run_transcription(
    job_id: str,
    segments: list[dict],
    model_size: str,
    language: Optional[str],
    batch_size: int,
    compute_type: str = "default",
    beam_size: int = 5,
    vad_filter: bool = False,
    align: bool = False,
) -> None:
    """Background task: load model, process segments in batches, update job state."""
    loop = asyncio.get_running_loop()

    try:
        # Load (or retrieve cached) model in executor to avoid blocking event loop.
        # batch_size drives CTranslate2 worker count for concurrent transcription.
        model = await loop.run_in_executor(
            None, load_model, model_size, batch_size, compute_type
        )

        total = len(segments)

        # Process in batches; each batch's segments transcribe concurrently
        # (bounded by batch_size) inside process_batch.
        for batch_start in range(0, total, batch_size):
            batch = segments[batch_start : batch_start + batch_size]

            # Run blocking transcription in thread pool
            results = await loop.run_in_executor(
                None,
                process_batch,
                model,
                batch,
                language,
                batch_size,
                beam_size,
                vad_filter,
                align,
            )

            # Append results to cumulative list (thread-safe via asyncio lock)
            lock = _job_locks.get(job_id)
            if lock is None:
                # Job was removed; abort
                return
            async with lock:
                job = _jobs.get(job_id)
                if job is None:
                    return
                job["completed_segments"].extend(results)
                completed = len(job["completed_segments"])
                job["progress"] = int((completed / total) * 100) if total > 0 else 100

        # Mark complete
        lock = _job_locks.get(job_id)
        if lock:
            async with lock:
                job = _jobs.get(job_id)
                if job:
                    job["status"] = "complete"
                    job["progress"] = 100

    except Exception as exc:
        lock = _job_locks.get(job_id)
        if lock:
            async with lock:
                job = _jobs.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = str(exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


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

    _evict_if_full()

    job_state = {
        "job_id": req.job_id,
        "status": "running",
        "progress": 0,
        "completed_segments": [],
        "error": None,
    }
    _jobs[req.job_id] = job_state
    _job_locks[req.job_id] = asyncio.Lock()

    segments_plain = [
        {
            "id": s.id,
            "wav_path": s.wav_path,
            "start_secs": s.start_secs,
            "resegment": s.resegment,
        }
        for s in req.segments
    ]

    # Launch background task and retain a strong reference until it finishes.
    task = asyncio.create_task(
        _run_transcription(
            req.job_id,
            segments_plain,
            req.model,
            req.language,
            req.batch_size,
            req.compute_type,
            req.beam_size,
            req.vad_filter,
            req.align,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"job_id": req.job_id, "segment_count": len(req.segments)}


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

    lock = _job_locks.get(job_id)
    if lock:
        async with lock:
            # Return a shallow copy to avoid mutation while serialising
            job = dict(_jobs[job_id])
            job["completed_segments"] = list(job["completed_segments"])
    else:
        job = dict(_jobs[job_id])

    return job


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
