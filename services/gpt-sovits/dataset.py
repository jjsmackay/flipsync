"""Dataset manifest → GPT-SoVITS `.list` adapter, plus reference-segment
selection for the inference conditioning clip.

Pure stdlib; no ML dependencies. ``engine.finetune`` (next task) calls
``manifest_to_list`` to turn a FlipSync dataset manifest
(``models/{model_id}/dataset.json``, absolute ``audio_file`` paths — see
``spec/data-models.md`` "Dataset manifest") into the pipe-delimited `.list`
file the vendored GPT-SoVITS prep stages expect.

`.list` format pinned in research-gpt-sovits.md §6 (all four prep stages parse
it identically): ``wav_path|speaker_name|language|text``, exactly 4 fields,
split unbounded on ``|`` — a stray pipe in the transcript breaks the row.
"""

from __future__ import annotations

import json

# Fixed speaker label — FlipSync trains a single-speaker voice per project
# (matches the xtts service's convention).
SPEAKER_NAME = "target"

# v1 is English-only; unsupported/typo'd language tokens are silently DROPPED
# by upstream prep (research §6), so the adapter always writes this exact
# lowercase token rather than passing anything through from the manifest.
_LANGUAGE = "en"

# Reference-segment conditioning clip band. Upstream hard-raises at synthesis
# time when the reference resamples to under 48000 or over 160000 samples of
# 16 kHz audio (TTS_infer_pack/TTS.py) — an out-of-band reference trains to
# "ready" and then fails every preview forever, so packaging must guarantee
# the shipped reference.wav is strictly in-band.
_REF_MIN_SECS = 3.0
_REF_MAX_SECS = 10.0
# Safety margin inside the exact boundaries (effective band 3.1-9.9 s of
# measured audio) so resample rounding can never trip the sample-count check.
_REF_MARGIN_SECS = 0.1
# Trim target for over-band candidates — comfortably inside the band.
_REF_TRIM_SECS = 9.5


def load_manifest(manifest_path: str) -> list[dict]:
    """Load a dataset manifest and return its ``segments`` list.

    Raises ``ValueError`` if the file is missing a non-empty ``segments`` list.
    """
    with open(manifest_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    segments = data.get("segments")
    if not segments:
        raise ValueError(f"manifest {manifest_path} has no segments to train on")
    return segments


def _clean_text(text: str) -> str:
    """Make text safe for a single `.list` row.

    A pipe would be parsed as a field separator and a newline would break the
    row, so both collapse to spaces; surrounding whitespace is trimmed.
    """
    return (text or "").replace("|", " ").replace("\n", " ").replace("\r", " ").strip()


def manifest_to_list(manifest_path: str, list_path: str) -> str:
    """Convert a manifest to a GPT-SoVITS `.list` file; return its path.

    One row per segment: ``wav_path|speaker_name|language|text``. Segments
    with an empty/whitespace-only transcript are skipped (upstream would
    otherwise choke on an empty text field). ``audio_file`` is written as-is —
    the manifest already carries absolute paths, and prep is run with
    ``inp_wav_dir`` empty so each row's path is used verbatim (research §6).
    No audio transformation happens here; prep resamples itself.
    """
    segments = load_manifest(manifest_path)

    rows = []
    for seg in segments:
        text = _clean_text(seg.get("text", ""))
        if not text:
            continue
        rows.append(f"{seg['audio_file']}|{SPEAKER_NAME}|{_LANGUAGE}|{text}")

    with open(list_path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(row + "\n")

    return list_path


def select_reference_plan(candidates: list[dict]) -> dict:
    """Pick the inference conditioning reference from measured candidates.

    ``candidates`` are manifest segments each carrying ``measured_secs`` — the
    real decoded duration of the WAV on disk, NOT the manifest's
    ``duration_secs`` (cleanup silence-trims after that metadata is set, so it
    can overstate the audio). Pure function: measuring the files, and any
    trimming, is the caller's (packaging's) job.

    Returns ``{"segment": seg, "trim_to_secs": None | float}``:

    - A strictly in-band candidate wins, tie-broken by longest transcript then
      lowest ``id`` (deterministic); no trim needed.
    - Otherwise the shortest over-band candidate is chosen with
      ``trim_to_secs = _REF_TRIM_SECS`` — the shortest loses the least
      transcript/audio alignment when cut.
    - Under-band-only sets raise ``ValueError``: a too-short reference can
      never synthesise, so packaging must fail clearly rather than ship a
      bundle whose every preview would fail.
    """
    usable = [s for s in candidates if (s.get("text") or "").strip()]
    if not usable:
        raise ValueError(
            "no segment has both a non-empty transcript and readable audio "
            "to use as the synthesis reference"
        )

    def _tie_break(seg: dict):
        return (-len((seg.get("text") or "").strip()), seg["id"])

    lo = _REF_MIN_SECS + _REF_MARGIN_SECS
    hi = _REF_MAX_SECS - _REF_MARGIN_SECS
    in_band = [s for s in usable if lo <= s["measured_secs"] <= hi]
    if in_band:
        return {"segment": sorted(in_band, key=_tie_break)[0], "trim_to_secs": None}

    over_band = [s for s in usable if s["measured_secs"] > hi]
    if over_band:
        chosen = sorted(over_band, key=lambda s: (s["measured_secs"],) + _tie_break(s))[0]
        return {"segment": chosen, "trim_to_secs": _REF_TRIM_SECS}

    raise ValueError(
        "no training segment between 3 and 10 seconds is available to use as "
        "the synthesis reference — GPT-SoVITS cannot synthesise without one; "
        "include at least one segment in that range and retrain"
    )
