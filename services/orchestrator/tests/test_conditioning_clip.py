"""Custom inference-only conditioning clip: upload endpoint + source='custom'.

Distinct from the project reference clip (the diarisation speaker gate) — this
is a one-off clip XTTS conditions on at synthesis time, referenced by clip_id.
"""

import io
import json
import re
import uuid
import wave
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _wav_bytes(secs: float, sr: int = 22050) -> bytes:
    """A valid mono PCM16 WAV of the given duration (silence)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(sr * secs))
    return buf.getvalue()


def _upload(client, project, secs: float):
    return client.post(
        f"/projects/{project}/previews/conditioning",
        files={"file": ("clip.wav", _wav_bytes(secs), "audio/wav")},
    )


class TestConditioningClipUpload:
    def test_upload_returns_clip_id_and_writes_file(self, client, project, isolated_data_dir):
        import db
        resp = _upload(client, project, 3.0)
        assert resp.status_code == 201
        clip_id = resp.json()["clip_id"]
        assert re.fullmatch(r"[0-9a-f]{32}", clip_id)
        assert (db.project_dir(project) / "conditioning" / f"{clip_id}.wav").exists()

    def test_upload_too_short_422(self, client, project, isolated_data_dir):
        resp = _upload(client, project, 0.5)
        assert resp.status_code == 422
        assert resp.json()["error"] == "conditioning_too_short"


class TestCustomConditioningPreview:
    def test_preview_with_custom_clip_persists_clip_id(self, client, project, isolated_data_dir):
        clip_id = _upload(client, project, 3.0).json()["clip_id"]
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(
                f"/projects/{project}/previews",
                json={"text": "hello", "conditioning": {"source": "custom", "clip_id": clip_id}},
            )
        assert resp.status_code == 202
        import db
        job_id = resp.json()["enqueued_job"]["id"]
        params = json.loads(db.get_conn(project).execute(
            "SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()["params"])
        assert params["conditioning"]["source"] == "custom"
        assert params["conditioning"]["clip_id"] == clip_id

    def test_preview_custom_missing_clip_409(self, client, project, isolated_data_dir):
        # Valid hex shape but no such file → resolves to nothing.
        with patch("service_client.is_healthy", new=AsyncMock(return_value=True)):
            resp = client.post(
                f"/projects/{project}/previews",
                json={"text": "hello", "conditioning": {"source": "custom", "clip_id": "0" * 32}},
            )
        assert resp.status_code == 409
        assert resp.json()["error"] == "conditioning_unavailable"

    def test_custom_resolves_to_uploaded_clip(self, client, project, isolated_data_dir):
        """_resolve_conditioning returns the uploaded clip's absolute path."""
        import db
        import jobs
        clip_id = _upload(client, project, 3.0).json()["clip_id"]
        conn = db.get_conn(project)
        prow = conn.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
        resolved, paths = jobs._resolve_conditioning(
            conn, prow, project, "custom", 5, clip_id=clip_id,
        )
        assert resolved == "custom"
        assert len(paths) == 1 and paths[0].endswith(f"conditioning/{clip_id}.wav")


def _insert_segment_with_wav(conn, pdir, project, secs):
    """Insert a source + segment and write a real raw WAV of `secs`."""
    sid = str(uuid.uuid4())
    src = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (src, project, "e.wav", "source/e.wav", "complete", now, now),
    )
    conn.execute(
        "INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs, "
        "speaker_label, match_confidence, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,'S0',0.9,'pending',?,?)",
        (sid, project, src, f"segments/raw/{sid}.wav", 0.0, secs, now, now),
    )
    conn.commit()
    raw = pdir / "segments" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / f"{sid}.wav").write_bytes(_wav_bytes(secs))
    return sid


class TestConditioningFromSegment:
    def test_promote_segment_returns_clip(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        sid = _insert_segment_with_wav(conn, pdir, project, 4.0)
        resp = client.post(f"/projects/{project}/previews/conditioning/from-segment",
                           json={"segment_id": sid})
        assert resp.status_code == 201
        clip_id = resp.json()["clip_id"]
        assert re.fullmatch(r"[0-9a-f]{32}", clip_id)
        assert (pdir / "conditioning" / f"{clip_id}.wav").exists()
        # Original segment WAV untouched.
        assert (pdir / "segments" / "raw" / f"{sid}.wav").exists()

    def test_promote_missing_segment_404(self, client, project, isolated_data_dir):
        resp = client.post(f"/projects/{project}/previews/conditioning/from-segment",
                           json={"segment_id": str(uuid.uuid4())})
        assert resp.status_code == 404

    def test_promote_missing_audio_409(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        sid = str(uuid.uuid4())
        src = str(uuid.uuid4())
        now = _now()
        conn.execute(
            "INSERT INTO sources (id, project_id, filename, file_path, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", (src, project, "e.wav", "s/e.wav", "complete", now, now))
        conn.execute(
            "INSERT INTO segments (id, project_id, source_id, raw_path, start_secs, end_secs, "
            "speaker_label, match_confidence, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,'S0',0.9,'pending',?,?)",
            (sid, project, src, f"segments/raw/{sid}.wav", 0.0, 4.0, now, now))
        conn.commit()  # no WAV written
        resp = client.post(f"/projects/{project}/previews/conditioning/from-segment",
                           json={"segment_id": sid})
        assert resp.status_code == 409
        assert resp.json()["error"] == "audio_unavailable"

    def test_promote_too_short_422(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        sid = _insert_segment_with_wav(conn, pdir, project, 0.5)
        resp = client.post(f"/projects/{project}/previews/conditioning/from-segment",
                           json={"segment_id": sid})
        assert resp.status_code == 422
        assert resp.json()["error"] == "conditioning_too_short"


class TestListConditioningClips:
    def test_empty_when_none(self, client, project, isolated_data_dir):
        resp = client.get(f"/projects/{project}/previews/conditioning")
        assert resp.status_code == 200
        assert resp.json() == {"clips": []}

    def test_lists_uploaded_and_promoted(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        pdir = db.project_dir(project)
        up = _upload(client, project, 3.0).json()["clip_id"]
        sid = _insert_segment_with_wav(conn, pdir, project, 4.0)
        prom = client.post(f"/projects/{project}/previews/conditioning/from-segment",
                           json={"segment_id": sid}).json()["clip_id"]
        clips = client.get(f"/projects/{project}/previews/conditioning").json()["clips"]
        ids = {c["clip_id"] for c in clips}
        assert up in ids and prom in ids
        assert all("duration_secs" in c for c in clips)
