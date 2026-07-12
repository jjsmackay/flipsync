-- Migration 009: XTTS-v2 fine-tuning support (v1.5).
-- Adds the models table (one row per fine-tuned XTTS-v2 model) and a
-- progress_detail column on jobs for rich long-running-job progress
-- (fine-tune epoch/loss/ETA). Both changes are additive.

CREATE TABLE IF NOT EXISTS models (
    id                    TEXT PRIMARY KEY,   -- UUID
    project_id            TEXT NOT NULL REFERENCES projects(id),
    status                TEXT NOT NULL,      -- pending | training | ready | failed | cancelled
    dataset_mode          TEXT NOT NULL,      -- approved | auto
    min_confidence        REAL,               -- auto mode only; NULL for approved
    segment_count         INTEGER,            -- set after dataset build
    dataset_duration_secs REAL,               -- set after dataset build
    dataset_manifest_path TEXT,               -- models/{id}/dataset.json
    checkpoint_dir        TEXT,               -- models/{id}/, set when ready
    params                TEXT,               -- JSON hyperparameters
    eval_loss             REAL,               -- final eval loss, set when ready
    error                 TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_models_project ON models(project_id);

ALTER TABLE jobs ADD COLUMN progress_detail TEXT;
