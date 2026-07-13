-- Migration 012: optional wav2vec2 forced-alignment pass for transcription.
-- When enabled, the transcription service refines whisper word timestamps via
-- forced alignment before sentence-aligned re-segmentation. Off by default;
-- only affects segments being re-segmented. Changing it does not retro-apply —
-- re-transcribe to adopt.
ALTER TABLE projects ADD COLUMN align_words INTEGER NOT NULL DEFAULT 0;
