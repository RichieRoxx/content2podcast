import logging
from datetime import UTC, datetime, timedelta

import feedparser
import pytest

from content2podcast import repository as repo
from content2podcast.config import AppConfig, RetentionConfig
from content2podcast.db import connect
from content2podcast.feed import write_feed
from content2podcast.layout import episode_work_dir
from content2podcast.retention import apply_retention

NOW = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "data" / "db.sqlite3")
    yield c
    c.close()


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "public", tmp_path / "data"


@pytest.fixture
def source_id(conn):
    return repo.upsert_source(conn, "Blog", "https://blog.example.com/feed", "rss")


def publish(conn, source_id, output_dir, name, age_days, *, write_file=True, audio_file=None):
    """A published episode whose MP3 exists in the output directory."""
    article = repo.insert_article_if_new(
        conn, source_id, f"https://blog.example.com/{name}", title=name
    )
    repo.set_article_status(conn, article, "processed")
    episode_id = repo.create_episode(
        conn, guid=f"guid-{name}", mode="per_article", title=name, articles=[(article, "discussed")]
    )
    relpath = audio_file or f"episodes/{name}.mp3"
    repo.publish_episode(conn, episode_id, audio_file=relpath, audio_bytes=3, duration_s=1.0)
    published = (NOW - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE episodes SET published_at = ? WHERE id = ?", (published, episode_id))
    conn.commit()
    if write_file:
        path = output_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp3")
    return episode_id


def status_of(conn, name):
    return repo.get_episode_by_guid(conn, f"guid-{name}")["status"]


def run(conn, dirs, **retention):
    output_dir, data_dir = dirs
    return apply_retention(conn, output_dir, data_dir, RetentionConfig(**retention), now=NOW)


# --- age ---------------------------------------------------------------------------------


def test_age_based_pruning(conn, source_id, dirs):
    output_dir, _ = dirs
    for name, age in (("fresh", 5), ("recent", 20), ("old", 40), ("ancient", 90)):
        publish(conn, source_id, output_dir, name, age)
    report = run(conn, dirs, max_age_days=30)
    assert sorted(p.title for p in report.pruned) == ["ancient", "old"]
    assert all(p.reason == "age" and p.file_deleted for p in report.pruned)
    assert [status_of(conn, n) for n in ("fresh", "recent", "old", "ancient")] == [
        "published",
        "published",
        "pruned",
        "pruned",
    ]
    assert (output_dir / "episodes" / "fresh.mp3").exists()
    assert (output_dir / "episodes" / "recent.mp3").exists()
    assert not (output_dir / "episodes" / "old.mp3").exists()
    assert not (output_dir / "episodes" / "ancient.mp3").exists()


def test_age_boundary_is_strictly_older(conn, source_id, dirs):
    output_dir, _ = dirs
    publish(conn, source_id, output_dir, "exactly", 30)
    publish(conn, source_id, output_dir, "older", 30.01)
    run(conn, dirs, max_age_days=30)
    assert status_of(conn, "exactly") == "published" and status_of(conn, "older") == "pruned"


def test_default_age_is_30_days(conn, source_id, dirs):
    output_dir, data_dir = dirs
    publish(conn, source_id, output_dir, "old", 31)
    publish(conn, source_id, output_dir, "new", 29)
    apply_retention(conn, output_dir, data_dir, AppConfig().feed.retention, now=NOW)
    assert status_of(conn, "old") == "pruned" and status_of(conn, "new") == "published"


# --- count -------------------------------------------------------------------------------


def test_count_based_pruning_keeps_the_newest(conn, source_id, dirs):
    output_dir, _ = dirs
    for name, age in (("e1", 1), ("e2", 2), ("e3", 3), ("e4", 4), ("e5", 5)):
        publish(conn, source_id, output_dir, name, age)
    report = run(conn, dirs, max_age_days=None, max_episodes=3)
    assert sorted(p.title for p in report.pruned) == ["e4", "e5"]
    assert all(p.reason == "count" for p in report.pruned)
    assert [status_of(conn, n) for n in ("e1", "e2", "e3", "e4", "e5")] == [
        "published",
        "published",
        "published",
        "pruned",
        "pruned",
    ]


def test_max_episodes_larger_than_the_number_of_episodes(conn, source_id, dirs):
    output_dir, _ = dirs
    publish(conn, source_id, output_dir, "only", 1)
    assert run(conn, dirs, max_age_days=None, max_episodes=10).pruned == []


def test_nothing_is_pruned_when_both_limits_are_off(conn, source_id, dirs):
    output_dir, _ = dirs
    publish(conn, source_id, output_dir, "ancient", 1000)
    assert run(conn, dirs, max_age_days=None, max_episodes=None).pruned == []
    assert status_of(conn, "ancient") == "published"


# --- combined ----------------------------------------------------------------------------


def test_age_and_count_combine(conn, source_id, dirs):
    output_dir, _ = dirs
    for name, age in (("e1", 1), ("e2", 2), ("e3", 3), ("e4", 10), ("e5", 50)):
        publish(conn, source_id, output_dir, name, age)
    report = run(conn, dirs, max_age_days=30, max_episodes=3)
    reasons = {p.title: p.reason for p in report.pruned}
    assert reasons == {"e4": "count", "e5": "age+count"}
    assert [status_of(conn, n) for n in ("e1", "e2", "e3")] == ["published"] * 3

    # with a looser count the age limit alone decides
    publish(conn, source_id, output_dir, "e6", 40)
    report = run(conn, dirs, max_age_days=30, max_episodes=10)
    assert {p.title: p.reason for p in report.pruned} == {"e6": "age"}


def test_rerun_is_a_noop(conn, source_id, dirs):
    output_dir, _ = dirs
    publish(conn, source_id, output_dir, "old", 90)
    assert len(run(conn, dirs, max_age_days=30).pruned) == 1
    again = run(conn, dirs, max_age_days=30)
    assert again.pruned == [] and again.work_dirs_removed == 0


# --- files and articles ------------------------------------------------------------------


def test_missing_mp3_is_tolerated(conn, source_id, dirs):
    output_dir, _ = dirs
    publish(conn, source_id, output_dir, "gone", 90, write_file=False)
    report = run(conn, dirs, max_age_days=30)
    assert [(p.title, p.file_deleted) for p in report.pruned] == [("gone", False)]
    assert status_of(conn, "gone") == "pruned"


def test_articles_stay_processed(conn, source_id, dirs):
    output_dir, _ = dirs
    episode_id = publish(conn, source_id, output_dir, "old", 90)
    run(conn, dirs, max_age_days=30)
    [link] = repo.episode_articles(conn, episode_id)  # the link is kept as well
    assert repo.get_article(conn, link["article_id"])["status"] == "processed"


def test_audio_paths_outside_the_output_directory_are_never_deleted(
    conn, source_id, dirs, tmp_path, caplog
):
    output_dir, _ = dirs
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"precious")
    publish(conn, source_id, output_dir, "evil", 90, write_file=False, audio_file="../outside.mp3")
    with caplog.at_level(logging.WARNING):
        report = run(conn, dirs, max_age_days=30)
    assert outside.read_bytes() == b"precious"
    assert report.pruned[0].file_deleted is False and status_of(conn, "evil") == "pruned"
    assert any("outside the output directory" in r.message for r in caplog.records)


