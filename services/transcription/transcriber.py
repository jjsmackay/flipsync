"""Transcriber — faster-whisper wrapping, batch logic, confidence scoring."""

import wave
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

# Lazy import to avoid crashing at startup if faster-whisper isn't installed
_model_cache: dict = {"model": None, "model_size": None}

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


def load_model(model_size: str, num_workers: int = 1):
    """Load (or return cached) WhisperModel. Replaces cache if model_size changed.

    ``num_workers`` sets how many transcriptions CTranslate2 can run in parallel
    (see ``process_batch``). The model is cached across jobs keyed on
    ``model_size`` only, so the first job's ``batch_size`` fixes ``num_workers``
    for the process lifetime; later jobs still bound their own concurrency via
    the batch thread pool, so a smaller ``batch_size`` for OOM recovery works
    regardless of the cached value.
    """
    global _model_cache

    if _model_cache["model"] is not None and _model_cache["model_size"] == model_size:
        return _model_cache["model"]

    from faster_whisper import WhisperModel
    from ctranslate2 import get_cuda_device_count

    device = "cuda" if get_cuda_device_count() > 0 else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    model = WhisperModel(
        model_size,
        device=device,
        compute_type=compute_type,
        num_workers=max(1, num_workers),
    )
    _model_cache["model"] = model
    _model_cache["model_size"] = model_size
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
) -> dict:
    """
    Transcribe a single WAV segment.

    Returns a dict with keys: id, transcript, transcript_confidence.
    Short segments (< 0.5s) get empty transcript without calling the model.
    """
    # Check duration first
    duration = get_wav_duration(wav_path)
    if duration < SHORT_SEGMENT_THRESHOLD_SECS:
        return {
            "id": segment_id,
            "transcript": "",
            "transcript_confidence": 0.0,
        }

    transcribe_kwargs = {"word_timestamps": True}
    if language is not None:
        transcribe_kwargs["language"] = language

    segments_gen, _info = model.transcribe(wav_path, **transcribe_kwargs)

    all_words = []
    text_parts = []

    for seg in segments_gen:
        text_parts.append(seg.text)
        if seg.words:
            all_words.extend(seg.words)

    transcript = "".join(text_parts).strip()
    confidence = compute_confidence(all_words)

    return {
        "id": segment_id,
        "transcript": transcript,
        "transcript_confidence": confidence,
    }


def _transcribe_one(model, seg: dict, language: Optional[str]) -> dict:
    """Transcribe one segment, converting any failure into a per-segment error.

    A single unreadable or otherwise failing WAV must not abort the whole job,
    so the exception is captured on the segment result. The orchestrator still
    receives a ``transcript`` (empty) and ``transcript_confidence`` (0.0) so the
    cumulative-results contract holds; the extra ``error`` field is additive.
    """
    try:
        return transcribe_segment(model, seg["id"], seg["wav_path"], language)
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
        return [_transcribe_one(model, seg, language) for seg in batch]

    with ThreadPoolExecutor(max_workers=min(max_workers, len(batch))) as pool:
        return list(pool.map(lambda seg: _transcribe_one(model, seg, language), batch))
