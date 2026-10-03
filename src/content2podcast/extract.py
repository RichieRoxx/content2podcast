"""Full-text extraction of articles (trafilatura) with a feed-summary fallback."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal

import httpx
import trafilatura

from content2podcast import repository as repo
from content2podcast.http import HttpError, loggable_url, request_with_retry
from content2podcast.logging_setup import kv

log = logging.getLogger(__name__)

DEFAULT_MIN_CHARS = 300
DEFAULT_MAX_ATTEMPTS = 3

Origin = Literal["cached", "extracted", "summary"]


@dataclass(frozen=True)
class ContentResult:
    """``text`` is None when nothing usable was found; ``status`` is the article's status
    afterwards (``failed`` once the attempts are used up)."""

    text: str | None
    origin: Origin | None
    status: str
    error: str | None = None


def extract_text(html: bytes | str, url: str | None = None) -> str | None:
    """Main text of an article page (no navigation, comments or tables), or None."""
    text = trafilatura.extract(
        html,
        url=url,
        favor_precision=True,
        include_comments=False,
        include_tables=False,
    )
    return text.strip() if text and text.strip() else None


def extract_metadata(html: bytes | str, url: str | None = None) -> tuple[str | None, str | None]:
    """``(title, date)`` of an article page; ``date`` is ``YYYY-MM-DD`` when the page has one."""
    meta = trafilatura.extract_metadata(html, default_url=url)
    if meta is None:
        return None, None
    return (meta.title or None), (meta.date or None)


def _choose(
    extracted: str | None, summary: str | None, min_chars: int
) -> tuple[str, Origin] | None:
    """The extraction if it is long enough, else the feed summary if that is longer."""
    extracted = extracted or ""
    summary = (summary or "").strip()
    if len(extracted) >= min_chars:
        return extracted, "extracted"
    if len(summary) > len(extracted):
        return summary, "summary"
    return None  # a short extraction without a better summary is usually a login wall or stub


def ensure_content(
    conn: sqlite3.Connection,
    http: httpx.Client,
    article: sqlite3.Row,
    *,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> ContentResult:
    """Make sure ``article`` has text, fetching and extracting it if needed.

    * cached ``content`` is returned without any request
    * otherwise fetch + extract; text shorter than ``min_chars`` falls back to ``feed_summary``
      if that is longer (this also covers fetch errors)
    * nothing usable: ``attempts`` is incremented and ``last_error`` stored; after
      ``max_attempts`` the article becomes ``failed``
    """
    if article["content"]:
        return ContentResult(article["content"], "cached", article["status"])

    url = article["url"]
    extracted: str | None = None
    error: str | None = None
    try:
        response = request_with_retry(http, "GET", url)
        extracted = extract_text(response.content, str(response.url))
        if extracted is None:
            error = "no article text found"
    except HttpError as exc:
        error = str(exc)

    chosen = _choose(extracted, article["feed_summary"], min_chars)
    if chosen is not None:
        text, origin = chosen
        repo.set_article_content(conn, article["id"], text)
        if origin == "summary":
            log.info(
                "Using feed summary as article text: %s",
                kv(url=loggable_url(url), reason=error or "extraction too short"),
            )
        return ContentResult(text, origin, article["status"])

    error = error or f"extracted text shorter than {min_chars} characters"
    status = repo.record_extraction_failure(conn, article["id"], error, max_attempts=max_attempts)
    log.warning(
        "No usable article text: %s",
        kv(url=loggable_url(url), error=error, attempts=article["attempts"] + 1, status=status),
    )
    return ContentResult(None, None, status, error)
