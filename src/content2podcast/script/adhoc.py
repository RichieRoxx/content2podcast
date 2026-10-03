"""Ad-hoc article fetching for ``podcast script URL...`` (no database involved)."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from content2podcast.extract import extract_metadata, extract_text
from content2podcast.http import request_with_retry
from content2podcast.script.generator import ScriptArticle
from content2podcast.sources.models import SourceError


def fetch_article(http: httpx.Client, url: str) -> ScriptArticle:
    """Fetch ``url`` and extract its text. Raises ``HttpError`` or ``SourceError``."""
    response = request_with_retry(http, "GET", url)
    final_url = str(response.url)
    text = extract_text(response.content, final_url)
    if text is None:
        raise SourceError(f"No article text found at {url}")
    title, published = extract_metadata(response.content, final_url)
    host = (urlsplit(final_url).hostname or "").removeprefix("www.")
    return ScriptArticle(
        url=url, title=title or url, text=text, source=host or "unknown", published=published
    )
