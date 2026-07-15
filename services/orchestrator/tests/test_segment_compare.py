"""Segment-vs-model-output comparison: q filter, preview segment_id,
conditioning exclusion. Conventions follow tests/test_wave_xtts.py."""

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _insert_source(conn, project_id, status="complete"):
    source_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (source_id, project_id, "ep01.mkv", "source/ep01.mkv", status, now, now),
    )
    conn.commit()
    return source_id


def _insert_seg(conn, project_id, source_id, status="approved", confidence=0.9,
                start=0.0, end=10.0, transcript="hello world", transcript_edited=None,
                cleaned_path=None):
    seg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """INSERT INTO segments
           (id, project_id, source_id, raw_path, export_path, cleaned_path, start_secs,
            end_secs, speaker_label, match_confidence, status, transcript,
            transcript_edited, flags, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seg_id, project_id, source_id, f"segments/raw/{seg_id}.wav", None,
         cleaned_path, start, end, "SPEAKER_00", confidence, status, transcript,
         transcript_edited, None, now, now),
    )
    conn.commit()
    return seg_id


def _set_reference(conn, project_id, pdir):
    ref = pdir / "reference.wav"
    ref.write_bytes(b"\x00" * 100)
    conn.execute("UPDATE projects SET reference_path='reference.wav' WHERE id=?", (project_id,))
    conn.commit()


class TestSegmentsQFilter:
    def test_q_matches_transcript_substring(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        hit = _insert_seg(conn, project, src, status="approved", transcript="the quick brown fox")
        _insert_seg(conn, project, src, status="approved", transcript="lazy dog")

        resp = client.get(f"/projects/{project}/segments",
                          params={"status": "approved", "q": "quick"})
        assert resp.status_code == 200
        segs = resp.json()["segments"]
        assert [s["id"] for s in segs] == [hit]

    def test_q_is_case_insensitive(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        hit = _insert_seg(conn, project, src, status="approved", transcript="Quick Brown Fox")

        resp = client.get(f"/projects/{project}/segments",
                          params={"status": "approved", "q": "quick"})
        assert [s["id"] for s in resp.json()["segments"]] == [hit]

    def test_q_matches_edited_transcript_over_original(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        # Edited transcript replaces the original for search purposes.
        _insert_seg(conn, project, src, status="approved",
                    transcript="quick", transcript_edited="slow")

        miss = client.get(f"/projects/{project}/segments",
                          params={"status": "approved", "q": "quick"})
        assert miss.json()["segments"] == []
        hit = client.get(f"/projects/{project}/segments",
                         params={"status": "approved", "q": "slow"})
        assert len(hit.json()["segments"]) == 1

    def test_q_treats_like_wildcards_as_literals(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_seg(conn, project, src, status="approved", transcript="one hundred")
        hit = _insert_seg(conn, project, src, status="approved", transcript="100% sure")

        resp = client.get(f"/projects/{project}/segments",
                          params={"status": "approved", "q": "100%"})
        assert [s["id"] for s in resp.json()["segments"]] == [hit]

    def test_q_composes_with_status_filter(self, client, project):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        _insert_seg(conn, project, src, status="rejected", transcript="quick fox")

        resp = client.get(f"/projects/{project}/segments",
                          params={"status": "approved", "q": "quick"})
        assert resp.json()["segments"] == []


class TestConditioningExclusion:
    def _prow(self, conn, project_id):
        return conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    def test_excluded_segment_dropped_from_raw_pool(self, client, project, isolated_data_dir):
        import db, jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, confidence=0.99)
        other = _insert_seg(conn, project, src, confidence=0.9)

        _src, refs = jobs._resolve_conditioning(
            conn, self._prow(conn, project), project, "segments_raw", 5,
            exclude_segment_id=target,
        )
        assert len(refs) == 1
        assert other in refs[0]
        assert all(target not in p for p in refs)

    def test_excluded_segment_dropped_from_cleaned_pool(self, client, project, isolated_data_dir):
        import db, jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, cleaned_path="cleaned/t.wav")
        _insert_seg(conn, project, src, cleaned_path="cleaned/o.wav")

        _src, refs = jobs._resolve_conditioning(
            conn, self._prow(conn, project), project, "segments_cleaned", 5,
            exclude_segment_id=target,
        )
        assert len(refs) == 1
        assert "cleaned/o.wav" in refs[0]

    def test_exclusion_can_empty_the_pool(self, client, project, isolated_data_dir):
        import db, jobs
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src)

        with pytest.raises(LookupError):
            jobs._resolve_conditioning(
                conn, self._prow(conn, project), project, "segments_raw", 5,
                exclude_segment_id=target,
            )

    def test_reference_clip_unaffected(self, client, project, isolated_data_dir):
        import db, jobs
        conn = db.get_conn(project)
        _set_reference(conn, project, db.project_dir(project))
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src)

        _src, refs = jobs._resolve_conditioning(
            conn, self._prow(conn, project), project, "reference_clip", 5,
            exclude_segment_id=target,
        )
        assert refs[0].endswith("reference.wav")

    def test_handle_preview_threads_exclusion(self, client, project, isolated_data_dir):
        """A preview job with segment_id must not condition on that segment."""
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, confidence=0.99)
        other = _insert_seg(conn, project, src, confidence=0.9)

        captured = {}

        async def mock_submit(service, payload):
            captured["payload"] = payload
            return {"job_id": payload["job_id"]}

        async def mock_poll(service, job_id, interval_secs=2.0, on_progress=None):
            return {"job_id": job_id, "status": "complete",
                    "result": {"output_path": "x", "duration_secs": 1.0}}

        import asyncio, jobs
        from jobs import enqueue

        with patch("service_client.submit_job", new=AsyncMock(side_effect=mock_submit)), \
             patch("service_client.poll_until_complete", new=AsyncMock(side_effect=mock_poll)):
            job_id = enqueue(project, "preview",
                             params={"text": "hello world", "segment_id": target,
                                     "model_id": None,
                                     "conditioning": {"source": "segments_raw",
                                                      "segment_count": 5}})
            loop = asyncio.new_event_loop()
            loop.run_until_complete(jobs._execute_job(project, job_id))
            loop.close()

        refs = captured["payload"]["reference_wavs"]
        assert all(target not in p for p in refs)
        assert any(other in p for p in refs)


class TestPreviewSegmentId:
    def test_segment_id_derives_text_and_stores_params(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        # Two segments so the conditioning pool survives excluding the target.
        target = _insert_seg(conn, project, src, transcript="say this exactly")
        _insert_seg(conn, project, src)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"segment_id": target})
        assert resp.status_code == 202
        job_id = resp.json()["enqueued_job"]["id"]
        p = json.loads(conn.execute(
            "SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()["params"])
        assert p["text"] == "say this exactly"
        assert p["segment_id"] == target

    def test_segment_id_uses_edited_transcript(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, transcript="machine words",
                             transcript_edited="human words")
        _insert_seg(conn, project, src)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"segment_id": target})
        job_id = resp.json()["enqueued_job"]["id"]
        p = json.loads(conn.execute(
            "SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()["params"])
        assert p["text"] == "human words"

    def test_segment_id_ignores_client_text(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, transcript="the real line")
        _insert_seg(conn, project, src)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"segment_id": target, "text": "something else"})
        job_id = resp.json()["enqueued_job"]["id"]
        p = json.loads(conn.execute(
            "SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()["params"])
        assert p["text"] == "the real line"

    def test_missing_segment_409(self, client, project, isolated_data_dir):
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"segment_id": str(uuid.uuid4())})
        assert resp.status_code == 409
        assert resp.json()["error"] == "segment_not_comparable"

    def test_segment_without_transcript_409(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, transcript=None)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"segment_id": target})
        assert resp.status_code == 409
        assert resp.json()["error"] == "segment_not_comparable"

    def test_whitespace_only_transcript_409(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, transcript="   ")

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"segment_id": target})
        assert resp.status_code == 409
        assert resp.json()["error"] == "segment_not_comparable"

    def test_empty_string_segment_id_422(self, client, project):
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews",
                               json={"segment_id": ""})
        assert resp.status_code == 422

    def test_neither_text_nor_segment_id_422(self, client, project):
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews", json={})
        assert resp.status_code == 422

    def test_list_surfaces_segment_id(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, transcript="a line")
        _insert_seg(conn, project, src)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            client.post(f"/projects/{project}/previews", json={"segment_id": target})
            client.post(f"/projects/{project}/previews", json={"text": "plain preview"})

        resp = client.get(f"/projects/{project}/previews")
        previews = resp.json()["previews"]
        by_text = {p["text"]: p for p in previews}
        assert by_text["a line"]["segment_id"] == target
        assert by_text["plain preview"]["segment_id"] is None


class TestListSurfacesSampling:
    def test_list_returns_model_id_and_sampling(self, client, project, isolated_data_dir):
        """Past comparisons need model + the sampling knobs used to render provenance."""
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        target = _insert_seg(conn, project, src, transcript="a line")
        _insert_seg(conn, project, src)

        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            client.post(f"/projects/{project}/previews",
                        json={"segment_id": target, "temperature": 0.9, "speed": 1.1,
                              "top_k": 30, "top_p": 0.7, "repetition_penalty": 8.0})

        p = client.get(f"/projects/{project}/previews").json()["previews"][0]
        assert p["model_id"] is None
        assert p["sampling"] == {"temperature": 0.9, "speed": 1.1, "top_k": 30,
                                 "top_p": 0.7, "repetition_penalty": 8.0,
                                 "enable_text_splitting": None}


class TestDeletePreview:
    def _make_preview(self, client, project, conn, src):
        target = _insert_seg(conn, project, src, transcript="delete me")
        _insert_seg(conn, project, src)
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(f"/projects/{project}/previews", json={"segment_id": target})
        return resp.json()["enqueued_job"]["id"]

    def test_delete_removes_row_and_wav(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        preview_id = self._make_preview(client, project, conn, src)

        # Mark terminal and drop a fake WAV on disk to prove both are cleaned up.
        conn.execute("UPDATE jobs SET status='complete' WHERE id=?", (preview_id,))
        conn.commit()
        wav = db.project_dir(project) / "previews" / f"{preview_id}.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"\x00" * 10)

        resp = client.delete(f"/projects/{project}/previews/{preview_id}")
        assert resp.status_code == 204
        assert conn.execute("SELECT 1 FROM jobs WHERE id=?", (preview_id,)).fetchone() is None
        assert not wav.exists()

    def test_delete_missing_404(self, client, project, isolated_data_dir):
        resp = client.delete(f"/projects/{project}/previews/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json()["error"] == "preview_not_found"

    def test_delete_non_preview_job_404(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        job_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?,?,?,?,?)",
            (job_id, project, "export", "complete", _now()),
        )
        conn.commit()
        resp = client.delete(f"/projects/{project}/previews/{job_id}")
        assert resp.status_code == 404

    def test_delete_running_preview_409(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        preview_id = self._make_preview(client, project, conn, src)
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (preview_id,))
        conn.commit()

        resp = client.delete(f"/projects/{project}/previews/{preview_id}")
        assert resp.status_code == 409
        assert resp.json()["error"] == "preview_running"
        # The row survives a rejected delete.
        assert conn.execute("SELECT 1 FROM jobs WHERE id=?", (preview_id,)).fetchone() is not None

    def test_delete_terminal_without_wav_still_204(self, client, project, isolated_data_dir):
        """A failed preview never wrote a WAV; deleting it must not error."""
        import db
        conn = db.get_conn(project)
        src = _insert_source(conn, project)
        preview_id = self._make_preview(client, project, conn, src)
        conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (preview_id,))
        conn.commit()

        resp = client.delete(f"/projects/{project}/previews/{preview_id}")
        assert resp.status_code == 204
