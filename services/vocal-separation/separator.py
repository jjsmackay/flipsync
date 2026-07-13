"""Demucs vocal separation logic.

Handles model loading, whole-file processing, chunked processing with overlap,
and WAV stitching. Designed to be called from the FastAPI job runner.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Model registry — loaded on demand, cached across jobs.
_model_cache: dict[str, object] = {}

# Overlap for chunked processing in seconds.
CHUNK_OVERLAP_SECS = 1

# Supported model names.
VALID_MODELS = {"htdemucs", "mdx_extra"}


def _load_model(model_name: str):
    """Load a Demucs model, caching it for reuse."""
    import torch
    if model_name in _model_cache:
        return _model_cache[model_name]

    from demucs.pretrained import get_model

    logger.info("Loading Demucs model: %s", model_name)
    model = get_model(model_name)
    model.eval()
    if torch.cuda.is_available():
        model = model.cuda()
    _model_cache[model_name] = model
    logger.info("Model %s loaded successfully", model_name)
    return model


def _apply_demucs(model, audio, sample_rate: int, shifts: int = 0):
    """Run Demucs on a (channels, samples) tensor.

    Returns the vocals stem as a (channels, samples) tensor.
    """
    import torch
    from demucs.apply import apply_model

    # Demucs expects (batch, channels, samples)
    waveform = audio.unsqueeze(0)
    if torch.cuda.is_available():
        waveform = waveform.cuda()

    with torch.no_grad():
        sources = apply_model(
            model,
            waveform,
            overlap=0.25,
            shifts=shifts,
            split=True,
            progress=False,
        )

    # sources: (batch, stems, channels, samples)
    # Get the vocals stem index from model.sources list
    stem_names = list(model.sources)
    vocals_idx = stem_names.index("vocals")
    vocals = sources[0, vocals_idx]  # (channels, samples)
    return vocals.cpu()


def separate(
    input_path: str,
    output_path: str,
    model_name: str = "htdemucs",
    chunk_secs: Optional[int] = None,
    shifts: int = 0,
    progress_callback=None,
) -> None:  # noqa: E501
    """Separate vocals from audio file and write to output_path.

    Args:
        input_path: Path to input WAV file.
        output_path: Path to write vocals WAV.
        model_name: Demucs model name.
        chunk_secs: If set, process in chunks of this many seconds.
        shifts: Demucs test-time augmentation passes (0 = none).
        progress_callback: Optional callable(progress: int) called during processing.

    Raises:
        torch.cuda.OutOfMemoryError: On CUDA OOM.
        FileNotFoundError: If input_path does not exist.
    """
    import torchaudio

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    model = _load_model(model_name)

    # Load audio
    audio, sample_rate = torchaudio.load(input_path)
    # audio: (channels, samples)

    # Resample if needed (Demucs models have a fixed sample rate)
    model_samplerate = model.samplerate
    if sample_rate != model_samplerate:
        resampler = torchaudio.transforms.Resample(
            orig_freq=sample_rate, new_freq=model_samplerate
        )
        audio = resampler(audio)
        sample_rate = model_samplerate

    if chunk_secs is None:
        # Whole-file processing
        if progress_callback:
            progress_callback(50)

        vocals = _apply_demucs(model, audio, sample_rate, shifts=shifts)

        if progress_callback:
            progress_callback(100)
    else:
        # Chunked processing with overlap
        vocals = _process_chunked(
            model, audio, sample_rate, chunk_secs, progress_callback, shifts=shifts
        )

    torchaudio.save(output_path, vocals, sample_rate)
    logger.info("Wrote vocals to %s", output_path)


def _process_chunked(
    model,
    audio: torch.Tensor,
    sample_rate: int,
    chunk_secs: int,
    progress_callback=None,
    shifts: int = 0,
) -> torch.Tensor:
    """Process audio in overlapping chunks and stitch vocals back together.

    Args:
        model: Loaded Demucs model.
        audio: (channels, samples) tensor.
        sample_rate: Audio sample rate.
        chunk_secs: Size of each chunk in seconds.
        progress_callback: Optional callable(progress: int).

    Returns:
        Stitched vocals as (channels, samples) tensor.
    """
    total_samples = audio.shape[1]
    chunk_samples = int(chunk_secs * sample_rate)
    overlap_samples = int(CHUNK_OVERLAP_SECS * sample_rate)

    # Calculate chunks
    chunks = compute_chunks(total_samples, chunk_samples, overlap_samples)
    n_chunks = len(chunks)
    logger.info(
        "Chunked processing: %d chunks, chunk_secs=%d, overlap=%ds",
        n_chunks,
        chunk_secs,
        CHUNK_OVERLAP_SECS,
    )

    # Process each chunk
    processed_chunks = []
    for i, (start, end) in enumerate(chunks):
        chunk_audio = audio[:, start:end]
        vocals_chunk = _apply_demucs(model, chunk_audio, sample_rate, shifts=shifts)
        processed_chunks.append(vocals_chunk)

        if progress_callback:
            progress = int((i + 1) / n_chunks * 100)
            progress_callback(progress)

    # Stitch chunks together
    return stitch_chunks(processed_chunks, overlap_samples)


def compute_chunks(
    total_samples: int, chunk_samples: int, overlap_samples: int
) -> list[tuple[int, int]]:
    """Compute (start, end) sample indices for each chunk.

    Args:
        total_samples: Total number of samples.
        chunk_samples: Chunk size in samples.
        overlap_samples: Overlap between chunks in samples.

    Returns:
        List of (start, end) tuples (end is exclusive).
    """
    step_samples = chunk_samples - overlap_samples
    if step_samples <= 0:
        raise ValueError("chunk_samples must be greater than overlap_samples")

    chunks = []
    start = 0
    while start < total_samples:
        end = min(start + chunk_samples, total_samples)
        chunks.append((start, end))
        if end == total_samples:
            break
        start += step_samples

    return chunks


def stitch_chunks(chunks: list, overlap_samples: int):
    """Stitch processed chunks with linear crossfade over the overlap region.

    Args:
        chunks: List of (channels, samples) tensors.
        overlap_samples: Number of overlap samples between consecutive chunks.

    Returns:
        Stitched (channels, samples) tensor.
    """
    import torch

    if len(chunks) == 0:
        raise ValueError("No chunks to stitch")
    if len(chunks) == 1:
        return chunks[0]

    # Build the crossfade ramps once
    fade_out = torch.linspace(1.0, 0.0, overlap_samples)  # (overlap_samples,)
    fade_in = torch.linspace(0.0, 1.0, overlap_samples)   # (overlap_samples,)

    # We accumulate output by maintaining a running output buffer.
    # For each chunk after the first:
    #   - the tail (overlap) of the previous chunk fades out
    #   - the head (overlap) of the current chunk fades in
    # The non-overlapping body of each chunk is appended as-is.

    result = chunks[0]  # Start with first full chunk

    for i in range(1, len(chunks)):
        curr = chunks[i]
        n_channels = result.shape[0]

        # The overlap region: last overlap_samples of result, first overlap_samples of curr
        overlap_end = result.shape[1]
        overlap_start = overlap_end - overlap_samples

        # Clamp in case last chunk is shorter than overlap
        actual_overlap = min(overlap_samples, result.shape[1], curr.shape[1])
        if actual_overlap <= 0:
            # No overlap possible, just concatenate
            result = torch.cat([result, curr], dim=1)
            continue

        actual_fade_out = torch.linspace(1.0, 0.0, actual_overlap)
        actual_fade_in = torch.linspace(0.0, 1.0, actual_overlap)

        # Crossfade region
        region_result = result[:, result.shape[1] - actual_overlap:]
        region_curr = curr[:, :actual_overlap]

        # Broadcast fades over channels
        crossfaded = (
            region_result * actual_fade_out.unsqueeze(0)
            + region_curr * actual_fade_in.unsqueeze(0)
        )

        # Concatenate: result without last overlap + crossfade + rest of current chunk
        result = torch.cat(
            [
                result[:, : result.shape[1] - actual_overlap],
                crossfaded,
                curr[:, actual_overlap:],
            ],
            dim=1,
        )

    return result


def is_model_loaded() -> bool:
    """True while any Demucs model is resident in the cache (and thus VRAM)."""
    return bool(_model_cache)


def unload_models() -> None:
    """Drop all cached Demucs models and return their VRAM to the driver.

    Clearing the cache drops the Python refs; ``gc.collect()`` finalises any
    lingering tensors and ``torch.cuda.empty_cache()`` releases Torch's reserved
    blocks back to CUDA so another process/container can allocate them. Torch may
    be absent (unit tests) — the cache clear still happens, the rest is a no-op.
    """
    import gc

    _model_cache.clear()
    gc.collect()
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:
        pass


def preload_models(model_names: list[str]) -> None:
    """Preload models at startup to avoid first-job latency.

    Failures are NOT swallowed: if a model cannot be loaded (e.g. a torch
    version that breaks Demucs checkpoint unpickling), the exception
    propagates so the caller can surface it loudly and mark the service
    unhealthy rather than silently accepting jobs that will all fail.
    """
    for name in model_names:
        _load_model(name)
