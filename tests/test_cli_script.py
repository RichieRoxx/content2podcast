import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from content2podcast import repository as repo
from content2podcast.cli import app
from content2podcast.db import connect, db_path
from content2podcast.providers.llm.base import LLMError
from content2podcast.providers.llm.fake import FakeLLM

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures" / "articles"
ARTICLE_HTML = (FIXTURES / "article.html").read_bytes()
PAGE_TITLE = "Warum Python-Pakete heute schneller installieren"

FEED = "https://blog.example.com/feed.xml"
OLD_URL = "https://blog.example.com/posts/old"
NEW_URL = "https://blog.example.com/posts/new"
NEW_URL_2 = "https://blog.example.com/posts/newer"
RECENT = datetime.now(UTC) - timedelta(hours=3)

CANNED = {
    "title": "Python wird schneller",
    "summary": "Es geht um Paketmanager.",
    "segments": [
        {"speaker": "host", "style": "neutral", "text": "Hallo und willkommen."},
        {"speaker": "expert", "style": "cheerful", "text": "Schön, dabei zu sein."},
    ],
}


def feed(*items: tuple[str, str]) -> bytes:
    body = "".join(
        f"<item><title>{title}</title><link>{url}</link>"
        f"<pubDate>{format_datetime(RECENT)}</pubDate></item>"
        for title, url in items
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'
    ).encode()


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


def write_config(root: Path, extra: str = "", tts: bool = True):
    # JSON is valid YAML, so a flow-style mapping keeps this short
    llm = f"llm:\n  provider: fake\n  response: {json.dumps(CANNED)}\n"
    config = (
        "paths:\n  data_dir: data\n" + llm + ("tts:\n  provider: fake\n" if tts else "") + extra
    )
    (root / "config.yaml").write_text(config, encoding="utf-8")


def invoke(*args):
    return runner.invoke(app, list(args))


def scripts(root: Path, sub: str = "dry-run"):
    return sorted((root / "data" / sub).glob("*/*/script.json"))


# --- podcast run --dry-run ---------------------------------------------------------------


@respx.mock
def test_dry_run_writes_scripts_and_leaves_articles_pending(project):
    blog = respx.get(FEED).mock(return_value=httpx.Response(200, content=feed(("Old", OLD_URL))))
    respx.get(NEW_URL).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))

    first = invoke("run", "--dry-run")
    assert first.exit_code == 0, first.output
    assert "baseline set" in first.output and "no pending articles" in first.output
    assert not scripts(project)

    blog.mock(
        return_value=httpx.Response(200, content=feed(("Old", OLD_URL), ("Neuer Beitrag", NEW_URL)))
    )
    second = invoke("run", "--dry-run")
    assert second.exit_code == 0, second.output
    assert "1 of 1 script(s) written" in second.output
    [script_json] = scripts(project)
    assert script_json.parent.name == "python-wird-schneller"
    assert (script_json.parent / "script.md").is_file()

    data = json.loads(script_json.read_text(encoding="utf-8"))
    assert data["title"] == "Python wird schneller"
    assert data["sources"] == [{"title": "Neuer Beitrag", "url": NEW_URL}]
    markdown = (script_json.parent / "script.md").read_text(encoding="utf-8")
    assert "**Mia** *(neutral)*: Hallo und willkommen." in markdown
    assert "**Klaus** *(cheerful)*" in markdown
    assert f"[Neuer Beitrag]({NEW_URL})" in markdown

    conn = connect(db_path(project / "data"))
    [article] = repo.list_articles(conn, "pending")  # not processed
    assert article["url"] == NEW_URL
    assert "Python-Projekt" in article["content"] and article["extracted_at"]  # cached
    assert repo.list_articles(conn, "processed") == []


