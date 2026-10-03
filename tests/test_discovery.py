import logging
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest
import respx

from content2podcast import repository as repo
from content2podcast.config import SourceConfig
from content2podcast.db import connect
from content2podcast.http import make_client
from content2podcast.sources.discovery import discover

NOW = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
FRESH = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
OLD = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

BLOG = "https://blog.example.com/feed.xml"
NEWS = "https://news.example.org/feed.xml"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "db.sqlite3")
    yield c
    c.close()


@pytest.fixture
def http():
    with make_client() as c:
        yield c


def feed(*items: tuple[str, datetime | None]) -> bytes:
    body = "".join(
        f"<item><title>{url.rsplit('/', 1)[-1]}</title><link>{url}</link>"
        + (f"<pubDate>{format_datetime(date)}</pubDate>" if date else "")
        + "</item>"
        for url, date in items
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'
    ).encode()


def rss(name="Blog", url=BLOG, **kwargs) -> SourceConfig:
    return SourceConfig(name=name, url=url, type="rss", **kwargs)


def run(conn, http, sources, **kwargs):
    return discover(conn, sources, http, now=lambda: NOW, **kwargs)


def statuses(conn):
    return {r["url"]: r["status"] for r in repo.list_articles(conn)}


A1 = "https://blog.example.com/a1"
A2 = "https://blog.example.com/a2"


@respx.mock
def test_first_check_is_baseline_only(conn, http):
    respx.get(BLOG).mock(
        return_value=httpx.Response(
            200, content=feed((A1, FRESH), (A2, OLD), ("https://blog.example.com/a3", None))
        )
    )
    report = run(conn, http, [rss()])
    assert report.sources[0].baseline == 3
    assert (report.new, report.skipped, report.errors) == (0, 0, 0)
    assert set(statuses(conn).values()) == {"baseline"}  # even old and undated ones
    source = repo.get_source(conn, "Blog")
    assert source["baseline_at"] and source["last_success_at"] and source["last_error"] is None


@respx.mock
def test_next_check_with_new_item_yields_one_pending(conn, http):
    route = respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed((A1, FRESH))))
    run(conn, http, [rss()])
    route.mock(return_value=httpx.Response(200, content=feed((A1, FRESH), (A2, FRESH))))
    report = run(conn, http, [rss()])
    assert (report.new, report.baseline, report.sources[0].known) == (1, 0, 1)
    assert statuses(conn) == {A1: "baseline", A2: "pending"}


@respx.mock
def test_rerun_without_changes_adds_nothing(conn, http):
    respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed((A1, FRESH))))
    run(conn, http, [rss()])
    report = run(conn, http, [rss()])
    assert (report.new, report.baseline, report.skipped) == (0, 0, 0)
    assert len(repo.list_articles(conn)) == 1


@respx.mock
def test_same_article_in_two_sources_is_one_row(conn, http):
    respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed((A1, FRESH))))
    news_route = respx.get(NEWS).mock(return_value=httpx.Response(200, content=feed()))
    sources = [rss(), rss("News", NEWS)]
    run(conn, http, sources)  # both baselined (News empty)
    shared = "https://www.blog.example.com/a9/?utm_source=news"
    respx.get(BLOG).mock(
        return_value=httpx.Response(
            200, content=feed((A1, FRESH), ("https://blog.example.com/a9", FRESH))
        )
    )
    news_route.mock(return_value=httpx.Response(200, content=feed((shared, FRESH))))
    report = run(conn, http, sources)
    assert len(repo.list_articles(conn, "pending")) == 1
    assert report.new == 1
    news = next(s for s in report.sources if s.name == "News")
    assert (news.new, news.known) == (0, 1)


@respx.mock
def test_old_article_is_skipped(conn, http):
    route = respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed((A1, FRESH))))
    run(conn, http, [rss()])
    route.mock(
        return_value=httpx.Response(
            200, content=feed((A1, FRESH), (A2, OLD), ("https://blog.example.com/a3", None))
        )
    )
    report = run(conn, http, [rss()], max_article_age_days=7)
    assert report.skipped == 1 and report.new == 1
    assert statuses(conn) == {
        A1: "baseline",
        A2: "skipped",
        "https://blog.example.com/a3": "pending",
    }


@respx.mock
def test_age_limit_is_configurable(conn, http):
    route = respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed()))
    run(conn, http, [rss()])
    route.mock(
        return_value=httpx.Response(200, content=feed((A1, datetime(2026, 9, 20, tzinfo=UTC))))
    )
    run(conn, http, [rss()], max_article_age_days=30)
    assert statuses(conn) == {A1: "pending"}


@respx.mock
def test_failing_source_does_not_stop_others_and_sets_no_baseline(conn, http, caplog):
    respx.get(BLOG).mock(return_value=httpx.Response(404))
    respx.get(NEWS).mock(
        return_value=httpx.Response(200, content=feed(("https://news.example.org/n1", FRESH)))
    )
    with caplog.at_level(logging.ERROR):
        report = run(conn, http, [rss(), rss("News", NEWS)])
    broken, working = report.sources
    assert broken.error and "404" in broken.error
    assert working.error is None and working.baseline == 1
    assert report.errors == 1
    blog = repo.get_source(conn, "Blog")
    assert blog["baseline_at"] is None
    assert blog["last_checked_at"] and blog["last_success_at"] is None
    assert "404" in blog["last_error"]
    assert repo.get_source(conn, "News")["baseline_at"]
    assert any("Blog" in r.message for r in caplog.records)  # logged with the source name


