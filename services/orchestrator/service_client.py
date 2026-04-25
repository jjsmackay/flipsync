"""Generic HTTP client for calling processing services.

Handles:
- POST /jobs to submit work
- GET /jobs/{job_id} polling at 2-second intervals
- GET /health startup polling with configurable timeout (default 5 minutes)

Service URLs are read from environment variables:
  VOCAL_SEPARATION_URL, DIARISATION_URL, TRANSCRIPTION_URL, CLEANUP_URL
"""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

SERVICE_URLS: dict[str, str] = {
    "vocal_separation": os.environ.get("VOCAL_SEPARATION_URL", "http://vocal-separation:8001"),
    "diarisation": os.environ.get("DIARISATION_URL", "http://diarisation:8002"),
    "transcription": os.environ.get("TRANSCRIPTION_URL", "http://transcription:8003"),
    "cleanup": os.environ.get("CLEANUP_URL", "http://cleanup:8004"),
}

# Shared async client — created lazily, reused across requests.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


def get_service_url(service_name: str) -> str:
    url = SERVICE_URLS.get(service_name)
    if url is None:
        raise ValueError(f"Unknown service: {service_name}")
    return url


async def check_health(service_name: str, timeout_secs: int = 300) -> bool:
    """Poll GET /health until 200 or timeout. Returns True if healthy."""
    url = get_service_url(service_name)
    client = _get_client()
    deadline = asyncio.get_event_loop().time() + timeout_secs

    while asyncio.get_event_loop().time() < deadline:
        try:
            resp = await client.get(f"{url}/health", timeout=5)
            if resp.status_code == 200:
                logger.info("Service %s is healthy at %s", service_name, url)
                return True
        except Exception:
            pass
        await asyncio.sleep(5)

    logger.warning("Service %s at %s did not become healthy within %ds", service_name, url, timeout_secs)
    return False


async def submit_job(service_name: str, payload: dict) -> dict:
    """POST /jobs to a processing service. Returns the response JSON.

    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    url = get_service_url(service_name)
    client = _get_client()
    resp = await client.post(f"{url}/jobs", json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


async def poll_job(service_name: str, job_id: str) -> dict:
    """GET /jobs/{job_id} once. Returns the response JSON."""
    url = get_service_url(service_name)
    client = _get_client()
    resp = await client.get(f"{url}/jobs/{job_id}", timeout=30)
    resp.raise_for_status()
    return resp.json()


async def poll_until_complete(
    service_name: str,
    job_id: str,
    interval_secs: float = 2.0,
    on_progress: callable = None,
) -> dict:
    """Poll GET /jobs/{job_id} every interval_secs until status is complete or failed.

    Args:
        service_name: Which service to poll.
        job_id: The job ID to poll.
        interval_secs: Seconds between polls (default 2).
        on_progress: Optional callback(poll_response_dict) called on each poll.

    Returns:
        The final poll response dict with status 'complete' or 'failed'.
    """
    while True:
        result = await poll_job(service_name, job_id)
        status = result.get("status")

        if on_progress is not None:
            on_progress(result)

        if status in ("complete", "failed"):
            return result

        await asyncio.sleep(interval_secs)
