"""HTTP endpoint tests for the cleanup service API."""

import asyncio
import os
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

# Add service root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main as app_module
from main import app, _run_job
from cleaner import BinaryNotFoundError

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helper constants
# ---------------------------------------------------------------------------

LOUDNORM_JSON = """{
    "input_i" : "-23.25",
    "input_tp" : "-3.59",
    "input_lra" : "5.30",
    "input_thresh" : "-33.57",
    "output_i" : "-23.00",
    "output_tp" : "-2.00",
    "output_lra" : "5.30",
    "output_thresh" : "-33.32",
    "target_offset" : "-0.25"
}"""


def _ffmpeg_success_side_effect(output_paths: list):
    """
    Return a mock subprocess.run factory that succeeds and writes WAV files
    to provided output paths in order for pass2 and pass3 calls.
    """
    call_count = [0]
    output_idx = [0]

    def mock_run(cmd, **kwargs):
        call_count[0] += 1
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""

        if call_count[0] == 1:
            # Pass 1: return loudnorm JSON
            result.stderr = LOUDNORM_JSON
        else:
            result.stderr = ""
            # Write a WAV to the output path (last arg of cmd)
            out = cmd[-1]
            sf.write(out, np.zeros(22050, dtype=np.float32), 22050, subtype="PCM_16")

        return result

    return mock_run


# ---------------------------------------------------------------------------
# Test: GET /health
# ---------------------------------------------------------------------------


