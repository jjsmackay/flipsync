"""HTTP endpoint tests for the diarisation service.

All pyannote calls are mocked — tests run without GPU or model downloads.
"""

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# App client
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Return a TestClient with pyannote models mocked out."""
    # Patch _load_models to be a no-op so the app doesn't try to download models
    with patch("main._load_models"):
        from main import app
        with TestClient(app) as c:
            yield c


@pytest.fixture(autouse=True)
def clear_jobs():
    """Reset the in-memory job store between tests."""
    import main
    main._jobs.clear()
    yield
    main._jobs.clear()


# ---------------------------------------------------------------------------
# Test: GET /health
# ---------------------------------------------------------------------------


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Test: POST /jobs — valid request
# ---------------------------------------------------------------------------


def test_submit_job_returns_202(client, sample_wav_path, reference_wav_path, output_dir):
    job_id = str(uuid.uuid4())

    with patch("main._run_job"):  # prevent actual background execution
        resp = client.post(
            "/jobs",
            json={
                "job_id": job_id,
                "input_path": sample_wav_path,
                "reference_path": reference_wav_path,
                "output_dir": output_dir,
                "params": {
                    "min_segment_duration": 1.0,
                    "min_speakers": 1,
                    "max_speakers": 10,
                },
            },
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data["job_id"] == job_id


# ---------------------------------------------------------------------------
# Test: POST /jobs — missing required field returns 422
# ---------------------------------------------------------------------------


def test_submit_job_missing_field(client):
    resp = client.post(
        "/jobs",
        json={
            # Missing input_path, reference_path, output_dir
            "job_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 422


def test_validation_error_flat_format(client):
    """422 validation errors use the standard flat error envelope."""
    resp = client.post("/jobs", json={"job_id": str(uuid.uuid4())})
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "validation_error"
    assert "message" in data
    assert "detail" in data
    # Must NOT be FastAPI's default {"detail": [...]} shape
    assert not isinstance(data["detail"], list)


# ---------------------------------------------------------------------------
# Test: GET /jobs/{job_id} — unknown job returns not_found
# ---------------------------------------------------------------------------


def test_get_unknown_job(client):
    resp = client.get(f"/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "not_found"
    assert "message" in data
    assert "detail" in data


# ---------------------------------------------------------------------------
# Test: GET /jobs/{job_id} — after submit returns valid status dict
# ---------------------------------------------------------------------------


def test_get_job_after_submit(client, sample_wav_path, reference_wav_path, output_dir):
    job_id = str(uuid.uuid4())

    with patch("main._run_job"):
        client.post(
            "/jobs",
            json={
                "job_id": job_id,
                "input_path": sample_wav_path,
                "reference_path": reference_wav_path,
                "output_dir": output_dir,
            },
        )

    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert data["status"] in ("running", "complete", "failed")
    assert "progress" in data
    assert "segments" in data
    assert "coverage_ratio" in data
    assert "error" in data


# ---------------------------------------------------------------------------
# Test: GET /jobs/{job_id} is idempotent (two reads, same result)
# ---------------------------------------------------------------------------


def test_get_job_idempotent(client, sample_wav_path, reference_wav_path, output_dir):
    job_id = str(uuid.uuid4())

    with patch("main._run_job"):
        client.post(
            "/jobs",
            json={
                "job_id": job_id,
                "input_path": sample_wav_path,
                "reference_path": reference_wav_path,
                "output_dir": output_dir,
            },
        )

    resp1 = client.get(f"/jobs/{job_id}")
    resp2 = client.get(f"/jobs/{job_id}")
    assert resp1.json() == resp2.json()


# ---------------------------------------------------------------------------
# Test: Full pipeline execution with mocked pyannote
# ---------------------------------------------------------------------------


def _make_mock_diarization():
    """Build a minimal mock pyannote Diarization object."""
    mock_turn_0a = MagicMock()
    mock_turn_0a.start = 0.5
    mock_turn_0a.end = 3.0

    mock_turn_0b = MagicMock()
    mock_turn_0b.start = 5.0
    mock_turn_0b.end = 8.0

    mock_turn_1a = MagicMock()
    mock_turn_1a.start = 3.5
    mock_turn_1a.end = 4.8

    diarization = MagicMock()
    diarization.itertracks.return_value = [
        (mock_turn_0a, None, "SPEAKER_00"),
        (mock_turn_1a, None, "SPEAKER_01"),
        (mock_turn_0b, None, "SPEAKER_00"),
    ]
    return diarization


# ---------------------------------------------------------------------------
# Test: health reports not-ready until models are preloaded
# ---------------------------------------------------------------------------


def test_health_not_ready(client):
    import main

    main._models_ready = False
    try:
        resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["error"] == "models_not_ready"
        assert "detail" in data
    finally:
        main._models_ready = True


# ---------------------------------------------------------------------------
# Test: failure paths surface distinct error codes + messages in the poll
# ---------------------------------------------------------------------------


def _seed_running_job(main_mod, job_id):
    main_mod._jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "progress": 0,
        "segments": None,
        "coverage_ratio": None,
        "error": None,
        "message": None,
    }


def test_run_job_huggingface_token_missing():
    import main
    from diariser import HuggingFaceTokenMissing

    job_id = str(uuid.uuid4())
    _seed_running_job(main, job_id)

    with patch("main._load_models", side_effect=HuggingFaceTokenMissing("HF_TOKEN required")):
        main._run_job(job_id, MagicMock())

    job = main._jobs[job_id]
    assert job["status"] == "failed"
    assert job["error"] == "huggingface_token_missing"
    assert "HF_TOKEN" in job["message"]


def test_run_job_model_download_failed():
    import main
    from diariser import ModelDownloadFailed

    job_id = str(uuid.uuid4())
    _seed_running_job(main, job_id)

    with patch("main._load_models"), patch(
        "diariser.run_diarisation", side_effect=ModelDownloadFailed("network down")
    ):
        main._run_job(job_id, MagicMock())

    job = main._jobs[job_id]
    assert job["status"] == "failed"
    assert job["error"] == "model_download_failed"
    assert "network down" in job["message"]


def test_run_job_generic_failure_has_message():
    import main

    job_id = str(uuid.uuid4())
    _seed_running_job(main, job_id)

    with patch("main._load_models"), patch(
        "diariser.run_diarisation", side_effect=ValueError("boom in pyannote")
    ):
        main._run_job(job_id, MagicMock())

    job = main._jobs[job_id]
    assert job["status"] == "failed"
    assert job["error"] == "diarisation_failed"
    assert "boom in pyannote" in job["message"]


# ---------------------------------------------------------------------------
# Test: finished-job store is bounded; running jobs are retained
# ---------------------------------------------------------------------------


def test_finished_job_store_bounded():
    import main

    main._jobs.clear()
    running_id = "still-running"
    main._jobs[running_id] = {"job_id": running_id, "status": "running"}
    for i in range(main._MAX_FINISHED_JOBS + 5):
        main._jobs[f"done-{i}"] = {"job_id": f"done-{i}", "status": "complete"}

    main._evict_finished_jobs()

    finished = [j for j in main._jobs.values() if j["status"] in ("complete", "failed")]
    assert len(finished) == main._MAX_FINISHED_JOBS
    assert running_id in main._jobs  # running jobs never evicted


def test_full_pipeline_with_mocks(sample_wav_path, reference_wav_path, output_dir):
    """End-to-end diariser.run_diarisation with mocked pyannote."""
    import numpy as np
    from diariser import run_diarisation

    mock_pipeline = MagicMock()
    mock_pipeline.return_value = _make_mock_diarization()

    # Return distinct embeddings so cosine similarity works
    ref_emb = np.array([1.0, 0.0, 0.0])
    spk0_emb = np.array([0.9, 0.1, 0.0])   # similar to reference
    spk1_emb = np.array([0.0, 1.0, 0.0])   # dissimilar

    call_count = {"n": 0}

    def mock_embedding_call(path_or_seg):
        # First call is for reference clip
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ref_emb
        return spk0_emb  # simplified: all segment embeddings same

    def mock_crop(path, segment):
        return spk0_emb

    mock_inference = MagicMock()
    mock_inference.__call__ = MagicMock(side_effect=mock_embedding_call)
    mock_inference.crop = MagicMock(return_value=spk0_emb)

    segments, coverage_ratio = run_diarisation(
        pipeline=mock_pipeline,
        embedding_model=mock_inference,
        input_path=sample_wav_path,
        reference_path=reference_wav_path,
        output_dir=output_dir,
        min_segment_duration=1.0,
    )

    assert len(segments) > 0, "Expected at least one segment"
    # All segments returned (no filtering by confidence in service)
    assert len(segments) == 3

    # Verify segment structure
    for seg in segments:
        assert "id" in seg
        assert "start_secs" in seg
        assert "end_secs" in seg
        assert "speaker_label" in seg
        assert "match_confidence" in seg
        assert "wav_path" in seg
        # Full UUID (not truncated)
        assert len(seg["id"]) == 36
        assert Path(seg["wav_path"]).exists()

    assert isinstance(coverage_ratio, float)
    assert 0.0 <= coverage_ratio <= 1.0


def test_all_segments_returned_regardless_of_confidence(sample_wav_path, reference_wav_path, output_dir):
    """Service returns all speakers' segments even if confidence is low."""
    import numpy as np
    from diariser import run_diarisation

    mock_diarization = MagicMock()
    turn_a = MagicMock()
    turn_a.start = 0.5
    turn_a.end = 3.0
    turn_b = MagicMock()
    turn_b.start = 4.0
    turn_b.end = 7.0
    mock_diarization.itertracks.return_value = [
        (turn_a, None, "SPEAKER_00"),
        (turn_b, None, "SPEAKER_01"),
    ]

    mock_pipeline = MagicMock(return_value=mock_diarization)

    ref_emb = np.array([1.0, 0.0])
    emb_high = np.array([0.99, 0.1])
    emb_low = np.array([0.0, 1.0])

    call_counter = {"n": 0}

    def mock_call(path):
        call_counter["n"] += 1
        return ref_emb  # reference embedding

    mock_inference = MagicMock()
    mock_inference.__call__ = MagicMock(side_effect=mock_call)
    mock_inference.crop = MagicMock(side_effect=[emb_high, emb_low])

    segments, _ = run_diarisation(
        pipeline=mock_pipeline,
        embedding_model=mock_inference,
        input_path=sample_wav_path,
        reference_path=reference_wav_path,
        output_dir=output_dir,
        min_segment_duration=1.0,
    )

    # Both segments returned — orchestrator filters, not the service
    assert len(segments) == 2
    speaker_labels = {s["speaker_label"] for s in segments}
    assert "SPEAKER_00" in speaker_labels
    assert "SPEAKER_01" in speaker_labels
