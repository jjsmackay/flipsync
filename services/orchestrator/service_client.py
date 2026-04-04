"""HTTP client primitives for FlipSync processing services.

Services use an async job pattern:
  POST /jobs → 202 with {job_id}
  GET /jobs/{job_id} → poll until status is 'complete' or 'failed'

The orchestrator polls every poll_interval seconds (default 2s per spec).
on_progress is an optional async callable invoked on each non-terminal poll.
"""

import asyncio

import httpx


async def submit_job(service_url: str, payload: dict) -> dict:
    """POST /jobs to a processing service. Returns the 202 response body."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{service_url}/jobs", json=payload)
        resp.raise_for_status()
        return resp.json()


async def poll_job(
    service_url: str,
    job_id: str,
    poll_interval: float = 2.0,
    on_progress=None,
) -> dict:
    """Poll GET /jobs/{job_id} until status is 'complete' or 'failed'.

    Calls on_progress(result) on each non-terminal poll — must be async if provided.
    Returns the final result dict.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(f"{service_url}/jobs/{job_id}")
            resp.raise_for_status()
            result = resp.json()
            if result.get("status") in ("complete", "failed"):
                return result
            if on_progress is not None:
                await on_progress(result)
            await asyncio.sleep(poll_interval)
