"""Tests for the manifest → Coqui CSV conversion (pure stdlib, no ML deps)."""

from __future__ import annotations

import csv

import pytest

import dataset


def _segments(n: int) -> list[dict]:
    return [
        {
            "id": f"{i:04d}-seg",
            "audio_file": f"/data/projects/p/segments/cleaned/{i:04d}.wav",
            "text": f"Line number {i}.",
        }
        for i in range(n)
    ]


def _read_csv(path: str) -> list[list[str]]:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh, delimiter="|"))


def test_twelve_segments_split(manifest_file, tmp_path):
    manifest = manifest_file(_segments(12))
    train_csv, eval_csv = dataset.manifest_to_coqui_csv(
        manifest, str(tmp_path / "out"), eval_split=0.1
    )
    train = _read_csv(train_csv)
    ev = _read_csv(eval_csv)

    # Header on both.
    assert train[0] == ["audio_file", "text", "speaker_name"]
    assert ev[0] == ["audio_file", "text", "speaker_name"]

    train_rows, eval_rows = train[1:], ev[1:]
    # index % 10 == 0 → eval (indices 0, 10) ⇒ 2 eval, 10 train.
    assert 10 <= len(train_rows) <= 11
    assert len(eval_rows) >= 1
    assert len(train_rows) + len(eval_rows) == 12

    # speaker_name fixed, absolute paths preserved.
    for row in train_rows + eval_rows:
        assert row[0].startswith("/data/projects/p/segments/")
        assert row[2] == "target"


def test_two_segments_one_each(manifest_file, tmp_path):
    manifest = manifest_file(_segments(2))
    train_csv, eval_csv = dataset.manifest_to_coqui_csv(
        manifest, str(tmp_path / "out"), eval_split=0.1
    )
    assert len(_read_csv(train_csv)) - 1 == 1
    assert len(_read_csv(eval_csv)) - 1 == 1


def test_pipe_and_newline_safe_text(manifest_file, tmp_path):
    segs = [
        {"id": "a", "audio_file": "/x/a.wav", "text": "hello | world\nsecond line"},
        {"id": "b", "audio_file": "/x/b.wav", "text": "plain"},
    ]
    manifest = manifest_file(segs)
    train_csv, eval_csv = dataset.manifest_to_coqui_csv(
        manifest, str(tmp_path / "out"), eval_split=0.1
    )
    rows = _read_csv(train_csv)[1:] + _read_csv(eval_csv)[1:]
    for row in rows:
        # Exactly three columns ⇒ no stray pipe/newline broke the row.
        assert len(row) == 3
        assert "|" not in row[1]
        assert "\n" not in row[1]


def test_empty_segments_raises(manifest_file, tmp_path):
    manifest = manifest_file([])
    with pytest.raises(ValueError):
        dataset.manifest_to_coqui_csv(manifest, str(tmp_path / "out"))


def test_missing_segments_key_raises(tmp_path):
    path = str(tmp_path / "bad.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"version": "1"}')
    with pytest.raises(ValueError):
        dataset.load_manifest(path)
