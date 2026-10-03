import logging
from pathlib import Path

import httpx
import pytest
import respx

from content2podcast.config import SourceConfig
from content2podcast.http import make_client
from content2podcast.sources.filters import url_allowed
from content2podcast.sources.html import extract_links, fetch_html
from content2podcast.sources.models import SourceError

FIXTURES = Path(__file__).parent / "fixtures" / "html"
PAGE = "https://example.com/blog/"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def client():
    with make_client() as c:
        yield c


def urls(articles):
    return [a.url for a in articles]


# --- selector on anchors -----------------------------------------------------------------


def test_selector_on_anchors_in_page_order_deduped_and_filtered():
    articles = extract_links(load("anchors.html"), PAGE, "a.post")
    assert urls(articles) == [
        "https://example.com/posts/one",
        "https://example.com/blog/posts/two?utm_source=home",  # original URL kept for fetching
        "https://example.com/posts/three",  # protocol-relative
        "https://blog.example.com/posts/four",  # subdomain of the page's site
        "https://www.example.com/posts/six",  # www is ignored for the site check
    ]
    # duplicates (fragment / tracking variants) collapsed, external site, #top, javascript:,
    # mailto:, empty and missing hrefs dropped


def test_titles_from_text_or_title_attribute():
    titles = [a.title for a in extract_links(load("anchors.html"), PAGE, "a.post")]
    assert titles == ["First post", "Second post", None, "Fourth (title attr)", "Six"]


def test_same_site_can_be_disabled():
    articles = extract_links(load("anchors.html"), PAGE, "a.post", same_site=False)
    assert "https://other.example.net/posts/five" in urls(articles)


def test_unrelated_links_outside_selector_are_ignored():
    assert not any("/about" in u for u in urls(extract_links(load("anchors.html"), PAGE, "a.post")))


# --- selector on containers --------------------------------------------------------------


def test_selector_on_containers_takes_first_link_inside():
    articles = extract_links(load("containers.html"), PAGE, "article.item")
    assert urls(articles) == [
        "https://example.com/news/a",  # first link, not the "comments" link
        "https://example.com/news/b",
        "https://example.com/ads/x",
    ]
    assert articles[0].title == "Story A"  # container without any link is skipped


def test_descendant_selector_matching_anchors_directly():
    assert urls(extract_links(load("containers.html"), PAGE, "article h2 a")) == [
        "https://example.com/news/a",
        "https://example.com/news/b",
    ]


# --- include / exclude -------------------------------------------------------------------


def test_include_and_exclude_apply_to_absolute_url():
    articles = extract_links(load("containers.html"), PAGE, "article.item", exclude=[r"/ads/"])
    assert urls(articles) == ["https://example.com/news/a", "https://example.com/news/b"]
    articles = extract_links(load("containers.html"), PAGE, "article.item", include=[r"/news/b$"])
    assert urls(articles) == ["https://example.com/news/b"]
    articles = extract_links(
        load("containers.html"), PAGE, "article.item", include=[r"/news/"], exclude=[r"/news/a"]
    )
    assert urls(articles) == ["https://example.com/news/b"]


@pytest.mark.parametrize(
    ("include", "exclude", "expected"),
    [
        ([], [], True),
        (["news"], [], True),
        (["blog"], [], False),
        (["news", "blog"], [], True),  # any include pattern is enough
        ([], ["news"], False),
        (["news"], ["a$"], False),  # exclude wins
        ([], ["^$"], True),
    ],
)
def test_url_allowed(include, exclude, expected):
    assert url_allowed("https://example.com/news/a", include, exclude) is expected


# --- errors and warnings -----------------------------------------------------------------


def test_invalid_selector_raises_source_error():
    with pytest.raises(SourceError, match="Invalid CSS selector"):
        extract_links(load("anchors.html"), PAGE, "a[")


URL = "https://example.com/blog/"


@respx.mock
def test_missing_selector_is_a_clear_error(client):
    for selector in (None, "", "   "):
        with pytest.raises(SourceError, match="selector required"):
            fetch_html(client, URL, selector=selector)
    assert not respx.calls  # nothing was fetched


@respx.mock
def test_no_matches_warns_and_returns_empty(client, caplog):
    respx.get(URL).mock(return_value=httpx.Response(200, content=load("anchors.html")))
    with caplog.at_level(logging.WARNING):
        result = fetch_html(client, URL, selector="div.does-not-exist")
    assert result.articles == []
    assert any("No article links found" in r.message for r in caplog.records)


# --- fetching ----------------------------------------------------------------------------


@respx.mock
def test_fetch_resolves_relative_links_against_final_url(client):
    respx.get(URL).mock(
        return_value=httpx.Response(301, headers={"Location": "https://example.com/news/"})
    )
    respx.get("https://example.com/news/").mock(
        return_value=httpx.Response(200, content=load("containers.html"))
    )
    result = fetch_html(client, URL, selector="article h2 a")
    assert urls(result.articles) == ["https://example.com/news/a", "https://example.com/news/b"]
    assert not result.not_modified


@respx.mock
def test_fetch_honours_charset_header(client):
    body = '<html><body><a class="p" href="/x">Grüße</a></body></html>'.encode("iso-8859-1")
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, content=body, headers={"Content-Type": "text/html; charset=iso-8859-1"}
        )
    )
    assert fetch_html(client, URL, selector="a.p").articles[0].title == "Grüße"


@respx.mock
def test_fetch_passes_filters_through(client):
    respx.get(URL).mock(return_value=httpx.Response(200, content=load("containers.html")))
    result = fetch_html(client, URL, selector="article.item", exclude=["/ads/"])
    assert urls(result.articles) == ["https://example.com/news/a", "https://example.com/news/b"]


# --- config ------------------------------------------------------------------------------


def test_source_config_same_site_defaults_to_true():
    assert SourceConfig(name="n", url="https://x", type="html", selector="a").same_site is True
