"""Sentence-aligned re-segmentation — word splitting, normalisation, WAV slicing.

Implements spec/pipeline.md §Sentence-aligned re-segmentation. The word
sequence (faster-whisper words with .word/.start/.end/.probability) is split
into utterances at sentence-terminal punctuation and long inter-word gaps,
normalised to the 1–15 s target band, and each utterance is sliced out of the
parent segment WAV as a child WAV with a service-generated UUID filename.

Only the stdlib `wave` module is used for slicing: segment WAVs are
uncompressed PCM written by the diarisation service, so frame-accurate
copy-through needs no audio dependency.
"""

import os
import uuid
import wave

# Inter-word silence at or above this splits utterances.
GAP_SPLIT_SECS = 0.6
# Utterances longer than this are force-split at their largest internal gap.
MAX_UTTERANCE_SECS = 15.0
# Utterances shorter than this are merged into a neighbour when possible.
MIN_UTTERANCE_SECS = 1.0

SENTENCE_TERMINALS = ".!?…"
# Closing quotes/brackets that may trail sentence-terminal punctuation.
CLOSING_CHARS = "\"')]}»”’"


def ends_sentence(token: str) -> bool:
    """True if the word token ends with sentence-terminal punctuation,
    optionally followed by a closing quote or bracket."""
    stripped = token.rstrip().rstrip(CLOSING_CHARS)
    return bool(stripped) and stripped[-1] in SENTENCE_TERMINALS


def _span_secs(utterance: list) -> float:
    """Duration of an utterance: first word start to last word end."""
    return utterance[-1].end - utterance[0].start


def split_into_utterances(words: list) -> list[list]:
    """Initial split: at sentence-terminal punctuation and gaps >= 0.6 s."""
    utterances: list[list] = []
    current: list = []
    for i, w in enumerate(words):
        current.append(w)
        boundary = ends_sentence(w.word)
        if not boundary and i + 1 < len(words):
            gap = words[i + 1].start - w.end
            boundary = gap >= GAP_SPLIT_SECS
        if boundary:
            utterances.append(current)
            current = []
    if current:
        utterances.append(current)
    return utterances


def _largest_gap_split(utterance: list) -> tuple[list, list]:
    """Split an utterance in two at its largest internal inter-word gap."""
    best_i = 0
    best_gap = float("-inf")
    for i in range(len(utterance) - 1):
        gap = utterance[i + 1].start - utterance[i].end
        if gap > best_gap:
            best_gap = gap
            best_i = i
    return utterance[: best_i + 1], utterance[best_i + 1 :]


def normalise_utterances(utterances: list[list]) -> list[list]:
    """Normalise utterances to the target band.

    1. Force-split utterances > 15 s at their largest internal inter-word gap,
       repeated until every piece is <= 15 s (a single-word utterance cannot
       be split and is left as-is).
    2. Merge utterances < 1 s into the following utterance (or the preceding
       one, if last) provided the merged span stays <= 15 s.
    """
    # Pass 1 — force-split. Stack preserves order: push right then left.
    sized: list[list] = []
    stack = list(reversed(utterances))
    while stack:
        utt = stack.pop()
        if _span_secs(utt) > MAX_UTTERANCE_SECS and len(utt) >= 2:
            left, right = _largest_gap_split(utt)
            stack.append(right)
            stack.append(left)
        else:
            sized.append(utt)

    # Pass 2 — merge short utterances forward.
    merged: list[list] = []
    i = 0
    while i < len(sized):
        utt = sized[i]
        while (
            _span_secs(utt) < MIN_UTTERANCE_SECS
            and i + 1 < len(sized)
            and sized[i + 1][-1].end - utt[0].start <= MAX_UTTERANCE_SECS
        ):
            i += 1
            utt = utt + sized[i]
        merged.append(utt)
        i += 1

    # A short final utterance merges into the preceding one.
    if (
        len(merged) >= 2
        and _span_secs(merged[-1]) < MIN_UTTERANCE_SECS
        and merged[-1][-1].end - merged[-2][0].start <= MAX_UTTERANCE_SECS
    ):
        last = merged.pop()
        merged[-1] = merged[-1] + last

    return merged


def compute_boundaries(
    utterances: list[list], file_duration: float
) -> list[tuple[float, float]]:
    """In-file (start, end) boundaries for each utterance.

    Adjacent children meet at the midpoint of the inter-word gap between
    them. The first child starts at 0.0; the last ends at the file duration.
    No audio is lost or duplicated.
    """
    edges = [0.0]
    for prev, nxt in zip(utterances, utterances[1:]):
        edges.append((prev[-1].end + nxt[0].start) / 2.0)
    edges.append(file_duration)
    return list(zip(edges, edges[1:]))


def slice_children(
    parent_wav_path: str, boundaries: list[tuple[float, float]]
) -> list[dict]:
    """Slice child WAVs from the parent at the given in-file boundaries.

    Children are written to the parent's directory with full-UUID filenames.
    Boundary seconds are converted to frame indices once, so adjacent
    children share the exact boundary frame — sample-accurate, no overlap,
    no dropped frames. Returns [{"id", "wav_path"}] in boundary order.
    """
    out_dir = os.path.dirname(parent_wav_path)
    results: list[dict] = []

    with wave.open(parent_wav_path, "rb") as parent:
        n_channels = parent.getnchannels()
        samp_width = parent.getsampwidth()
        rate = parent.getframerate()
        n_frames = parent.getnframes()

        # Shared frame edges: first is 0, last is n_frames; interior edges
        # come from the boundary end times, clamped monotonic.
        edges = [0]
        for _start, end in boundaries[:-1]:
            frame = round(end * rate)
            edges.append(min(n_frames, max(edges[-1], frame)))
        edges.append(n_frames)

        for i in range(len(boundaries)):
            child_id = str(uuid.uuid4())
            child_path = os.path.join(out_dir, f"{child_id}.wav")
            parent.setpos(edges[i])
            frames = parent.readframes(edges[i + 1] - edges[i])
            with wave.open(child_path, "wb") as child:
                child.setnchannels(n_channels)
                child.setsampwidth(samp_width)
                child.setframerate(rate)
                child.writeframes(frames)
            results.append({"id": child_id, "wav_path": child_path})

    return results
