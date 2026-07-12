-- Migration 007: curatable scout candidates.
-- Scout now surfaces a bounded pool of individual turn slices per candidate so
-- the user can exclude a wrong-voice turn; the reference is assembled from the
-- kept turns (longest-first up to the reference cap). The single-montage column
-- is replaced by pool_json (a JSON list of {index,start,end,duration}); slice
-- paths are derived from scout_job_id + speaker_label + index.
--
-- Candidate rows are transient — re-running the scout repopulates them — so we
-- rebuild the table rather than migrate the montage schema forward.
DROP INDEX IF EXISTS idx_speaker_candidates_project;
DROP TABLE IF EXISTS speaker_candidates;

CREATE TABLE speaker_candidates (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id),
    scout_job_id   TEXT NOT NULL REFERENCES jobs(id),
    source_id      TEXT NOT NULL REFERENCES sources(id),
    speaker_label  TEXT NOT NULL,
    pool_json      TEXT NOT NULL,
    total_secs     REAL NOT NULL,
    segment_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_speaker_candidates_project ON speaker_candidates(project_id);
