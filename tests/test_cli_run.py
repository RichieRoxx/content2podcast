import json
import subprocess
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import feedparser
import httpx
import pytest
import respx
from typer.testing import CliRunner

from content2podcast import repository as repo
from content2podcast.audio import AudioError
from content2podcast.cli import app
from content2podcast.db import connect, db_path

runner = CliRunner()
ARTICLE_HTML = (Path(__file__).parent / "fixtures" / "articles" / "article.html").read_bytes()
FEED = "https://blog.example.com/feed.xml"
POST = "https://blog.example.com/posts/new"
RECENT = datetime.now(UTC) - timedelta(hours=2)

CANNED = {
    "title": "Python wird schneller",
    "summary": "Es geht um Paketmanager.",
    "segments": [
        {"speaker": "host", "style": "neutral", "text": "Hallo und willkommen zur Folge."},
        {"speaker": "expert", "style": "cheerful", "text": "Schön, dabei zu sein."},
    ],
}


def feed(*items: tuple[str, str]) -> bytes:
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link><pubDate>{format_datetime(RECENT)}</pubDate></item>"
        for t, u in items
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'
    ).encode()


def write_config(root: Path, *, llm=True, tts=True, extra=""):
    text = "paths:\n  data_dir: data\n  output_dir: public\n"
    text += "feed:\n  base_url: https://nas.example.ts.net/\npodcast:\n  title: Mein Podcast\n"
    if llm:
        text += f"llm:\n  provider: fake\n  response: {json.dumps(CANNED)}\n"
    if tts:
        text += "tts:\n  provider: fake\n"
    (root / "config.yaml").write_text(text + extra, encoding="utf-8")


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith(("C2P_", "AZURE_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path)
    (tmp_path / "sources.yaml").write_text(f"sources:\n  - name: Blog\n    url: {FEED}\n")
    return tmp_path


def invoke(*args):
    return runner.invoke(app, list(args))


@pytest.mark.ffmpeg
@respx.mock
def test_run_publishes_an_episode_end_to_end(project):
    blog = respx.get(FEED).mock(return_value=httpx.Response(200, content=feed()))
    respx.get(POST).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    first = invoke("run")
    assert first.exit_code == 0, first.output
    assert "baseline set" in first.output and "0 published, 0 failed" in first.output

    blog.mock(return_value=httpx.Response(200, content=feed(("Neuer Beitrag", POST))))
    result = invoke("run")
    assert result.exit_code == 0, result.output
    assert "Published Python wird schneller (from Neuer Beitrag)" in result.output
    assert "1 published, 0 failed, 0 pruned" in result.output

    public = project / "public"
    [mp3] = list((public / "episodes").glob("*.mp3"))
    assert mp3.name.startswith(f"{datetime.now(UTC):%Y-%m-%d}-python-wird-schneller-")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert float(probe.stdout) > 0.3

    parsed = feedparser.parse((public / "feed.xml").read_bytes())
    [entry] = parsed.entries
    assert entry.title == "Python wird schneller"
    assert entry.enclosures[0].href == f"https://nas.example.ts.net/episodes/{mp3.name}"
    assert int(entry.enclosures[0].length) == mp3.stat().st_size

    conn = connect(db_path(project / "data"))
    [article] = repo.list_articles(conn, "processed")
    assert article["url"] == POST and repo.list_articles(conn, "pending") == []
    assert [e["number"] for e in repo.list_episodes(conn, "published")] == [1]

    again = invoke("run")  # nothing new: no second episode
    assert again.exit_code == 0 and "0 published" in again.output
    assert len(list((public / "episodes").glob("*.mp3"))) == 1


@respx.mock
def test_run_exit_code_1_when_every_episode_failed(project, monkeypatch):
    from content2podcast.providers.llm.base import LLMError
    from content2podcast.providers.llm.fake import FakeLLM

    monkeypatch.setattr(
        "content2podcast.cli.build_llm", lambda cfg, secrets: FakeLLM(error=LLMError("boom"))
    )
    monkeypatch.setattr("content2podcast.cli.find_tools", lambda: None)
    blog = respx.get(FEED).mock(return_value=httpx.Response(200, content=feed()))
    respx.get(POST).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    invoke("run")
    blog.mock(return_value=httpx.Response(200, content=feed(("Neu", POST))))
    result = invoke("run")
    assert result.exit_code == 1
    assert (
        "FAILED Neu: LLMError: boom" in result.output and "0 published, 1 failed" in result.output
    )


def test_run_without_tts_is_a_config_error(project):
    write_config(project, tts=False)
    result = invoke("run")
    assert result.exit_code == 2 and "No tts provider configured" in result.output


def test_run_without_llm_is_a_config_error(project):
    write_config(project, llm=False)
    result = invoke("run")
    assert result.exit_code == 2 and "No llm provider configured" in result.output


def test_run_without_ffmpeg_fails_before_touching_any_article(project, monkeypatch):
    def missing():
        raise AudioError("ffmpeg and ffprobe not found on PATH")

    monkeypatch.setattr("content2podcast.cli.find_tools", missing)
    with respx.mock:
        route = respx.get(FEED).mock(return_value=httpx.Response(200, content=feed()))
        result = invoke("run")
    assert result.exit_code == 1 and "not found on PATH" in result.output
    assert not route.called  # not even discovery ran


def test_run_with_a_broken_prompt_template_is_a_config_error(project, monkeypatch):
    monkeypatch.setattr("content2podcast.cli.find_tools", lambda: None)
    prompts = project / "prompts"
    prompts.mkdir()
    (prompts / "per_article_de.md").write_text("<!-- system -->\n$nobody\n<!-- user -->\nU\n")
    write_config(project, extra="episode:\n  prompts_dir: prompts\n")
    with respx.mock:
        blog = respx.get(FEED).mock(return_value=httpx.Response(200, content=feed()))
        respx.get(POST).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
        invoke("run")
        blog.mock(return_value=httpx.Response(200, content=feed(("Neu", POST))))
        result = invoke("run")
    assert result.exit_code == 2 and "$nobody" in result.output


def test_run_force_option_is_accepted(project, monkeypatch):
    monkeypatch.setattr("content2podcast.cli.find_tools", lambda: None)
    with respx.mock:
        respx.get(FEED).mock(return_value=httpx.Response(200, content=feed()))
        assert invoke("run", "--force").exit_code == 0


def test_run_is_blocked_by_the_run_lock(project):
    from content2podcast.lock import run_lock

    with run_lock(project / "data"):
        assert invoke("run").exit_code == 3
