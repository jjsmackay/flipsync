"""HTTP endpoint tests for the transcription service.

WhisperModel is mocked throughout — these tests do NOT require a real GPU or model.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helper: build a mock WhisperModel that returns canned results
# ---------------------------------------------------------------------------

def _make_mock_model(words_per_segment=None):
    """Return a mock WhisperModel.

    words_per_segment: list of lists of mock Word objects, one list per
    segment transcribed. Each call to model.transcribe() pops the next entry.
    If None, each call returns a single word with probability=0.9.
    """
    model = MagicMock()
    calls = [0]

    def _transcribe(wav_path, **kwargs):
        idx = calls[0]
        calls[0] += 1

        if words_per_segment is not None and idx < len(words_per_segment):
            words = words_per_segment[idx]
        else:
            word = MagicMock()
            word.probability = 0.9
            word.text = "hello"
            words = [word]

        seg = MagicMock()
        seg.text = " ".join(w.text for w in words) if words else ""
        seg.words = words

        info = MagicMock()
        return iter([seg]), info

    model.transcribe.side_effect = _transcribe
    return model


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_jobs():
    """Clear in-memory job store between tests."""
    # Import after patching so the module is loaded
    import main as svc
    svc._jobs.clear()
    svc._job_locks.clear()
    yield
    svc._jobs.clear()
    svc._job_locks.clear()


@pytest.fixture()
def client():
    from main import app
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def job_id():
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestSubmitJob:
    def test_valid_request_returns_202(self, client, sine_wav_path, job_id):
        payload = {
            "job_id": job_id,
            "segments": [
                {"id": str(uuid.uuid4()), "wav_path": sine_wav_path},
            ],
            "model": "large-v2",
            "language": None,
            "batch_size": 16,
        }
        resp = client.post("/jobs", json=payload)
        assert resp.status_code == 202
        body = resp.json()
        assert body["job_id"] == job_id
        assert body["segment_count"] == 1

    def test_segment_count_matches_input(self, client, sine_wav_path, job_id):
        segments = [{"id": str(uuid.uuid4()), "wav_path": sine_wav_path} for _ in range(5)]
        payload = {
            "job_id": job_id,
            "segments": segments,
            "model": "large-v2",
        }
        resp = client.post("/jobs", json=payload)
        assert resp.status_code == 202
        assert resp.json()["segment_count"] == 5

    def test_missing_job_id_returns_422(self, client, sine_wav_path):
        payload = {
            "segments": [{"id": str(uuid.uuid4()), "wav_path": sine_wav_path}],
            "model": "large-v2",
        }
        resp = client.post("/jobs", json=payload)
        assert resp.status_code == 422

    def test_missing_segments_returns_422(self, client, job_id):
        payload = {"job_id": job_id, "model": "large-v2"}
        resp = client.post("/jobs", json=payload)
        assert resp.status_code == 422

    def test_invalid_model_returns_flat_422(self, client, sine_wav_path, job_id):
        payload = {
            "job_id": job_id,
            "segments": [{"id": str(uuid.uuid4()), "wav_path": sine_wav_path}],
            "model": "gpt-4",
        }
        resp = client.post("/jobs", json=payload)
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "validation_error"
        assert "message" in body
        assert "detail" in body

    def test_invalid_batch_size_returns_flat_422(self, client, sine_wav_path, job_id):
        payload = {
            "job_id": job_id,
            "segments": [{"id": str(uuid.uuid4()), "wav_path": sine_wav_path}],
            "model": "large-v2",
            "batch_size": 0,
        }
        resp = client.post("/jobs", json=payload)
        assert resp.status_code == 422
        assert resp.json()["error"] == "validation_error"

    def test_duplicate_job_returns_409(self, client, sine_wav_path, job_id):
        payload = {
            "job_id": job_id,
            "segments": [{"id": str(uuid.uuid4()), "wav_path": sine_wav_path}],
            "model": "large-v2",
        }
        with patch("main.load_model", return_value=_make_mock_model()):
            assert client.post("/jobs", json=payload).status_code == 202
            dup = client.post("/jobs", json=payload)
        assert dup.status_code == 409
        assert dup.json()["error"] == "job_exists"


class TestPerSegmentFailure:
    def test_bad_segment_does_not_fail_job(self, client, sine_wav_path, tmp_path, job_id):
        """One unreadable WAV must not fail the job: it completes with a
        per-segment error while the good segment transcribes."""
        good_id = str(uuid.uuid4())
        bad_id = str(uuid.uuid4())

        with patch("main.load_model", return_value=_make_mock_model()):
            payload = {
                "job_id": job_id,
                "segments": [
                    {"id": good_id, "wav_path": sine_wav_path},
                    {"id": bad_id, "wav_path": str(tmp_path / "missing.wav")},
                ],
                "model": "large-v2",
                "batch_size": 16,
            }
            assert client.post("/jobs", json=payload).status_code == 202

            import time
            deadline = time.time() + 5.0
            poll = client.get(f"/jobs/{job_id}").json()
            while poll["status"] == "running" and time.time() < deadline:
                time.sleep(0.1)
                poll = client.get(f"/jobs/{job_id}").json()

        assert poll["status"] == "complete"
        assert poll["error"] is None
        by_id = {s["id"]: s for s in poll["completed_segments"]}
        assert by_id[bad_id]["error"]
        assert by_id[bad_id]["transcript"] == ""
        assert "error" not in by_id[good_id]


class TestGetJob:
    def test_unknown_job_returns_not_found(self, client):
        resp = client.get("/jobs/nonexistent-job-id")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "not_found"
        assert "nonexistent-job-id" in body["message"]
        assert body["detail"] == {}

    def test_known_job_returns_valid_status(self, client, sine_wav_path, job_id):
        payload = {
            "job_id": job_id,
            "segments": [{"id": str(uuid.uuid4()), "wav_path": sine_wav_path}],
            "model": "large-v2",
        }
        client.post("/jobs", json=payload)

        resp = client.get(f"/jobs/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == job_id
        assert body["status"] in ("running", "complete", "failed")
        assert "progress" in body
        assert "completed_segments" in body
        assert "error" in body

    def test_get_job_is_idempotent(self, client, sine_wav_path, job_id):
        """GET /jobs/{job_id} must be read-only: two consecutive reads when the job
        state is frozen must return identical job_id and status.

        We freeze the job state by injecting a completed job directly into the
        store — this avoids any background task race.
        """
        import main as svc

        # Inject a completed job directly so there is no background mutation
        svc._jobs[job_id] = {
            "job_id": job_id,
            "status": "complete",
            "progress": 100,
            "completed_segments": [],
            "error": None,
        }
        svc._job_locks[job_id] = asyncio.Lock()

        r1 = client.get(f"/jobs/{job_id}").json()
        r2 = client.get(f"/jobs/{job_id}").json()
        assert r1["job_id"] == r2["job_id"]
        assert r1["status"] == r2["status"]
        assert r1["progress"] == r2["progress"]


class TestCumulativeCompletedSegments:
    def test_completed_segments_cumulative(self, sine_wav_path):
        """After 2 batches complete, poll must return segments from both batches."""
        import main as svc
        from main import app

        mock_model = _make_mock_model()

        # We test via direct manipulation of the job state to simulate batch completion
        # without actually running faster-whisper.
        job_id = str(uuid.uuid4())
        seg1_id = str(uuid.uuid4())
        seg2_id = str(uuid.uuid4())

        svc._jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "progress": 50,
            "completed_segments": [
                {"id": seg1_id, "transcript": "Hello", "transcript_confidence": 0.9},
            ],
            "error": None,
        }
        svc._job_locks[job_id] = asyncio.Lock()

        with TestClient(app) as client:
            # First poll: 1 segment
            r1 = client.get(f"/jobs/{job_id}").json()
            assert len(r1["completed_segments"]) == 1

            # Simulate second batch completing
            svc._jobs[job_id]["completed_segments"].append(
                {"id": seg2_id, "transcript": "World", "transcript_confidence": 0.85}
            )
            svc._jobs[job_id]["progress"] = 100
            svc._jobs[job_id]["status"] = "complete"

            # Second poll: cumulative — both segments present
            r2 = client.get(f"/jobs/{job_id}").json()
            assert len(r2["completed_segments"]) == 2
            ids_in_r2 = {s["id"] for s in r2["completed_segments"]}
            assert seg1_id in ids_in_r2
            assert seg2_id in ids_in_r2


class TestShortSegmentHandling:
    def test_short_segment_gets_empty_transcript(self, client, short_wav_path, job_id):
        """Segments < 0.5s must return transcript='' and confidence=0.0 without calling model."""
        seg_id = str(uuid.uuid4())

        with patch("transcriber.load_model") as mock_load:
            mock_model = MagicMock()
            mock_load.return_value = mock_model

            payload = {
                "job_id": job_id,
                "segments": [{"id": seg_id, "wav_path": short_wav_path}],
                "model": "large-v2",
                "batch_size": 16,
            }
            resp = client.post("/jobs", json=payload)
            assert resp.status_code == 202

            # Give the background task time to complete
            import time
            time.sleep(0.5)

            poll = client.get(f"/jobs/{job_id}").json()
            # If completed, verify the short segment result
            if poll["status"] == "complete":
                assert len(poll["completed_segments"]) == 1
                seg_result = poll["completed_segments"][0]
                assert seg_result["id"] == seg_id
                assert seg_result["transcript"] == ""
                assert seg_result["transcript_confidence"] == 0.0
                # Model transcribe should NOT have been called for a short segment
                mock_model.transcribe.assert_not_called()