def test_health_returns_ok():
    """GET /health returns 200 with {"status": "ok"}."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Test: POST /jobs
# ---------------------------------------------------------------------------


def test_post_jobs_valid_returns_202(tmp_path):
    """POST /jobs with valid request returns 202 with job_id."""
    # Clear job store to avoid state leakage between tests
    app_module._jobs.clear()

    payload = {
        "job_id": "test-job-001",
        "segments": [
            {
                "id": "seg-001",
                "input_path": str(tmp_path / "input.wav"),
                "output_path": str(tmp_path / "output" / "seg-001.wav"),
            }
        ],
    }
    response = client.post("/jobs", json=payload)
    assert response.status_code == 202
    data = response.json()
    assert data["job_id"] == "test-job-001"


def test_post_jobs_missing_required_field():
    """POST /jobs without job_id returns 422 validation error."""
    app_module._jobs.clear()

    payload = {
        # job_id is missing
        "segments": []
    }
    response = client.post("/jobs", json=payload)
    assert response.status_code == 422


def test_post_jobs_missing_segments():
    """POST /jobs without segments field returns 422 validation error."""
    app_module._jobs.clear()

    payload = {
        "job_id": "test-job-missing-segs",
        # segments is missing
    }
    response = client.post("/jobs", json=payload)
    assert response.status_code == 422


def test_validation_error_uses_flat_format():
    """C6: 422 validation errors use the standard flat error format."""
    app_module._jobs.clear()

    response = client.post("/jobs", json={"segments": []})  # missing job_id
    assert response.status_code == 422
    data = response.json()
    assert data["error"] == "validation_error"
    assert isinstance(data["message"], str)
    assert isinstance(data["detail"], dict)
    # Must NOT be FastAPI's default {"detail": [...]} shape.
    assert not isinstance(data.get("detail"), list)


def test_duplicate_job_id_returns_409(tmp_path):
    """C5: submitting a second job with an existing job_id returns 409."""
    app_module._jobs.clear()

    payload = {
        "job_id": "dup-job",
        "segments": [
            {
                "id": "seg-001",
                "input_path": str(tmp_path / "input.wav"),
                "output_path": str(tmp_path / "output" / "seg-001.wav"),
            }
        ],
    }
    first = client.post("/jobs", json=payload)
    assert first.status_code == 202

    second = client.post("/jobs", json=payload)
    assert second.status_code == 409
    data = second.json()
    assert data["error"] == "job_exists"
    assert "detail" in data


# ---------------------------------------------------------------------------
# Test: GET /jobs/{job_id}
# ---------------------------------------------------------------------------


def test_get_job_not_found():
    """GET /jobs/{job_id} on unknown job returns not_found error."""
    app_module._jobs.clear()

    response = client.get("/jobs/nonexistent-job-id")
    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "not_found"
    assert "nonexistent-job-id" in data["message"]
    assert "detail" in data


def test_get_job_after_submit_returns_valid_status(tmp_path):
    """GET /jobs/{job_id} after submit returns valid job status dict."""
    app_module._jobs.clear()

    job_id = "test-job-002"
    payload = {
        "job_id": job_id,
        "segments": [
            {
                "id": "seg-002",
                "input_path": str(tmp_path / "input.wav"),
                "output_path": str(tmp_path / "output" / "seg-002.wav"),
            }
        ],
    }
    client.post("/jobs", json=payload)

    response = client.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == job_id
    assert data["status"] in ("running", "complete", "failed")
    assert "progress" in data
    assert "results" in data
    assert "error" in data


# ---------------------------------------------------------------------------
# Test: Job does NOT abort on single segment failure
# ---------------------------------------------------------------------------


def test_job_continues_after_segment_failure(tmp_path):
    """
    CRITICAL: Job must NOT abort when one segment fails.
    Mock first segment to fail, verify other segments still processed,
    and job ends as 'complete' not 'failed'.

    This test calls _run_job directly via asyncio.run() to avoid the TestClient
    background-task scheduling limitation with asyncio.create_task.
    """
    app_module._jobs.clear()

    seg1_input = str(tmp_path / "seg1_input.wav")
    seg2_input = str(tmp_path / "seg2_input.wav")
    seg1_output = str(tmp_path / "output" / "seg1.wav")
    seg2_output = str(tmp_path / "output" / "seg2.wav")

    # Write real WAV files for inputs
    sf.write(seg1_input, np.zeros(22050, dtype=np.float32), 22050)
    sf.write(seg2_input, np.zeros(22050, dtype=np.float32), 22050)

    job_id = "test-job-resilience"

    # Pre-register the job (simulating POST /jobs)
    app_module._jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "progress": 0,
        "results": None,
        "error": None,
    }

    # Build segment and params models (same types _run_job expects)
    from main import SegmentInputModel, CleanupParamsModel

    segments = [
        SegmentInputModel(id="seg-fail", input_path=seg1_input, output_path=seg1_output),
        SegmentInputModel(id="seg-ok", input_path=seg2_input, output_path=seg2_output),
    ]
    params = CleanupParamsModel()

    call_count = [0]

    def mock_process_segment(segment, cleaner_params):
        from cleaner import SegmentResult
        call_count[0] += 1
        if segment.id == "seg-fail":
            return SegmentResult(
                id=segment.id,
                output_path=None,
                clipping_warning=False,
                auto_rejected=False,
                error="ffmpeg_error: exit code 1, simulated failure",
            )
        else:
            return SegmentResult(
                id=segment.id,
                output_path=segment.output_path,
                clipping_warning=False,
                auto_rejected=False,
                error=None,
            )

    with patch("cleaner.process_segment", side_effect=mock_process_segment):
        asyncio.run(_run_job(job_id, segments, params))

    data = app_module._jobs[job_id]

    # Job must be complete, not failed
    assert data["status"] == "complete", f"Expected 'complete', got {data.get('status')}"

    # Both segments must have been processed
    assert call_count[0] == 2, f"Expected 2 segments processed, got {call_count[0]}"

    results = data["results"]
    assert results is not None
    assert len(results) == 2

    # Find results by id
    res_by_id = {r["id"]: r for r in results}

    # First segment: failed, no output_path, has error
    assert res_by_id["seg-fail"]["output_path"] is None
    assert res_by_id["seg-fail"]["error"] is not None
    assert "ffmpeg_error" in res_by_id["seg-fail"]["error"]

    # Second segment: success, has output_path, no error
    assert res_by_id["seg-ok"]["output_path"] == seg2_output
    assert res_by_id["seg-ok"]["error"] is None


# ---------------------------------------------------------------------------
# Test: C3 — missing binary fails the whole job (not N per-segment errors)
# ---------------------------------------------------------------------------


def test_missing_binary_fails_job(tmp_path):
    """C3: a missing ffmpeg binary must fail the entire job with a job-level
    error, NOT complete with per-segment errors (which would make the
    orchestrator auto-reject the user's whole approved set)."""
    app_module._jobs.clear()

    seg_input = str(tmp_path / "in.wav")
    sf.write(seg_input, np.zeros(22050, dtype=np.float32), 22050)

    job_id = "job-nobin"
    app_module._jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "progress": 0,
        "results": None,
        "error": None,
    }

    from main import SegmentInputModel, CleanupParamsModel

    segments = [
        SegmentInputModel(id="seg-a", input_path=seg_input, output_path=str(tmp_path / "a.wav")),
        SegmentInputModel(id="seg-b", input_path=seg_input, output_path=str(tmp_path / "b.wav")),
    ]

    def raise_binary_missing(segment, params):
        raise BinaryNotFoundError("ffmpeg binary not found")

    with patch("cleaner.process_segment", side_effect=raise_binary_missing):
        asyncio.run(_run_job(job_id, segments, CleanupParamsModel()))

    data = app_module._jobs[job_id]
    assert data["status"] == "failed"
    assert data["results"] is None
    assert data["error"] is not None
    assert "binary_not_found" in data["error"]


