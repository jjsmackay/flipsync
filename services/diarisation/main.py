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
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    token = os.environ.get("HF_TOKEN")
    if not token:
        logger.warning(
            "HF_TOKEN is not set. pyannote models cannot be downloaded. "
            "Jobs will fail until HF_TOKEN is provided."
        )
    else:
        logger.info("HF_TOKEN is set. Models will be downloaded on first job.")
    yield


app = FastAPI(title="FlipSync Diarisation Service", lifespan=lifespan)

# In-memory job store
_jobs: dict[str, dict] = {}

# Thread pool for blocking pyannote calls (one worker keeps GPU usage sequential)
_executor = ThreadPoolExecutor(max_workers=1)

# Lazy-loaded models (None until first job)
_pipeline = None
_embedding_model = None


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DiariseParams(BaseModel):
    min_segment_duration: float = 1.0
    min_speakers: int = 1
    max_speakers: int = 10


class JobRequest(BaseModel):
    job_id: str
    input_path: str
    reference_path: str
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


def _load_models():
    """Load pyannote pipeline and embedding model (called once in background)."""
    global _pipeline, _embedding_model
    if _pipeline is None or _embedding_model is None:
        logger.info("Loading pyannote models…")
        from diariser import load_pipeline, load_embedding_model

        _pipeline = load_pipeline()
        _embedding_model = load_embedding_model()
        logger.info("pyannote models loaded")


def _run_job(job_id: str, request: JobRequest):
    """Blocking job runner — executes in thread pool."""
    from diariser import run_diarisation

    def _progress(pct: int):
        if job_id in _jobs:
            _jobs[job_id]["progress"] = pct

    try:
        _load_models()

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
            }
        )
        logger.info("Job %s complete — %d segments", job_id, len(segments))

    except Exception as exc:
        logger.exception("Job %s failed: %s", job_id, exc)
        _jobs[job_id].update(
            {
                "status": "failed",
                "segments": None,
                "coverage_ratio": None,
                "error": "diarisation_failed",
            }
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def submit_job(req: JobRequest):
    job_id = req.job_id

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "progress": 0,
        "segments": None,
        "coverage_ratio": None,
        "error": None,
    }

    loop = asyncio.get_running_loop()
    loop.run_in_executor(_executor, _run_job, job_id, req)

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
