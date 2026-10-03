"""SQLite connection handling and migrations (ordered SQL scripts, tracked via user_version)."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

DB_FILENAME = "content2podcast.db"
BUSY_TIMEOUT_MS = 5000


def db_path(data_dir: Path | str) -> Path:
    """Database location, derived from ``paths.data_dir``."""
    return Path(data_dir) / DB_FILENAME


def utcnow() -> str:
    """Current time as UTC ISO-8601 (``2026-01-31T05:30:00Z``)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database (creating parent directories) and apply pending migrations."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    migrate(conn)
    return conn


def _load_migrations() -> list[tuple[int, str]]:
    """Return ``(version, sql)`` sorted by version; files are named ``NNNN_description.sql``."""
    found: list[tuple[int, str]] = []
    for entry in resources.files("content2podcast").joinpath("migrations").iterdir():
        match = re.fullmatch(r"(\d+)_.+\.sql", entry.name)
        if match:
            found.append((int(match.group(1)), entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"Migrations must be numbered 1..N without gaps, found {versions}")
    return found


def latest_version() -> int:
    return len(_load_migrations())


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply all pending migrations, each in its own transaction. Returns the new version."""
    current = schema_version(conn)
    migrations = _load_migrations()
    if current > len(migrations):
        raise RuntimeError(
            f"Database schema v{current} is newer than this program (v{len(migrations)})"
        )
    for version, sql in migrations[current:]:
        try:
            # executescript commits any open transaction first; user_version is transactional
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            raise
    return schema_version(conn)