@respx.mock
def test_dry_run_reuses_cached_text_and_does_not_overwrite_earlier_scripts(project):
    blog = respx.get(FEED).mock(return_value=httpx.Response(200, content=feed()))
    page = respx.get(NEW_URL).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    invoke("run", "--dry-run")
    blog.mock(return_value=httpx.Response(200, content=feed(("Neu", NEW_URL))))
    invoke("run", "--dry-run")
    invoke("run", "--dry-run")
    assert page.call_count == 1  # extraction cache
    assert len(scripts(project)) == 2  # second run got its own directory
    assert {p.parent.name for p in scripts(project)} == {
        "python-wird-schneller",
        "python-wird-schneller-1",
    }


@respx.mock
def test_dry_run_respects_max_episodes_per_run(project):
    write_config(project, "episode:\n  max_episodes_per_run: 1\n")
    blog = respx.get(FEED).mock(return_value=httpx.Response(200, content=feed()))
    respx.get(NEW_URL).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    respx.get(NEW_URL_2).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    invoke("run", "--dry-run")
    blog.mock(return_value=httpx.Response(200, content=feed(("A", NEW_URL), ("B", NEW_URL_2))))
    result = invoke("run", "--dry-run")
    assert "1 of 1 script(s)" in result.output
    assert len(scripts(project)) == 1


@respx.mock
def test_dry_run_article_without_text_is_reported_and_counts_an_attempt(project):
    blog = respx.get(FEED).mock(return_value=httpx.Response(200, content=feed()))
    respx.get(NEW_URL).mock(return_value=httpx.Response(403))
    invoke("run", "--dry-run")
    blog.mock(return_value=httpx.Response(200, content=feed(("Gesperrt", NEW_URL))))
    result = invoke("run", "--dry-run")
    assert result.exit_code == 1
    assert "FAILED Gesperrt: no article text" in result.output
    conn = connect(db_path(project / "data"))
    assert repo.list_articles(conn)[0]["attempts"] == 1


@respx.mock
def test_dry_run_llm_failure_is_reported_with_exit_1(project, monkeypatch):
    monkeypatch.setattr(
        "content2podcast.cli.build_llm", lambda cfg, secrets: FakeLLM(error=LLMError("boom"))
    )
    blog = respx.get(FEED).mock(return_value=httpx.Response(200, content=feed()))
    respx.get(NEW_URL).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    invoke("run", "--dry-run")
    blog.mock(return_value=httpx.Response(200, content=feed(("Neu", NEW_URL))))
    result = invoke("run", "--dry-run")
    assert result.exit_code == 1 and "FAILED Neu: boom" in result.output
    assert not scripts(project)


def test_dry_run_without_llm_configuration_is_a_config_error(project):
    (project / "config.yaml").write_text("paths:\n  data_dir: data\n")
    result = invoke("run", "--dry-run")
    assert result.exit_code == 2
    assert "No llm provider configured" in result.output


def test_dry_run_without_tts_uses_only_the_neutral_style(project, monkeypatch):
    write_config(project, tts=False)
    llm = FakeLLM(
        CANNED
        | {"segments": [CANNED["segments"][0], {**CANNED["segments"][1], "style": "neutral"}]}
    )
    monkeypatch.setattr("content2podcast.cli.build_llm", lambda cfg, secrets: llm)
    with respx.mock:
        blog = respx.get(FEED).mock(return_value=httpx.Response(200, content=feed()))
        respx.get(NEW_URL).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
        invoke("run", "--dry-run")
        blog.mock(return_value=httpx.Response(200, content=feed(("Neu", NEW_URL))))
        result = invoke("run", "--dry-run")
    assert "only use the 'neutral' style" in result.output
    assert result.exit_code == 0
    assert "Erlaubte Werte für das Feld „style“: neutral." in llm.calls[0].user


def test_secrets_come_from_the_env_file_next_to_the_config(project, monkeypatch):
    (project / ".env").write_text("AZURE_FOUNDRY_API_KEY=from-dotenv\n")
    seen = {}

    def fake_build(cfg, secrets):
        seen["key"] = secrets.azure_foundry_api_key.get_secret_value()
        return FakeLLM(CANNED)

    monkeypatch.setattr("content2podcast.cli.build_llm", fake_build)
    with respx.mock:
        respx.get(FEED).mock(return_value=httpx.Response(200, content=feed()))
        invoke("run", "--dry-run")
    assert seen == {"key": "from-dotenv"}


