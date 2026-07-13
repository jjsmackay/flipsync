# Alignment dependency-solve spike (Task C0)

**Date:** 2026-07-13
**Verdict: GO, but with the torchaudio-native backend — NOT whisperx.**

## What was tested

Joint resolution against the transcription image's pinned engine
(`faster-whisper==1.2.1` / `ctranslate2==4.8.1`), Python 3.12. No GPU on the
spike host; alignment inference itself is owed on the deploy GPU host.

Both candidates resolve (`uv pip compile` → EXIT 0). The decision is footprint,
not feasibility.

| backend | total pkgs | torch | drags in |
|---|---|---|---|
| whisperx 3.8.6 | **116** | 2.8.0 | pyannote-audio 4.0.7, pytorch-lightning, transformers, torchvision |
| **torchaudio-native** | **60** | pin to base | torch + torchaudio (+ transformers, droppable) — no pyannote, no lightning |

Both keep `faster-whisper==1.2.1` / `ctranslate2==4.8.1` intact.

## Decision → torchaudio-native forced alignment

whisperx hauls the entire pyannote diarisation + Lightning training stack into a
lean image for a feature we explicitly do NOT use (whisperx diarisation was ruled
out earlier — our pyannote diarisation stage owns speaker selection). WhisperX's
alignment *is* wav2vec2 forced alignment; torchaudio's native forced-alignment API
delivers the same technique at ~half the dependency footprint and none of the
pyannote baggage. User approved proceeding on this recommendation.

## Reference implementation for `aligner.align_words` (Task C2 step 3, real body)

Behind the lazy import + try/except fallback already specified. `merge_alignment`
(the unit-tested core) is unchanged and backend-agnostic — it protects
correctness: any token-count mismatch falls back to whisper's original timestamps.

```python
def align_words(wav_path, whisper_words, language):
    """Refine word timestamps via torchaudio MMS forced alignment.
    Falls back to whisper's timestamps on any failure or token-count mismatch."""
    if not whisper_words or language is None:
        return merge_alignment(whisper_words, [])
    try:
        import torch, torchaudio
        from torchaudio.pipelines import MMS_FA as bundle

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = bundle.get_model().to(device)
        tokenizer = bundle.get_tokenizer()
        aligner = bundle.get_aligner()

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
        token_spans = aligner(emission[0], tokenizer(norm))
        ratio = waveform.size(1) / emission.size(1) / bundle.sample_rate
        aligned = [(spans[0].start * ratio, spans[-1].end * ratio) for spans in token_spans]
        return merge_alignment(whisper_words, aligned)
    except Exception:
        return merge_alignment(whisper_words, [])
```

## C6 dependency change (torchaudio path)

```
# Optional forced-alignment pass (align_words) — torchaudio-native MMS forced
# aligner (see docs/.../2026-07-13-alignment-dep-solve.md). MUST NOT perturb
# ctranslate2==4.8.1 system-CUDA linkage — pin torch to a cu12 build matching
# the CUDA 12.9 base image.
torch==<pin to match base image CUDA>
torchaudio==<matching>
```
Drop `transformers` unless the chosen alignment model needs it (MMS_FA does not).

## Still owed (deploy GPU host)
- Alignment actually runs and emits word-level start/end for the fixture.
- ctranslate2 CUDA linkage confirmed intact with the torch cu12 wheels present
  in the same image (two CUDA runtimes coexisting — the real risk).
- Sanity-check refined boundaries improve resegment splits vs whisper-native.
