-- Migration 003: Auto-approve configuration.
-- Segments clearing both confidence bars are moved pending -> auto_approved
-- when transcription results land; re-evaluated on PATCH /projects.
-- See spec/pipeline.md §Auto-approval.

ALTER TABLE projects ADD COLUMN auto_approve_enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE projects ADD COLUMN auto_approve_match_threshold REAL NOT NULL DEFAULT 0.85;
ALTER TABLE projects ADD COLUMN auto_approve_transcript_threshold REAL NOT NULL DEFAULT 0.90;