# ---------------------------------------------------------------------------
# Test: auto_rejected segment has output_path = null
# ---------------------------------------------------------------------------


def test_auto_rejected_segment_has_null_output_path(tmp_path):
    """auto_rejected segment must have output_path = null in the result."""
    app_module._jobs.clear()

    seg_input = str(tmp_path / "seg_silent_input.wav")
    seg_output = str(tmp_path / "output" / "seg_silent.wav")
    sf.write(seg_input, np.zeros(22050, dtype=np.float32), 22050)

    job_id = "test-job-auto-reject"
    payload = {
        "job_id": job_id,
        "segments": [
            {"id": "seg-silent", "input_path": seg_input, "output_path": seg_output},
        ],
    }

    def mock_process_segment(segment, params):
        from cleaner import SegmentResult
        return SegmentResult(
            id=segment.id,
            output_path=None,
            clipping_warning=False,
            auto_rejected=True,
            error=None,
        )

    with patch("cleaner.process_segment", side_effect=mock_process_segment):
        client.post("/jobs", json=payload)

        deadline = time.time() + 5.0
        while time.time() < deadline:
            resp = client.get(f"/jobs/{job_id}")
            if resp.json().get("status") == "complete":
                break
            time.sleep(0.05)

    data = client.get(f"/jobs/{job_id}").json()
    assert data["status"] == "complete"
    results = data["results"]
    assert len(results) == 1
    assert results[0]["auto_rejected"] is True
    assert results[0]["output_path"] is None
    assert results[0]["error"] is None


# ---------------------------------------------------------------------------
# Test: successful segment has correct output_path
# ---------------------------------------------------------------------------


def test_successful_segment_has_output_path_and_no_clipping_warning(tmp_path):
    """Successful segment result has correct output_path and clipping_warning=False."""
    app_module._jobs.clear()

    seg_input = str(tmp_path / "clean_input.wav")
    seg_output = str(tmp_path / "output" / "clean_out.wav")
    sf.write(seg_input, np.zeros(22050, dtype=np.float32), 22050)

    job_id = "test-job-success"
    payload = {
        "job_id": job_id,
        "segments": [
            {"id": "seg-clean", "input_path": seg_input, "output_path": seg_output},
        ],
    }

    def mock_process_segment(segment, params):
        from cleaner import SegmentResult
        return SegmentResult(
            id=segment.id,
            output_path=segment.output_path,
            clipping_warning=False,
            auto_rejected=False,
            error=None,
        )

    with patch("cleaner.process_segment", side_effect=mock_process_segment):
        client.post("/jobs", json=payload)

        deadline = time.time() + 5.0
        while time.time() < deadline:
            resp = client.get(f"/jobs/{job_id}")
            if resp.json().get("status") == "complete":
                break
            time.sleep(0.05)

    data = client.get(f"/jobs/{job_id}").json()
    assert data["status"] == "complete"
    results = data["results"]
    assert len(results) == 1
    assert results[0]["output_path"] == seg_output
    assert results[0]["clipping_warning"] is False
    assert results[0]["error"] is None
    assert results[0]["auto_rejected"] is False
