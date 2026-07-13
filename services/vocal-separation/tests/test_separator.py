"""Unit tests for separator.py chunking and stitching logic.

These tests do NOT require Demucs to be installed or a GPU.
They test the pure math: chunk index computation and crossfade stitching.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

# stitch_chunks operates on torch tensors, so these tests need torch installed.
# Skip cleanly (rather than error at collection) in environments without it.
torch = pytest.importorskip("torch")

from separator import compute_chunks, stitch_chunks, CHUNK_OVERLAP_SECS


SAMPLE_RATE = 44100


# ---------------------------------------------------------------------------
# shifts forwarding (Increment C)
# ---------------------------------------------------------------------------

class TestShiftsForwarding:
    def test_separate_forwards_shifts_to_apply_demucs(self):
        from unittest.mock import MagicMock, patch
        import separator

        fake_model = MagicMock()
        fake_model.samplerate = SAMPLE_RATE
        fake_model.sources = ["drums", "bass", "other", "vocals"]
        audio = torch.zeros(1, SAMPLE_RATE)
        with patch("separator._load_model", return_value=fake_model), \
             patch("separator._apply_demucs", return_value=torch.zeros(1, SAMPLE_RATE)) as m_apply, \
             patch("separator.os.path.exists", return_value=True), \
             patch("separator.os.makedirs"), \
             patch("torchaudio.load", return_value=(audio, SAMPLE_RATE)), \
             patch("torchaudio.save"):
            separator.separate("/in.wav", "/out.wav", model_name="htdemucs", shifts=3)
        assert m_apply.call_args.kwargs.get("shifts") == 3


# ---------------------------------------------------------------------------
# compute_chunks
# ---------------------------------------------------------------------------

class TestComputeChunks:
    """Tests for chunk boundary computation."""

    def _sr(self, secs: float) -> int:
        return int(secs * SAMPLE_RATE)

    def test_single_chunk_when_audio_shorter_than_chunk(self):
        total = self._sr(3)
        chunk = self._sr(5)
        overlap = self._sr(1)
        chunks = compute_chunks(total, chunk, overlap)
        assert len(chunks) == 1
        assert chunks[0] == (0, total)

    def test_exact_two_chunks(self):
        # 7s audio, 5s chunk, 1s overlap → step=4s
        # chunk 1: 0→5s, chunk 2: 4s→7s (end clamped)
        chunk_secs = 5
        overlap_secs = 1
        total_secs = 7
        total = self._sr(total_secs)
        chunk = self._sr(chunk_secs)
        overlap = self._sr(overlap_secs)
        chunks = compute_chunks(total, chunk, overlap)
        assert len(chunks) == 2
        assert chunks[0][0] == 0
        assert chunks[0][1] == self._sr(5)
        assert chunks[1][0] == self._sr(4)  # step = 5-1 = 4
        assert chunks[1][1] == total

    def test_multiple_chunks_correct_count(self):
        # 12s audio, 5s chunk, 1s overlap → step=4s
        # starts: 0, 4, 8 → 3 chunks
        chunk_secs = 5
        overlap_secs = 1
        total_secs = 12
        total = self._sr(total_secs)
        chunk = self._sr(chunk_secs)
        overlap = self._sr(overlap_secs)
        chunks = compute_chunks(total, chunk, overlap)
        assert len(chunks) == 3

    def test_chunks_cover_full_audio(self):
        """First chunk starts at 0, last chunk ends at total_samples."""
        total = self._sr(20)
        chunk = self._sr(5)
        overlap = self._sr(1)
        chunks = compute_chunks(total, chunk, overlap)
        assert chunks[0][0] == 0
        assert chunks[-1][1] == total

    def test_consecutive_chunks_overlap(self):
        """Each consecutive pair of chunks overlaps by exactly overlap_samples."""
        total = self._sr(20)
        chunk = self._sr(5)
        overlap = self._sr(1)
        step = chunk - overlap
        chunks = compute_chunks(total, chunk, overlap)
        for i in range(len(chunks) - 1):
            # start of chunk i+1 should be start of chunk i + step
            assert chunks[i + 1][0] == chunks[i][0] + step

    def test_zero_overlap_no_overlap_between_chunks(self):
        total = self._sr(10)
        chunk = self._sr(5)
        overlap = 0
        chunks = compute_chunks(total, chunk, overlap)
        assert len(chunks) == 2
        assert chunks[0] == (0, self._sr(5))
        assert chunks[1] == (self._sr(5), total)

    def test_invalid_overlap_raises(self):
        with pytest.raises(ValueError):
            compute_chunks(44100, 44100, 88200)  # overlap >= chunk

    def test_single_sample(self):
        chunks = compute_chunks(1, 44100, 0)
        assert len(chunks) == 1
        assert chunks[0] == (0, 1)


# ---------------------------------------------------------------------------
# stitch_chunks
# ---------------------------------------------------------------------------

class TestStitchChunks:
    """Tests for crossfade stitching logic."""

    def _sine_chunk(self, n_samples: int, channels: int = 1, frequency: float = 440.0) -> torch.Tensor:
        t = torch.linspace(0.0, n_samples / SAMPLE_RATE, n_samples)
        mono = torch.sin(2 * math.pi * frequency * t)
        return mono.unsqueeze(0).expand(channels, -1).clone()

    def test_single_chunk_returned_unchanged(self):
        chunk = self._sine_chunk(SAMPLE_RATE * 3)
        result = stitch_chunks([chunk], overlap_samples=SAMPLE_RATE)
        assert torch.allclose(result, chunk)

    def test_two_chunks_output_length(self):
        """Stitched output length = sum of chunk lengths - overlap."""
        chunk_secs = 5
        overlap_secs = 1
        c1 = self._sine_chunk(SAMPLE_RATE * chunk_secs)
        # Second chunk shorter (simulates last chunk)
        c2 = self._sine_chunk(SAMPLE_RATE * 3)
        overlap = int(overlap_secs * SAMPLE_RATE)
        result = stitch_chunks([c1, c2], overlap_samples=overlap)

        expected_len = c1.shape[1] + c2.shape[1] - overlap
        assert result.shape[1] == expected_len

    def test_three_chunks_output_length(self):
        chunk_secs = 5
        overlap_secs = 1
        overlap = int(overlap_secs * SAMPLE_RATE)
        chunks = [self._sine_chunk(SAMPLE_RATE * chunk_secs) for _ in range(3)]
        result = stitch_chunks(chunks, overlap_samples=overlap)
        # Each stitch removes one overlap region
        expected_len = sum(c.shape[1] for c in chunks) - overlap * (len(chunks) - 1)
        assert result.shape[1] == expected_len

    def test_stereo_chunks_preserve_channels(self):
        c1 = self._sine_chunk(SAMPLE_RATE * 5, channels=2)
        c2 = self._sine_chunk(SAMPLE_RATE * 3, channels=2)
        overlap = SAMPLE_RATE
        result = stitch_chunks([c1, c2], overlap_samples=overlap)
        assert result.shape[0] == 2

    def test_empty_list_raises(self):
        with pytest.raises(ValueError):
            stitch_chunks([], overlap_samples=SAMPLE_RATE)

    def test_crossfade_boundary_not_discontinuous(self):
        """The transition region should not have abrupt jumps (smoke test)."""
        overlap = SAMPLE_RATE  # 1 second
        # Use constant-value chunks so we can predict the crossfade result.
        c1 = torch.ones(1, SAMPLE_RATE * 5)
        c2 = torch.ones(1, SAMPLE_RATE * 5) * 0.5
        result = stitch_chunks([c1, c2], overlap_samples=overlap)

        # The crossfade region is at samples [4*SAMPLE_RATE : 5*SAMPLE_RATE]
        # At the start of the crossfade: c1=1.0, c2=0.5 → weighted ~1.0
        # At the end of the crossfade: c1=1.0, c2=0.5 → weighted ~0.5
        crossfade_start = result[0, 4 * SAMPLE_RATE].item()
        crossfade_end = result[0, 5 * SAMPLE_RATE - 1].item()
        # Start of crossfade should be closer to 1.0 (c1 dominates)
        assert crossfade_start > 0.9
        # End of crossfade should be closer to 0.5 (c2 dominates)
        assert crossfade_end < 0.6

    def test_no_overlap_is_pure_concatenation(self):
        c1 = torch.ones(1, SAMPLE_RATE * 3)
        c2 = torch.ones(1, SAMPLE_RATE * 2) * 2.0
        result = stitch_chunks([c1, c2], overlap_samples=0)
        expected = torch.cat([c1, c2], dim=1)
        assert torch.allclose(result, expected)

    def test_approximate_output_length_with_real_chunks(self, sample_wav):
        """Integration smoke: stitch of real-audio chunks has expected length.

        The stitched result length = sum(chunk_lengths) - n_overlaps * overlap_s
        because each stitch removes the trailing overlap of the previous chunk
        and replaces it with the crossfaded region (overlap_s long).
        For non-overlapping coverage this equals approximately total_samples.
        """
        import torchaudio

        audio, sr = torchaudio.load(sample_wav)
        total_samples = audio.shape[1]
        chunk_secs = 3
        overlap_secs = 1
        chunk_s = int(chunk_secs * sr)
        overlap_s = int(overlap_secs * sr)

        from separator import compute_chunks

        chunk_indices = compute_chunks(total_samples, chunk_s, overlap_s)
        chunks = [audio[:, start:end] for start, end in chunk_indices]
        result = stitch_chunks(chunks, overlap_samples=overlap_s)

        n_overlaps = len(chunks) - 1
        # Expected = sum of all chunk lengths minus the overlaps that are merged
        expected = sum(end - start for start, end in chunk_indices) - n_overlaps * overlap_s
        # Allow ±1 sample for rounding
        assert abs(result.shape[1] - expected) <= 1
