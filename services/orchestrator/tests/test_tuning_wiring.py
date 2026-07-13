"""Increment B: per-project tuning config (migration 011) must flow into the
job payloads the orchestrator submits to each processing service.

Each test sets the relevant project-config columns, runs the stage handler with
service_client mocked, and asserts the captured payload carries the configured
values (not the old hardcoded literals). Payloads are captured at submit time,
so post-completion processing is irrelevant to the assertions.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _insert_source(conn, project_id, status="separation_pending", vocals_path=None):
    sid = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO sources
           (id, project_id, filename, file_path, vocals_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (sid, project_id, "ep01.mkv", "source/ep01.mkv", vocals_path, status, now, now),
    )
    conn.commit()
    return sid


def _insert_seg(conn, project_id, source_id, status="pending", confidence=0.9,
                start=0.0, end=10.0, transcript=None, cleaned_path=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments
           (id, project_id, source_id, raw_path, cleaned_path, start_secs, end_secs,
            speaker_label, match_confidence, status, transcript, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, f"segments/raw/{seg_id}.wav", cleaned_path,
         start, end, "SPEAKER_00", confidence, status, transcript, now, now),
    )
    conn.commit()
    return seg_id


def _insert_model(conn, project_id, status="pending", mode="approved",
                  min_confidence=None, manifest="models/m/dataset.json"):
    mid = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO models
           (id, project_id, status, dataset_mode, min_confidence, dataset_manifest_path,
            checkpoint_dir, params, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (mid, project_id, status, mode, min_confidence, manifest, None,
         json.dumps({"epochs": 10}), now, now),
    )
    conn.commit()
    return mid


def _set_config(conn, project_id, **cols):
    set_clause = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE projects SET {set_clause} WHERE id=?", (*cols.values(), project_id))
    conn.commit()


def _run_job(project_id, job_id):
    import jobs
    loop = asyncio.new_event_loop()
    loop.run_until_complete(jobs._execute_job(project_id, job_id))
    loop.close()


def _enqueue_and_run(project_id, job_type, source_id=None, params=None):
    import jobs
    job_id = jobs.enqueue(project_id, job_type, source_id=source_id, params=params)
    try:
        jobs._queues[project_id].get_nowait()
    except Exception:
        pass
    _run_job(project_id, job_id)
    return job_id


def _capture():
    captured = {}

    async def mock_submit(service, payload):
        captured["service"] = service
        captured["payload"] = payload
        return {"job_id": payload["job_id"]}

    return captured, mock_submit


class TestSeparationWiring:
    def test_demucs_model_and_shifts_from_config(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_config(conn, project, demucs_model="mdx_extra", demucs_shifts=2)
        sid = _insert_source(conn, project, status="separation_pending")
        captured, mock_submit = _capture()

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete"}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "vocal_separation", source_id=sid)

        assert captured["service"] == "vocal_separation"
        assert captured["payload"]["model"] == "mdx_extra"
        assert captured["payload"]["shifts"] == 2


class TestDiarisationWiring:
    def test_speaker_bounds_from_config(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project,))
        _set_config(conn, project, diar_min_speakers=2, diar_max_speakers=4,
                    diar_min_segment_duration=0.5)
        sid = _insert_source(conn, project, status="diarisation_pending",
                             vocals_path=f"audio/vocals/{uuid.uuid4()}.wav")
        captured, mock_submit = _capture()

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "segments": []}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "diarisation", source_id=sid)

        params = captured["payload"]["params"]
        assert params["min_speakers"] == 2
        assert params["max_speakers"] == 4
        assert params["min_segment_duration"] == 0.5


class TestScoutWiring:
    def test_speaker_bounds_from_config(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_config(conn, project, diar_min_speakers=2, diar_max_speakers=5,
                    diar_min_segment_duration=0.75)
        sid = _insert_source(conn, project, status="diarisation_pending",
                             vocals_path=f"audio/vocals/{uuid.uuid4()}.wav")
        captured, mock_submit = _capture()

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "mode": "scout", "speakers": []}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "scout_speakers", source_id=sid)

        params = captured["payload"]["params"]
        assert params["min_speakers"] == 2
        assert params["max_speakers"] == 5
        assert params["min_segment_duration"] == 0.75


class TestTranscriptionWiring:
    def test_bulk_beam_and_vad_from_config(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_config(conn, project, whisper_beam_size=3, whisper_vad_filter=1)
        sid = _insert_source(conn, project, status="diarisation_pending")
        seg = _insert_seg(conn, project, sid, status="pending")
        captured, mock_submit = _capture()

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "completed_segments": []}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "transcription_bulk", params={"segment_ids": [seg]})

        assert captured["payload"]["beam_size"] == 3
        assert captured["payload"]["vad_filter"] is True

    def test_segment_beam_and_vad_from_config(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_config(conn, project, whisper_beam_size=2, whisper_vad_filter=1)
        sid = _insert_source(conn, project, status="diarisation_pending")
        seg = _insert_seg(conn, project, sid, status="approved", transcript="hi")
        captured, mock_submit = _capture()

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "completed_segments": []}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "transcription_segment", params={"segment_ids": [seg]})

        assert captured["payload"]["beam_size"] == 2
        assert captured["payload"]["vad_filter"] is True


class TestCleanupWiring:
    def test_dataset_cleanup_params_from_config(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_config(conn, project, target_lufs=-18.0, highpass_hz=120,
                    silence_threshold_db=-45.0, silence_min_duration_secs=0.25)
        sid = _insert_source(conn, project, status="complete")
        # >=300s of approved, uncleaned audio (10s clips, within the dataset
        # 1-11s duration filter) so dataset_build runs cleanup.
        seg_ids = [_insert_seg(conn, project, sid, status="approved", transcript="hi",
                               start=i * 20.0, end=i * 20.0 + 10.0) for i in range(35)]
        model_id = _insert_model(conn, project)
        captured, mock_submit = _capture()

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete",
                    "results": [{"id": s, "output_path": f"/x/{s}.wav",
                                 "clipping_warning": False, "auto_rejected": False,
                                 "error": None} for s in seg_ids]}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "dataset_build",
                             params={"model_id": model_id, "mode": "approved",
                                     "min_confidence": None})

        params = captured["payload"]["params"]
        assert params["target_lufs"] == -18.0
        assert params["highpass_hz"] == 120
        assert params["silence_threshold_db"] == -45.0
        assert params["silence_min_duration_secs"] == 0.25


class TestFinetuneWiring:
    def test_hyperparams_default_from_config(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_config(conn, project, xtts_epochs=25, xtts_batch_size=4,
                    xtts_grad_accum=2, xtts_learning_rate=1e-5)
        model_id = _insert_model(conn, project, status="pending")
        captured, mock_submit = _capture()

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "checkpoint_dir": "models/m", "eval_loss": 0.1}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "finetune", params={"model_id": model_id})

        params = captured["payload"]["params"]
        assert params["epochs"] == 25
        assert params["batch_size"] == 4
        assert params["grad_accum"] == 2
        assert params["learning_rate"] == 1e-5

    def test_per_run_params_override_config(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        _set_config(conn, project, xtts_epochs=25)
        model_id = _insert_model(conn, project, status="pending")
        captured, mock_submit = _capture()

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"status": "complete", "checkpoint_dir": "models/m", "eval_loss": 0.1}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "finetune",
                             params={"model_id": model_id, "params": {"epochs": 5}})

        assert captured["payload"]["params"]["epochs"] == 5
