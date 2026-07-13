"""Vocals stem streaming endpoint — GET /projects/{id}/sources/{sid}/vocals.

Serves the vocal-separation WAV for a source so the frontend tuning UI can
play back the pre-diarisation stem. Model: routers/segments.py get_segment_audio.
"""

import uuid
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _insert_source(conn, project_id, vocals_path=None, status="separation_pending"):
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


class TestGetSourceVocals:
    def test_200_when_vocals_path_set_and_file_exists(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        rel = "audio/vocals/x.wav"
        sid = _insert_source(conn, project, vocals_path=rel)

        wav = db.project_dir(project) / rel
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"RIFF....WAVEfmt ")

        resp = client.get(f"/projects/{project}/sources/{sid}/vocals")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.content == b"RIFF....WAVEfmt "

    def test_404_audio_not_found_when_vocals_path_null(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        sid = _insert_source(conn, project, vocals_path=None)

        resp = client.get(f"/projects/{project}/sources/{sid}/vocals")
        assert resp.status_code == 404
        assert resp.json()["error"] == "audio_not_found"

    def test_404_audio_not_found_when_file_missing_on_disk(self, client, project, isolated_data_dir):
        import db
        conn = db.get_conn(project)
        sid = _insert_source(conn, project, vocals_path="audio/vocals/missing.wav")

        resp = client.get(f"/projects/{project}/sources/{sid}/vocals")
        assert resp.status_code == 404
        assert resp.json()["error"] == "audio_not_found"

    def test_404_not_found_for_unknown_source(self, client, project, isolated_data_dir):
        resp = client.get(f"/projects/{project}/sources/{uuid.uuid4()}/vocals")
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"
