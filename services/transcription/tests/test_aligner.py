import aligner
from aligner import Word


class _W:
    def __init__(self, word, start, end, probability):
        self.word = word; self.start = start; self.end = end; self.probability = probability


def test_merge_overwrites_timestamps_keeps_text_and_probability():
    words = [_W(" Hello", 0.0, 0.4, 0.9), _W(" world.", 0.4, 0.9, 0.8)]
    merged = aligner.merge_alignment(words, [(0.05, 0.42), (0.42, 0.95)])
    assert [ (w.word, w.start, w.end, w.probability) for w in merged ] == [
        (" Hello", 0.05, 0.42, 0.9),
        (" world.", 0.42, 0.95, 0.8),
    ]


def test_merge_falls_back_on_count_mismatch():
    words = [_W(" Hi", 0.0, 0.3, 0.9), _W(" there.", 0.3, 0.7, 0.7)]
    merged = aligner.merge_alignment(words, [(0.0, 0.5)])  # wrong count
    assert [ (w.start, w.end) for w in merged ] == [(0.0, 0.3), (0.3, 0.7)]
    assert all(isinstance(w, Word) for w in merged)
