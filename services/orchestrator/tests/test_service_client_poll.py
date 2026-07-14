"""poll_until_complete must translate polling HTTP/transport errors into a clean
terminal 'failed' result — never leaking the internal service URL into the job
error the user sees.

Regression: an xtts restart mid-job made GET /jobs/{id} return 404; the raw
httpx.HTTPStatusError string ("... for url 'http://xtts:8005/jobs/...'") escaped
all the way into the job error field and was shown to the user.
"""

import asyncio

import httpx
import pytest
from unittest.mock import patch

import service_client


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _http_status_error(status: int, url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    response = httpx.Response(status, request=request)
    # Mirror httpx's own message, which embeds the URL.
    return httpx.HTTPStatusError(
        f"Client error '{status} Not Found' for url '{url}'",
        request=request,
        response=response,
    )


class TestPollErrorTranslation:
    def test_404_returns_clean_failed_without_url(self):
        url = "http://xtts:8005/jobs/abc"

        async def raise_404(service, job_id):
            raise _http_status_error(404, url)

        with patch("service_client.poll_job", new=raise_404):
            result = _run(service_client.poll_until_complete("xtts", "abc", interval_secs=0))

        assert result["status"] == "failed"
        err = result["error"]
        # No internal address, no scheme, no port — nothing routable leaks.
        assert "xtts:8005" not in err
        assert "http://" not in err
        assert "8005" not in err
        # Still a meaningful, job-scoped message.
        assert "job" in err.lower()

    def test_other_http_status_returns_clean_failed_without_url(self):
        url = "http://xtts:8005/jobs/abc"

        async def raise_500(service, job_id):
            raise _http_status_error(500, url)

        with patch("service_client.poll_job", new=raise_500):
            result = _run(service_client.poll_until_complete("xtts", "abc", interval_secs=0))

        assert result["status"] == "failed"
        assert "http://" not in result["error"]
        assert "xtts:8005" not in result["error"]

    def test_transport_error_returns_clean_failed_without_url(self):
        async def raise_conn(service, job_id):
            raise httpx.ConnectError(
                "connection refused",
                request=httpx.Request("GET", "http://xtts:8005/jobs/abc"),
            )

        with patch("service_client.poll_job", new=raise_conn):
            result = _run(service_client.poll_until_complete("xtts", "abc", interval_secs=0))

        assert result["status"] == "failed"
        assert "http://" not in result["error"]
        assert "xtts:8005" not in result["error"]

    def test_normal_completion_still_passes_through(self):
        async def ok(service, job_id):
            return {"status": "complete", "result": {"final_eval_loss": 0.1}}

        with patch("service_client.poll_job", new=ok):
            result = _run(service_client.poll_until_complete("xtts", "abc", interval_secs=0))

        assert result["status"] == "complete"
