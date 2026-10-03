from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx
from typer.testing import CliRunner

from content2podcast import repository as repo
from content2podcast.cli import app
from content2podcast.db import connect, db_path

runner = CliRunner()

BLOG = "https://blog.example.com/feed.xml"
NEWS = "https://news.example.org/feed.xml"
RECENT = datetime.now(UTC) - timedelta(days=1)


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
        if key.startswith("C2P_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("paths:\n  data_dir: data\n")
    (tmp_path / "sources.yaml").write_text(
        f"sources:\n  - name: Blog\n    url: {BLOG}\n  - name: News\n    url: {NEWS}\n"
    )
    return tmp_path


def invoke(*args):
    return runner.invoke(app, list(args))


def db(project):
    return connect(db_path(project / "data"))


@respx.mock
def test_check_first_run_baselines_then_lists_new_articles():
    blog = respx.get(BLOG).mock(
        return_value=httpx.Response(200, content=feed(("Old post", "https://blog.example.com/old")))
    )
    respx.get(NEWS).mock(return_value=httpx.Response(200, content=feed()))
    first = invoke("check")
    assert first.exit_code == 0
    assert "Blog: baseline set (1 existing articles)" in first.output
    assert "Summary: 0 new, 1 baseline, 0 skipped, 0 error(s)" in first.output

    blog.mock(
        return_value=httpx.Response(
            200,
            content=feed(
                ("Old post", "https://blog.example.com/old"),
                ("Brand new", "https://blog.example.com/new"),
            ),
        )
    )
    second = invoke("check")
    assert second.exit_code == 0
    assert "Blog: 1 new, 0 skipped" in second.output
    assert "- Brand new (" in second.output
    assert RECENT.strftime("%Y-%m-%d") in second.output
    assert "https://blog.example.com/new" in second.output
    assert "Old post" not in second.output.split("Blog:")[1]


@respx.mock
def test_check_source_option_limits_the_check():
    blog = respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed()))
    news = respx.get(NEWS).mock(return_value=httpx.Response(200, content=feed()))
    result = invoke("check", "--source", "Blog")
    assert result.exit_code == 0
    assert blog.called and not news.called
    assert "News" not in result.output


def test_check_unknown_source_is_a_usage_error():
    result = invoke("check", "--source", "Nope")
    assert result.exit_code == 2
    assert "Unknown source 'Nope'" in result.output
    assert "Blog, News" in result.output


def test_check_disabled_source_is_rejected(project):
    (project / "sources.yaml").write_text(
        f"sources:\n  - name: Blog\n    url: {BLOG}\n    enabled: false\n"
    )
    result = invoke("check", "--source", "Blog")
    assert result.exit_code == 2 and "disabled" in result.output


@respx.mock
def test_check_exit_code_is_1_only_if_all_sources_failed():
    respx.get(BLOG).mock(return_value=httpx.Response(403))
    news = respx.get(NEWS).mock(return_value=httpx.Response(403))
    result = invoke("check")
    assert result.exit_code == 1
    assert "Blog: ERROR" in result.output and "News: ERROR" in result.output
    assert "2 error(s)" in result.output

    news.mock(return_value=httpx.Response(200, content=feed()))
    partial = invoke("check")
    assert partial.exit_code == 0
    assert "Blog: ERROR" in partial.output and "News: baseline set" in partial.output


def test_missing_sources_file_is_a_config_error(project):
    (project / "sources.yaml").unlink()
    for args in (["check"], ["sources", "list"], ["sources", "baseline"]):
        result = invoke(*args)
        assert result.exit_code == 2, args
        assert "Sources file not found" in result.output


def test_invalid_sources_file_is_a_config_error(project):
    (project / "sources.yaml").write_text(
        f"sources:\n  - name: A\n    url: {BLOG}\n  - name: A\n    url: {NEWS}\n"
    )
    result = invoke("check")
    assert result.exit_code == 2 and "duplicate source name" in result.output


def test_sources_list_before_any_check():
    result = invoke("sources", "list")
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].split() == [
        "NAME",
        "TYPE",
        "STATE",
        "BASELINE",
        "LAST",
        "CHECK",
        "LAST",
        "SUCCESS",
        "ARTICLES",
    ]
    assert lines[1].split() == ["Blog", "rss", "enabled", "no", "never", "never", "-"]


