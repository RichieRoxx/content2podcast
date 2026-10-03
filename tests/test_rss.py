import logging
from pathlib import Path

import httpx
import pytest
import respx

from content2podcast.http import make_client
from content2podcast.sources.models import SourceError
from content2podcast.sources.rss import fetch_rss, parse_feed
from content2podcast.sources.text import strip_html

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def client():
    with make_client() as c:
        yield c


def test_rss2_entries_mapped():
    first, second = parse_feed(load("rss2.xml"), "https://example.com/feed.xml")
    assert first.url == "https://example.com/posts/1?utm_source=rss"  # original kept
    assert first.title == "First post"
    assert first.summary == "Hello world & friends."  # tags, script removed; entities decoded
    assert first.published_at == "2026-09-29T04:30:00Z"  # converted to UTC
    assert (second.url, second.summary) == ("https://example.com/posts/2", "Plain summary")
    assert second.published_at == "2026-09-28T12:00:00Z"


def test_atom_entries_mapped():
    articles = parse_feed(load("atom.xml"), "https://atom.example.org/feed")
    assert [a.url for a in articles] == [
        "https://atom.example.org/one",  # alternate link, not "replies"
        "https://atom.example.org/two",  # falls back to a URL-shaped id
    ]  # the entry with a urn: id and no link is skipped
    one, two = articles
    assert one.title == "Atom <one>"
    assert one.summary == "Short text"
    assert one.published_at == "2026-09-28T08:15:00Z"  # published preferred over updated
    assert two.summary == "From content"  # content used when there is no summary
    assert two.published_at == "2026-09-27T08:15:00Z"  # updated as fallback


def test_relative_links_resolved_against_feed_url():
    urls = [
        a.url for a in parse_feed(load("relative_links.xml"), "https://rel.example.com/blog/feed")
    ]
    assert urls == [
        "https://rel.example.com/posts/a",
        "https://rel.example.com/blog/posts/b",
        "https://other.example.net/c",
    ]


def test_missing_and_invalid_dates_tolerated():
    articles = parse_feed(load("no_dates.xml"), "https://nodate.example.com/feed")
    assert len(articles) == 3
    assert all(a.published_at is None for a in articles)
    assert articles[2].title is None


def test_malformed_but_parseable_feed_warns_and_continues(caplog):
    with caplog.at_level(logging.WARNING):
        articles = parse_feed(load("malformed.xml"), "https://bad.example.com/feed")
    assert [a.url for a in articles] == ["https://bad.example.com/a", "https://bad.example.com/b"]
    assert any("malformed but parseable" in r.message for r in caplog.records)


def test_not_a_feed_raises():
    html = b"<!doctype html><html><body><h1>Not found</h1></body></html>"
    with pytest.raises(SourceError, match="not a readable feed"):
        parse_feed(html, "https://example.com/feed")


def test_empty_valid_feed_is_fine():
    empty = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
    assert parse_feed(empty, "https://example.com/feed") == []


def test_non_http_links_skipped():
    feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        b"<item><title>m</title><link>mailto:a@example.com</link></item>"
        b"<item><title>ok</title><link>https://example.com/ok</link></item>"
        b"</channel></rss>"
    )
    assert [a.url for a in parse_feed(feed, "https://example.com/feed")] == [
        "https://example.com/ok"
    ]


FEED_URL = "https://example.com/feed.xml"


@respx.mock
def test_fetch_returns_articles_and_validators(client):
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            content=load("rss2.xml"),
            headers={"ETag": '"v1"', "Last-Modified": "Mon, 28 Sep 2026 12:00:00 GMT"},
        )
    )
    result = fetch_rss(client, FEED_URL)
    assert not result.not_modified
    assert len(result.articles) == 2
    assert result.etag == '"v1"'
    assert result.last_modified == "Mon, 28 Sep 2026 12:00:00 GMT"


@respx.mock
def test_first_fetch_sends_no_validators(client):
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=load("rss2.xml")))
    result = fetch_rss(client, FEED_URL)
    request = route.calls[0].request
    assert "if-none-match" not in request.headers
    assert "if-modified-since" not in request.headers
    assert (result.etag, result.last_modified) == (None, None)


