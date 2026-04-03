"""Database management for the FlipSync orchestrator.

One SQLite database per project at {data_dir}/projects/{project_id}/project.db.
Connections use WAL mode for concurrent reads during writes.
"""

import os
import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# One connection per project, kept open for process lifetime.
_connections: dict[str, sqlite3.Connection] = {}


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def project_dir(project_id: str) -> Path:
    return _data_dir() / "projects" / project_id


def db_path(project_id: str) -> Path:
    return project_dir(project_id) / "project.db"


def get_conn(project_id: str) -> sqlite3.Connection:
    """Return (and cache) a SQLite connection for the given project.

    Raises sqlite3.OperationalError if the database file does not exist.
    """
    if project_id not in _connections:
        path = db_path(project_id)
        if not path.exists():
            raise sqlite3.OperationalError(f"Database not found for project {project_id!r}")
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _connections[project_id] = conn
    return _connections[project_id]


def project_exists(project_id: str) -> bool:
    """Return True if the project DB file exists."""
    return db_path(project_id).exists()


def close_conn(project_id: str) -> None:
    conn = _connections.pop(project_id, None)
    if conn:
        conn.close()


def create_project_db(project_id: str) -> None:
    """Create the project directory and run all migrations."""
    pdir = project_dir(project_id)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "source").mkdir(exist_ok=True)
    (pdir / "audio" / "raw").mkdir(parents=True, exist_ok=True)
    (pdir / "audio" / "vocals").mkdir(parents=True, exist_ok=True)
    (pdir / "segments" / "raw").mkdir(parents=True, exist_ok=True)
    (pdir / "export").mkdir(exist_ok=True)

    # Create the DB file (sqlite3.connect creates it on first open).
    # Bypass get_conn's existence check since the file doesn't exist yet.
    path = db_path(project_id)
    if project_id not in _connections:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _connections[project_id] = conn

    conn = _connections[project_id]
    _run_migrations(conn)


def _run_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations (filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()

    applied = {row[0] for row in conn.execute("SELECT filename FROM _migrations")}
    migration_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))

    for mf in migration_files:
        if mf.name in applied:
            continue
        sql = mf.read_text()
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO _migrations (filename, applied_at) VALUES (?, datetime('now'))",
            (mf.name,),
        )
        conn.commit()


def list_project_ids() -> list[str]:
    """Enumerate project IDs by scanning the projects directory."""
    projects_dir = _data_dir() / "projects"
    if not projects_dir.exists():
        return []
    return [
        d.name
        for d in projects_dir.iterdir()
        if d.is_dir() and (d / "project.db").exists()
    ]
