"""Diarisation and speaker matching logic.

Wraps pyannote.audio for speaker diarisation and embedding extraction,
then performs cosine similarity matching against a reference speaker clip.
"""

import logging
import os
import uuid
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


def _get_hf_token() -> Optional[str]:
    token = os.environ.get("HF_TOKEN")
    if not token:
        logger.warning(
            "HF_TOKEN environment variable is not set. "
            "pyannote models will not be downloadable on first run."
        )
    return token


def load_pipeline():
    """Load and return the pyannote speaker diarisation pipeline."""
    from pyannote.audio import Pipeline

    token = _get_hf_token()
    if not token:
        raise RuntimeError(
            "HF_TOKEN environment variable is required to load pyannote models."
        )

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=token,
    )
    return pipeline


def load_embedding_model():
    """Load and return the pyannote speaker embedding model."""
    from pyannote.audio import Model
    from pyannote.audio import Inference

    token = _get_hf_token()
    if not token:
        raise RuntimeError(
            "HF_TOKEN environment variable is required to load pyannote models."
        )

    model = Model.from_pretrained("pyannote/embedding", use_auth_token=token)
    inference = Inference(model, window="whole")
    return inference


def extract_embedding(inference, audio_path: str) -> np.ndarray:
    """Extract a single speaker embedding from an audio file."""
    embedding = inference(audio_path)
    # Inference returns an SlidingWindowFeature or ndarray; normalise to 1D ndarray
    if hasattr(embedding, "data"):
        arr = np.squeeze(embedding.data)
    else:
        arr = np.squeeze(np.array(embedding))
    return arr


def extract_segment_embedding(inference, audio_path: str, start: float, end: float) -> np.ndarray:
    """Extract an embedding for a time segment of an audio file."""
    from pyannote.core import Segment

    seg = Segment(start, end)
    embedding = inference.crop(audio_path, seg)
    if hasattr(embedding, "data"):
        arr = np.squeeze(embedding.data)
    else:
        arr = np.squeeze(np.array(embedding))
    return arr


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors.

    Returns a value in [-1, 1] where 1 = identical direction.
    Uses the formula: 1 - cosine_distance.
    """
    from scipy.spatial.distance import cosine as cosine_distance

    a = a.flatten()
    b = b.flatten()

    # Handle zero-norm vectors
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0

    distance = cosine_distance(a, b)
    return float(1.0 - distance)


def compute_coverage_ratio(segments: list[dict], top_speaker_label: str, total_duration: float) -> float:
    """Compute the fraction of total audio covered by the top-matched speaker."""
    if total_duration <= 0:
        return 0.0
    total_covered = sum(
        seg["end_secs"] - seg["start_secs"]
        for seg in segments
        if seg["speaker_label"] == top_speaker_label
    )
    return total_covered / total_duration


def slice_wav(audio_data: np.ndarray, sample_rate: int, start_secs: float, end_secs: float) -> np.ndarray:
    """Return the audio samples for a given time range."""
    start_sample = int(start_secs * sample_rate)
    end_sample = int(end_secs * sample_rate)
    # Clamp to valid range
    start_sample = max(0, min(start_sample, len(audio_data)))
    end_sample = max(start_sample, min(end_sample, len(audio_data)))
    return audio_data[start_sample:end_sample]


def run_diarisation(
    pipeline,
    embedding_model,
    input_path: str,
    reference_path: str,
    output_dir: str,
    min_segment_duration: float = 1.0,
    min_speakers: int = 1,
    max_speakers: int = 10,
    progress_callback=None,
) -> tuple[list[dict], float]:
    """Run full diarisation pipeline and return (segments, coverage_ratio).

    progress_callback(pct: int) is called at milestone points.
    """
    import soundfile as sf

    def _progress(pct: int):
        if progress_callback:
            progress_callback(pct)

    # ---- Phase 1: Diarisation ----
    logger.info("Running pyannote diarisation on %s", input_path)
    diarization = pipeline(
        input_path,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
    )

    raw_turns = []
    for turn, _, speaker_label in diarization.itertracks(yield_label=True):
        duration = turn.end - turn.start
        if duration >= min_segment_duration:
            raw_turns.append({
                "start": turn.start,
                "end": turn.end,
                "speaker_label": speaker_label,
            })

    logger.info("Diarisation found %d segments (after duration filter)", len(raw_turns))
    _progress(10)

    if not raw_turns:
        logger.warning("No segments found after diarisation")
        return [], 0.0

    # ---- Phase 2: Speaker matching ----
    logger.info("Extracting reference embedding from %s", reference_path)
    reference_embedding = extract_embedding(embedding_model, reference_path)

    # Group turns by speaker
    speakers: dict[str, list[dict]] = {}
    for turn in raw_turns:
        label = turn["speaker_label"]
        speakers.setdefault(label, []).append(turn)

    # Compute per-speaker average embedding
    speaker_embeddings: dict[str, np.ndarray] = {}
    for label, turns in speakers.items():
        embeddings = []
        for turn in turns:
            try:
                emb = extract_segment_embedding(
                    embedding_model, input_path, turn["start"], turn["end"]
                )
                embeddings.append(emb)
            except Exception as exc:
                logger.warning(
                    "Could not extract embedding for %s [%.2f-%.2f]: %s",
                    label, turn["start"], turn["end"], exc,
                )
        if embeddings:
            avg = np.mean(np.stack(embeddings, axis=0), axis=0)
            speaker_embeddings[label] = avg
        else:
            logger.warning("No embeddings for speaker %s; assigning zero confidence", label)
            speaker_embeddings[label] = np.zeros_like(reference_embedding)

    # Compute cosine similarity for each speaker
    speaker_confidence: dict[str, float] = {
        label: cosine_similarity(reference_embedding, emb)
        for label, emb in speaker_embeddings.items()
    }

    logger.info("Speaker confidences: %s", speaker_confidence)
    _progress(50)

    # ---- Phase 3: WAV slicing ----
    logger.info("Slicing WAV segments into %s", output_dir)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    _progress(75)

    # Load source audio once
    audio_data, sample_rate = sf.read(input_path, dtype="float32", always_2d=False)

    segments = []
    for turn in raw_turns:
        label = turn["speaker_label"]
        segment_id = str(uuid.uuid4())
        wav_path = str(Path(output_dir) / f"{segment_id}.wav")

        # Slice audio
        chunk = slice_wav(audio_data, sample_rate, turn["start"], turn["end"])
        sf.write(wav_path, chunk, sample_rate)

        segments.append({
            "id": segment_id,
            "start_secs": turn["start"],
            "end_secs": turn["end"],
            "speaker_label": label,
            "match_confidence": speaker_confidence.get(label, 0.0),
            "wav_path": wav_path,
        })

    logger.info("Wrote %d segment WAV files", len(segments))
    _progress(100)

    # Compute coverage ratio
    top_speaker = max(speaker_confidence, key=speaker_confidence.get)
    audio_info = sf.info(input_path)
    total_duration = audio_info.duration
    coverage = compute_coverage_ratio(segments, top_speaker, total_duration)
    logger.info(
        "Coverage ratio: %.4f (top speaker: %s, confidence: %.4f)",
        coverage, top_speaker, speaker_confidence[top_speaker],
    )

    return segments, coverage
