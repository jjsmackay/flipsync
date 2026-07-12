-- Migration 010: dataset cleanup cache (v1.5).
-- Dataset builds write cleaned WAVs to cleaned/{id}.wav, tracked here —
-- fully decoupled from export/ and export_path, so a staged re-export can
-- no longer delete dataset audio, and dataset builds no longer touch the
-- live export set or segment review statuses. NULL = no cleaned dataset
-- audio exists for the segment yet.
ALTER TABLE segments ADD COLUMN cleaned_path TEXT;
