-- Migration 008: expose whisper transcription tuning as project config.
-- whisper_batch_size bounds how many segments transcribe concurrently on the
-- GPU (the direct OOM-recovery lever). whisper_compute_type lets a constrained
-- GPU drop to a lighter precision (e.g. int8_float16) to cut VRAM; 'default'
-- keeps the service's own device-derived choice (float16 on GPU, int8 on CPU).
ALTER TABLE projects ADD COLUMN whisper_batch_size INTEGER NOT NULL DEFAULT 16;
ALTER TABLE projects ADD COLUMN whisper_compute_type TEXT NOT NULL DEFAULT 'default';
