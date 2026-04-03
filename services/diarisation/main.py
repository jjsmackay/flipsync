"""Diarisation Service — placeholder implementation.

Full pyannote + cosine similarity integration is Wave 2.
"""

from typing import Optional

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="FlipSync Diarisation Service")

_jobs: dict[str, dict] = {}


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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def submit_job(req: JobRequest):
    _jobs[req.job_id] = {
        "job_id": req.job_id,
        "status": "running",
        "progress": 0,
        "segments": None,
        "coverage_ratio": None,
        "error": None,
    }
    return {"job_id": req.job_id}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in _jobs:
        return {"error": "not_found", "message": f"Job {job_id} not found.", "detail": {}}
    return _jobs[job_id]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
