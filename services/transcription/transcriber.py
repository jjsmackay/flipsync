"""Transcriber — faster-whisper wrapping, batch logic, confidence scoring."""

import wave
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from resegment import (
    compute_boundaries,
    merge_degenerate_children,
    normalise_utterances,
    slice_children,
    split_into_utterances,
)

# Lazy import to avoid crashing at startup if faster-whisper isn't installed
_model_cache: dict = {"model": None, "model_size": None, "compute_type": None}

VALID_MODELS = {"tiny", "base", "small", "medium", "large-v2", "large-v3"}
SHORT_SEGMENT_THRESHOLD_SECS = 0.5


def get_wav_duration(wav_path: str) -> float:
    """Return duration of a WAV file in seconds.

    Raises if the file is missing or unreadable — callers treat that as a
    per-segment error rather than silently transcribing a bad file.
    """
    with wave.open(wav_path, "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        return frames / float(rate)


def load_model(model_size: str, num_workers: int = 1, compute_type: str = "default"):
    """Load (or return cached) WhisperModel. Reloads if model_size OR compute_type changed.

    ``num_workers`` sets how many transcriptions CTranslate2 can run in parallel
    (see ``process_batch``). The model is cached across jobs keyed on
    ``(model_size, compute_type)``; the first job's ``batch_size`` fixes
    ``num_workers`` for the process lifetime, but later jobs still bound their
    own concurrency via the batch thread pool, so a smaller ``batch_size`` for
    OOM recovery works regardless of the cached value.

    ``compute_type`` of ``"default"`` keeps the device-derived choice (float16 on
    GPU, int8 on CPU); any other value (e.g. ``int8_float16``) is passed straight
    to CTranslate2 to trade precision for VRAM on a constrained GPU.
    """
    global _model_cache

    if (
        _model_cache["model"] is not None
        and _model_cache["model_size"] == model_size
        and _model_cache["compute_type"] == compute_type
    ):
        return _model_cache["model"]

    from faster_whisper import WhisperModel
    from ctranslate2 import get_cuda_device_count

    device = "cuda" if get_cuda_device_count() > 0 else "cpu"
    resolved_compute_type = (
        ("float16" if device == "cuda" else "int8")
        if compute_type == "default"
        else compute_type
    )

    model = WhisperModel(
        model_size,
        device=device,
        compute_type=resolved_compute_type,
        num_workers=max(1, num_workers),
    )
    _model_cache["model"] = model
    _model_cache["model_size"] = model_size
    _model_cache["compute_type"] = compute_type
    return model


def compute_confidence(words) -> float:
    """Compute mean word probability, clamped to [0.0, 1.0]."""
    if not words:
        return 0.0
    total = sum(w.probability for w in words)
    mean = total / len(words)
    return max(0.0, min(1.0, mean))


def transcribe_segment(
    model,
    segment_id: str,
    wav_path: str,
    language: Optional[str],
    start_secs: float = 0.0,
    resegment: bool = False,
    beam_size: int = 5,
    vad_filter: bool = False,
) -> dict:
    """
    Transcribe a single WAV segment.

    Returns a dict with keys: id, transcript, transcript_confidence.
    Short segments (< 0.5s) get empty transcript without calling the model.

    When ``resegment`` is true and the transcription yields 2+ normalised
    utterances, the segment is split instead: child WAVs are sliced from the
    parent and the result is ``{"id": <parent>, "children": [...]}`` where
    each child carries a service-generated UUID, its WAV path, absolute
    ``start_secs``/``end_secs`` (parent ``start_secs`` + in-file offsets),
    and its own transcript and confidence. A single utterance or a no-word
    transcription returns the unsplit shape, exactly as if ``resegment``
    were false.
    """
    # Check duration first
    duration = get_wav_duration(wav_path)
    if duration < SHORT_SEGMENT_THRESHOLD_SECS:
        return {
            "id": segment_id,
            "transcript": "",
            "transcript_confidence": 0.0,
        }

    transcribe_kwargs = {
        "word_timestamps": True,
        "beam_size": beam_size,
        "vad_filter": vad_filter,
    }
    if language is not None:
        transcribe_kwargs["language"] = language

    segments_gen, _info = model.transcribe(wav_path, **transcribe_kwargs)

    all_words = []
    text_parts = []

    for seg in segments_gen:
        text_parts.append(seg.text)
        if seg.words:
            all_words.extend(seg.words)

    if resegment and all_words:
        utterances = normalise_utterances(split_into_utterances(all_words))
        if len(utterances) >= 2:
            split = _split_segment(
                segment_id, wav_path, duration, start_secs, utterances
            )
            if split is not None:
                return split

    transcript = "".join(text_parts).strip()
    confidence = compute_confidence(all_words)

    return {
        "id": segment_id,
        "transcript": transcript,
        "transcript_confidence": confidence,
    }


def _split_segment(
    segment_id: str,
    wav_path: str,
    duration: float,
    start_secs: float,
    utterances: list[list],
) -> dict | None:
    """Slice child WAVs for each utterance and build the children result.

    Boundary clamping can merge degenerate children away (overshooting
    whisper word timestamps); if fewer than two children survive, returns
    ``None`` so the caller falls back to the unsplit shape.
    """
    boundaries = compute_boundaries(utterances, duration)
    utterances, boundaries = merge_degenerate_children(utterances, boundaries)
    if len(utterances) < 2:
        return None
    child_files = slice_children(wav_path, boundaries)

    children = []
    for utt, (b_start, b_end), child in zip(utterances, boundaries, child_files):
        children.append(
            {
                "id": child["id"],
                "wav_path": child["wav_path"],
                "start_secs": start_secs + b_start,
                "end_secs": start_secs + b_end,
                # Whisper word tokens carry their own leading spacing.
                "transcript": "".join(w.word for w in utt).strip(),
                "transcript_confidence": compute_confidence(utt),
            }
        )

    return {"id": segment_id, "children": children}


def _transcribe_one(model, seg: dict, language: Optional[str],
                    beam_size: int = 5, vad_filter: bool = False) -> dict:
    """Transcribe one segment, converting any failure into a per-segment error.

    A single unreadable or otherwise failing WAV must not abort the whole job,
    so the exception is captured on the segment result. The orchestrator still
    receives a ``transcript`` (empty) and ``transcript_confidence`` (0.0) so the
    cumulative-results contract holds; the extra ``error`` field is additive.
    """
    try:
        return transcribe_segment(
            model,
            seg["id"],
            seg["wav_path"],
            language,
            start_secs=seg.get("start_secs") or 0.0,
            resegment=seg.get("resegment", False),
            beam_size=beam_size,
            vad_filter=vad_filter,
        )
    except Exception as exc:
        return {
            "id": seg["id"],
            "transcript": "",
            "transcript_confidence": 0.0,
            "error": str(exc),
        }


def process_batch(
    model,
    batch: list[dict],
    language: Optional[str],
    max_workers: int = 1,
    beam_size: int = 5,
    vad_filter: bool = False,
) -> list[dict]:
    """Transcribe a list of segment dicts ({id, wav_path}) and return results.

    ``max_workers`` (driven by the job's ``batch_size``) bounds how many
    segments transcribe concurrently. CTranslate2 releases the GIL during
    inference and is designed for concurrent calls into a single model
    (``num_workers`` on the model caps real parallelism), so this turns
    ``batch_size`` into a genuine throughput / GPU-memory knob rather than a
    progress-reporting granularity. Results are returned in input order; a
    failing segment yields a result with an ``error`` field instead of raising.
    """
    if max_workers <= 1 or len(batch) <= 1:
        return [_transcribe_one(model, seg, language, beam_size, vad_filter) for seg in batch]

    with ThreadPoolExecutor(max_workers=min(max_workers, len(batch))) as pool:
        return list(pool.map(
            lambda seg: _transcribe_one(model, seg, language, beam_size, vad_filter), batch))
