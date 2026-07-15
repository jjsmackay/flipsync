"""Custom inference-only conditioning clip: upload endpoint + source='custom'.

Distinct from the project reference clip (the diarisation speaker gate) — this
is a one-off clip XTTS conditions on at synthesis time, referenced by clip_id.
"""

import io
import json
import re
import wave
from unittest.mock import AsyncMock, patch


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