@respx.mock
def test_conditional_get_sends_stored_validators_and_handles_304(client):
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(304))
    result = fetch_rss(client, FEED_URL, etag='"v1"', last_modified="Mon, 28 Sep 2026 12:00:00 GMT")
    request = route.calls[0].request
    assert request.headers["if-none-match"] == '"v1"'
    assert request.headers["if-modified-since"] == "Mon, 28 Sep 2026 12:00:00 GMT"
    assert result.not_modified and result.articles == []
    assert result.etag == '"v1"'  # stored validators stay valid
    assert result.last_modified == "Mon, 28 Sep 2026 12:00:00 GMT"


@respx.mock
def test_relative_links_use_final_url_after_redirect(client):
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(301, headers={"Location": "https://new.example.com/blog/feed"})
    )
    respx.get("https://new.example.com/blog/feed").mock(
        return_value=httpx.Response(200, content=load("relative_links.xml"))
    )
    urls = [a.url for a in fetch_rss(client, FEED_URL).articles]
    assert urls[0] == "https://new.example.com/posts/a"


@respx.mock
def test_declared_charset_header_is_honoured(client):
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        "<item><title>Grüße</title><link>https://example.com/g</link></item></channel></rss>"
    ).encode("iso-8859-1")
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200, content=body, headers={"Content-Type": "application/rss+xml; charset=iso-8859-1"}
        )
    )
    assert fetch_rss(client, FEED_URL).articles[0].title == "Grüße"


@respx.mock
def test_http_errors_propagate(client):
    from content2podcast.http import HttpError

    respx.get(FEED_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(HttpError):
        fetch_rss(client, FEED_URL)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<p>a</p><p>b</p>", "a b"),
        ("x<br>y", "x y"),
        ("&lt;tag&gt; &amp; &quot;q&quot;", '<tag> & "q"'),
        ("<style>p{}</style>text<script>1</script>", "text"),
        ("  <b> </b> ", None),
        ("", None),
        (None, None),
    ],
)
def test_strip_html(raw, expected):
    assert strip_html(raw) == expected


@respx.mock
def test_feed_served_as_text_html_is_still_parsed_without_warning(client, caplog):
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200, content=load("rss2.xml"), headers={"Content-Type": "text/html"}
        )
    )
    with caplog.at_level(logging.WARNING):
        result = fetch_rss(client, FEED_URL)
    assert len(result.articles) == 2
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def feed_xml(entry: str) -> bytes:
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        f"<link>https://example.com/</link>{entry}</channel></rss>"
    ).encode()


def test_entry_url_fallbacks():
    xml = feed_xml(
        "<item><title>relative</title><link>/posts/1</link></item>"
        "<item><title>guid url</title><guid>https://example.com/posts/2</guid></item>"
        "<item><title>guid not a url</title><guid isPermaLink='false'>abc-123</guid></item>"
        "<item><title>ftp</title><link>ftp://example.com/x</link></item>"
        "<item><title>none</title></item>"
    )
    urls = [a.url for a in parse_feed(xml, "https://example.com/feed.xml")]
    assert urls == ["https://example.com/posts/1", "https://example.com/posts/2"]


def test_summary_falls_back_to_content():
    atom = (
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>T</title>'
        b"<entry><title>E</title><id>https://example.com/e</id>"
        b'<link href="https://example.com/e"/><updated>2024-01-01T00:00:00Z</updated>'
        b'<content type="html">&lt;p&gt;Body &lt;b&gt;text&lt;/b&gt;&lt;/p&gt;</content>'
        b"</entry></feed>"
    )
    (article,) = parse_feed(atom, "https://example.com/feed.xml")
    assert article.summary == "Body text"
    assert article.published_at == "2024-01-01T00:00:00Z"


def test_unrepresentable_dates_are_ignored():
    import time

    from content2podcast.sources.rss import _published_at

    assert (
        _published_at({"published_parsed": time.struct_time((99999, 1, 1, 0, 0, 0, 0, 1, 0))})
        is None
    )
