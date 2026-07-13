"""Reference clip endpoints: upload, and diarise + pick (scout)."""

import asyncio
import io
import json
import os
import uuid
import wave
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import APIRouter, UploadFile, File, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from audio import get_duration
from db import get_conn, project_dir, require_project, utc_now
from errors import AppError
from jobs import enqueue
from status import recompute_project_status

router = APIRouter(prefix="/projects/{project_id}/reference", tags=["reference"])

CHUNK_SIZE = 1024 * 1024  # 1 MB
MIN_DURATION_SECS = 5.0
# Maximum assembled reference length. Turns are taken longest-first up to this
# cap; the final turn is truncated to land exactly on it.
REFERENCE_MAX_SECS = 30.0


async def _finalise_reference(conn, project_id: str, origin: dict, staged_wav) -> float:
    """Duration-gate `staged_wav`, install it as reference.wav, record its
    provenance, and recompute project status (a project resting in
    awaiting_reference moves on once a reference exists).

    Shared by the upload and diarise+pick paths so the finalisation steps
    cannot drift. Both stage a disposable temp file, so installation is an
    atomic rename. The gate runs before installation, so a too-short candidate
    never destroys an existing valid reference. Returns the duration.
    """
    duration = await get_duration(str(staged_wav))
    if duration < MIN_DURATION_SECS:
        raise AppError(
            422, "reference_too_short",
            f"Reference clip must be at least {MIN_DURATION_SECS} seconds. Provided clip is {duration:.1f} seconds.",
            {"duration_secs": duration, "minimum_secs": MIN_DURATION_SECS},
        )

    dest = project_dir(project_id) / "reference.wav"
    os.replace(staged_wav, dest)

    conn.execute(
        "UPDATE projects SET reference_path='reference.wav', reference_origin=?, updated_at=? WHERE id=?",
        (json.dumps(origin), utc_now(), project_id),
    )
    conn.commit()
    recompute_project_status(project_id)
    return duration


@router.post("")
async def upload_reference(project_id: str, file: UploadFile = File(...)):
    conn = require_project(project_id)

    pdir = project_dir(project_id)
    # Write to a temp file first so a failed/too-short upload never destroys the
    # existing valid reference. Only an atomic rename replaces it.
    tmp = pdir / f".reference.{uuid.uuid4().hex}.tmp"

    try:
        async with aiofiles.open(tmp, "wb") as out:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                await out.write(chunk)

        duration = await _finalise_reference(conn, project_id, {"type": "uploaded"}, tmp)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    return {"reference_path": "reference.wav", "duration_secs": duration}


# ---------------------------------------------------------------------------
# Diarise + pick (scout)
# ---------------------------------------------------------------------------


class ScoutRequest(BaseModel):
    source_id: str
    # Optional: force pyannote to this exact speaker count for the next scan.
    expected_speaker_count: Optional[int] = None


class SelectRequest(BaseModel):
    speaker_label: str
    # Pool turn indices to leave out of the assembled reference (wrong-voice
    # turns the user excluded). Empty = today's behaviour (whole montage).
    excluded_indices: list[int] = []


@router.post("/scout", status_code=202)
async def scout_speakers(project_id: str, body: ScoutRequest):
    conn = require_project(project_id)

    source = conn.execute(
        "SELECT id, vocals_path FROM sources WHERE id=? AND project_id=?",
        (body.source_id, project_id),
    ).fetchone()
    if source is None:
        raise AppError(404, "not_found", "Source not found.")
    if not source["vocals_path"]:
        raise AppError(
            422, "vocals_not_ready",
            "Source has no vocals stem yet; run step 1 first.",
        )

    params = None
    if body.expected_speaker_count is not None:
        if body.expected_speaker_count < 1:
            raise AppError(
                422, "invalid_speaker_count",
                "Expected speaker count must be at least 1.",
            )
        params = {"expected_speaker_count": body.expected_speaker_count}

    job_id = enqueue(project_id, "scout_speakers", source_id=body.source_id, params=params)
    recompute_project_status(project_id)
    return {"job_id": job_id, "type": "scout_speakers"}


