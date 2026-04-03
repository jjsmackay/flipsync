-- Migration 001: Initial schema

CREATE TABLE IF NOT EXISTS projects (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'new',
    reference_path       TEXT,
    whisper_model        TEXT NOT NULL DEFAULT 'large-v2',
    language             TEXT,
    match_threshold      REAL NOT NULL DEFAULT 0.75,
    target_lufs          REAL NOT NULL DEFAULT -23.0,
    target_duration_secs REAL NOT NULL DEFAULT 1800.0
);

CREATE TABLE IF NOT EXISTS sources (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id),
    filename       TEXT NOT NULL,
    file_path      TEXT NOT NULL,
    audio_path     TEXT,
    vocals_path    TEXT,
    duration_secs  REAL,
    status         TEXT NOT NULL DEFAULT 'uploaded',
    step1_model    TEXT,
    step1_error    TEXT,
    step2_error    TEXT,
    coverage_ratio REAL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects(id),
    source_id             TEXT NOT NULL REFERENCES sources(id),
    raw_path              TEXT NOT NULL,
    export_path           TEXT,
    start_secs            REAL NOT NULL,
    end_secs              REAL NOT NULL,
    duration_secs         REAL GENERATED ALWAYS AS (end_secs - start_secs) STORED,
    speaker_label         TEXT NOT NULL,
    match_confidence      REAL NOT NULL,
    transcript            TEXT,
    transcript_edited     TEXT,
    transcript_confidence REAL,
    status                TEXT NOT NULL DEFAULT 'pending',
    clipping_warning      INTEGER NOT NULL DEFAULT 0,
    flags                 TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id),
    source_id    TEXT,
    type         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',
    params       TEXT,
    error        TEXT,
    progress     INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_segments_project    ON segments(project_id);
CREATE INDEX IF NOT EXISTS idx_segments_source     ON segments(source_id);
CREATE INDEX IF NOT EXISTS idx_segments_status     ON segments(status);
CREATE INDEX IF NOT EXISTS idx_segments_confidence ON segments(match_confidence);
CREATE INDEX IF NOT EXISTS idx_jobs_project_status ON jobs(project_id, status);
CREATE INDEX IF NOT EXISTS idx_sources_project     ON sources(project_id);
