-- Migration 006: per-segment speaker match confidence.
-- Diarisation returns a secondary cluster-level similarity score per segment
-- (spec/pipeline.md — reviewers see it alongside match_confidence). Nullable:
-- segments written before this migration, or by service versions that do not
-- emit the score, simply have no value.
ALTER TABLE segments ADD COLUMN speaker_match_confidence REAL;
