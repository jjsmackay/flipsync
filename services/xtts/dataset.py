"""Dataset manifest → Coqui formatter CSV conversion.

Pure stdlib (json + csv); no ML dependencies. ``engine.finetune`` calls
``manifest_to_coqui_csv`` to turn a FlipSync dataset manifest (the export
manifest schema, with absolute ``audio_file`` paths) into the pipe-delimited
``metadata_train.csv`` / ``metadata_eval.csv`` files the Coqui ``coqui``
formatter expects.
"""

from __future__ import annotations

import csv
import json
import os

# Fixed speaker label — FlipSync trains a single-speaker voice per project.
_SPEAKER_NAME = "target"
# Coqui "coqui" formatter header.
_HEADER = ["audio_file", "text", "speaker_name"]


def load_manifest(manifest_path: str) -> list[dict]:
    """Load a dataset manifest and return its ``segments`` list.

    Raises ``ValueError`` if the file is missing a non-empty ``segments`` list.
    """
    with open(manifest_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    segments = data.get("segments")
    if not segments:
        raise ValueError(
            f"manifest {manifest_path} has no segments to train on"
        )
    return segments


def _clean_text(text: str) -> str:
    """Make text safe for a pipe-delimited single-line CSV cell.

    Pipes would break the column boundary and newlines would break the row, so
    both collapse to spaces; surrounding whitespace is trimmed.
    """
    return (text or "").replace("|", " ").replace("\n", " ").replace("\r", " ").strip()


def manifest_to_coqui_csv(
    manifest_path: str, out_dir: str, eval_split: float = 0.1
) -> tuple[str, str]:
    """Convert a manifest to Coqui train/eval CSVs; return their paths.

    The split is deterministic: segments are sorted by ``id`` and every
    ``round(1/eval_split)``-th row (index % 10 == 0 for the default 0.1) is held
    out for eval, the rest go to train. At least one row is guaranteed in each
    file — if the split would leave either empty, one row is moved across.
    """
    segments = load_manifest(manifest_path)
    segments = sorted(segments, key=lambda s: s["id"])

    stride = max(round(1 / eval_split), 2) if eval_split > 0 else len(segments) + 1

    train_rows: list[list[str]] = []
    eval_rows: list[list[str]] = []
    for idx, seg in enumerate(segments):
        row = [seg["audio_file"], _clean_text(seg.get("text", "")), _SPEAKER_NAME]
        if idx % stride == 0:
            eval_rows.append(row)
        else:
            train_rows.append(row)

    # Guarantee at least one row on each side.
    if not eval_rows and train_rows:
        eval_rows.append(train_rows.pop())
    if not train_rows and eval_rows:
        train_rows.append(eval_rows.pop())

    os.makedirs(out_dir, exist_ok=True)
    train_csv = os.path.join(out_dir, "metadata_train.csv")
    eval_csv = os.path.join(out_dir, "metadata_eval.csv")
    _write_csv(train_csv, train_rows)
    _write_csv(eval_csv, eval_rows)
    return train_csv, eval_csv


def _write_csv(path: str, rows: list[list[str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="|", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(_HEADER)
        writer.writerows(rows)
