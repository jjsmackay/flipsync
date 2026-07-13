"""Task C3: thread the optional `align` flag through the transcriber.

The forced-alignment pass must run ONLY when align and resegment and there
are words to align — never for a non-resegmented segment. These tests inject
a fake aligner via `aligner_fn=` so the real torchaudio-backed
`aligner.align_words` (which needs torch/torchaudio) is never invoked here.
"""

import transcriber
from aligner import Word


class _FakeModel:
    """Yields one whisper 'segment' with two words spanning a long clip."""

    def transcribe(self, wav_path, **kw):
        class _WType:
            def __init__(s, word, start, end, p):
                s.word = word; s.start = start; s.end = end; s.probability = p

        class _Seg:
            text = " one two."
            words = [
                _WType(" one", 0.0, 3.0, 0.9),
                _WType(" two.", 3.5, 7.0, 0.9),
            ]

        return iter([_Seg()]), object()


def test_align_invoked_only_when_align_and_resegment(write_wav):
    wav = write_wav(duration_secs=8.0)
    calls = {"n": 0}

    def fake_align(wav_path, words, language):
        calls["n"] += 1
        # shift timestamps so we can detect the effect if we want
        return [Word(w.word, w.start, w.end, w.probability) for w in words]

    # resegment True + align True -> aligner called once
    transcriber.transcribe_segment(_FakeModel(), "seg1", wav, None,
                                   start_secs=0.0, resegment=True, align=True,
                                   aligner_fn=fake_align)
    assert calls["n"] == 1

    # align True but resegment False -> aligner NOT called
    calls["n"] = 0
    transcriber.transcribe_segment(_FakeModel(), "seg2", wav, None,
                                   start_secs=0.0, resegment=False, align=True,
                                   aligner_fn=fake_align)
    assert calls["n"] == 0


def test_align_defaults_off(write_wav):
    """align defaults False: existing resegment behaviour is byte-identical
    when align is never passed."""
    wav = write_wav(duration_secs=8.0)
    calls = {"n": 0}

    def fake_align(wav_path, words, language):
        calls["n"] += 1
        return [Word(w.word, w.start, w.end, w.probability) for w in words]

    transcriber.transcribe_segment(_FakeModel(), "seg3", wav, None,
                                   start_secs=0.0, resegment=True,
                                   aligner_fn=fake_align)
    assert calls["n"] == 0
