import httpx
import pytest
import respx

from content2podcast import repository as repo
from content2podcast.config import AppConfig, SourceConfig
from content2podcast.db import connect
from content2podcast.http import make_client
from content2podcast.providers.llm.base import LLMError
from content2podcast.providers.llm.fake import FakeLLM
from content2podcast.sources.discovery import discover
from content2podcast.sources.llm_links import (
    Candidate,
    LinkSelection,
    LinkSelector,
    candidates_hash,
    collect_candidates,
    map_indexes,
    render_candidates,
)
from content2podcast.sources.models import SourceError

PAGE = "https://news.example.com/"

HTML = """
<html><body>
<nav><a href="/about-the-site-team">About the whole site team</a></nav>
<header><h1>Example News</h1></header>
<main>
  <h2>Top stories</h2>
  <a href="/2026/10/first-story-about-python">First story about Python</a>
  <a href="/2026/10/first-story-about-python/">First story about Python</a>
  <a href="/2026/10/second-story">Second story with a long title</a>
  <h2>Elsewhere</h2>
  <a href="https://other.example.org/post">An article on another site</a>
  <a href="/tag/python">Python</a>
  <a href="#comments">Jump to the comments</a>
  <a href="mailto:me@example.com">Mail the editors now</a>
  <a href="/">News home page overview</a>
  <a href="/images-only"><img src="x.png"></a>
</main>
<footer><a href="/imprint-and-privacy">Imprint and privacy policy</a></footer>
</body></html>
"""


def urls(candidates):
    return [c.url for c in candidates]


def test_candidates_are_filtered_deduplicated_and_have_context():
    candidates = collect_candidates(HTML, PAGE)
    assert urls(candidates) == [
        "https://news.example.com/2026/10/first-story-about-python",
        "https://news.example.com/2026/10/second-story",
    ]
    assert candidates[0].text == "First story about Python"
    assert candidates[0].context == "Top stories"


def test_candidates_other_sites_filters_and_cap():
    assert "https://other.example.org/post" in urls(collect_candidates(HTML, PAGE, same_site=False))
    assert urls(collect_candidates(HTML, PAGE, include=["second"])) == [
        "https://news.example.com/2026/10/second-story"
    ]
    assert urls(collect_candidates(HTML, PAGE, exclude=["second"])) == [
        "https://news.example.com/2026/10/first-story-about-python"
    ]
    assert len(collect_candidates(HTML, PAGE, max_candidates=1)) == 1


def test_candidate_text_and_context_are_clipped():
    long = "word " * 100
    html = f"<h2>{long}</h2><a href='/a'>{long}</a>"
    (candidate,) = collect_candidates(html, PAGE)
    assert len(candidate.text) <= 160 and candidate.text.endswith("…")
    assert len(candidate.context) <= 80


def test_heading_inside_the_link_is_not_its_context():
    html = "<a href='/story'><h3>A headline inside the link</h3></a>"
    (candidate,) = collect_candidates(html, PAGE)
    assert candidate.context is None


def test_render_shortens_own_site_addresses():
    candidates = [
        Candidate("https://news.example.com/a/b?x=1", "A long enough title", "Top"),
        Candidate("https://other.example.org/p", "Another long title"),
        Candidate("https://news.example.com", "Home like title"),
    ]
    text = render_candidates(candidates, PAGE)
    assert "[0] A long enough title | /a/b?x=1 | under: Top" in text
    assert "[1] Another long title | https://other.example.org/p" in text
    assert "[2] Home like title | /" in text
    assert PAGE in text


def test_map_indexes_ignores_invalid_and_keeps_page_order():
    c = [Candidate(f"https://x.test/{i}", f"title number {i}") for i in range(4)]
    assert urls(map_indexes(c, [3, 1, 1, 9, -1])) == ["https://x.test/1", "https://x.test/3"]
    assert map_indexes(c, []) == []


def test_hash_depends_on_candidates_only():
    a = [Candidate("https://x.test/1", "title number 1")]
    assert candidates_hash(a) == candidates_hash(list(a))
    assert candidates_hash(a) != candidates_hash([Candidate("https://x.test/1", "title changed")])


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "db.sqlite3")
    yield c
    c.close()


@pytest.fixture
def source_id(conn):
    return repo.upsert_source(conn, "News", PAGE, "html")


def selector(conn, llm):
    return LinkSelector(conn, lambda: llm)


def test_select_maps_indexes_and_caches(conn, source_id):
    llm = FakeLLM({"article_indexes": [1]})
    picker = selector(conn, llm)
    first = picker.select(source_id, HTML, PAGE)
    assert [(a.url, a.title) for a in first] == [
        ("https://news.example.com/2026/10/second-story", "Second story with a long title")
    ]
    assert len(llm.calls) == 1
    assert llm.calls[0].schema is LinkSelection
    assert "second-story" in llm.calls[0].user

    again = picker.select(source_id, HTML, PAGE)  # unchanged page: no second call
    assert again == first
    assert len(llm.calls) == 1


def test_changed_page_asks_again_and_replaces_the_cache(conn, source_id):
    llm = FakeLLM({"article_indexes": [0]})
    picker = selector(conn, llm)
    picker.select(source_id, HTML, PAGE)
    changed = HTML.replace("Second story with a long title", "A completely new headline")
    picker.select(source_id, changed, PAGE)
    assert len(llm.calls) == 2
    assert conn.execute("SELECT COUNT(*) FROM link_selections").fetchone()[0] == 1


def test_empty_selection_is_cached_too(conn, source_id):
    llm = FakeLLM({"article_indexes": []})
    picker = selector(conn, llm)
    assert picker.select(source_id, HTML, PAGE) == []
    assert picker.select(source_id, HTML, PAGE) == []
    assert len(llm.calls) == 1


