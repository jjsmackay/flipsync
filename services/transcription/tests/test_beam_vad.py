"""Increment C: whisper `beam_size` and `vad_filter` promoted from faster-whisper
defaults to per-job parameters set from project config.
"""

from unittest.mock import MagicMock, patch

import transcriber


class _FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, wav_path, **kwargs):
        self.calls.append(kwargs)
        return iter([]), None


def test_transcribe_segment_forwards_beam_and_vad():
    model = _FakeModel()
    with patch("transcriber.get_wav_duration", return_value=5.0):
        transcriber.transcribe_segment(
            model, "seg1", "/x.wav", None, beam_size=3, vad_filter=True
        )
    assert model.calls[0]["beam_size"] == 3
    assert model.calls[0]["vad_filter"] is True


def test_process_batch_forwards_beam_and_vad():
    model = _FakeModel()
    batch = [{"id": "s1", "wav_path": "/a.wav"}]
    with patch("transcriber.get_wav_duration", return_value=5.0):
        transcriber.process_batch(model, batch, None, max_workers=1,
                                  beam_size=2, vad_filter=True)
    assert model.calls[0]["beam_size"] == 2
    assert model.calls[0]["vad_filter"] is True


def test_defaults_preserve_historical_behaviour():
    """Unset → beam_size 5, vad_filter off (faster-whisper's defaults)."""
    model = _FakeModel()
    with patch("transcriber.get_wav_duration", return_value=5.0):
        transcriber.transcribe_segment(model, "seg1", "/x.wav", None)
    assert model.calls[0]["beam_size"] == 5
    assert model.calls[0]["vad_filter"] is False


def test_jobrequest_accepts_beam_and_vad():
    from main import JobRequest
    req = JobRequest(job_id="j", segments=[])
    assert req.beam_size == 5
    assert req.vad_filter is False
    req2 = JobRequest(job_id="j", segments=[], beam_size=1, vad_filter=True)
    assert req2.beam_size == 1
    assert req2.vad_filter is True
