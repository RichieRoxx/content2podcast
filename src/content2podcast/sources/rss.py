"""RSS 2.0 / Atom source fetcher with conditional GET."""

from __future__ import annotations

import calendar
import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from content2podcast.http import loggable_url, request_with_retry
from content2podcast.logging_setup import kv
from content2podcast.sources.models import DiscoveredArticle, FetchResult, SourceError
from content2podcast.sources.text import strip_html

log = logging.getLogger(__name__)


def _published_at(entry: Any) -> str | None:
    # raw dict lookups: FeedParserDict aliases updated_parsed to published_parsed with a warning
    parsed: time.struct_time | None = dict.get(entry, "published_parsed") or dict.get(
        entry, "updated_parsed"
    )
    if parsed is None:
        return None
    try:
        moment = datetime.fromtimestamp(calendar.timegm(parsed), UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_url(entry: Any, base_url: str) -> str | None:
    """``link``, else ``id`` if it is a URL; relative links are resolved against ``base_url``."""
    link = (entry.get("link") or "").strip()
    if not link:
        entry_id = (entry.get("id") or "").strip()
        link = entry_id if urlsplit(entry_id).scheme in ("http", "https") else ""
    if not link:
        return None
    url = urljoin(base_url, link)
    return url if urlsplit(url).scheme in ("http", "https") else None


def _entry_summary(entry: Any) -> str | None:
    summary = entry.get("summary")
    if not summary and entry.get("content"):
        summary = entry["content"][0].get("value")
    return strip_html(summary)


def parse_feed(
    content: bytes, base_url: str, headers: httpx.Headers | None = None
) -> list[DiscoveredArticle]:
    """Parse feed bytes into articles. Raises ``SourceError`` if nothing feed-like is found;
    a malformed feed that still yields entries only logs a warning."""
    response_headers = {"content-location": base_url}
    if headers and "content-type" in headers:
        response_headers["content-type"] = headers["content-type"]
    parsed = feedparser.parse(content, response_headers=response_headers)

    error = type(parsed.get("bozo_exception")).__name__
    if not parsed.version and not parsed.entries:
        raise SourceError(f"{loggable_url(base_url)} is not a readable feed ({error})")
    # a wrong Content-Type (e.g. text/html) is common and harmless; other errors are worth a warning
    if parsed.bozo and error != "NonXMLContentType":
        log.warning(
            "Feed is malformed but parseable: %s", kv(url=loggable_url(base_url), error=error)
        )

    articles = []
    for entry in parsed.entries:
        url = _entry_url(entry, base_url)
        if url is None:
            log.debug("Skipping feed entry without usable URL: %s", kv(title=entry.get("title")))
            continue
        articles.append(
            DiscoveredArticle(
                url=url,
                title=strip_html(entry.get("title")),
                summary=_entry_summary(entry),
                published_at=_published_at(entry),
            )
        )
    return articles


def fetch_rss(
    client: httpx.Client,
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    """Fetch and parse a feed, sending the stored validators as a conditional GET.

    A ``304`` yields ``not_modified=True`` with the validators unchanged. Otherwise the fresh
    ``ETag`` / ``Last-Modified`` headers are returned for the caller to store.
    """
    headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml, */*;q=0.5"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    response = request_with_retry(client, "GET", url, headers=headers)
    if response.status_code == 304:
        return FetchResult(not_modified=True, etag=etag, last_modified=last_modified)

    articles = parse_feed(response.content, str(response.url), response.headers)
    return FetchResult(
        articles=articles,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
    )
