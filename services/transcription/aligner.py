"""Optional wav2vec2 forced-alignment pass to sharpen whisper word timestamps.

Enabled per project via `align_words`. Refines ONLY word start/end times; the
word token text and whisper's per-word probability (which drive confidence and
auto-approval) are preserved. Alignment affects sentence-aligned
re-segmentation boundaries only — it is a no-op for non-resegmented segments.

Backend: torchaudio-native MMS forced alignment (`torchaudio.pipelines.MMS_FA`),
not whisperx — see docs/superpowers/plans/notes/2026-07-13-alignment-dep-solve.md
for the dependency-solve rationale (whisperx drags in the whole pyannote/
Lightning stack we don't use). `merge_alignment` is the backend-agnostic,
unit-tested core; the real alignment call is lazily imported so this module
imports fine without torch/torchaudio installed.
"""

from dataclasses import dataclass


@dataclass
class Word:
    word: str
    start: float
    end: float
    probability: float


def merge_alignment(whisper_words, aligned) -> list[Word]:
    """Positionally overwrite word start/end with aligned spans.

    On any length mismatch the alignment is untrustworthy, so the original
    timestamps are returned unchanged (never corrupt timestamps).
    """
    if len(aligned) != len(whisper_words):
        return [Word(w.word, w.start, w.end, w.probability) for w in whisper_words]
    return [
        Word(w.word, a_start, a_end, w.probability)
        for w, (a_start, a_end) in zip(whisper_words, aligned)
    ]


def align_words(wav_path: str, whisper_words, language):
    """Refine word timestamps via torchaudio MMS forced alignment.

    Lazy-imports torch/torchaudio so this module (and callers with
    ``align=False``) work fine without them installed. Falls back to
    ``merge_alignment`` with an empty alignment (i.e. unchanged timestamps,
    normalised to ``Word`` instances) on any failure or token-count mismatch.
    ``language`` may be None (skip: no alignment model without a language).
    """
    if not whisper_words or language is None:
        return merge_alignment(whisper_words, [])  # normalises to Word list unchanged
    try:
        import torch, torchaudio
        from torchaudio.pipelines import MMS_FA as bundle

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = bundle.get_model().to(device)
        tokenizer = bundle.get_tokenizer()
        aligner_ = bundle.get_aligner()

        waveform, sr = torchaudio.load(wav_path)
        if sr != bundle.sample_rate:
            waveform = torchaudio.functional.resample(waveform, sr, bundle.sample_rate)
        waveform = waveform.mean(0, keepdim=True).to(device)  # mono

        # One transcript token per whisper word, normalised (lowercase, strip
        # punctuation) so token count == whisper word count. Words that
        # normalise to empty break the 1:1 mapping -> return unchanged.
        norm = ["".join(c for c in w.word.lower() if c.isalnum()) for w in whisper_words]
        if any(t == "" for t in norm):
            return merge_alignment(whisper_words, [])

        with torch.inference_mode():
            emission, _ = model(waveform)
        token_spans = aligner_(emission[0], tokenizer(norm))
        ratio = waveform.size(1) / emission.size(1) / bundle.sample_rate
        aligned = [(spans[0].start * ratio, spans[-1].end * ratio) for spans in token_spans]
        return merge_alignment(whisper_words, aligned)
    except Exception:
        return merge_alignment(whisper_words, [])
