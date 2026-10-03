"""HTML source fetcher: finds article links on an overview page with a CSS selector."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from soupsieve import SelectorSyntaxError

from content2podcast.http import loggable_url, request_with_retry
from content2podcast.logging_setup import kv
from content2podcast.sources.filters import url_allowed
from content2podcast.sources.models import DiscoveredArticle, FetchResult, SourceError
from content2podcast.sources.normalize import normalize_url

log = logging.getLogger(__name__)

LinkPicker = Callable[[bytes, str, str | None], list[DiscoveredArticle]]

_IGNORED_HREF = re.compile(r"^\s*(#|javascript:|mailto:|tel:|data:)", re.IGNORECASE)


def _bare_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _same_site(url: str, page_url: str) -> bool:
    """Same host (ignoring ``www.``) or a subdomain of the page's host."""
    host, page_host = _bare_host(url), _bare_host(page_url)
    return host == page_host or host.endswith("." + page_host)


def _link_for(element: Any) -> Any | None:
    """The element itself if it is an ``a[href]``, else the first ``a[href]`` inside it."""
    if element.name == "a" and element.get("href"):
        return element
    return element.select_one("a[href]")


def _title(link: Any) -> str | None:
    text = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
    return text or (link.get("title") or "").strip() or None


def extract_links(
    html: bytes | str,
    page_url: str,
    selector: str,
    *,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    same_site: bool = True,
    encoding: str | None = None,
) -> list[DiscoveredArticle]:
    """Articles linked from ``html`` below ``selector``, in page order, de-duplicated by
    normalized URL. Relative links are resolved against ``page_url``."""
    soup = BeautifulSoup(html, "lxml", from_encoding=encoding if isinstance(html, bytes) else None)
    try:
        elements = soup.select(selector)
    except SelectorSyntaxError as exc:
        raise SourceError(f"Invalid CSS selector {selector!r}: {exc}") from None

    seen: set[str] = set()
    articles: list[DiscoveredArticle] = []
    for element in elements:
        link = _link_for(element)
        href = (link.get("href") or "") if link is not None else ""
        if not href.strip() or _IGNORED_HREF.match(href):
            continue
        url = urljoin(page_url, href.strip())
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        if same_site and not _same_site(url, page_url):
            continue
        if not url_allowed(url, include, exclude):
            continue
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        articles.append(DiscoveredArticle(url=url, title=_title(link)))
    return articles


def fetch_html(
    client: httpx.Client,
    url: str,
    *,
    selector: str | None,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    same_site: bool = True,
    link_picker: LinkPicker | None = None,
) -> FetchResult:
    """Fetch an overview page and return the article links found with ``selector``.

    Without a selector the ``link_picker`` (``(html, page_url, encoding) -> articles``, e.g. the
    LLM based one) chooses the links; without both ``SourceError`` is raised. A selector that
    matches nothing is not an error (the page may be temporarily empty) but logs a warning.
    """
    has_selector = bool(selector and selector.strip())
    if not has_selector and link_picker is None:
        raise SourceError(f"selector required for HTML source {loggable_url(url)}")

    response = request_with_retry(client, "GET", url)
    page_url = str(response.url)
    if has_selector:
        assert selector is not None
        articles = extract_links(
            response.content,
            page_url,
            selector,
            include=include,
            exclude=exclude,
            same_site=same_site,
            encoding=response.charset_encoding,
        )
    else:
        assert link_picker is not None
        articles = link_picker(response.content, page_url, response.charset_encoding)
    if not articles:
        log.warning(
            "No article links found: %s",
            kv(url=loggable_url(page_url), selector=selector or "(llm)"),
        )
    return FetchResult(articles=articles)