def test_drafts_and_already_pruned_episodes_are_left_alone(conn, source_id, dirs):
    output_dir, _ = dirs
    draft = repo.create_episode(conn, guid="guid-draft", mode="per_article", title="draft")
    conn.execute("UPDATE episodes SET created_at = '2020-01-01T00:00:00Z' WHERE id = ?", (draft,))
    pruned_id = publish(conn, source_id, output_dir, "done", 90)
    repo.prune_episode(conn, pruned_id)
    conn.commit()
    assert run(conn, dirs, max_age_days=30).pruned == []
    assert status_of(conn, "draft") == "draft"


# --- work directories --------------------------------------------------------------------


def test_work_dirs_of_published_and_pruned_episodes_are_removed_but_not_drafts(
    conn, source_id, dirs
):
    output_dir, data_dir = dirs
    publish(conn, source_id, output_dir, "kept", 1)
    publish(conn, source_id, output_dir, "old", 90)
    done = publish(conn, source_id, output_dir, "done", 2)
    repo.prune_episode(conn, done)
    repo.create_episode(conn, guid="guid-draft", mode="per_article", title="draft")
    for guid in ("guid-kept", "guid-old", "guid-done", "guid-draft"):
        directory = episode_work_dir(data_dir, guid)
        directory.mkdir(parents=True)
        (directory / "part.wav").write_bytes(b"x")
    stray = data_dir / "work" / "unknown-guid"
    stray.mkdir()

    report = run(conn, dirs, max_age_days=30)
    assert report.work_dirs_removed == 3
    assert not episode_work_dir(data_dir, "guid-kept").exists()
    assert not episode_work_dir(data_dir, "guid-old").exists()
    assert not episode_work_dir(data_dir, "guid-done").exists()
    assert episode_work_dir(data_dir, "guid-draft").exists()  # a draft may still be resumed
    assert stray.exists()  # nothing is deleted that belongs to no episode


def test_work_dir_name_is_filesystem_safe(tmp_path):
    assert episode_work_dir(tmp_path, "../../etc").parent == tmp_path / "work"
    assert episode_work_dir(tmp_path, "a/b c").name == "a_b_c"
    assert episode_work_dir(tmp_path, "").name == "episode"


# --- feed --------------------------------------------------------------------------------


def test_pruned_episodes_disappear_from_the_feed(conn, source_id, dirs, tmp_path):
    output_dir, _ = dirs
    publish(conn, source_id, output_dir, "new", 1)
    publish(conn, source_id, output_dir, "old", 90)
    cfg = AppConfig()
    cfg.paths.output_dir = output_dir
    assert len(feedparser.parse(write_feed(cfg, conn, now=NOW).read_bytes()).entries) == 2
    run(conn, dirs, max_age_days=30)
    entries = feedparser.parse(write_feed(cfg, conn, now=NOW).read_bytes()).entries
    assert [e.title for e in entries] == ["new"]
