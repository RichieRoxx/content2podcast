from pathlib import Path

import feedparser
import pytest
from typer.testing import CliRunner

from content2podcast import repository as repo
from content2podcast.cli import app
from content2podcast.db import connect, db_path

runner = CliRunner()


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith(("C2P_", "AZURE_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "paths:\n  data_dir: data\n  output_dir: public\n"
        "podcast:\n  title: Mein Podcast\n  cover_image: cover.png\n"
        "feed:\n  base_url: https://nas.example.ts.net/\n",
        encoding="utf-8",
    )
    (tmp_path / "cover.png").write_bytes(b"png")
    return tmp_path


def invoke(*args):
    return runner.invoke(app, list(args))


def seed(root: Path):
    """Two published episodes (with MP3s), a draft and a pruned one."""
    conn = connect(db_path(root / "data"))
    source = repo.upsert_source(conn, "Blog", "https://blog.example.com/feed", "rss")

    def episode(n, title, status, published=None, *, mp3=True, articles=1, duration=125.0):
        links = []
        for k in range(articles):
            article = repo.insert_article_if_new(
                conn, source, f"https://blog.example.com/{n}-{k}", title=f"Artikel {n}.{k}"
            )
            links.append((article, "discussed"))
        episode_id = repo.create_episode(
            conn, guid=f"guid-{n}", mode="per_article", title=title, summary=f"S{n}",
            number=n, articles=links,
        )  # fmt: skip
        if status in ("published", "pruned"):
            repo.publish_episode(
                conn,
                episode_id,
                audio_file=f"episodes/e{n}.mp3",
                audio_bytes=100,
                duration_s=duration,
            )
            conn.execute(
                "UPDATE episodes SET published_at = ?, status = ? WHERE id = ?",
                (published, status, episode_id),
            )
            if mp3:
                (root / "public" / "episodes").mkdir(parents=True, exist_ok=True)
                (root / "public" / "episodes" / f"e{n}.mp3").write_bytes(b"mp3")
        conn.commit()

    episode(1, "Erste Folge", "published", "2026-10-01T05:30:00Z", articles=2)
    episode(
        2, "Zweite Folge mit Äpfeln & Birnen", "published", "2026-10-02T05:30:00Z", duration=3725.0
    )
    episode(3, "Entwurf", "draft", articles=3)
    episode(4, "Alte Folge", "pruned", "2026-08-01T05:30:00Z", mp3=False)
    conn.close()


# --- feed rebuild ------------------------------------------------------------------------


def test_feed_rebuild_writes_feed_and_copies_the_cover(project):
    seed(project)
    result = invoke("feed", "rebuild")
    assert result.exit_code == 0, result.output
    assert "Feed written to" in result.output and "feed.xml (2 episode(s))" in result.output
    feed = project / "public" / "feed.xml"
    parsed = feedparser.parse(feed.read_bytes())
    assert [e.title for e in parsed.entries] == ["Zweite Folge mit Äpfeln & Birnen", "Erste Folge"]
    assert parsed.entries[0].enclosures[0].href == "https://nas.example.ts.net/episodes/e2.mp3"
    assert parsed.feed.image.href == "https://nas.example.ts.net/cover.png"
    assert (project / "public" / "cover.png").read_bytes() == b"png"


def test_feed_rebuild_without_episodes_gives_a_valid_empty_feed(project):
    result = invoke("feed", "rebuild")
    assert result.exit_code == 0 and "(0 episode(s))" in result.output
    parsed = feedparser.parse((project / "public" / "feed.xml").read_bytes())
    assert not parsed.bozo and parsed.entries == []


def test_feed_rebuild_is_repeatable_and_reflects_changes(project):
    seed(project)
    invoke("feed", "rebuild")
    conn = connect(db_path(project / "data"))
    repo.prune_episode(conn, repo.get_episode_by_guid(conn, "guid-1")["id"])
    conn.close()
    invoke("feed", "rebuild")
    parsed = feedparser.parse((project / "public" / "feed.xml").read_bytes())
    assert [e.title for e in parsed.entries] == ["Zweite Folge mit Äpfeln & Birnen"]


def test_feed_rebuild_warns_about_missing_audio_files(project):
    seed(project)
    (project / "public" / "episodes" / "e1.mp3").unlink()
    result = invoke("feed", "rebuild")
    assert result.exit_code == 0
    assert "audio file missing for 'Erste Folge': episodes/e1.mp3" in result.output
    assert "e2.mp3" not in result.output.replace("feed.xml", "")


def test_feed_rebuild_does_not_need_the_sources_file(project):
    assert not (project / "sources.yaml").exists()
    assert invoke("feed", "rebuild").exit_code == 0


def test_feed_rebuild_missing_cover_is_a_config_error(project):
    (project / "cover.png").unlink()
    result = invoke("feed", "rebuild")
    assert result.exit_code == 2 and "podcast.cover_image not found" in result.output


def test_feed_rebuild_config_error(project):
    (project / "config.yaml").write_text("episode:\n  mode: weekly\n")
    result = invoke("feed", "rebuild")
    assert result.exit_code == 2 and "episode.mode" in result.output


# --- episodes list -----------------------------------------------------------------------


def rows(output: str) -> list[list[str]]:
    return [line.split() for line in output.splitlines() if line.strip()]


def test_episodes_list_shows_published_episodes_newest_first(project):
    seed(project)
    result = invoke("episodes", "list")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split() == ["NO", "DATE", "TITLE", "DURATION", "STATUS", "ARTICLES"]
    assert len(lines) == 3
    second, first = lines[1].split(), lines[2].split()
    assert second[:2] == ["2", "2026-10-02"] and second[-3:] == ["62:05", "published", "1"]
    assert first[:2] == ["1", "2026-10-01"] and first[-3:] == ["2:05", "published", "2"]
    assert "Zweite Folge mit Äpfeln & Birnen" in lines[1]
    assert "Entwurf" not in result.output and "Alte Folge" not in result.output


def test_episodes_list_all_includes_drafts_and_pruned(project):
    seed(project)
    result = invoke("episodes", "list", "--all")
    assert result.exit_code == 0
    by_title = {line: line.split() for line in result.output.splitlines()[1:]}
    statuses = [parts[-2] for parts in by_title.values()]
    assert sorted(statuses) == ["draft", "pruned", "published", "published"]
    draft = next(parts for line, parts in by_title.items() if "Entwurf" in line)
    assert draft[-3:] == ["-", "draft", "3"]  # no duration yet, three articles
    pruned = next(parts for line, parts in by_title.items() if "Alte Folge" in line)
    assert pruned[1] == "2026-08-01" and pruned[-2] == "pruned"
    assert len(by_title) == 4


def test_episodes_list_orders_by_date_with_drafts_by_creation_date(project):
    seed(project)
    titles = [line for line in invoke("episodes", "list", "--all").output.splitlines()[1:]]
    # the draft was created "now" (2026), so it sorts first; the pruned one (August) last
    assert "Entwurf" in titles[0] and "Alte Folge" in titles[-1]


def test_episodes_list_long_titles_are_shortened(project):
    conn = connect(db_path(project / "data"))
    repo.create_episode(conn, guid="g", mode="per_article", title="x" * 80)
    conn.close()
    line = invoke("episodes", "list", "--all").output.splitlines()[1]
    assert "x" * 49 + "…" in line and "x" * 50 not in line


def test_episodes_list_empty(project):
    assert "No published episodes" in invoke("episodes", "list").output
    assert "No episodes." in invoke("episodes", "list", "--all").output


def test_episodes_list_config_error(project):
    (project / "config.yaml").write_text("episode:\n  mode: weekly\n")
    assert invoke("episodes", "list").exit_code == 2
