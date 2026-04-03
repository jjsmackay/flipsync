"""Cleanup Service — placeholder implementation.

Full FFmpeg cleanup integration is Wave 2.
"""

from typing import Optional

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="FlipSync Cleanup Service")

_jobs: dict[str, dict] = {}


class SegmentInput(BaseModel):
    id: str
    input_path: str
    output_path: str


class CleanupParams(BaseModel):
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
    segments: list[SegmentInput]
    params: CleanupParams = CleanupParams()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def submit_job(req: JobRequest):
    _jobs[req.job_id] = {
        "job_id": req.job_id,
        "status": "running",
        "progress": 0,
        "results": None,
        "error": None,
    }
    return {"job_id": req.job_id}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in _jobs:
        return {"error": "not_found", "message": f"Job {job_id} not found.", "detail": {}}
    return _jobs[job_id]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8004)
