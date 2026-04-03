"""Transcription Service — faster-whisper implementation."""

import asyncio
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="FlipSync Transcription Service")

# In-memory job store: job_id -> job state dict
_jobs: dict[str, dict] = {}
# Lock per job to protect concurrent mutation during background processing
_job_locks: dict[str, asyncio.Lock] = {}


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class SegmentRef(BaseModel):
    id: str
    wav_path: str


class JobRequest(BaseModel):
    job_id: str
    segments: list[SegmentRef]
    model: str = "large-v2"
    language: Optional[str] = None
    batch_size: int = 16


# ---------------------------------------------------------------------------
# Background transcription task
# ---------------------------------------------------------------------------

async def _run_transcription(
    job_id: str,
    segments: list[dict],
    model_size: str,
    language: Optional[str],
    batch_size: int,
) -> None:
    """Background task: load model, process segments in batches, update job state."""
    loop = asyncio.get_running_loop()

    try:
        # Load (or retrieve cached) model in executor to avoid blocking event loop
        from transcriber import load_model, process_batch

        model = await loop.run_in_executor(None, load_model, model_size)

        total = len(segments)

        # Process in batches
        for batch_start in range(0, total, batch_size):
            batch = segments[batch_start : batch_start + batch_size]

            # Run blocking transcription in thread pool
            results = await loop.run_in_executor(
                None,
                process_batch,
                model,
                batch,
                language,
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

    job_state = {
        "job_id": req.job_id,
        "status": "running",
        "progress": 0,
        "completed_segments": [],
        "error": None,
    }
    _jobs[req.job_id] = job_state
    _job_locks[req.job_id] = asyncio.Lock()

    segments_plain = [{"id": s.id, "wav_path": s.wav_path} for s in req.segments]

    # Fire and forget background task
    asyncio.create_task(
        _run_transcription(
            req.job_id,
            segments_plain,
            req.model,
            req.language,
            req.batch_size,
        )
    )

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
