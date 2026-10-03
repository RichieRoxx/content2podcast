"""Plain-SQL repository functions for sources, articles and episodes (no ORM).

Every write function commits on success. Timestamps are UTC ISO-8601 strings.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from content2podcast.db import utcnow
from content2podcast.urlnorm import normalize_url

ARTICLE_STATUSES = ("baseline", "pending", "failed", "skipped", "processed")


# --- sources -----------------------------------------------------------------------------


def upsert_source(conn: sqlite3.Connection, name: str, url: str, type: str) -> int:
    """Insert the source or update its url/type; returns its id."""
    with conn:
        conn.execute(
            "INSERT INTO sources (name, url, type) VALUES (?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET url = excluded.url, type = excluded.type",
            (name, url, type),
        )
    return conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()["id"]


def get_source(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE name = ?", (name,)).fetchone()


def list_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sources ORDER BY name").fetchall()


def mark_source_checked(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    error: str | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> None:
    """Record a fetch attempt. On success (no error) also set ``last_success_at`` and clear the
    error; cache validators are only replaced on success."""
    now = utcnow()
    with conn:
        if error is None:
            conn.execute(
                "UPDATE sources SET last_checked_at = ?, last_success_at = ?, last_error = NULL, "
                "etag = ?, last_modified = ? WHERE id = ?",
                (now, now, etag, last_modified, source_id),
            )
        else:
            conn.execute(
                "UPDATE sources SET last_checked_at = ?, last_error = ? WHERE id = ?",
                (now, error, source_id),
            )


def set_baseline(conn: sqlite3.Connection, source_id: int) -> None:
    """Mark the source as baselined now and all its still-pending articles as ``baseline``."""
    with conn:
        conn.execute("UPDATE sources SET baseline_at = ? WHERE id = ?", (utcnow(), source_id))
        conn.execute(
            "UPDATE articles SET status = 'baseline' WHERE source_id = ? AND status = 'pending'",
            (source_id,),
        )


# --- articles ----------------------------------------------------------------------------


def insert_article_if_new(
    conn: sqlite3.Connection,
    source_id: int,
    url: str,
    *,
    title: str | None = None,
    feed_summary: str | None = None,
    published_at: str | None = None,
    status: str = "pending",
) -> int | None:
    """Insert the article unless its normalized URL is known. Returns the id, or None if known."""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO articles "
            "(source_id, url, url_norm, title, feed_summary, published_at, discovered_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                url,
                normalize_url(url),
                title,
                feed_summary,
                published_at,
                utcnow(),
                status,
            ),
        )
    return cur.lastrowid if cur.rowcount else None


def get_article(conn: sqlite3.Connection, article_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()


def list_articles(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status is None:
        return conn.execute("SELECT * FROM articles ORDER BY id").fetchall()
    return conn.execute("SELECT * FROM articles WHERE status = ? ORDER BY id", (status,)).fetchall()


def set_article_content(conn: sqlite3.Connection, article_id: int, content: str) -> None:
    with conn:
        conn.execute(
            "UPDATE articles SET content = ?, extracted_at = ? WHERE id = ?",
            (content, utcnow(), article_id),
        )


def set_article_status(
    conn: sqlite3.Connection, article_id: int, status: str, *, error: str | None = None
) -> None:
    """Transition an article. ``failed`` increments ``attempts`` and stores the error; any other
    status clears the error."""
    if status not in ARTICLE_STATUSES:
        raise ValueError(f"Unknown article status: {status!r}")
    failed = status == "failed"
    with conn:
        conn.execute(
            "UPDATE articles SET status = ?, last_error = ?, attempts = attempts + ? WHERE id = ?",
            (status, error if failed else None, 1 if failed else 0, article_id),
        )


# --- episodes ----------------------------------------------------------------------------


def next_episode_number(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM episodes").fetchone()[0]


def create_episode(
    conn: sqlite3.Connection,
    *,
    guid: str,
    mode: str,
    title: str,
    summary: str | None = None,
    script: Any = None,
    number: int | None = None,
    articles: Iterable[tuple[int, str]] = (),
) -> int:
    """Create a ``draft`` episode, linking ``(article_id, role)`` pairs. ``script`` is stored as
    JSON. Returns the episode id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO episodes (guid, mode, number, title, summary, script_json, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)",
            (
                guid,
                mode,
                number,
                title,
                summary,
                None if script is None else json.dumps(script, ensure_ascii=False),
                utcnow(),
            ),
        )
        episode_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO episode_articles (episode_id, article_id, role) VALUES (?, ?, ?)",
            [(episode_id, article_id, role) for article_id, role in articles],
        )
    return episode_id


def get_episode(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()


def get_episode_by_guid(conn: sqlite3.Connection, guid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM episodes WHERE guid = ?", (guid,)).fetchone()


def list_episodes(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    """Episodes newest first (by publish date, then id)."""
    sql = "SELECT * FROM episodes"
    args: tuple[str, ...] = ()
    if status is not None:
        sql += " WHERE status = ?"
        args = (status,)
    return conn.execute(sql + " ORDER BY published_at DESC, id DESC", args).fetchall()


def episode_articles(conn: sqlite3.Connection, episode_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT article_id, role FROM episode_articles WHERE episode_id = ? ORDER BY article_id",
        (episode_id,),
    ).fetchall()


def publish_episode(
    conn: sqlite3.Connection,
    episode_id: int,
    *,
    audio_file: str,
    audio_bytes: int,
    duration_s: float,
) -> None:
    with conn:
        conn.execute(
            "UPDATE episodes SET status = 'published', audio_file = ?, audio_bytes = ?, "
            "duration_s = ?, published_at = ? WHERE id = ?",
            (audio_file, audio_bytes, duration_s, utcnow(), episode_id),
        )


def prune_episode(conn: sqlite3.Connection, episode_id: int) -> None:
    """Mark an episode pruned (its audio is gone) but keep the row and article links."""
    with conn:
        conn.execute("UPDATE episodes SET status = 'pruned' WHERE id = ?", (episode_id,))


def delete_episode(conn: sqlite3.Connection, episode_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
