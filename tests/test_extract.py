from pathlib import Path

import httpx
import pytest
import respx

from content2podcast import repository as repo
from content2podcast.db import connect
from content2podcast.extract import ensure_content, extract_text
from content2podcast.http import make_client

FIXTURES = Path(__file__).parent / "fixtures" / "articles"
URL = "https://blog.example.com/posts/python-speed"
LONG_SUMMARY = "Zusammenfassung aus dem Feed. " * 4  # 120 characters


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "db.sqlite3")
    yield c
    c.close()


@pytest.fixture
def http():
    with make_client() as c:
        yield c


def add_article(conn, summary=None, url=URL):
    source = repo.upsert_source(conn, "Blog", "https://blog.example.com/feed", "rss")
    article_id = repo.insert_article_if_new(conn, source, url, title="T", feed_summary=summary)
    return article_id


def article(conn, article_id):
    return repo.get_article(conn, article_id)


# --- extract_text ------------------------------------------------------------------------


def test_extracts_article_body_without_boilerplate():
    text = extract_text(fixture("article.html"), URL)
    assert "Wer vor ein paar Jahren ein größeres Python-Projekt" in text
    assert "Am Ende zählt, dass Entwicklerinnen weniger warten" in text
    assert text.count("\n") >= 3  # paragraphs are kept
    for boilerplate in (
        "Impressum",
        "Cookie-Einstellungen",
        "Danke für den Artikel",
        "Mehr aus dem Blog",
    ):
        assert boilerplate not in text


def test_extract_text_accepts_str_and_returns_none_for_nothing():
    assert "Python-Projekt" in extract_text(fixture("article.html").decode("utf-8"), URL)
    assert extract_text("<html><body></body></html>", URL) is None
    assert extract_text(b"", URL) is None


# --- ensure_content ----------------------------------------------------------------------


@respx.mock
def test_fetches_extracts_and_stores_content(conn, http):
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=fixture("article.html")))
    aid = add_article(conn)
    result = ensure_content(conn, http, article(conn, aid))
    assert result.origin == "extracted" and result.status == "pending"
    assert "Python-Projekt" in result.text
    row = article(conn, aid)
    assert row["content"] == result.text and row["extracted_at"]
    assert row["attempts"] == 0 and row["last_error"] is None
    assert route.call_count == 1


@respx.mock
def test_cached_content_is_never_refetched(conn, http):
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=fixture("article.html")))
    aid = add_article(conn)
    first = ensure_content(conn, http, article(conn, aid))
    second = ensure_content(conn, http, article(conn, aid))
    assert route.call_count == 1
    assert (second.origin, second.text) == ("cached", first.text)


@respx.mock
def test_prefilled_content_needs_no_request(conn, http):
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    aid = add_article(conn)
    repo.set_article_content(conn, aid, "already here")
    result = ensure_content(conn, http, article(conn, aid))
    assert result.text == "already here" and result.origin == "cached"
    assert not route.called


@respx.mock
def test_short_extraction_falls_back_to_longer_feed_summary(conn, http):
    respx.get(URL).mock(return_value=httpx.Response(200, content=fixture("empty.html")))
    aid = add_article(conn, summary=LONG_SUMMARY)
    result = ensure_content(conn, http, article(conn, aid))
    assert result.origin == "summary"
    assert article(conn, aid)["content"] == LONG_SUMMARY.strip()
    assert article(conn, aid)["attempts"] == 0


@respx.mock
def test_min_chars_is_configurable(conn, http):
    respx.get(URL).mock(return_value=httpx.Response(200, content=fixture("article.html")))
    aid = add_article(conn, summary="x" * 5000)
    # with a high minimum the (shorter) extraction loses against the longer summary
    result = ensure_content(conn, http, article(conn, aid), min_chars=3000)
    assert result.origin == "summary"
    other = add_article(conn, summary="x" * 5000, url="https://blog.example.com/posts/other")
    respx.get("https://blog.example.com/posts/other").mock(
        return_value=httpx.Response(200, content=fixture("article.html"))
    )
    assert ensure_content(conn, http, article(conn, other), min_chars=100).origin == "extracted"


@respx.mock
def test_http_error_falls_back_to_summary(conn, http):
    respx.get(URL).mock(return_value=httpx.Response(404))
    aid = add_article(conn, summary=LONG_SUMMARY)
    result = ensure_content(conn, http, article(conn, aid))
    assert result.origin == "summary" and article(conn, aid)["attempts"] == 0


@respx.mock
def test_nothing_usable_counts_attempts_then_fails_after_three(conn, http):
    respx.get(URL).mock(return_value=httpx.Response(200, content=fixture("empty.html")))
    aid = add_article(conn)
    statuses = []
    for _ in range(3):
        result = ensure_content(conn, http, article(conn, aid))
        statuses.append(result.status)
        assert result.text is None and result.origin is None and result.error
    assert statuses == ["pending", "pending", "failed"]
    row = article(conn, aid)
    assert (row["attempts"], row["status"]) == (3, "failed")
    assert row["last_error"] and row["content"] is None


@respx.mock
def test_http_error_without_summary_is_an_attempt(conn, http):
    respx.get(URL).mock(return_value=httpx.Response(403))
    aid = add_article(conn)
    result = ensure_content(conn, http, article(conn, aid))
    assert result.text is None and "403" in result.error
    assert article(conn, aid)["attempts"] == 1


@respx.mock
def test_max_attempts_is_configurable(conn, http):
    respx.get(URL).mock(return_value=httpx.Response(403))
    aid = add_article(conn)
    result = ensure_content(conn, http, article(conn, aid), max_attempts=1)
    assert result.status == "failed"


@respx.mock
def test_success_after_failed_attempt_clears_error_and_keeps_count(conn, http):
    route = respx.get(URL).mock(return_value=httpx.Response(403))
    aid = add_article(conn)
    ensure_content(conn, http, article(conn, aid))
    route.mock(return_value=httpx.Response(200, content=fixture("article.html")))
    result = ensure_content(conn, http, article(conn, aid))
    row = article(conn, aid)
    assert result.origin == "extracted" and row["last_error"] is None
    assert row["attempts"] == 1 and row["status"] == "pending"


@respx.mock
def test_short_extraction_without_better_summary_is_unusable(conn, http):
    respx.get(URL).mock(return_value=httpx.Response(200, content=fixture("empty.html")))
    aid = add_article(conn, summary="kurz")
    result = ensure_content(conn, http, article(conn, aid))
    assert result.text is None and "shorter than 300" in result.error


@respx.mock
def test_redirect_target_is_extracted(conn, http):
    respx.get(URL).mock(
        return_value=httpx.Response(301, headers={"Location": "https://blog.example.com/new"})
    )
    respx.get("https://blog.example.com/new").mock(
        return_value=httpx.Response(200, content=fixture("article.html"))
    )
    aid = add_article(conn)
    assert ensure_content(conn, http, article(conn, aid)).origin == "extracted"
