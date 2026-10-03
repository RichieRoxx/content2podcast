"""Plain-SQL repository functions for sources, articles and episodes (no ORM).

Every write function commits on success. Timestamps are UTC ISO-8601 strings.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from content2podcast.db import utcnow
from content2podcast.sources.normalize import normalize_url

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


def reset_baseline(conn: sqlite3.Connection, source_id: int) -> None:
    """Forget the source's baseline (and cache validators) so its next check baselines again;
    pending articles of the source become ``baseline`` so they are never turned into episodes."""
    with conn:
        conn.execute(
            "UPDATE sources SET baseline_at = NULL, etag = NULL, last_modified = NULL WHERE id = ?",
            (source_id,),
        )
        conn.execute(
            "UPDATE articles SET status = 'baseline' WHERE source_id = ? AND status = 'pending'",
            (source_id,),
        )


def article_counts(conn: sqlite3.Connection) -> dict[int, dict[str, int]]:
    """Article counts by status per source id."""
    counts: dict[int, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT source_id, status, COUNT(*) AS n FROM articles GROUP BY source_id, status"
    ):
        counts.setdefault(row["source_id"], {})[row["status"]] = row["n"]
    return counts


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


def record_article_failure(
    conn: sqlite3.Connection, article_id: int, error: str, *, max_attempts: int
) -> str:
    """Count a failed processing attempt (any stage); ``failed`` once ``max_attempts`` is
    reached. Returns the resulting status."""
    return record_extraction_failure(conn, article_id, error, max_attempts=max_attempts)


def requeue_articles(
    conn: sqlite3.Connection, statuses: Sequence[str] = ("failed", "skipped")
) -> int:
    """Put articles with the given statuses back to ``pending`` (attempts and error reset).
    Returns how many were requeued."""
    marks = ", ".join("?" for _ in statuses)
    with conn:
        cur = conn.execute(
            "UPDATE articles SET status = 'pending', attempts = 0, last_error = NULL "
            f"WHERE status IN ({marks})",
            tuple(statuses),
        )
    return cur.rowcount


def list_pending_articles(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Pending articles, oldest first (by publication, else discovery time), with their source
    name as ``source_name``."""
    sql = (
        "SELECT a.*, s.name AS source_name FROM articles a "
        "JOIN sources s ON s.id = a.source_id WHERE a.status = 'pending' "
        "ORDER BY COALESCE(a.published_at, a.discovered_at), a.id"
    )
    if limit is not None:
        return conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def set_article_content(conn: sqlite3.Connection, article_id: int, content: str) -> None:
    """Store the article text; a successful extraction clears an earlier error."""
    with conn:
        conn.execute(
            "UPDATE articles SET content = ?, extracted_at = ?, last_error = NULL WHERE id = ?",
            (content, utcnow(), article_id),
        )


def record_extraction_failure(
    conn: sqlite3.Connection, article_id: int, error: str, *, max_attempts: int
) -> str:
    """Count a failed attempt and store the error. Once ``attempts`` reaches ``max_attempts``
    the article becomes ``failed``. Returns the resulting status."""
    with conn:
        conn.execute(
            "UPDATE articles SET attempts = attempts + 1, last_error = ?, "
            "status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE status END WHERE id = ?",
            (error, max_attempts, article_id),
        )
    return conn.execute("SELECT status FROM articles WHERE id = ?", (article_id,)).fetchone()[
        "status"
    ]


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


def record_published_episode(
    conn: sqlite3.Connection,
    *,
    guid: str,
    mode: str,
    title: str,
    summary: str | None,
    script: Any,
    audio_file: str,
    audio_bytes: int,
    duration_s: float,
    articles: Iterable[tuple[int, str]],
) -> int:
    """Create a ``published`` episode with the next number, link its articles and mark the
    ``discussed`` ones ``processed`` -- all in one transaction. Returns the episode id."""
    articles = list(articles)
    now = utcnow()
    with conn:
        number = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM episodes").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO episodes (guid, mode, number, title, summary, script_json, status, "
            "audio_file, audio_bytes, duration_s, created_at, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'published', ?, ?, ?, ?, ?)",
            (
                guid,
                mode,
                number,
                title,
                summary,
                None if script is None else json.dumps(script, ensure_ascii=False),
                audio_file,
                audio_bytes,
                duration_s,
                now,
                now,
            ),
        )
        episode_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO episode_articles (episode_id, article_id, role) VALUES (?, ?, ?)",
            [(episode_id, article_id, role) for article_id, role in articles],
        )
        conn.executemany(
            "UPDATE articles SET status = 'processed', last_error = NULL WHERE id = ?",
            [(article_id,) for article_id, role in articles if role == "discussed"],
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


def list_episodes_with_counts(
    conn: sqlite3.Connection, status: str | None = None
) -> list[sqlite3.Row]:
    """Episodes newest first (publish date, or creation date for drafts) with the number of
    linked articles as ``article_count``."""
    where, args = ("WHERE e.status = ?", (status,)) if status else ("", ())
    return conn.execute(
        "SELECT e.*, COUNT(ea.article_id) AS article_count FROM episodes e "
        f"LEFT JOIN episode_articles ea ON ea.episode_id = e.id {where} "
        "GROUP BY e.id ORDER BY COALESCE(e.published_at, e.created_at) DESC, e.id DESC",
        args,
    ).fetchall()


def episode_articles(conn: sqlite3.Connection, episode_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT article_id, role FROM episode_articles WHERE episode_id = ? ORDER BY article_id",
        (episode_id,),
    ).fetchall()


def episode_sources(conn: sqlite3.Connection, episode_id: int) -> list[sqlite3.Row]:
    """The articles behind an episode with title, url, source name and role
    (``discussed`` first), for show notes."""
    return conn.execute(
        "SELECT a.title, a.url, s.name AS source_name, ea.role FROM episode_articles ea "
        "JOIN articles a ON a.id = ea.article_id JOIN sources s ON s.id = a.source_id "
        "WHERE ea.episode_id = ? ORDER BY ea.role = 'mentioned', a.id",
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
