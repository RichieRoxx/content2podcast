"""End-to-end tests: the real CLI over the whole pipeline, fully offline.

Sources and articles are mocked with respx, the providers are the deterministic fakes and the
audio is assembled by the real ffmpeg (tests marked ``ffmpeg``) or a stub.
"""

import json
import os
import socket
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlsplit

import feedparser
import httpx
import pytest
import respx
from typer.testing import CliRunner

from content2podcast import repository as repo
from content2podcast.cli import app
from content2podcast.db import connect, db_path
from content2podcast.pipeline import Pipeline
from pipeline_helpers import ARTICLE_HTML

pytestmark = pytest.mark.ffmpeg  # the whole module needs the real ffmpeg

runner = CliRunner()
BLOG_FEED = "https://blog.example.com/feed.xml"
NEWS_FEED = "https://news.example.org/feed.xml"
RECENT = datetime.now(UTC) - timedelta(hours=1)
SCRIPT = {
    "title": "Python wird schneller",
    "summary": "Es geht um Paketmanager und warum sie heute schnell sind.",
    "segments": [
        {"speaker": "host", "style": "neutral", "text": "Hallo und willkommen zu dieser Folge."},
        {"speaker": "expert", "style": "cheerful", "text": "Schön, dass ich dabei sein darf."},
        {"speaker": "host", "style": "serious", "text": "Dann steigen wir direkt ins Thema ein."},
    ],
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Any attempt to open a real network connection fails loudly."""

    def refuse(self, address, *args, **kwargs):
        if isinstance(address, tuple):  # AF_INET(6); local unix sockets (sqlite, pipes) are fine
            raise RuntimeError(f"network access in an offline test: {address}")
        return real_connect(self, address, *args, **kwargs)

    real_connect = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", refuse)


def post_url(host: str, n: int) -> str:
    return f"https://{host}/posts/{n}"


def rss(host: str, *posts: int, age_hours: float = 1) -> bytes:
    published = format_datetime(RECENT - timedelta(hours=age_hours - 1))
    items = "".join(
        f"<item><title>Beitrag {n}</title><link>{post_url(host, n)}</link>"
        f"<pubDate>{published}</pubDate></item>"
        for n in posts
    )
    head = '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
    return f"{head}{items}</channel></rss>".encode()


class Project:
    """A temporary installation driven through the CLI."""

    def __init__(self, root: Path, *, mode="per_article", extra_episode="", sources=("blog",)):
        self.root = root
        episode = f"episode:\n  mode: {mode}\n  max_articles: 2\n{extra_episode}"
        (root / "config.yaml").write_text(
            "paths:\n  data_dir: data\n  output_dir: public\n"
            "podcast:\n  title: Mein Podcast\n  author: Erika\n"
            "feed:\n  base_url: https://nas.example.ts.net/\n"
            f"{episode}"
            f"llm:\n  provider: fake\n  response: {json.dumps(SCRIPT)}\n"
            "tts:\n  provider: fake\n",
            encoding="utf-8",
        )
        feeds = {"blog": BLOG_FEED, "news": NEWS_FEED}
        (root / "sources.yaml").write_text(
            "sources:\n" + "".join(f"  - name: {n}\n    url: {feeds[n]}\n" for n in sources)
        )

    def run(self, *args: str):
        return runner.invoke(app, list(args))

    @property
    def public(self) -> Path:
        return self.root / "public"

    def db(self):
        return connect(db_path(self.root / "data"))

    def mp3s(self) -> list[Path]:
        return sorted((self.public / "episodes").glob("*.mp3")) if self.public.exists() else []

    def feed(self):
        return feedparser.parse((self.public / "feed.xml").read_bytes())


@pytest.fixture
def project(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("C2P_", "AZURE_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    return Project(tmp_path)


def serve(host: str, feed_url: str, *posts: int, status=200):
    route = respx.get(feed_url).mock(return_value=httpx.Response(200, content=rss(host, *posts)))
    for n in posts:
        respx.get(post_url(host, n)).mock(return_value=httpx.Response(status, content=ARTICLE_HTML))
    return route


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return float(out.stdout)


def assert_feed_serves_its_files(project: Project):
    """Every enclosure points at a file in the output directory with the announced size."""
    parsed = project.feed()
    assert not parsed.bozo and parsed.version == "rss20"
    for entry in parsed.entries:
        enclosure = entry.enclosures[0]
        path = project.public / urlsplit(enclosure.href).path.lstrip("/")
        assert path.is_file(), enclosure.href
        assert int(enclosure.length) == path.stat().st_size
        assert enclosure.type == "audio/mpeg"
    ids = [e.id for e in parsed.entries]
    assert len(ids) == len(set(ids))  # unique GUIDs


# --- baseline, then episodes -------------------------------------------------------------


@respx.mock
def test_first_run_is_baseline_only_then_new_items_become_episodes(project):
    blog = serve("blog.example.com", BLOG_FEED, 1)
    first = project.run("run")
    assert first.exit_code == 0, first.output
    assert "baseline set (1 existing articles)" in first.output
    assert project.mp3s() == [] and not (project.public / "feed.xml").exists()
    assert {a["status"] for a in project.db().execute("SELECT status FROM articles")} == {
        "baseline"
    }

    blog.mock(return_value=httpx.Response(200, content=rss("blog.example.com", 1, 2, 3)))
    for n in (2, 3):
        respx.get(post_url("blog.example.com", n)).mock(
            return_value=httpx.Response(200, content=ARTICLE_HTML)
        )
    second = project.run("run")
    assert second.exit_code == 0, second.output
    assert "2 published, 0 failed" in second.output

    assert len(project.mp3s()) == 2
    for mp3 in project.mp3s():
        assert duration(mp3) > 0.5
    parsed = project.feed()
    assert [e.title for e in parsed.entries] == ["Python wird schneller"] * 2
    assert_feed_serves_its_files(project)

    # the feed is a valid podcast feed
    channel = ET.fromstring((project.public / "feed.xml").read_bytes()).find("channel")
    itunes = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
    assert channel.findtext(f"{itunes}author") == "Erika"
    assert channel.find("item").findtext(f"{itunes}duration")
    assert (
        channel.find("item/enclosure").get("url").startswith("https://nas.example.ts.net/episodes/")
    )

    # articles are processed, numbering starts at 1
    conn = project.db()
    assert {a["status"] for a in repo.list_articles(conn, "processed")} == {"processed"}
    assert sorted(e["number"] for e in repo.list_episodes(conn, "published")) == [1, 2]


@respx.mock
def test_running_again_without_news_changes_nothing(project):
    blog = serve("blog.example.com", BLOG_FEED, 1)
    project.run("run")
    blog.mock(return_value=httpx.Response(200, content=rss("blog.example.com", 1, 2)))
    respx.get(post_url("blog.example.com", 2)).mock(
        return_value=httpx.Response(200, content=ARTICLE_HTML)
    )
    project.run("run")
    before = ((project.public / "feed.xml").read_bytes(), [p.name for p in project.mp3s()])
    again = project.run("run")
    assert again.exit_code == 0 and "0 published, 0 failed" in again.output
    assert ((project.public / "feed.xml").read_bytes(), [p.name for p in project.mp3s()]) == before


@respx.mock
def test_the_maintenance_commands_agree_with_the_run(project):
    blog = serve("blog.example.com", BLOG_FEED, 1)
    project.run("run")
    blog.mock(return_value=httpx.Response(200, content=rss("blog.example.com", 1, 2)))
    respx.get(post_url("blog.example.com", 2)).mock(
        return_value=httpx.Response(200, content=ARTICLE_HTML)
    )
    project.run("run")
    listing = project.run("episodes", "list").output
    assert "Python wird schneller" in listing and "published" in listing
    sources = project.run("sources", "list").output
    assert "blog" in sources and "baseline=1" in sources and "processed=1" in sources

    feed_before = project.feed()
    rebuilt = project.run("feed", "rebuild")
    assert rebuilt.exit_code == 0 and "(1 episode(s))" in rebuilt.output
    assert [e.id for e in project.feed().entries] == [e.id for e in feed_before.entries]


# --- both modes --------------------------------------------------------------------------


@respx.mock
def test_daily_digest_mode_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = Project(tmp_path, mode="daily_digest")
    blog = serve("blog.example.com", BLOG_FEED, 1)
    project.run("run")
    blog.mock(return_value=httpx.Response(200, content=rss("blog.example.com", 1, 2, 3, 4)))
    for n in (2, 3, 4):
        respx.get(post_url("blog.example.com", n)).mock(
            return_value=httpx.Response(200, content=ARTICLE_HTML)
        )
    result = project.run("run")
    assert result.exit_code == 0 and "1 published" in result.output
    assert len(project.mp3s()) == 1
    conn = project.db()
    [episode] = repo.list_episodes(conn, "published")
    roles = [link["role"] for link in repo.episode_articles(conn, episode["id"])]
    assert sorted(roles) == ["discussed", "discussed", "mentioned"]  # max_articles = 2
    assert "Außerdem neu" in project.feed().entries[0].content[0].value
    assert_feed_serves_its_files(project)

    second = project.run("run")  # the same day: nothing more
    assert second.exit_code == 0 and "0 published" in second.output and len(project.mp3s()) == 1


# --- isolation ---------------------------------------------------------------------------


@respx.mock
def test_a_broken_source_and_a_broken_article_do_not_stop_the_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = Project(tmp_path, sources=("blog", "news"))
    blog = serve("blog.example.com", BLOG_FEED)
    news = serve("news.example.org", NEWS_FEED)
    project.run("run")  # both baselined (empty)
    blog.mock(return_value=httpx.Response(200, content=rss("blog.example.com", 1, 2)))
    respx.get(post_url("blog.example.com", 1)).mock(return_value=httpx.Response(403))  # broken
    respx.get(post_url("blog.example.com", 2)).mock(
        return_value=httpx.Response(200, content=ARTICLE_HTML)
    )
    news.mock(return_value=httpx.Response(403))  # the whole source is down

    result = project.run("run")
    assert result.exit_code == 0, result.output
    assert "news: ERROR" in result.output
    assert "FAILED" in result.output and "1 published, 1 failed" in result.output
    assert len(project.mp3s()) == 1
    conn = project.db()
    states = {a["url"]: (a["status"], a["attempts"]) for a in repo.list_articles(conn)}
    assert states[post_url("blog.example.com", 1)] == ("pending", 1)
    assert states[post_url("blog.example.com", 2)][0] == "processed"
    sources = project.run("sources", "list").output
    assert "last error - news:" in sources


@respx.mock
def test_a_run_where_everything_fails_exits_with_1_and_publishes_nothing(project):
    blog = serve("blog.example.com", BLOG_FEED)
    project.run("run")
    blog.mock(return_value=httpx.Response(200, content=rss("blog.example.com", 1)))
    respx.get(post_url("blog.example.com", 1)).mock(return_value=httpx.Response(403))
    result = project.run("run")
    assert result.exit_code == 1 and "0 published, 1 failed" in result.output
    assert project.mp3s() == [] and not (project.public / "feed.xml").exists()


# --- dry run -----------------------------------------------------------------------------


@respx.mock
def test_dry_run_leaves_the_published_state_untouched(project):
    blog = serve("blog.example.com", BLOG_FEED)
    project.run("run")
    blog.mock(return_value=httpx.Response(200, content=rss("blog.example.com", 1)))
    page = respx.get(post_url("blog.example.com", 1)).mock(
        return_value=httpx.Response(200, content=ARTICLE_HTML)
    )

    dry = project.run("run", "--dry-run")
    assert dry.exit_code == 0 and "1 of 1 script(s) written" in dry.output
    [script_json] = (project.root / "data" / "dry-run").glob("*/*/script.json")
    assert json.loads(script_json.read_text(encoding="utf-8"))["title"] == SCRIPT["title"]
    assert project.mp3s() == [] and not (project.public / "feed.xml").exists()
    conn = project.db()
    assert repo.list_episodes(conn) == []  # no episode, no draft
    [article] = repo.list_articles(conn, "pending")  # not processed
    assert article["content"] and article["extracted_at"]  # but the text is cached

    real = project.run("run")  # the real run then reuses the cached text
    assert real.exit_code == 0 and "1 published" in real.output
    assert page.call_count == 1
    assert len(project.mp3s()) == 1


# --- crash and resume with the real assembler --------------------------------------------


class Crash(BaseException):
    pass


@respx.mock
def test_crash_before_assembly_then_resume_with_real_ffmpeg(project):
    from content2podcast.config import load_config, load_secrets, load_sources
    from content2podcast.http import make_client
    from content2podcast.providers.llm.fake import FakeLLM
    from content2podcast.providers.registry import build_tts
    from content2podcast.providers.tts.base import allowed_styles

    blog = serve("blog.example.com", BLOG_FEED)
    project.run("run")
    blog.mock(return_value=httpx.Response(200, content=rss("blog.example.com", 1)))
    respx.get(post_url("blog.example.com", 1)).mock(
        return_value=httpx.Response(200, content=ARTICLE_HTML)
    )

    config = load_config(project.root / "config.yaml")
    sources = load_sources(config.paths.sources_file).sources
    secrets = load_secrets(project.root / ".env")
    llm = FakeLLM(SCRIPT)
    tts = build_tts(config.tts, secrets)
    styles = allowed_styles(tts, config.roles.host.voice, config.roles.expert.voice)

    def crash(*args, **kwargs):
        raise Crash

    conn = project.db()
    with make_client(config.http) as http, pytest.raises(Crash):
        Pipeline(config, conn, http, llm, tts, styles, assemble=crash).run(sources)
    assert len(tts.calls) == 3 and len(llm.calls) == 1
    assert project.mp3s() == []
    assert [e["status"] for e in repo.list_episodes(conn)] == ["draft"]
    conn.close()

    # a normal `podcast run` (new process state, real ffmpeg) picks the draft up
    result = project.run("run")
    assert result.exit_code == 0, result.output
    assert "1 published" in result.output and len(project.mp3s()) == 1
    assert duration(project.mp3s()[0]) > 0.5
    conn = project.db()
    assert [e["status"] for e in repo.list_episodes(conn)] == ["published"]
    assert len(project.feed().entries) == 1
    assert_feed_serves_its_files(project)
    assert not any((project.root / "data" / "work").glob("*"))  # scratch space cleaned up


# --- retention ---------------------------------------------------------------------------


@respx.mock
def test_retention_prunes_old_episodes_and_the_feed_follows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = Project(tmp_path)
    (tmp_path / "config.yaml").write_text(
        (tmp_path / "config.yaml").read_text() + "feed:\n  retention:\n    max_episodes: 1\n"
    )
    blog = serve("blog.example.com", BLOG_FEED)
    project.run("run")
    blog.mock(return_value=httpx.Response(200, content=rss("blog.example.com", 1)))
    respx.get(post_url("blog.example.com", 1)).mock(
        return_value=httpx.Response(200, content=ARTICLE_HTML)
    )
    project.run("run")
    first = project.mp3s()
    blog.mock(return_value=httpx.Response(200, content=rss("blog.example.com", 1, 2)))
    respx.get(post_url("blog.example.com", 2)).mock(
        return_value=httpx.Response(200, content=ARTICLE_HTML)
    )
    result = project.run("run")
    assert result.exit_code == 0 and "1 pruned" in result.output
    assert len(project.mp3s()) == 1 and project.mp3s() != first  # the older file is gone
    assert len(project.feed().entries) == 1
    statuses = [e["status"] for e in repo.list_episodes(project.db())]
    assert sorted(statuses) == ["pruned", "published"]
    assert_feed_serves_its_files(project)
    all_ = project.run("episodes", "list", "--all").output
    assert "pruned" in all_ and "published" in all_


def test_the_offline_guard_really_blocks_network_connections():
    with pytest.raises(RuntimeError, match="network access in an offline test"):
        socket.create_connection(("203.0.113.1", 80), timeout=1)