def test_page_without_candidates_needs_no_llm(conn, source_id):
    llm = FakeLLM({"article_indexes": [0]})
    assert selector(conn, llm).select(source_id, "<p>nothing</p>", PAGE) == []
    assert llm.calls == []


def test_llm_error_becomes_a_source_error_and_is_not_cached(conn, source_id):
    picker = selector(conn, FakeLLM(error=LLMError("boom")))
    with pytest.raises(SourceError, match="boom"):
        picker.select(source_id, HTML, PAGE)
    assert conn.execute("SELECT COUNT(*) FROM link_selections").fetchone()[0] == 0


def test_llm_is_built_lazily_and_only_once(conn, source_id):
    built = []

    def factory():
        built.append(1)
        return FakeLLM({"article_indexes": [0]})

    picker = LinkSelector(conn, factory)
    picker.select(source_id, "<p>nothing</p>", PAGE)
    assert built == []
    picker.select(source_id, HTML, PAGE)
    picker.select(source_id, HTML.replace("Second", "Third"), PAGE)
    assert built == [1]


def test_factory_failure_becomes_a_source_error(conn, source_id):
    def factory():
        raise ValueError("No llm provider configured")

    with pytest.raises(SourceError, match="No llm provider"):
        LinkSelector(conn, factory).select(source_id, HTML, PAGE)


def test_cache_follows_the_source_row(conn, source_id):
    repo.set_link_selection(conn, source_id, "h", ["https://x.test/a"])
    assert repo.get_link_selection(conn, source_id, "h") == ["https://x.test/a"]
    assert repo.get_link_selection(conn, source_id, "other") is None
    conn.execute("DELETE FROM sources")
    assert conn.execute("SELECT COUNT(*) FROM link_selections").fetchone()[0] == 0


# --- through discovery -----------------------------------------------------------------------


@pytest.fixture
def http():
    with make_client() as c:
        yield c


def html_source(**kw):
    return SourceConfig(name="News", url=PAGE, type="html", **kw)


def check(conn, http, source, llm, *, default=False):
    return discover(
        conn,
        [source],
        http,
        link_selector=selector(conn, llm),
        llm_links_default=default,
    )


@respx.mock
def test_selector_less_source_uses_the_llm_when_enabled_globally(conn, http):
    respx.get(PAGE).mock(return_value=httpx.Response(200, html=HTML))
    llm = FakeLLM({"article_indexes": [0, 1]})
    check(conn, http, html_source(), llm, default=True)  # baseline
    assert len(llm.calls) == 1
    row = repo.get_source(conn, "News")
    assert row["baseline_at"] is not None
    count = conn.execute("SELECT COUNT(*) FROM articles WHERE status = 'baseline'").fetchone()[0]
    assert count == 2


@respx.mock
def test_per_source_flag_overrides_the_global_default(conn, http):
    respx.get(PAGE).mock(return_value=httpx.Response(200, html=HTML))
    llm = FakeLLM({"article_indexes": [0]})
    report = check(conn, http, html_source(llm_links=True), llm, default=False)
    assert report.errors == 0 and len(llm.calls) == 1

    llm_off = FakeLLM({"article_indexes": [0]})
    report = check(conn, http, html_source(llm_links=False), llm_off, default=True)
    assert llm_off.calls == []
    assert "selector required" in report.sources[0].error


@respx.mock
def test_selector_less_source_without_llm_is_a_clear_source_error(conn, http):
    respx.get(PAGE).mock(return_value=httpx.Response(200, html=HTML))
    llm = FakeLLM({"article_indexes": [0]})
    report = check(conn, http, html_source(), llm, default=False)
    assert llm.calls == []
    assert "selector required" in report.sources[0].error


@respx.mock
def test_a_selector_wins_over_the_llm(conn, http):
    respx.get(PAGE).mock(return_value=httpx.Response(200, html=HTML))
    llm = FakeLLM({"article_indexes": [0]})
    report = check(conn, http, html_source(selector="main h2 + a"), llm, default=True)
    assert report.errors == 0 and llm.calls == []


@respx.mock
def test_llm_failure_is_recorded_on_the_source(conn, http):
    respx.get(PAGE).mock(return_value=httpx.Response(200, html=HTML))
    report = check(conn, http, html_source(llm_links=True), FakeLLM(error=LLMError("down")))
    assert "down" in report.sources[0].error
    assert "down" in repo.get_source(conn, "News")["last_error"]


def test_config_defaults_and_overrides():
    config = AppConfig()
    assert config.link_extraction.enabled is False
    assert config.link_extraction.model is None
    assert html_source().llm_links is None
    cfg = AppConfig.model_validate({"link_extraction": {"enabled": True, "model": "mini"}})
    assert cfg.link_extraction.model == "mini"


def test_cli_builds_the_link_llm_with_the_configured_model(conn, monkeypatch):
    from types import SimpleNamespace

    from content2podcast import cli

    built = []
    monkeypatch.setattr(cli, "build_llm", lambda options, secrets: built.append(options) or "llm")
    config = AppConfig.model_validate(
        {
            "llm": {"provider": "azure_foundry", "model": "big"},
            "link_extraction": {"model": "small", "max_candidates": 7},
        }
    )
    ctx = SimpleNamespace(obj=SimpleNamespace(secrets=None))
    workspace = SimpleNamespace(config=config, conn=conn)
    picker = cli._link_selector(ctx, workspace)
    assert picker.max_candidates == 7
    assert picker._get_llm() == "llm"
    assert built[0].model == "small"
    assert config.llm.model == "big"  # the main provider is untouched