@respx.mock
def test_dry_run_holds_the_run_lock(project):
    from content2podcast.lock import run_lock

    with run_lock(project / "data"):
        result = invoke("run", "--dry-run")
    assert result.exit_code == 3


# --- podcast script URL... ---------------------------------------------------------------


@respx.mock
def test_script_command_writes_files_without_touching_the_database(project):
    respx.get(NEW_URL).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    out = project / "out"
    result = invoke("script", NEW_URL, "-o", str(out))
    assert result.exit_code == 0, result.output
    assert "Python wird schneller" in result.output
    data = json.loads((out / "python-wird-schneller" / "script.json").read_text(encoding="utf-8"))
    assert data["sources"] == [{"title": PAGE_TITLE, "url": NEW_URL}]
    assert (out / "python-wird-schneller" / "script.md").is_file()
    assert not db_path(project / "data").exists()


@respx.mock
def test_script_command_default_output_is_below_data_dir(project):
    respx.get(NEW_URL).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    assert invoke("script", NEW_URL).exit_code == 0
    assert len(scripts(project, "adhoc")) == 1


@respx.mock
def test_script_command_one_script_per_url_and_failures_do_not_stop_it(project):
    respx.get(NEW_URL).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    respx.get(NEW_URL_2).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    respx.get("https://blog.example.com/gone").mock(return_value=httpx.Response(404))
    out = project / "out"
    result = invoke("script", NEW_URL, "https://blog.example.com/gone", NEW_URL_2, "-o", str(out))
    assert result.exit_code == 0
    assert "FAILED https://blog.example.com/gone" in result.output
    assert len(list(out.glob("*/script.json"))) == 2


@respx.mock
def test_script_command_exit_1_when_nothing_could_be_written(project):
    respx.get(NEW_URL).mock(return_value=httpx.Response(404))
    result = invoke("script", NEW_URL, "-o", str(project / "out"))
    assert result.exit_code == 1 and "FAILED" in result.output


@respx.mock
def test_script_command_page_without_article_text(project):
    respx.get(NEW_URL).mock(
        return_value=httpx.Response(200, content=(FIXTURES / "empty.html").read_bytes())
    )
    # trafilatura returns a short login-wall sentence for this page; an empty page has nothing
    respx.get(NEW_URL_2).mock(
        return_value=httpx.Response(200, content=b"<html><body></body></html>")
    )
    result = invoke("script", NEW_URL_2, "-o", str(project / "out"))
    assert result.exit_code == 1 and "No article text found" in result.output


def test_script_command_rejects_unknown_mode(project):
    result = invoke("script", NEW_URL, "--mode", "weekly")
    assert result.exit_code == 2 and "Unknown mode 'weekly'" in result.output


@respx.mock
def test_script_command_daily_digest_makes_one_script_for_all_urls(project):
    respx.get(NEW_URL).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    respx.get(NEW_URL_2).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    out = project / "out"
    result = invoke("script", NEW_URL, NEW_URL_2, "--mode", "daily_digest", "-o", str(out))
    assert result.exit_code == 0, result.output
    [script_json] = list(out.glob("*/script.json"))
    sources = json.loads(script_json.read_text(encoding="utf-8"))["sources"]
    assert [s["url"] for s in sources] == [NEW_URL, NEW_URL_2]


@respx.mock
def test_script_command_missing_template_is_a_config_error(project):
    (project / "config.yaml").write_text(
        (project / "config.yaml").read_text() + "podcast:\n  language: fr-FR\n"
    )
    respx.get(NEW_URL).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    result = invoke("script", NEW_URL, "-o", str(project / "out"))
    assert result.exit_code == 2
    assert "per_article_fr.md" in result.output


def test_script_command_requires_a_url():
    assert invoke("script").exit_code == 2
