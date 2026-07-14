"""Tests for the manifest → GPT-SoVITS `.list` conversion and reference-segment
selection (pure stdlib, no ML deps).
"""

from __future__ import annotations

import pytest

import dataset


def _segments(n: int, **over) -> list[dict]:
    segs = [
        {
            "id": f"{i:04d}-seg",
            "audio_file": f"/data/projects/p/segments/cleaned/{i:04d}.wav",
            "text": f"Line number {i}.",
            "duration_secs": 4.0,
        }
        for i in range(n)
    ]
    for seg, extra in zip(segs, [over] * n):
        seg.update(extra)
    return segs


def _read_list(path: str) -> list[list[str]]:
    with open(path, "r", encoding="utf-8") as fh:
        return [line.rstrip("\n").split("|") for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# manifest_to_list
# ---------------------------------------------------------------------------


def test_happy_path_four_fields(manifest_file, tmp_path):
    manifest = manifest_file(_segments(3))
    list_path = dataset.manifest_to_list(manifest, str(tmp_path / "train.list"))
    rows = _read_list(list_path)

    assert len(rows) == 3
    for i, row in enumerate(rows):
        assert len(row) == 4
        wav_path, speaker, lang, text = row
        assert wav_path == f"/data/projects/p/segments/cleaned/{i:04d}.wav"
        assert lang == "en"
        assert text == f"Line number {i}."


def test_absolute_wav_paths_preserved(manifest_file, tmp_path):
    segs = _segments(2)
    manifest = manifest_file(segs)
    list_path = dataset.manifest_to_list(manifest, str(tmp_path / "train.list"))
    rows = _read_list(list_path)
    for row in rows:
        assert row[0].startswith("/data/projects/p/segments/")


def test_language_token_always_lowercase_en(manifest_file, tmp_path):
    manifest = manifest_file(_segments(1))
    list_path = dataset.manifest_to_list(manifest, str(tmp_path / "train.list"))
    rows = _read_list(list_path)
    assert rows[0][2] == "en"


def test_pipe_in_text_is_sanitised(manifest_file, tmp_path):
    segs = [
        {"id": "a", "audio_file": "/x/a.wav", "text": "hello | world", "duration_secs": 4.0},
        {"id": "b", "audio_file": "/x/b.wav", "text": "plain text", "duration_secs": 4.0},
    ]
    manifest = manifest_file(segs)
    list_path = dataset.manifest_to_list(manifest, str(tmp_path / "train.list"))
    rows = _read_list(list_path)
    assert len(rows) == 2
    for row in rows:
        # Exactly 4 fields — no stray pipe broke the row.
        assert len(row) == 4
        assert "|" not in row[3]


def test_empty_transcript_segments_are_skipped(manifest_file, tmp_path):
    segs = [
        {"id": "a", "audio_file": "/x/a.wav", "text": "", "duration_secs": 4.0},
        {"id": "b", "audio_file": "/x/b.wav", "text": "   ", "duration_secs": 4.0},
        {"id": "c", "audio_file": "/x/c.wav", "text": "kept", "duration_secs": 4.0},
    ]
    manifest = manifest_file(segs)
    list_path = dataset.manifest_to_list(manifest, str(tmp_path / "train.list"))
    rows = _read_list(list_path)
    assert len(rows) == 1
    assert rows[0][3] == "kept"


def test_speaker_name_fixed(manifest_file, tmp_path):
    manifest = manifest_file(_segments(2))
    list_path = dataset.manifest_to_list(manifest, str(tmp_path / "train.list"))
    rows = _read_list(list_path)
    for row in rows:
        assert row[1] == dataset.SPEAKER_NAME


def test_empty_segments_raises(manifest_file, tmp_path):
    manifest = manifest_file([])
    with pytest.raises(ValueError):
        dataset.manifest_to_list(manifest, str(tmp_path / "train.list"))


def test_missing_segments_key_raises(tmp_path):
    path = str(tmp_path / "bad.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"version": "1"}')
    with pytest.raises(ValueError):
        dataset.load_manifest(path)


def test_all_transcripts_empty_yields_empty_list_file(manifest_file, tmp_path):
    segs = [{"id": "a", "audio_file": "/x/a.wav", "text": "", "duration_secs": 4.0}]
    manifest = manifest_file(segs)
    list_path = dataset.manifest_to_list(manifest, str(tmp_path / "train.list"))
    assert _read_list(list_path) == []


# ---------------------------------------------------------------------------
# select_reference_plan
# ---------------------------------------------------------------------------


def _cand(id, text, measured):
    # measured_secs is the real decoded WAV duration attached by packaging —
    # manifest duration_secs deliberately diverges here to prove it is ignored.
    return {"id": id, "text": text, "duration_secs": 999.0, "measured_secs": measured}


class TestSelectReferencePlan:
    def test_picks_in_band_segment_without_trim(self):
        plan = dataset.select_reference_plan([
            _cand("a", "too short", 1.0),
            _cand("b", "just right", 6.0),
            _cand("c", "too long", 15.0),
        ])
        assert plan["segment"]["id"] == "b"
        assert plan["trim_to_secs"] is None

    def test_tie_break_prefers_longest_transcript(self):
        plan = dataset.select_reference_plan([
            _cand("a", "short", 5.0),
            _cand("b", "a much longer transcript here", 5.0),
        ])
        assert plan["segment"]["id"] == "b"

    def test_tie_break_falls_back_to_id(self):
        plan = dataset.select_reference_plan([
            _cand("z", "same length", 5.0),
            _cand("a", "same length", 5.0),
        ])
        assert plan["segment"]["id"] == "a"

    def test_margin_excludes_boundary_durations(self):
        """Exactly 3.0 s sits on upstream's < 48000-sample boundary after the
        16 kHz resample — the safety margin must keep it out of the band."""
        with pytest.raises(ValueError):
            dataset.select_reference_plan([_cand("a", "boundary", 3.0)])
        plan = dataset.select_reference_plan([_cand("a", "safely in", 3.2)])
        assert plan["trim_to_secs"] is None

    def test_over_band_only_returns_trim_plan(self):
        plan = dataset.select_reference_plan([
            _cand("a", "far over", 20.0),
            _cand("b", "closest over", 12.0),
        ])
        # Shortest over-band candidate loses the least transcript alignment
        # when trimmed; trim target must land strictly inside the band.
        assert plan["segment"]["id"] == "b"
        assert plan["trim_to_secs"] == dataset._REF_TRIM_SECS
        assert 3.0 < plan["trim_to_secs"] < 10.0

    def test_under_band_only_raises_actionable_error(self):
        with pytest.raises(ValueError, match="3 and 10 seconds"):
            dataset.select_reference_plan([
                _cand("a", "too short", 1.0),
                _cand("b", "also too short", 2.9),
            ])

    def test_empty_transcript_segments_excluded_from_consideration(self):
        plan = dataset.select_reference_plan([
            _cand("a", "", 6.0),
            _cand("b", "   ", 6.0),
            _cand("c", "the only real candidate", 6.0),
        ])
        assert plan["segment"]["id"] == "c"

    def test_raises_when_no_segment_has_a_transcript(self):
        with pytest.raises(ValueError):
            dataset.select_reference_plan([_cand("a", "", 6.0), _cand("b", "   ", 4.0)])

    def test_raises_on_empty_candidate_list(self):
        with pytest.raises(ValueError):
            dataset.select_reference_plan([])
