-- Migration 005: rename positional step1/step2 identifiers to semantic names.
-- New databases also pass through here: 001 creates the old column names,
-- this migration renames them, so both old and new DBs converge.

ALTER TABLE sources RENAME COLUMN step1_model TO separation_model;
ALTER TABLE sources RENAME COLUMN step1_error TO separation_error;
ALTER TABLE sources RENAME COLUMN step2_error TO diarisation_error;

UPDATE sources SET status = CASE status
    WHEN 'step1_pending' THEN 'separation_pending'
    WHEN 'step1_running' THEN 'separation_running'
    WHEN 'step1_failed'  THEN 'separation_failed'
    WHEN 'step2_pending' THEN 'diarisation_pending'
    WHEN 'step2_running' THEN 'diarisation_running'
    WHEN 'step2_failed'  THEN 'diarisation_failed'
    ELSE status
END;