def _candidate_speakers(conn, project_id: str) -> list[dict]:
    """Serialise the project's current speaker_candidates rows, each with its
    curation pool. Each pool turn carries a per-turn sample_url."""
    candidates = conn.execute(
        "SELECT * FROM speaker_candidates WHERE project_id=? ORDER BY total_secs DESC",
        (project_id,),
    ).fetchall()
    result = []
    for c in candidates:
        pool = json.loads(c["pool_json"])
        result.append({
            "speaker_label": c["speaker_label"],
            "total_secs": c["total_secs"],
            "segment_count": c["segment_count"],
            "pool": [
                {
                    "index": t["index"],
                    "start": t["start"],
                    "end": t["end"],
                    "duration": t["duration"],
                    "sample_url": (
                        f"/projects/{project_id}/reference/scout/samples/"
                        f"{c['speaker_label']}/{t['index']}"
                    ),
                }
                for t in pool
            ],
        })
    return result


def _select_reference_turns(pool: list[dict], excluded: set[int], cap_secs: float) -> list[tuple[int, float]]:
    """Choose pool turns for the reference: longest-first, minus excluded, up to
    cap_secs. Returns (index, take_secs) pairs; the final turn is truncated so
    the total lands exactly on the cap. Excluding a turn lets the next-longest
    kept turn take its place — the backfill."""
    included = sorted(
        (t for t in pool if t["index"] not in excluded),
        key=lambda t: t["duration"],
        reverse=True,
    )
    chosen: list[tuple[int, float]] = []
    total = 0.0
    for t in included:
        remaining = cap_secs - total
        if remaining <= 0:
            break
        take = min(t["duration"], remaining)
        chosen.append((t["index"], take))
        total += take
    return chosen


def _assemble_reference_wav(pool_dir: Path, chosen: list[tuple[int, float]], dest) -> None:
    """Concatenate the chosen pool slices into dest, truncating each to take_secs.
    Uses the stdlib wave module so the orchestrator needs no audio deps. `dest`
    may be a path (select's staged temp file) or a writable binary file object
    (preview's in-memory buffer) — wave.open accepts both."""
    target = dest if hasattr(dest, "write") else str(dest)
    with wave.open(target, "wb") as out:
        params_set = False
        for index, take in chosen:
            src = pool_dir / f"{index}.wav"
            if not src.exists():
                raise AppError(404, "audio_not_found", f"Pool slice {index} is missing.")
            with wave.open(str(src), "rb") as w:
                if not params_set:
                    out.setparams(w.getparams())
                    params_set = True
                nframes = min(w.getnframes(), int(take * w.getframerate()))
                out.writeframes(w.readframes(nframes))


def _assemble_reference_bytes(pool_dir: Path, chosen: list[tuple[int, float]]) -> bytes:
    """Assemble the chosen slices into an in-memory WAV and return its bytes.
    Used by the preview endpoint, which never touches disk or the reference."""
    buf = io.BytesIO()
    _assemble_reference_wav(pool_dir, chosen, buf)
    return buf.getvalue()


