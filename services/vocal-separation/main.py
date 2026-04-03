"""Vocal Separation Service — placeholder implementation.

Full Demucs integration is Wave 2. This service skeleton defines the HTTP
contract so the orchestrator can be tested end-to-end with stubs.
"""

import asyncio
import uuid
from typing import Optional

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="FlipSync Vocal Separation Service")

# In-memory job store (Wave 2 will replace with real Demucs processing)
_jobs: dict[str, dict] = {}


class JobRequest(BaseModel):
    job_id: str
    input_path: str
    output_path: str
    model: str = "htdemucs"
    chunk_secs: Optional[int] = None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def submit_job(req: JobRequest):
    _jobs[req.job_id] = {
        "job_id": req.job_id,
        "status": "running",
        "progress": 0,
        "output_path": None,
        "error": None,
        "retry_with_chunk_secs": None,
    }
    # Wave 2 will launch real Demucs processing here
    return {"job_id": req.job_id}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in _jobs:
        return {"error": "not_found", "message": f"Job {job_id} not found.", "detail": {}}
    return _jobs[job_id]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
