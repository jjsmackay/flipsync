-- Migration 003: reference-from-video (diarise + pick)
-- reference_origin: JSON provenance of the current reference; NULL if none set.
ALTER TABLE projects ADD COLUMN reference_origin TEXT;

-- Transient store for the most recent scout result of a project. Rows are
-- replaced whenever a new scout completes; kept after a reference is picked
-- so the user can re-pick without re-scouting.
CREATE TABLE IF NOT EXISTS speaker_candidates (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id),
    scout_job_id   TEXT NOT NULL REFERENCES jobs(id),
    source_id      TEXT NOT NULL REFERENCES sources(id),
    speaker_label  TEXT NOT NULL,
    montage_path   TEXT NOT NULL,
    total_secs     REAL NOT NULL,
    segment_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_speaker_candidates_project ON speaker_candidates(project_id);
