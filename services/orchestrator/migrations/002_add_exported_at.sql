-- Migration 002: Track when a project's export archive was produced.
-- Used to detect a stale/invalidated export: cleared whenever approvals or
-- sources change so project status no longer sticks at 'exported'.

ALTER TABLE projects ADD COLUMN exported_at TEXT;
