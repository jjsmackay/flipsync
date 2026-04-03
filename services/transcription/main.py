"""Transcription Service — placeholder implementation.

Full faster-whisper integration is Wave 2.
"""

from typing import Optional

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="FlipSync Transcription Service")

_jobs: dict[str, dict] = {}


class SegmentRef(BaseModel):
    id: str
    wav_path: str


class JobRequest(BaseModel):
    job_id: str
    segments: list[SegmentRef]
    model: str = "large-v2"
    language: Optional[str] = None
    batch_size: int = 16


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def submit_job(req: JobRequest):
    _jobs[req.job_id] = {
        "job_id": req.job_id,
        "status": "running",
        "progress": 0,
        "completed_segments": [],
        "error": None,
    }
    return {"job_id": req.job_id, "segment_count": len(req.segments)}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in _jobs:
        return {"error": "not_found", "message": f"Job {job_id} not found.", "detail": {}}
    return _jobs[job_id]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