@respx.mock
def test_sources_list_after_checks_shows_state_errors_and_removed_sources(project):
    respx.get(BLOG).mock(
        return_value=httpx.Response(200, content=feed(("A", "https://blog.example.com/a")))
    )
    respx.get(NEWS).mock(return_value=httpx.Response(403))
    invoke("check")
    # a source that was checked once and then removed from sources.yaml
    conn = db(project)
    repo.upsert_source(conn, "Gone", "https://gone.example.com/feed", "rss")
    conn.close()

    output = invoke("sources", "list").output
    blog = next(line for line in output.splitlines() if line.startswith("Blog")).split()
    assert blog[:3] == ["Blog", "rss", "enabled"]
    assert blog[3] != "no" and "baseline=1" in blog[-1]
    news = next(line for line in output.splitlines() if line.startswith("News"))
    assert " no " in news and "never" in news
    assert "Gone" in output and "removed" in output
    assert "last error - News:" in output and "403" in output


@respx.mock
def test_sources_list_marks_disabled_sources(project):
    (project / "sources.yaml").write_text(
        f"sources:\n  - name: Blog\n    url: {BLOG}\n    enabled: false\n"
    )
    assert "disabled" in invoke("sources", "list").output


@respx.mock
def test_sources_baseline_baselines_sources_without_baseline(project):
    respx.get(BLOG).mock(
        return_value=httpx.Response(200, content=feed(("A", "https://blog.example.com/a")))
    )
    respx.get(NEWS).mock(return_value=httpx.Response(200, content=feed()))
    result = invoke("sources", "baseline")
    assert result.exit_code == 0
    assert "Blog: baseline set (1 articles)" in result.output
    conn = db(project)
    assert repo.get_source(conn, "Blog")["baseline_at"]
    assert [a["status"] for a in repo.list_articles(conn)] == ["baseline"]

    again = invoke("sources", "baseline")
    assert "Blog: already baselined (use --reset to redo)" in again.output
    assert "News: already baselined" in again.output


@respx.mock
def test_sources_baseline_source_option(project):
    blog = respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed()))
    news = respx.get(NEWS).mock(return_value=httpx.Response(200, content=feed()))
    result = invoke("sources", "baseline", "--source", "News")
    assert result.exit_code == 0
    assert news.called and not blog.called


@respx.mock
def test_sources_baseline_reset_turns_pending_into_baseline(project):
    blog = respx.get(BLOG).mock(
        return_value=httpx.Response(200, content=feed(("A", "https://blog.example.com/a")))
    )
    respx.get(NEWS).mock(return_value=httpx.Response(200, content=feed()))
    invoke("check")
    blog.mock(
        return_value=httpx.Response(
            200,
            content=feed(("A", "https://blog.example.com/a"), ("B", "https://blog.example.com/b")),
        )
    )
    invoke("check")
    conn = db(project)
    pending = repo.list_articles(conn, "pending")
    assert [a["title"] for a in pending] == ["B"]
    repo.set_article_status(conn, pending[0]["id"], "pending")
    conn.close()

    blog.mock(
        return_value=httpx.Response(
            200,
            content=feed(
                ("A", "https://blog.example.com/a"),
                ("B", "https://blog.example.com/b"),
                ("C", "https://blog.example.com/c"),
            ),
        )
    )
    result = invoke("sources", "baseline", "--source", "Blog", "--reset")
    assert result.exit_code == 0
    assert "Blog: baseline set (1 articles, 2 already known)" in result.output
    conn = db(project)
    assert repo.list_articles(conn, "pending") == []
    assert {a["title"]: a["status"] for a in repo.list_articles(conn)} == {
        "A": "baseline",
        "B": "baseline",
        "C": "baseline",
    }
    assert repo.get_source(conn, "Blog")["baseline_at"]


@respx.mock
def test_sources_baseline_reset_keeps_processed_articles(project):
    respx.get(BLOG).mock(
        return_value=httpx.Response(200, content=feed(("A", "https://blog.example.com/a")))
    )
    respx.get(NEWS).mock(return_value=httpx.Response(200, content=feed()))
    invoke("check")
    conn = db(project)
    article = repo.list_articles(conn)[0]
    repo.set_article_status(conn, article["id"], "processed")
    conn.close()
    invoke("sources", "baseline", "--reset")
    conn = db(project)
    assert repo.get_article(conn, article["id"])["status"] == "processed"


@respx.mock
def test_sources_baseline_all_failed_exits_1():
    respx.get(BLOG).mock(return_value=httpx.Response(403))
    respx.get(NEWS).mock(return_value=httpx.Response(403))
    result = invoke("sources", "baseline")
    assert result.exit_code == 1
    assert "Blog: ERROR" in result.output
