"""Cleanup Service — full implementation.

Applies FFmpeg audio processing per segment:
- EBU R128 two-pass loudness normalisation
- Silence trimming + high-pass filter
- Clipping detection

Port: 8004
"""

import asyncio
import logging

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import cleaner as _cleaner_module
from cleaner import (
    CleanupParams as _CleanupParams,
    SegmentInput as _SegmentInput,
    SegmentResult,
    process_segment,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="FlipSync Cleanup Service")

# In-memory job store: job_id -> job dict
_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class SegmentInputModel(BaseModel):
    id: str
    input_path: str
    output_path: str


class CleanupParamsModel(BaseModel):
    target_lufs: float = -23.0
    true_peak_dbtp: float = -2.0
    lra: float = 7.0
    highpass_hz: int = 80
    silence_threshold_db: float = -50.0
    silence_min_duration_secs: float = 0.1
    clipping_threshold_db: float = -0.1
    clipping_min_consecutive_samples: int = 3
    output_sample_rate: int = 22050
    output_channels: int = 1


class JobRequest(BaseModel):
    job_id: str
    segments: list[SegmentInputModel]
    params: CleanupParamsModel = CleanupParamsModel()


# ---------------------------------------------------------------------------
# Background processing
# ---------------------------------------------------------------------------


async def _run_job(
    job_id: str,
    segments: list[SegmentInputModel],
    params: CleanupParamsModel,
) -> None:
    """Process all segments and update the in-memory job record."""
    job = _jobs[job_id]
    total = len(segments)
    results = []

    loop = asyncio.get_running_loop()
    cleaner_params = _CleanupParams(
        target_lufs=params.target_lufs,
        true_peak_dbtp=params.true_peak_dbtp,
        lra=params.lra,
        highpass_hz=params.highpass_hz,
        silence_threshold_db=params.silence_threshold_db,
        silence_min_duration_secs=params.silence_min_duration_secs,
        clipping_threshold_db=params.clipping_threshold_db,
        clipping_min_consecutive_samples=params.clipping_min_consecutive_samples,
        output_sample_rate=params.output_sample_rate,
        output_channels=params.output_channels,
    )

    for i, seg in enumerate(segments):
        cleaner_seg = _SegmentInput(
            id=seg.id,
            input_path=seg.input_path,
            output_path=seg.output_path,
        )
        try:
            result = await loop.run_in_executor(
                None, _cleaner_module.process_segment, cleaner_seg, cleaner_params
            )
        except Exception as e:
            # Unexpected error for this segment — record and continue
            logger.exception("Unexpected error processing segment %s", seg.id)
            result = SegmentResult(
                id=seg.id,
                output_path=None,
                clipping_warning=False,
                auto_rejected=False,
                error=f"ffmpeg_error: unexpected error: {e}",
            )

        results.append(
            {
                "id": result.id,
                "output_path": result.output_path,
                "clipping_warning": result.clipping_warning,
                "auto_rejected": result.auto_rejected,
                "error": result.error,
            }
        )

        processed = i + 1
        job["progress"] = int((processed / total) * 100) if total > 0 else 100
        logger.info(
            "Job %s: processed segment %s (%d/%d)", job_id, seg.id, processed, total
        )

    job["status"] = "complete"
    job["progress"] = 100
    job["results"] = results
    logger.info("Job %s complete: %d segments processed", job_id, total)


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
        "results": None,
        "error": None,
    }
    asyncio.create_task(_run_job(job_id, req.segments, req.params))
    return {"job_id": job_id}


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
    return _jobs[job_id]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8004)
