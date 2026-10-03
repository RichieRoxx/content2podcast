import httpx
import pytest
import respx
import yaml
from typer.testing import CliRunner

from content2podcast.cli import app
from content2podcast.http import make_client
from content2podcast.sources.autodiscover import (
    discover_feeds,
    snippet,
    suggest_selectors,
)
from content2podcast.sources.models import SourceError

SITE = "https://blog.example.com"
FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title>
<item><title>One</title><link>https://blog.example.com/one</link></item>
<item><title>Two</title><link>https://blog.example.com/two</link></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
<entry><title>E</title><id>https://blog.example.com/e</id>
<link href="https://blog.example.com/e"/></entry></feed>"""


def page(head: str = "", body: str = "") -> str:
    return f"<html><head><title>My Blog | Example</title>{head}</head><body>{body}</body></html>"


@pytest.fixture
def http():
    with make_client() as c:
        yield c


def no_feed_paths():
    for path in ("/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml"):
        respx.get(SITE + path).mock(return_value=httpx.Response(404))


@respx.mock
def test_link_alternate_tags_are_found_resolved_and_verified(http):
    head = (
        '<link rel="alternate" type="application/rss+xml" title="Posts" href="/posts.xml">'
        '<link rel="alternate" type="application/atom+xml" href="https://blog.example.com/a.atom">'
        '<link rel="alternate" type="application/rss+xml" href="/posts.xml">'  # duplicate
        '<link rel="alternate" type="application/rss+xml" href="/broken.xml">'
        '<link rel="alternate" type="application/feed+json" href="/feed.json">'
        '<link rel="stylesheet" href="/style.css">'
    )
    respx.get(SITE + "/").mock(return_value=httpx.Response(200, html=page(head)))
    respx.get(SITE + "/posts.xml").mock(return_value=httpx.Response(200, content=FEED))
    respx.get(SITE + "/a.atom").mock(return_value=httpx.Response(200, content=ATOM))
    respx.get(SITE + "/broken.xml").mock(return_value=httpx.Response(200, html="<p>no</p>"))
    result = discover_feeds(http, SITE + "/")
    assert [(f.url, f.title, f.via, f.articles) for f in result.feeds] == [
        (SITE + "/posts.xml", "Posts", "link", 2),
        (SITE + "/a.atom", None, "link", 1),
    ]
    assert result.name == "My Blog"
    assert result.selectors == []


@respx.mock
def test_common_paths_are_probed_when_there_are_no_link_tags(http):
    respx.get(SITE + "/").mock(return_value=httpx.Response(200, html=page()))
    no_feed_paths()
    respx.get(SITE + "/atom.xml").mock(return_value=httpx.Response(200, content=ATOM))
    respx.get(SITE + "/rss").mock(return_value=httpx.Response(200, html="<html>not a feed</html>"))
    result = discover_feeds(http, SITE + "/")
    assert [(f.url, f.via) for f in result.feeds] == [(SITE + "/atom.xml", "path")]


@respx.mock
def test_paths_are_not_probed_when_link_tags_exist(http):
    head = '<link rel="alternate" type="application/rss+xml" href="/posts.xml">'
    respx.get(SITE + "/").mock(return_value=httpx.Response(200, html=page(head)))
    respx.get(SITE + "/posts.xml").mock(return_value=httpx.Response(200, content=FEED))
    result = discover_feeds(http, SITE + "/")
    assert len(respx.calls) == 2 and result.feeds[0].via == "link"


@respx.mock
def test_probe_paths_use_the_site_root_and_can_be_disabled(http):
    respx.get(SITE + "/blog/").mock(return_value=httpx.Response(200, html=page()))
    respx.get(SITE + "/feed").mock(return_value=httpx.Response(200, content=FEED))
    for path in ("/feed/", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml"):
        respx.get(SITE + path).mock(return_value=httpx.Response(404))
    assert [f.url for f in discover_feeds(http, SITE + "/blog/").feeds] == [SITE + "/feed"]
    assert discover_feeds(http, SITE + "/blog/", probe_paths=False).feeds == []


@respx.mock
def test_the_url_may_already_be_a_feed(http):
    respx.get(SITE + "/feed.xml").mock(return_value=httpx.Response(200, content=FEED))
    result = discover_feeds(http, SITE + "/feed.xml")
    assert [(f.url, f.via, f.articles) for f in result.feeds] == [(SITE + "/feed.xml", "page", 2)]


@respx.mock
def test_unreachable_page_is_a_source_error(http):
    respx.get(SITE + "/").mock(return_value=httpx.Response(404))
    with pytest.raises(SourceError):
        discover_feeds(http, SITE + "/")


LISTING = """
<nav><a class="menu" href="/about-us-page">About us and the team</a>
<a class="menu" href="/contact-page">Contact us any time</a></nav>
<main>
<div class="post"><h2 class="title"><a href="/p/1">First article of the day</a></h2></div>
<div class="post"><h2 class="title"><a href="/p/2">Second article of the day</a></h2></div>
<div class="post"><h2 class="title"><a href="/p/3">Third article of the day</a></h2></div>
<div class="post"><h2 class="title"><a href="/p/4">Fourth article of the day</a></h2></div>
<p><a href="/archive-page">Browse the whole archive here</a></p>
</main>
"""


def test_selector_suggestions_pick_the_repeated_article_links():
    suggestions = suggest_selectors(page(body=LISTING), SITE + "/")
    assert suggestions
    best = suggestions[0]
    assert best.links == 4
    assert best.selector in ("h2.title a", "div.post a")
    assert best.examples == ("First article of the day", "Second article of the day")


def test_no_selector_for_a_page_without_repeated_links():
    html = page(body='<a href="/only-one-link-here">Just a single long link</a>')
    assert suggest_selectors(html, SITE + "/") == []


@respx.mock
def test_without_a_feed_selectors_are_suggested(http):
    respx.get(SITE + "/").mock(return_value=httpx.Response(200, html=page(body=LISTING)))
    no_feed_paths()
    result = discover_feeds(http, SITE + "/")
    assert result.feeds == [] and result.selectors


def test_snippet_for_a_feed_is_valid_yaml_and_quotes_names():
    from content2podcast.sources.autodiscover import DiscoveryResult, FeedCandidate

    result = DiscoveryResult(SITE, "Blog: the best", [FeedCandidate(SITE + "/f", None, "link", 2)])
    text = snippet(result)
    assert yaml.safe_load("sources:\n" + text) == {
        "sources": [{"name": "Blog: the best", "url": SITE + "/f", "type": "rss"}]
    }


@respx.mock
def test_snippet_for_selectors_and_nothing(http):
    from content2podcast.sources.autodiscover import DiscoveryResult

    respx.get(SITE + "/").mock(return_value=httpx.Response(200, html=page(body=LISTING)))
    no_feed_paths()
    loaded = yaml.safe_load("sources:\n" + snippet(discover_feeds(http, SITE + "/")))
    (entry,) = loaded["sources"]
    assert entry["type"] == "html" and entry["url"] == SITE + "/" and entry["selector"]
    assert snippet(DiscoveryResult(SITE, "x")) is None


# --- the command -----------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@respx.mock
def test_command_prints_feeds_and_a_snippet(runner, tmp_path):
    head = '<link rel="alternate" type="application/rss+xml" title="Posts" href="/posts.xml">'
    respx.get(SITE + "/").mock(return_value=httpx.Response(200, html=page(head)))
    respx.get(SITE + "/posts.xml").mock(return_value=httpx.Response(200, content=FEED))
    result = runner.invoke(
        app, ["--config", str(tmp_path / "missing.yaml"), "sources", "discover", SITE + "/"]
    )
    assert result.exit_code == 0, result.output
    assert f"{SITE}/posts.xml (2 entries, via link) - Posts" in result.output
    assert "Add to sources.yaml:" in result.output
    assert "type: rss" in result.output


@respx.mock
def test_command_suggests_selectors_without_a_feed(runner, tmp_path):
    respx.get(SITE + "/").mock(return_value=httpx.Response(200, html=page(body=LISTING)))
    no_feed_paths()
    result = runner.invoke(
        app, ["--config", str(tmp_path / "missing.yaml"), "sources", "discover", SITE + "/"]
    )
    assert result.exit_code == 0, result.output
    assert "No feed found" in result.output and "type: html" in result.output


@respx.mock
def test_command_fails_for_an_unreachable_page_and_empty_pages(runner, tmp_path):
    cfg = ["--config", str(tmp_path / "missing.yaml")]
    respx.get(SITE + "/").mock(return_value=httpx.Response(404))
    assert runner.invoke(app, [*cfg, "sources", "discover", SITE + "/"]).exit_code == 1
    respx.get(SITE + "/").mock(return_value=httpx.Response(200, html=page()))
    no_feed_paths()
    result = runner.invoke(app, [*cfg, "sources", "discover", SITE + "/"])
    assert result.exit_code == 1 and "No feed and no article links" in result.output