@respx.mock
def test_unexpected_exception_is_isolated(conn, http):
    respx.get(BLOG).mock(side_effect=RuntimeError("boom"))
    respx.get(NEWS).mock(return_value=httpx.Response(200, content=feed()))
    report = run(conn, http, [rss(), rss("News", NEWS)])
    assert "RuntimeError: boom" in report.sources[0].error
    assert report.sources[1].error is None


@respx.mock
def test_baseline_is_set_on_first_successful_check(conn, http):
    route = respx.get(BLOG).mock(return_value=httpx.Response(403))
    run(conn, http, [rss()])  # first check fails (403 is not retried, keeps the test fast)
    assert repo.get_source(conn, "Blog")["baseline_at"] is None
    route.mock(return_value=httpx.Response(200, content=feed((A1, FRESH))))
    report = run(conn, http, [rss()])
    assert report.sources[0].baseline == 1 and report.new == 0
    assert statuses(conn) == {A1: "baseline"}
    assert repo.get_source(conn, "Blog")["last_error"] is None


@respx.mock
def test_not_modified_uses_stored_validators(conn, http):
    route = respx.get(BLOG).mock(
        return_value=httpx.Response(
            200,
            content=feed((A1, FRESH)),
            headers={"ETag": '"v1"', "Last-Modified": "Fri, 02 Oct 2026 12:00:00 GMT"},
        )
    )
    run(conn, http, [rss()])
    assert repo.get_source(conn, "Blog")["etag"] == '"v1"'
    route.mock(return_value=httpx.Response(304))
    report = run(conn, http, [rss()])
    assert report.sources[0].not_modified and report.new == 0
    assert route.calls.last.request.headers["if-none-match"] == '"v1"'
    source = repo.get_source(conn, "Blog")
    assert source["etag"] == '"v1"'  # validators survive a 304
    assert source["last_success_at"]


@respx.mock
def test_no_validators_sent_before_baseline_exists(conn, http):
    repo.upsert_source(conn, "Blog", BLOG, "rss")
    conn.execute("UPDATE sources SET etag = '\"stale\"' WHERE name = 'Blog'")
    conn.commit()
    route = respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed((A1, FRESH))))
    run(conn, http, [rss()])
    assert "if-none-match" not in route.calls[0].request.headers


@respx.mock
def test_removed_sources_keep_history_and_are_not_fetched(conn, http):
    respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed((A1, FRESH))))
    news = respx.get(NEWS).mock(return_value=httpx.Response(200, content=feed()))
    run(conn, http, [rss(), rss("News", NEWS)])
    fetched_before = news.call_count
    run(conn, http, [rss()])  # News removed from the configuration
    assert news.call_count == fetched_before
    assert {s["name"] for s in repo.list_sources(conn)} == {"Blog", "News"}
    assert len(repo.list_articles(conn)) == 1


@respx.mock
def test_source_url_and_type_are_updated(conn, http):
    respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed()))
    run(conn, http, [rss()])
    new_url = "https://blog.example.com/new-feed.xml"
    respx.get(new_url).mock(return_value=httpx.Response(200, content=feed()))
    run(conn, http, [rss(url=new_url)])
    assert repo.get_source(conn, "Blog")["url"] == new_url


@respx.mock
def test_disabled_source_is_synced_but_not_fetched(conn, http):
    route = respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed((A1, FRESH))))
    report = run(conn, http, [rss(enabled=False)])
    assert not route.called and report.sources == []
    assert repo.get_source(conn, "Blog") is not None


@respx.mock
def test_include_exclude_apply_to_rss_entries(conn, http):
    route = respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed()))
    source = rss(include=[r"/posts/"], exclude=[r"/posts/skip"])
    run(conn, http, [source])
    route.mock(
        return_value=httpx.Response(
            200,
            content=feed(
                ("https://blog.example.com/posts/keep", FRESH),
                ("https://blog.example.com/posts/skip-me", FRESH),
                ("https://blog.example.com/other", FRESH),
            ),
        )
    )
    run(conn, http, [source])
    assert statuses(conn) == {"https://blog.example.com/posts/keep": "pending"}


@respx.mock
def test_html_source_is_discovered(conn, http):
    page = "https://html.example.com/news/"
    html = '<a class="p" href="/news/1">One</a><a class="p" href="/news/2">Two</a>'
    route = respx.get(page).mock(return_value=httpx.Response(200, text=html))
    source = SourceConfig(name="Html", url=page, type="html", selector="a.p")
    first = run(conn, http, [source])
    assert first.baseline == 2
    route.mock(
        return_value=httpx.Response(200, text=html + '<a class="p" href="/news/3">Three</a>')
    )
    second = run(conn, http, [source])
    assert second.new == 1
    assert statuses(conn)["https://html.example.com/news/3"] == "pending"
    assert repo.list_articles(conn, "pending")[0]["title"] == "Three"


@respx.mock
def test_html_source_without_selector_is_an_isolated_error(conn, http):
    source = SourceConfig(name="Html", url="https://html.example.com/", type="html")
    report = run(conn, http, [source])
    assert "selector required" in report.sources[0].error
    assert repo.get_source(conn, "Html")["baseline_at"] is None


@respx.mock
def test_article_fields_are_stored(conn, http):
    route = respx.get(BLOG).mock(return_value=httpx.Response(200, content=feed()))
    run(conn, http, [rss()])
    route.mock(return_value=httpx.Response(200, content=feed((A1, FRESH))))
    run(conn, http, [rss()])
    row = repo.list_articles(conn)[0]
    assert (row["url"], row["title"], row["published_at"]) == (A1, "a1", "2026-10-02T12:00:00Z")
    assert row["discovered_at"] and row["source_id"] == repo.get_source(conn, "Blog")["id"]
