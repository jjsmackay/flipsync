-- Migration 014: multi-engine voice models (GPT-SoVITS alongside XTTS-v2).
-- Adds the fine-tune engine used for a model. Existing rows default to
-- 'xtts' (the only engine before this migration).
ALTER TABLE models ADD COLUMN engine TEXT NOT NULL DEFAULT 'xtts';
-- Values: 'xtts' | 'gpt_sovits'. Existing rows default to 'xtts'.
