-- Migration 015: head/tail silence padding for cleanup.
--
-- Cleanup trims leading/trailing silence tight (migration 011 knobs). A hard cut
-- at the word boundary gives the voice-clone model an abrupt onset/offset and can
-- click. These two knobs re-add a small, controlled amount of digital silence to
-- the head and tail AFTER trimming, so every exported/dataset segment has a clean
-- attack and decay. Defaults match the common voice-clone recommendation.
--
-- Applied in the cleanup service after the silent-after-trim reject check, so
-- padding never masks a genuinely empty segment. Plain per-project config: a
-- change applies to the next cleanup run (export re-cleans every run; dataset
-- build only re-cleans segments whose cleaned cache is empty).

ALTER TABLE projects ADD COLUMN silence_pad_start_secs REAL NOT NULL DEFAULT 0.05;
ALTER TABLE projects ADD COLUMN silence_pad_end_secs REAL NOT NULL DEFAULT 0.2;

-- Whether cleanup trims leading/trailing silence at all. Off keeps the
-- diariser's boundaries intact (useful when trimming eats speech onsets).
ALTER TABLE projects ADD COLUMN do_trim_silence INTEGER NOT NULL DEFAULT 1;
