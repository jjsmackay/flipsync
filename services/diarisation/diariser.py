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

# Segments shorter than this produce noisy embeddings — their match_confidence
# falls back to the cluster-level score instead of their own embedding.
SHORT_SEGMENT_FALLBACK_SECS = 1.0


class DiarisationError(Exception):
    """Base for diarisation failures that carry a stable error code."""

    error_code = "diarisation_failed"


class HuggingFaceTokenMissing(DiarisationError):
    """HF_TOKEN is not set, so pyannote models cannot be loaded."""

    error_code = "huggingface_token_missing"


class ModelDownloadFailed(DiarisationError):
    """A pyannote model could not be downloaded or loaded from cache."""

    error_code = "model_download_failed"


def _get_hf_token() -> Optional[str]:
    token = os.environ.get("HF_TOKEN")
    if not token:
        logger.warning(
            "HF_TOKEN environment variable is not set. "
            "pyannote models will not be downloadable on first run."
        )
    return token


def _select_device():
    """Return the CUDA device when available, otherwise CPU."""
    import torch

    if torch.cuda.is_available():
        logger.info("CUDA is available; diarisation will run on GPU.")
        return torch.device("cuda")
    logger.warning(
        "CUDA is not available; diarisation will run on CPU. "
        "A full episode may take hours."
    )
    return torch.device("cpu")


def load_pipeline():
    """Load and return the pyannote speaker diarisation pipeline (on GPU if present)."""
    from pyannote.audio import Pipeline

    token = _get_hf_token()
    if not token:
        raise HuggingFaceTokenMissing(
            "HF_TOKEN environment variable is required to load pyannote models."
        )

    try:
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=token,
        )
    except Exception as exc:  # noqa: BLE001 — network/auth/licence all surface here
        raise ModelDownloadFailed(
            f"Failed to download or load the pyannote diarisation pipeline: {exc}"
        ) from exc

    # pyannote loads on CPU by default — move the pipeline to GPU explicitly.
    pipeline.to(_select_device())
    return pipeline


def load_embedding_model():
    """Load and return the pyannote speaker embedding model (on GPU if present)."""
    from pyannote.audio import Model
    from pyannote.audio import Inference

    token = _get_hf_token()
    if not token:
        raise HuggingFaceTokenMissing(
            "HF_TOKEN environment variable is required to load pyannote models."
        )

    try:
        model = Model.from_pretrained("pyannote/embedding", token=token)
    except Exception as exc:  # noqa: BLE001 — network/auth/licence all surface here
        raise ModelDownloadFailed(
            f"Failed to download or load the pyannote embedding model: {exc}"
        ) from exc

    # Inference defaults to CPU; pass device= so embedding extraction uses the GPU.
    inference = Inference(model, window="whole", device=_select_device())
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


def clamp_confidence(value: float) -> float:
    """Clamp a raw cosine similarity ([-1, 1]) to the spec's [0, 1] match range."""
    return float(max(0.0, min(1.0, value)))


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


def select_montage_turns(
    turns: list[dict], montage_max_secs: float
) -> list[tuple[dict, float]]:
    """Choose which turns go into a speaker montage, longest-first.

    Returns (turn, take_secs) pairs ordered by turn duration descending. The
    sum of take_secs never exceeds montage_max_secs; the final selected turn is
    truncated so the montage lands exactly on the cap. Leading with the longest
    turns keeps the sample representative and usable as a reference clip.
    """
    ordered = sorted(turns, key=lambda t: t["end"] - t["start"], reverse=True)

    selected: list[tuple[dict, float]] = []
    total = 0.0
    for turn in ordered:
        remaining = montage_max_secs - total
        if remaining <= 0:
            break
        duration = turn["end"] - turn["start"]
        take = min(duration, remaining)
        selected.append((turn, take))
        total += take
    return selected


