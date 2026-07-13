-- Migration 011: expose per-stage pipeline tuning as project config.
--
-- Promotes knobs the orchestrator previously hardcoded (or that only lived on a
-- service's job API) to persistent project settings. Every column defaults to
-- the value that was hardcoded before this migration, so existing projects
-- behave identically until a user changes something. (target_lufs already
-- existed from migration 001; it simply had no create/patch path and is wired
-- up in the router, not here.)
--
-- Changing a stage's config does not retro-apply: reprocess the source (or
-- retrain) to adopt it.

-- Vocal separation (Demucs)
ALTER TABLE projects ADD COLUMN demucs_model TEXT NOT NULL DEFAULT 'htdemucs';
ALTER TABLE projects ADD COLUMN demucs_shifts INTEGER NOT NULL DEFAULT 0;

-- Diarisation (pyannote)
ALTER TABLE projects ADD COLUMN diar_min_speakers INTEGER NOT NULL DEFAULT 1;
ALTER TABLE projects ADD COLUMN diar_max_speakers INTEGER NOT NULL DEFAULT 10;
ALTER TABLE projects ADD COLUMN diar_min_segment_duration REAL NOT NULL DEFAULT 1.0;

-- Transcription (faster-whisper)
ALTER TABLE projects ADD COLUMN whisper_beam_size INTEGER NOT NULL DEFAULT 5;
ALTER TABLE projects ADD COLUMN whisper_vad_filter INTEGER NOT NULL DEFAULT 0;

-- Cleanup (FFmpeg) — target_lufs already exists (migration 001)
ALTER TABLE projects ADD COLUMN highpass_hz INTEGER NOT NULL DEFAULT 80;
ALTER TABLE projects ADD COLUMN silence_threshold_db REAL NOT NULL DEFAULT -50.0;
ALTER TABLE projects ADD COLUMN silence_min_duration_secs REAL NOT NULL DEFAULT 0.1;

-- XTTS fine-tune hyperparameters
ALTER TABLE projects ADD COLUMN xtts_epochs INTEGER NOT NULL DEFAULT 10;
ALTER TABLE projects ADD COLUMN xtts_batch_size INTEGER NOT NULL DEFAULT 3;
ALTER TABLE projects ADD COLUMN xtts_grad_accum INTEGER NOT NULL DEFAULT 1;
ALTER TABLE projects ADD COLUMN xtts_learning_rate REAL NOT NULL DEFAULT 5e-06;
