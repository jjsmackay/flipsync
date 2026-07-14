-- Migration 013: transcript of the reference clip.
-- The reference clip is transcribed (whole-clip, via the transcription service)
-- so the text can be surfaced read-only in the UI and fed to engines that
-- require it (e.g. GPT-SoVITS needs a reference.txt transcript of reference.wav).
-- NULL = not yet transcribed / no reference. Cleared whenever the reference is
-- replaced, then repopulated by a reference_transcribe job.
ALTER TABLE projects ADD COLUMN reference_transcript TEXT;