def run_scout(
    pipeline,
    input_path: str,
    output_dir: str,
    min_segment_duration: float = 1.0,
    min_speakers: int = 1,
    max_speakers: int = 10,
    montage_max_secs: float = 30.0,
    progress_callback=None,
) -> list[dict]:
    """Reference-less scout pass: diarise into anonymous clusters and write one
    montage WAV per speaker.

    Runs pyannote diarisation exactly as match mode's phase 1 (same
    ``min_segment_duration`` filter) but computes no reference embedding, no
    cosine similarity, and no per-segment WAVs. For each speaker it writes a
    single montage to ``{output_dir}/{speaker_label}.wav`` — that speaker's
    turns concatenated longest-first up to ``montage_max_secs`` total.

    Returns a list of ``{speaker_label, montage_path, total_secs,
    segment_count}`` dicts sorted by total talk time descending. ``total_secs``
    is the speaker's total talk time in the source, not the montage length.
    """
    import soundfile as sf

    def _progress(pct: int):
        if progress_callback:
            progress_callback(pct)

    # ---- Phase 1: Diarisation (identical to match mode) ----
    logger.info("Running pyannote diarisation (scout) on %s", input_path)
    diarization = pipeline(
        input_path,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
    ).speaker_diarization

    raw_turns = []
    for turn, _, speaker_label in diarization.itertracks(yield_label=True):
        duration = turn.end - turn.start
        if duration >= min_segment_duration:
            raw_turns.append({
                "start": turn.start,
                "end": turn.end,
                "speaker_label": speaker_label,
            })

    logger.info("Scout found %d turns (after duration filter)", len(raw_turns))
    _progress(10)

    if not raw_turns:
        logger.warning("No turns found after diarisation")
        return []

    # Group turns by speaker
    speakers: dict[str, list[dict]] = {}
    for turn in raw_turns:
        speakers.setdefault(turn["speaker_label"], []).append(turn)

    _progress(50)

    # ---- Montage writing ----
    logger.info("Writing speaker montages into %s", output_dir)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Load source audio once. Same whole-file v1 memory note as run_diarisation:
    # this reads the entire vocals file into RAM; a future refinement could
    # seek/read per-turn to bound memory.
    audio_data, sample_rate = sf.read(input_path, dtype="float32", always_2d=False)

    results = []
    for label, turns in speakers.items():
        montage_path = str(Path(output_dir) / f"{label}.wav")

        chunks = []
        for turn, take_secs in select_montage_turns(turns, montage_max_secs):
            chunk = slice_wav(
                audio_data, sample_rate, turn["start"], turn["start"] + take_secs
            )
            chunks.append(chunk)

        montage = np.concatenate(chunks, axis=0) if chunks else np.zeros(0, dtype="float32")
        sf.write(montage_path, montage, sample_rate)

        total_secs = sum(t["end"] - t["start"] for t in turns)
        results.append({
            "speaker_label": label,
            "montage_path": montage_path,
            "total_secs": total_secs,
            "segment_count": len(turns),
        })

    # Most talkative speaker first — usually the target.
    results.sort(key=lambda s: s["total_secs"], reverse=True)
    _progress(100)
    logger.info("Scout wrote %d speaker montages", len(results))
    return results


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
    ).speaker_diarization

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

    # Extract one embedding per segment (single sequential pass). Each embedding
    # is used twice: for the segment's own match_confidence and for the
    # cluster-average speaker_match_confidence — no double extraction.
    for turn in raw_turns:
        try:
            turn["embedding"] = extract_segment_embedding(
                embedding_model, input_path, turn["start"], turn["end"]
            )
        except Exception as exc:
            # Per-segment failure never fails the job — fall back to cluster score.
            logger.warning(
                "Could not extract embedding for %s [%.2f-%.2f]: %s",
                turn["speaker_label"], turn["start"], turn["end"], exc,
            )
            turn["embedding"] = None

    # Group turns by speaker
    speakers: dict[str, list[dict]] = {}
    for turn in raw_turns:
        label = turn["speaker_label"]
        speakers.setdefault(label, []).append(turn)

    # Compute per-speaker average embedding from the successful per-segment embeddings
    speaker_embeddings: dict[str, np.ndarray] = {}
    for label, turns in speakers.items():
        embeddings = [t["embedding"] for t in turns if t["embedding"] is not None]
        if embeddings:
            avg = np.mean(np.stack(embeddings, axis=0), axis=0)
            speaker_embeddings[label] = avg
        else:
            logger.warning("No embeddings for speaker %s; assigning zero confidence", label)
            speaker_embeddings[label] = np.zeros_like(reference_embedding)

    # Cluster-level cosine similarity per speaker, clamped to the spec's [0, 1] range.
    # Reported on every segment as the secondary `speaker_match_confidence` signal.
    speaker_confidence: dict[str, float] = {
        label: clamp_confidence(cosine_similarity(reference_embedding, emb))
        for label, emb in speaker_embeddings.items()
    }

    # Per-segment match_confidence: the segment's OWN embedding vs the reference.
    # Fall back to the cluster score for sub-second segments (noisy embeddings)
    # and for segments whose embedding extraction failed.
    for turn in raw_turns:
        cluster_score = speaker_confidence.get(turn["speaker_label"], 0.0)
        duration = turn["end"] - turn["start"]
        if turn["embedding"] is None or duration < SHORT_SEGMENT_FALLBACK_SECS:
            turn["match_confidence"] = cluster_score
        else:
            turn["match_confidence"] = clamp_confidence(
                cosine_similarity(reference_embedding, turn["embedding"])
            )

    logger.info("Speaker confidences: %s", speaker_confidence)
    _progress(50)

    # ---- Phase 3: WAV slicing ----
    logger.info("Slicing WAV segments into %s", output_dir)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    _progress(75)

    # Load source audio once. NOTE: this reads the whole vocals file into RAM
    # (~2.4 GB for a 2 h stereo file). Left whole-file for v1 simplicity; a future
    # refinement could seek/read per-segment via sf.SoundFile to bound memory.
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
            "match_confidence": turn["match_confidence"],
            "speaker_match_confidence": speaker_confidence.get(label, 0.0),
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
