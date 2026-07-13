"""Tests for the reference-from-video feature: the reference gate
(awaiting_reference), pipeline/continue, the scout_speakers job, and the
reference scout endpoints.

External service HTTP calls are mocked via patch on service_client functions,
following the pattern in test_wave3_pipeline.py.
"""

import asyncio
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from state_machines import compute_project_status

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _insert_source(conn, project_id, status="separation_pending", vocals_path=None):
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO sources
           (id, project_id, filename, file_path, vocals_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (source_id, project_id, "ep01.mkv", "source/ep01.mkv", vocals_path, status, now, now),
    )
    conn.commit()
    return source_id


def _insert_scout_job(conn, project_id, source_id, status="complete"):
    job_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO jobs (id, project_id, source_id, type, status, created_at) VALUES (?,?,?,?,?,?)",
        (job_id, project_id, source_id, "scout_speakers", status, _now()),
    )
    conn.commit()
    return job_id


def _pool(*durations):
    """Build a pool turn list from a sequence of durations (sequential times)."""
    pool = []
    t = 0.0
    for i, d in enumerate(durations):
        pool.append({"index": i, "start": t, "end": t + d, "duration": d})
        t += d + 0.5
    return pool


def _insert_candidate(conn, project_id, source_id, label, pool, total_secs=30.0,
                      segment_count=10, scout_job_id=None):
    # speaker_candidates.scout_job_id has a FK to jobs(id); create a job row if
    # the caller didn't supply one.
    if scout_job_id is None:
        scout_job_id = _insert_scout_job(conn, project_id, source_id)
    conn.execute(
        """INSERT INTO speaker_candidates
           (id, project_id, scout_job_id, source_id, speaker_label, pool_json,
            total_secs, segment_count, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), project_id, scout_job_id, source_id,
         label, json.dumps(pool), total_secs, segment_count, _now()),
    )
    conn.commit()
    return scout_job_id


def _write_pool_slices(pdir, scout_job_id, label, indices, fixture="test_audio.wav"):
    """Copy a fixture WAV into each pool slice path the endpoints read."""
    for i in indices:
        dest = pdir / "reference_candidates" / scout_job_id / label / f"{i}.wav"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES_DIR / fixture, dest)


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


# ========================================================================
# compute_project_status precedence for awaiting_reference
# ========================================================================

class TestComputeStatusAwaitingReference:
    def test_processing_wins_with_active_jobs(self):
        # A running scout counts as an active job → processing, even with no
        # reference and a diarisation_pending source.
        assert compute_project_status(
            {"scout_speakers"}, has_sources=True,
            all_sources_complete=False, export_complete=False,
            reference_set=False, has_diarisation_pending=True,
        ) == "processing"

    def test_review_wins_when_all_complete(self):
        assert compute_project_status(
            frozenset(), has_sources=True,
            all_sources_complete=True, export_complete=False,
            reference_set=False, has_diarisation_pending=False,
        ) == "review"

    def test_awaiting_reference_when_diarisation_pending_and_no_reference(self):
        assert compute_project_status(
            frozenset(), has_sources=True,
            all_sources_complete=False, export_complete=False,
            reference_set=False, has_diarisation_pending=True,
        ) == "awaiting_reference"

    def test_ready_when_reference_set(self):
        assert compute_project_status(
            frozenset(), has_sources=True,
            all_sources_complete=False, export_complete=False,
            reference_set=True, has_diarisation_pending=True,
        ) == "ready"

    def test_ready_when_nothing_diarisation_pending(self):
        assert compute_project_status(
            frozenset(), has_sources=True,
            all_sources_complete=False, export_complete=False,
            reference_set=False, has_diarisation_pending=False,
        ) == "ready"


# ========================================================================
# The reference gate: vocal separation completing without a reference
# ========================================================================

class TestReferenceGate:
    def test_vocal_sep_without_reference_lands_in_awaiting_reference(self, client, project, isolated_data_dir):
        import db
        import jobs

        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "separation_pending")

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "progress": 100,
                    "output_path": f"/data/projects/{project}/audio/vocals/{source_id}.wav"}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "vocal_separation", source_id=source_id)

            # No diarisation should have been auto-enqueued.
            assert jobs._queues[project].empty()

        source = conn.execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
        assert source["status"] == "diarisation_pending"

        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "awaiting_reference"


# ========================================================================
# pipeline/continue
# ========================================================================

class TestPipelineContinue:
    def test_continue_without_reference_returns_409(self, client, project):
        import db
        conn = db.get_conn(project)
        _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/x.wav")

        resp = client.post(f"/projects/{project}/pipeline/continue")
        assert resp.status_code == 409
        assert resp.json()["error"] == "no_reference"

    def test_continue_with_no_pending_sources_returns_409(self, client, project):
        import db
        conn = db.get_conn(project)
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project,))
        conn.commit()
        _insert_source(conn, project, "complete")

        resp = client.post(f"/projects/{project}/pipeline/continue")
        assert resp.status_code == 409
        assert resp.json()["error"] == "no_pending_sources"

    def test_continue_happy_path_enqueues_diarisation(self, client, project):
        import db
        conn = db.get_conn(project)
        conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project,))
        conn.commit()
        _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/a.wav")
        _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/b.wav")

        resp = client.post(f"/projects/{project}/pipeline/continue")
        assert resp.status_code == 202
        jobs = resp.json()["enqueued_jobs"]
        assert len(jobs) == 2
        assert all(j["type"] == "diarisation" for j in jobs)

        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "processing"


# ========================================================================
# scout_speakers job handler
# ========================================================================

class TestScoutHandler:
    def test_scout_payload_and_candidates(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending",
                                   vocals_path=f"audio/vocals/{uuid.uuid4()}.wav")

        captured = {}

        async def mock_submit(service, payload):
            captured["service"] = service
            captured["payload"] = payload
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {
                "job_id": job_id, "status": "complete", "mode": "scout",
                "speakers": [
                    {"speaker_label": "SPEAKER_00",
                     "pool": [{"index": 0, "start": 0.0, "end": 12.0, "duration": 12.0}],
                     "total_secs": 120.0, "segment_count": 40},
                    {"speaker_label": "SPEAKER_01",
                     "pool": [{"index": 0, "start": 1.0, "end": 6.0, "duration": 5.0}],
                     "total_secs": 30.0, "segment_count": 8},
                ],
            }

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = _enqueue_and_run(project, "scout_speakers", source_id=source_id)

        # Payload: reference_path is None, correct input/output paths.
        import jobs as jobs_mod
        prefix = jobs_mod._data_prefix()
        assert captured["service"] == "diarisation"
        payload = captured["payload"]
        assert payload["reference_path"] is None
        # No expected count set → the default range, no num_speakers.
        assert "num_speakers" not in payload["params"]
        source = conn.execute("SELECT vocals_path FROM sources WHERE id=?", (source_id,)).fetchone()
        assert payload["input_path"] == f"{prefix}/projects/{project}/{source['vocals_path']}"
        assert payload["output_dir"] == f"{prefix}/projects/{project}/reference_candidates/{job_id}/"

        # Candidates inserted with their pool stored as JSON.
        cands = conn.execute(
            "SELECT * FROM speaker_candidates WHERE project_id=? ORDER BY total_secs DESC",
            (project,),
        ).fetchall()
        assert [c["speaker_label"] for c in cands] == ["SPEAKER_00", "SPEAKER_01"]
        assert json.loads(cands[0]["pool_json"]) == [
            {"index": 0, "start": 0.0, "end": 12.0, "duration": 12.0}
        ]
        assert cands[0]["source_id"] == source_id
        assert cands[0]["scout_job_id"] == job_id

        # Source status is never touched by a scout.
        src = conn.execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
        assert src["status"] == "diarisation_pending"

    def test_scout_forwards_expected_speaker_count(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending",
                                   vocals_path="audio/vocals/v.wav")
        captured = {}

        async def mock_submit(service, payload):
            captured["payload"] = payload
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete", "mode": "scout", "speakers": []}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "scout_speakers", source_id=source_id,
                             params={"expected_speaker_count": 3})

        assert captured["payload"]["params"]["num_speakers"] == 3

    def test_scout_replaces_previous_candidates(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending",
                                   vocals_path="audio/vocals/v.wav")

        speakers_by_run = [
            [{"speaker_label": "SPEAKER_00", "pool": [{"index": 0, "start": 0.0, "end": 10.0, "duration": 10.0}],
              "total_secs": 10.0, "segment_count": 2}],
            [{"speaker_label": "SPEAKER_09", "pool": [{"index": 0, "start": 0.0, "end": 50.0, "duration": 50.0}],
              "total_secs": 50.0, "segment_count": 20}],
        ]
        run = {"i": 0}

        async def mock_submit(service, payload):
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            speakers = speakers_by_run[run["i"]]
            return {"job_id": job_id, "status": "complete", "mode": "scout", "speakers": speakers}

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            _enqueue_and_run(project, "scout_speakers", source_id=source_id)
            run["i"] = 1
            _enqueue_and_run(project, "scout_speakers", source_id=source_id)

        cands = conn.execute(
            "SELECT speaker_label FROM speaker_candidates WHERE project_id=?", (project,)
        ).fetchall()
        assert [c["speaker_label"] for c in cands] == ["SPEAKER_09"]

    def test_scout_fails_when_vocals_not_ready(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "separation_pending", vocals_path=None)

        job_id = _enqueue_and_run(project, "scout_speakers", source_id=source_id)
        job = conn.execute("SELECT status, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert job["error"] == "vocals_not_ready"


# ========================================================================
# Reference scout endpoints
# ========================================================================

class TestScoutEndpoints:
    def test_scout_source_not_found_returns_404(self, client, project):
        resp = client.post(f"/projects/{project}/reference/scout", json={"source_id": "bad-id"})
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    def test_scout_vocals_not_ready_returns_422(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "separation_pending", vocals_path=None)

        resp = client.post(f"/projects/{project}/reference/scout", json={"source_id": source_id})
        assert resp.status_code == 422
        assert resp.json()["error"] == "vocals_not_ready"

    def test_scout_enqueues_and_project_processing(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")

        resp = client.post(f"/projects/{project}/reference/scout", json={"source_id": source_id})
        assert resp.status_code == 202
        assert resp.json()["type"] == "scout_speakers"

        p = conn.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
        assert p["status"] == "processing"

    def test_get_scout_no_scout_returns_404(self, client, project):
        resp = client.get(f"/projects/{project}/reference/scout")
        assert resp.status_code == 404
        assert resp.json()["error"] == "no_scout"

    def test_get_scout_running_shape(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")
        job_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO jobs (id, project_id, source_id, type, status, progress, created_at) VALUES (?,?,?,?,?,?,?)",
            (job_id, project, source_id, "scout_speakers", "running", 40, _now()),
        )
        conn.commit()

        resp = client.get(f"/projects/{project}/reference/scout")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "running"
        assert body["progress"] == 40
        assert body["source_id"] == source_id
        assert body["speakers"] == []

    def test_get_scout_complete_shape_sorted(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")
        job_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO jobs (id, project_id, source_id, type, status, created_at) VALUES (?,?,?,?,?,?)",
            (job_id, project, source_id, "scout_speakers", "complete", _now()),
        )
        conn.commit()
        j1 = _insert_candidate(conn, project, source_id, "SPEAKER_01", _pool(6.0),
                               total_secs=30.0, segment_count=8, scout_job_id=job_id)
        _insert_candidate(conn, project, source_id, "SPEAKER_00", _pool(12.0, 8.0),
                          total_secs=120.0, segment_count=40, scout_job_id=job_id)

        resp = client.get(f"/projects/{project}/reference/scout")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "complete"
        assert body["source_id"] == source_id
        labels = [s["speaker_label"] for s in body["speakers"]]
        assert labels == ["SPEAKER_00", "SPEAKER_01"]  # sorted by total_secs desc
        top = body["speakers"][0]
        assert len(top["pool"]) == 2
        assert top["pool"][0]["sample_url"] == \
            f"/projects/{project}/reference/scout/samples/SPEAKER_00/0"
        assert top["pool"][1]["sample_url"] == \
            f"/projects/{project}/reference/scout/samples/SPEAKER_00/1"

    def test_sample_unknown_speaker_returns_404(self, client, project):
        resp = client.get(f"/projects/{project}/reference/scout/samples/SPEAKER_99/0")
        assert resp.status_code == 404
        assert resp.json()["error"] == "unknown_speaker"

    def test_sample_unknown_index_returns_404(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")
        _insert_candidate(conn, project, source_id, "SPEAKER_00", _pool(10.0))

        resp = client.get(f"/projects/{project}/reference/scout/samples/SPEAKER_00/9")
        assert resp.status_code == 404
        assert resp.json()["error"] == "unknown_segment"

    def test_sample_streams_wav(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")
        pdir = db.project_dir(project)
        scout_job_id = _insert_candidate(conn, project, source_id, "SPEAKER_00", _pool(10.0))
        _write_pool_slices(pdir, scout_job_id, "SPEAKER_00", [0])

        resp = client.get(f"/projects/{project}/reference/scout/samples/SPEAKER_00/0")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"

    def test_preview_unknown_speaker_returns_404(self, client, project):
        resp = client.get(f"/projects/{project}/reference/scout/preview/SPEAKER_99")
        assert resp.status_code == 404
        assert resp.json()["error"] == "unknown_speaker"

    def test_preview_streams_montage_wav(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")
        pdir = db.project_dir(project)
        scout_job_id = _insert_candidate(conn, project, source_id, "SPEAKER_00", _pool(10.0, 8.0), total_secs=120.0)
        _write_pool_slices(pdir, scout_job_id, "SPEAKER_00", [0, 1])

        resp = client.get(f"/projects/{project}/reference/scout/preview/SPEAKER_00")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        # A real, playable WAV was returned, not an empty body.
        assert len(resp.content) > 44  # WAV header is 44 bytes

    def test_preview_reflects_exclusions(self, client, project, isolated_data_dir):
        # Excluding turns must shorten the montage — the preview mirrors select.
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")
        pdir = db.project_dir(project)
        scout_job_id = _insert_candidate(conn, project, source_id, "SPEAKER_00", _pool(10.0, 8.0), total_secs=120.0)
        _write_pool_slices(pdir, scout_job_id, "SPEAKER_00", [0, 1])

        full = client.get(f"/projects/{project}/reference/scout/preview/SPEAKER_00")
        partial = client.get(f"/projects/{project}/reference/scout/preview/SPEAKER_00?exclude=0")
        assert full.status_code == 200 and partial.status_code == 200
        # Dropping the longest turn leaves a shorter montage.
        assert len(partial.content) < len(full.content)

    def test_preview_all_excluded_returns_422(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")
        _insert_candidate(conn, project, source_id, "SPEAKER_00", _pool(10.0, 8.0), total_secs=120.0)

        resp = client.get(f"/projects/{project}/reference/scout/preview/SPEAKER_00?exclude=0&exclude=1")
        assert resp.status_code == 422
        assert resp.json()["error"] == "reference_too_short"

    def test_select_unknown_speaker_returns_404(self, client, project):
        resp = client.post(f"/projects/{project}/reference/scout/select",
                           json={"speaker_label": "SPEAKER_99"})
        assert resp.status_code == 404
        assert resp.json()["error"] == "unknown_speaker"

    def test_select_too_short_returns_422(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")
        pdir = db.project_dir(project)
        # A single ~3s pool turn → assembled reference is under the 5s minimum.
        scout_job_id = _insert_candidate(conn, project, source_id, "SPEAKER_00", _pool(3.0), total_secs=3.0)
        _write_pool_slices(pdir, scout_job_id, "SPEAKER_00", [0], fixture="test_audio_short.wav")

        resp = client.post(f"/projects/{project}/reference/scout/select",
                           json={"speaker_label": "SPEAKER_00"})
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "reference_too_short"
        assert body["detail"]["minimum_secs"] == 5.0
        assert "duration_secs" in body["detail"]

    def test_select_all_excluded_returns_422(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")
        _insert_candidate(conn, project, source_id, "SPEAKER_00", _pool(10.0, 8.0), total_secs=120.0)

        resp = client.post(f"/projects/{project}/reference/scout/select",
                           json={"speaker_label": "SPEAKER_00", "excluded_indices": [0, 1]})
        assert resp.status_code == 422
        assert resp.json()["error"] == "reference_too_short"

    def test_select_happy_path_sets_reference_and_origin(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")
        pdir = db.project_dir(project)
        scout_job_id = _insert_candidate(conn, project, source_id, "SPEAKER_02", _pool(10.0), total_secs=120.0)
        _write_pool_slices(pdir, scout_job_id, "SPEAKER_02", [0])  # 10s slice

        resp = client.post(f"/projects/{project}/reference/scout/select",
                           json={"speaker_label": "SPEAKER_02"})
        assert resp.status_code == 200
        assert resp.json()["reference_path"] == "reference.wav"
        assert resp.json()["duration_secs"] > 5.0

        assert (pdir / "reference.wav").exists()
        p = conn.execute("SELECT reference_path, reference_origin FROM projects WHERE id=?", (project,)).fetchone()
        assert p["reference_path"] == "reference.wav"
        origin = json.loads(p["reference_origin"])
        assert origin == {
            "type": "diarise_pick", "source_id": source_id, "speaker_label": "SPEAKER_02",
            "excluded_indices": [], "included_indices": [0],
        }

    def test_select_excludes_wrong_voice_turn(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")
        pdir = db.project_dir(project)
        # Two turns; exclude the longest (index 0). Reference builds from index 1.
        scout_job_id = _insert_candidate(conn, project, source_id, "SPEAKER_00", _pool(12.0, 8.0), total_secs=120.0)
        _write_pool_slices(pdir, scout_job_id, "SPEAKER_00", [0, 1])

        resp = client.post(f"/projects/{project}/reference/scout/select",
                           json={"speaker_label": "SPEAKER_00", "excluded_indices": [0]})
        assert resp.status_code == 200
        p = conn.execute("SELECT reference_origin FROM projects WHERE id=?", (project,)).fetchone()
        origin = json.loads(p["reference_origin"])
        assert origin["excluded_indices"] == [0]
        assert origin["included_indices"] == [1]

    def test_scout_endpoint_rejects_zero_speaker_count(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")

        resp = client.post(f"/projects/{project}/reference/scout",
                           json={"source_id": source_id, "expected_speaker_count": 0})
        assert resp.status_code == 422
        assert resp.json()["error"] == "invalid_speaker_count"

    def test_scout_endpoint_stores_expected_count_on_job(self, client, project):
        import db
        conn = db.get_conn(project)
        source_id = _insert_source(conn, project, "diarisation_pending", vocals_path="audio/vocals/v.wav")

        resp = client.post(f"/projects/{project}/reference/scout",
                           json={"source_id": source_id, "expected_speaker_count": 2})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        job = conn.execute("SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert json.loads(job["params"])["expected_speaker_count"] == 2


# ========================================================================
# Upload origin + project detail exposure
# ========================================================================

class TestReferenceOrigin:
    def test_upload_sets_uploaded_origin(self, client, project, test_wav):
        import db
        with open(test_wav, "rb") as f:
            resp = client.post(f"/projects/{project}/reference",
                               files={"file": ("ref.wav", f, "audio/wav")})
        assert resp.status_code == 200
        conn = db.get_conn(project)
        p = conn.execute("SELECT reference_origin FROM projects WHERE id=?", (project,)).fetchone()
        assert json.loads(p["reference_origin"]) == {"type": "uploaded"}

    def test_project_detail_exposes_reference_fields(self, client, project, test_wav):
        # No reference yet.
        resp = client.get(f"/projects/{project}")
        body = resp.json()
        assert body["reference_path"] is None
        assert body["reference_origin"] is None

        # After upload.
        with open(test_wav, "rb") as f:
            client.post(f"/projects/{project}/reference",
                        files={"file": ("ref.wav", f, "audio/wav")})
        resp = client.get(f"/projects/{project}")
        body = resp.json()
        assert body["reference_path"] == "reference.wav"
        assert body["reference_origin"] == {"type": "uploaded"}