@router.get("/scout")
async def get_scout(project_id: str):
    conn = require_project(project_id)

    # created_at has second resolution; rowid breaks ties deterministically in
    # favour of the most recently inserted job.
    job = conn.execute(
        "SELECT * FROM jobs WHERE project_id=? AND type='scout_speakers' ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if job is None:
        raise AppError(404, "no_scout", "No scout has been run for this project.")

    if job["status"] in ("queued", "running"):
        return {
            "status": "running",
            "progress": job["progress"] or 0,
            "source_id": job["source_id"],
            "speakers": [],
        }

    if job["status"] in ("failed", "cancelled"):
        # A failed re-scan must not hide candidates from an earlier successful
        # scan — they are still pickable, so return them alongside the failure.
        return {
            "status": "failed",
            "source_id": job["source_id"],
            "error": job["error"],
            "speakers": _candidate_speakers(conn, project_id),
        }

    # complete
    return {"status": "complete", "source_id": job["source_id"], "speakers": _candidate_speakers(conn, project_id)}


def _require_candidate(conn, project_id: str, speaker_label: str):
    """Fetch the speaker_candidates row or raise 404 unknown_speaker."""
    cand = conn.execute(
        "SELECT * FROM speaker_candidates WHERE project_id=? AND speaker_label=?",
        (project_id, speaker_label),
    ).fetchone()
    if cand is None:
        raise AppError(404, "unknown_speaker", "Speaker not in the current candidate set.")
    return cand


def _pool_dir(project_id: str, cand):
    """Directory holding a candidate's pool slice WAVs."""
    return (
        project_dir(project_id) / "reference_candidates"
        / cand["scout_job_id"] / cand["speaker_label"]
    )


def _select_or_422(pool: list, excluded: set):
    """Pick the reference turns, or raise 422 when exclusions empty the pool."""
    chosen = _select_reference_turns(pool, excluded, REFERENCE_MAX_SECS)
    if not chosen:
        raise AppError(
            422, "reference_too_short",
            "No turns left after exclusions — keep at least one segment.",
            {"excluded_indices": sorted(excluded)},
        )
    return chosen


@router.get("/scout/samples/{speaker_label}/{index}")
async def get_scout_sample(project_id: str, speaker_label: str, index: int):
    conn = require_project(project_id)
    cand = _require_candidate(conn, project_id, speaker_label)

    if index not in {t["index"] for t in json.loads(cand["pool_json"])}:
        raise AppError(404, "unknown_segment", "Segment not in this candidate's pool.")

    wav = _pool_dir(project_id, cand) / f"{index}.wav"
    if not wav.exists():
        raise AppError(404, "audio_not_found", "Pool slice WAV not found.")

    return FileResponse(str(wav), media_type="audio/wav")


@router.get("/scout/preview/{speaker_label}")
async def get_scout_preview(
    project_id: str,
    speaker_label: str,
    exclude: list[int] = Query(default=[]),
):
    """Stream the reference montage this speaker would produce — the included
    pool turns, longest-first, capped at REFERENCE_MAX_SECS — assembled in
    memory so the user can audition a candidate (and the effect of their
    exclusions) before committing to select. Identical assembly to select, so
    the preview matches the eventual reference exactly."""
    conn = require_project(project_id)
    cand = _require_candidate(conn, project_id, speaker_label)

    pool = json.loads(cand["pool_json"])
    chosen = _select_or_422(pool, set(exclude))

    data = await asyncio.to_thread(_assemble_reference_bytes, _pool_dir(project_id, cand), chosen)
    return Response(content=data, media_type="audio/wav")


@router.post("/scout/select")
async def select_speaker(project_id: str, body: SelectRequest):
    conn = require_project(project_id)
    cand = _require_candidate(conn, project_id, body.speaker_label)

    pool = json.loads(cand["pool_json"])
    excluded = set(body.excluded_indices)
    chosen = _select_or_422(pool, excluded)

    pool_dir = _pool_dir(project_id, cand)
    # Assemble into a temp file so a too-short result never destroys an existing
    # valid reference; _finalise_reference gates on duration then installs it.
    pdir = project_dir(project_id)
    tmp = pdir / f".reference.{uuid.uuid4().hex}.tmp"
    try:
        await asyncio.to_thread(_assemble_reference_wav, pool_dir, chosen, tmp)
        origin = {
            "type": "diarise_pick",
            "source_id": cand["source_id"],
            "speaker_label": cand["speaker_label"],
            "excluded_indices": sorted(excluded),
            "included_indices": [idx for idx, _ in chosen],
        }
        duration = await _finalise_reference(conn, project_id, origin, tmp)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    return {"reference_path": "reference.wav", "duration_secs": duration}
