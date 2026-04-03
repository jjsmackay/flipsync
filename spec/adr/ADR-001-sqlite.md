# ADR-001: SQLite for state persistence

**Status:** Accepted  
**Date:** 2026-04-03  
**Deciders:** Project lead

---

## Context

FlipSync needs to persist per-project state: source file processing status, segment metadata, review decisions, transcripts, job queue. The options are a filesystem layout (JSON files and directories), a lightweight embedded database, or a separate database service.

The review UI needs queries that are awkward on a filesystem: "all pending segments with confidence above X", "total approved duration", "count by status". These are trivial SQL queries.

The deployment target is a self-hosted single-machine setup. There is no expectation of concurrent users, horizontal scaling, or network-accessible database access.

---

## Decision

**SQLite.** One database file per project at `projects/{project_id}/project.db`.

---

## Reasons

**It fits the deployment model exactly.** A single-user self-hosted app has no need for a client-server database. SQLite is an embedded library — no service to start, no port to configure, no connection pool to manage.

**It survives Docker restarts.** The database file lives in the bind-mounted `./data/` directory on the host. `docker compose down` does not touch it.

**It supports the queries we need.** Filter by status, sort by confidence, aggregate duration, bulk status updates — all straightforward SQL with appropriate indexes.

**It is trivially backed up.** Copy the file. That's the backup.

**It is inspectable.** Any SQLite browser (DB Browser for SQLite, the sqlite3 CLI) can open the file and inspect state during development or debugging. A filesystem layout of JSON files is also inspectable but harder to query.

**The scale is appropriate.** A full TV season produces at most a few thousand segment rows. SQLite handles millions of rows without issue. There is no performance concern at this scale.

---

## Consequences

**`manifest.json` is a derived output, not the source of truth.** It is written at export time from the database. Any code that reads segment state reads the database, not a JSON file on disk.

**Migrations are required for schema changes.** Sequential numbered migration files in `services/orchestrator/migrations/`. The orchestrator applies pending migrations on startup. Additive changes (new columns with defaults) are straightforward. Destructive changes require a table copy.

**Concurrent writes from multiple orchestrator instances would cause problems.** This is not a concern for v1 (single-instance self-hosted), but a hosted multi-tenant version would require a different database. This is a known constraint, not a defect.

---

## Alternatives considered

**Filesystem (JSON + directory layout)**  
Simpler to inspect and requires no migration tooling. Adequate for linear read/write access. Falls short when the review UI needs filtered queries and aggregations — implementing these in application code is more complex than SQL and harder to optimise. Rejected.

**PostgreSQL**  
More capable and horizontally scalable. Requires a separate Docker service, connection configuration, and more operational overhead. Brings no benefit at this scale. Would be the right choice for a hosted multi-tenant version. Rejected for v1.

**Redis**  
Appropriate for a job queue, not for persistent relational state. Not considered for primary storage.
