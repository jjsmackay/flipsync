"""Transcriber — faster-whisper wrapping, batch logic, confidence scoring."""

import wave
from pathlib import Path
from typing import Optional

# Lazy import to avoid crashing at startup if faster-whisper isn't installed
_model_cache: dict = {"model": None, "model_size": None}

VALID_MODELS = {"tiny", "base", "small", "medium", "large-v2", "large-v3"}
SHORT_SEGMENT_THRESHOLD_SECS = 0.5


def get_wav_duration(wav_path: str) -> float:
    """Return duration of a WAV file in seconds."""
    try:
        with wave.open(wav_path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return frames / float(rate)
    except Exception:
        # If we can't read it, assume it's long enough and let faster-whisper handle errors
        return 999.0


def load_model(model_size: str):
    """Load (or return cached) WhisperModel. Replaces cache if model_size changed."""
    global _model_cache

    if _model_cache["model"] is not None and _model_cache["model_size"] == model_size:
        return _model_cache["model"]

    from faster_whisper import WhisperModel
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
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


def process_batch(
    model,
    batch: list[dict],
    language: Optional[str],
) -> list[dict]:
    """Process a list of segment dicts ({id, wav_path}) and return results."""
    results = []
    for seg in batch:
        result = transcribe_segment(model, seg["id"], seg["wav_path"], language)
        results.append(result)
    return results
