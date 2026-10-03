import sqlite3

import pytest

from content2podcast import repository as repo
from content2podcast.db import connect, db_path, latest_version, migrate, schema_version, utcnow


@pytest.fixture
def conn(tmp_path):
    c = connect(db_path(tmp_path / "data"))
    yield c
    c.close()


@pytest.fixture
def source_id(conn):
    return repo.upsert_source(conn, "Blog", "https://example.com/feed", "rss")


def test_db_path_derived_from_data_dir(tmp_path):
    assert db_path(tmp_path) == tmp_path / "content2podcast.db"


def test_fresh_db_migrates_to_latest_and_sets_pragmas(conn):
    assert schema_version(conn) == latest_version() >= 1
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sources", "articles", "episodes", "episode_articles"} <= tables


def test_rerunning_migrations_is_a_noop(conn, source_id):
    version = schema_version(conn)
    assert migrate(conn) == version
    assert repo.get_source(conn, "Blog")["id"] == source_id  # data untouched


def test_newer_schema_is_refused(conn):
    conn.execute(f"PRAGMA user_version = {latest_version() + 1}")
    with pytest.raises(RuntimeError, match="newer"):
        migrate(conn)


def test_url_norm_is_unique(conn, source_id):
    first = repo.insert_article_if_new(conn, source_id, "https://Example.com/a/?utm_source=x#top")
    again = repo.insert_article_if_new(conn, source_id, "https://example.com/a")
    assert first is not None and again is None
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(
            "INSERT INTO articles (source_id, url, url_norm, discovered_at, status) "
            "VALUES (?, 'u', ?, 'now', 'pending')",
            (source_id, repo.get_article(conn, first)["url_norm"]),
        )


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO sources (name, url, type) VALUES ('x', 'u', 'ftp')",
        "INSERT INTO articles (source_id, url, url_norm, discovered_at, status) "
        "VALUES (1, 'u', 'n', 'now', 'bogus')",
        "INSERT INTO episodes (guid, mode, title, status, created_at) "
        "VALUES ('g', 'm', 't', 'bogus', 'now')",
        "INSERT INTO episode_articles (episode_id, article_id, role) VALUES (1, 1, 'bogus')",
    ],
)
def test_check_constraints(conn, source_id, sql):
    repo.insert_article_if_new(conn, source_id, "https://example.com/a")
    repo.create_episode(conn, guid="g0", mode="per_article", title="T")
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(sql)


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(
            "INSERT INTO articles (source_id, url, url_norm, discovered_at, status) "
            "VALUES (999, 'u', 'n', 'now', 'pending')"
        )


def test_timestamp_format():
    assert utcnow().endswith("Z") and len(utcnow()) == 20


def test_upsert_source_updates_in_place(conn):
    a = repo.upsert_source(conn, "S", "https://a", "rss")
    b = repo.upsert_source(conn, "S", "https://b", "html")
    assert a == b
    row = repo.get_source(conn, "S")
    assert (row["url"], row["type"]) == ("https://b", "html")
    assert [r["name"] for r in repo.list_sources(conn)] == ["S"]


def test_mark_source_checked(conn, source_id):
    repo.mark_source_checked(conn, source_id, error="timeout")
    row = repo.get_source(conn, "Blog")
    assert row["last_error"] == "timeout" and row["last_success_at"] is None
    repo.mark_source_checked(conn, source_id, etag='"abc"', last_modified="Mon")
    row = repo.get_source(conn, "Blog")
    assert row["last_error"] is None and row["last_success_at"] and row["etag"] == '"abc"'
    repo.mark_source_checked(conn, source_id, error="boom")
    assert repo.get_source(conn, "Blog")["etag"] == '"abc"'  # validators kept on failure


def test_article_status_transitions(conn, source_id):
    aid = repo.insert_article_if_new(conn, source_id, "https://example.com/a", title="A")
    assert repo.get_article(conn, aid)["status"] == "pending"
    repo.set_article_status(conn, aid, "failed", error="404")
    row = repo.get_article(conn, aid)
    assert (row["status"], row["attempts"], row["last_error"]) == ("failed", 1, "404")
    repo.set_article_status(conn, aid, "failed", error="404 again")
    assert repo.get_article(conn, aid)["attempts"] == 2
    repo.set_article_content(conn, aid, "text")
    repo.set_article_status(conn, aid, "processed")
    row = repo.get_article(conn, aid)
    assert (row["status"], row["last_error"], row["content"]) == ("processed", None, "text")
    assert row["extracted_at"]
    assert [r["id"] for r in repo.list_articles(conn, "processed")] == [aid]
    with pytest.raises(ValueError):
        repo.set_article_status(conn, aid, "nonsense")


def test_set_baseline_marks_pending_articles(conn, source_id):
    a = repo.insert_article_if_new(conn, source_id, "https://example.com/a")
    b = repo.insert_article_if_new(conn, source_id, "https://example.com/b")
    repo.set_article_status(conn, b, "processed")
    repo.set_baseline(conn, source_id)
    assert repo.get_article(conn, a)["status"] == "baseline"
    assert repo.get_article(conn, b)["status"] == "processed"
    assert repo.get_source(conn, "Blog")["baseline_at"]


def test_episode_crud(conn, source_id):
    a = repo.insert_article_if_new(conn, source_id, "https://example.com/a")
    b = repo.insert_article_if_new(conn, source_id, "https://example.com/b")
    assert repo.next_episode_number(conn) == 1
    eid = repo.create_episode(
        conn,
        guid="g1",
        mode="per_article",
        title="Folge 1",
        script={"turns": [{"speaker": "host", "text": "Hallo äöü"}]},
        number=1,
        articles=[(a, "discussed"), (b, "mentioned")],
    )
    ep = repo.get_episode(conn, eid)
    assert ep["status"] == "draft" and "äöü" in ep["script_json"]
    assert repo.get_episode_by_guid(conn, "g1")["id"] == eid
    assert [(r["article_id"], r["role"]) for r in repo.episode_articles(conn, eid)] == [
        (a, "discussed"),
        (b, "mentioned"),
    ]
    assert repo.next_episode_number(conn) == 2

    repo.publish_episode(conn, eid, audio_file="e1.mp3", audio_bytes=123, duration_s=61.5)
    ep = repo.get_episode(conn, eid)
    assert (ep["status"], ep["audio_file"], ep["duration_s"]) == ("published", "e1.mp3", 61.5)
    assert ep["published_at"]
    assert [e["id"] for e in repo.list_episodes(conn, "published")] == [eid]

    repo.prune_episode(conn, eid)
    assert repo.get_episode(conn, eid)["status"] == "pruned"
    assert repo.list_episodes(conn, "published") == []
    assert len(repo.episode_articles(conn, eid)) == 2

    repo.delete_episode(conn, eid)
    assert repo.get_episode(conn, eid) is None
    assert repo.episode_articles(conn, eid) == []  # cascade


def test_duplicate_episode_guid_rejected(conn):
    repo.create_episode(conn, guid="g", mode="per_article", title="T")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_episode(conn, guid="g", mode="per_article", title="T2")


def test_failing_migration_is_rolled_back(tmp_path, monkeypatch):
    from content2podcast import db

    good = "CREATE TABLE a (x INTEGER);"
    bad = "CREATE TABLE b (x INTEGER); INSERT INTO nope VALUES (1);"
    monkeypatch.setattr(db, "_load_migrations", lambda: [(1, good), (2, bad)])
    c = sqlite3.connect(tmp_path / "m.db")
    with pytest.raises(sqlite3.Error):
        migrate(c)
    assert schema_version(c) == 1  # the first migration stayed, the second left nothing behind
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"a"}


def test_migration_files_must_not_have_gaps(monkeypatch):
    from importlib import resources

    from content2podcast import db

    class Entry:
        def __init__(self, name):
            self.name = name

        def read_text(self, encoding):
            return ""

    class Dir:
        def iterdir(self):
            return [Entry("0001_a.sql"), Entry("0003_c.sql"), Entry("notes.txt")]

    class Pkg:
        def joinpath(self, name):
            return Dir()

    monkeypatch.setattr(resources, "files", lambda package: Pkg())
    with pytest.raises(RuntimeError, match="without gaps"):
        db._load_migrations()
