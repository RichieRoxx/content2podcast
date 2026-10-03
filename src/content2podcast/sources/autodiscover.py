"""Feed autodiscovery for ``podcast sources discover URL``.

Finds the RSS/Atom feeds of a site (``<link rel="alternate">`` tags, then common feed paths) and,
for pages without a feed, suggests CSS selectors for the article links. Everything is read-only.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
import yaml
from bs4 import BeautifulSoup

from content2podcast.http import HttpError, request_with_retry
from content2podcast.sources.html import extract_links
from content2podcast.sources.llm_links import collect_candidates
from content2podcast.sources.models import SourceError
from content2podcast.sources.normalize import normalize_url
from content2podcast.sources.rss import parse_feed

log = logging.getLogger(__name__)

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")
COMMON_PATHS = ("/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml")
MIN_SELECTOR_MATCHES = 3
MAX_SUGGESTIONS = 3


@dataclass(frozen=True)
class FeedCandidate:
    url: str
    title: str | None
    via: str  # "page" (the URL is a feed), "link" (<link rel=alternate>) or "path" (common path)
    articles: int | None = None  # entries found when the feed was fetched


@dataclass(frozen=True)
class SelectorSuggestion:
    selector: str
    links: int
    examples: tuple[str, ...]


@dataclass
class DiscoveryResult:
    page_url: str
    name: str
    feeds: list[FeedCandidate] = field(default_factory=list)
    selectors: list[SelectorSuggestion] = field(default_factory=list)


def _fetch(client: httpx.Client, url: str, *, probe: bool = False) -> httpx.Response | None:
    try:
        return request_with_retry(client, "GET", url, max_attempts=1 if probe else 4)
    except HttpError as exc:
        if probe:
            log.debug("Probe failed: %s", exc)
            return None
        raise SourceError(str(exc)) from None


def _try_feed(response: httpx.Response) -> int | None:
    """Number of entries if the response is a readable feed, else ``None``."""
    try:
        return len(parse_feed(response.content, str(response.url), response.headers))
    except SourceError:
        return None


def _site_name(soup: BeautifulSoup, page_url: str) -> str:
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    title = re.split(r"\s[|–—-]\s", title)[0].strip()
    return title or (urlsplit(page_url).hostname or "Source").removeprefix("www.")


def _link_feeds(soup: BeautifulSoup, page_url: str) -> list[FeedCandidate]:
    found: list[FeedCandidate] = []
    for tag in soup.find_all("link"):
        rel = tag.get("rel") or []
        rel = rel.split() if isinstance(rel, str) else rel
        kind = (tag.get("type") or "").split(";")[0].strip().lower()
        href = (tag.get("href") or "").strip()
        if "alternate" not in [r.lower() for r in rel] or kind not in FEED_TYPES or not href:
            continue
        if kind == "application/feed+json":
            continue  # JSON Feed is not supported by the RSS source
        url = urljoin(page_url, href)
        if urlsplit(url).scheme in ("http", "https"):
            found.append(FeedCandidate(url, (tag.get("title") or "").strip() or None, "link"))
    return found


def _unique(candidates: list[FeedCandidate]) -> list[FeedCandidate]:
    seen: set[str] = set()
    unique = []
    for c in candidates:
        key = normalize_url(c.url)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def _verified(client: httpx.Client, candidate: FeedCandidate) -> FeedCandidate | None:
    response = _fetch(client, candidate.url, probe=True)
    if response is None:
        return None
    count = _try_feed(response)
    if count is None:
        return None
    return FeedCandidate(str(response.url), candidate.title, candidate.via, count)


def _signature(link) -> list[str]:
    """CSS selector candidates for a link, most specific class-based first."""

    def own(tag) -> str | None:
        classes = [c for c in (tag.get("class") or []) if re.fullmatch(r"[A-Za-z][\w-]*", c)]
        return f"{tag.name}.{classes[0]}" if classes else None

    options: list[str] = []
    if (sel := own(link)) is not None:
        options.append(sel)
    parent = link.parent
    for _ in range(3):
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        if (sel := own(parent)) is not None:
            options.append(f"{sel} a")
        elif parent.name in ("h1", "h2", "h3", "h4", "li", "article"):
            options.append(f"{parent.name} a")
        parent = parent.parent
    return options


def suggest_selectors(html: bytes | str, page_url: str, encoding: str | None = None):
    """Selectors that match several of the page's likely article links, best first.

    A selector scores by how many of the page's candidate links (see ``collect_candidates``) it
    selects and is penalized for matching links that are not candidates (navigation etc.)."""
    candidates = collect_candidates(html, page_url, max_candidates=500, encoding=encoding)
    wanted = {normalize_url(c.url) for c in candidates}
    soup = BeautifulSoup(html, "lxml", from_encoding=encoding if isinstance(html, bytes) else None)
    counts: Counter[str] = Counter()
    for link in soup.select("a[href]"):
        if normalize_url(urljoin(page_url, link["href"])) in wanted:
            counts.update(set(_signature(link)))
    scored: list[tuple[float, SelectorSuggestion]] = []
    for selector in counts:
        articles = extract_links(html, page_url, selector, encoding=encoding)
        hits = [a for a in articles if normalize_url(a.url) in wanted]
        if len(hits) < MIN_SELECTOR_MATCHES:
            continue
        precision = len(hits) / len(articles)
        scored.append(
            (
                len(hits) * precision,
                SelectorSuggestion(selector, len(hits), tuple(a.title or a.url for a in hits[:2])),
            )
        )
    scored.sort(key=lambda item: (-item[0], len(item[1].selector)))
    return [s for _, s in scored[:MAX_SUGGESTIONS]]


def discover_feeds(client: httpx.Client, url: str, *, probe_paths: bool = True) -> DiscoveryResult:
    """Look for feeds at ``url``.

    1. The URL itself may be a feed.
    2. ``<link rel="alternate" type="application/rss+xml|application/atom+xml">`` tags.
    3. Only if neither found anything: common paths (``/feed``, ``/rss``, ``/atom.xml``, ...) on
       the site root.

    Every candidate is fetched and must parse as a feed. Without any feed, CSS selectors for the
    article links are suggested. Raises ``SourceError`` if the page cannot be fetched.
    """
    response = _fetch(client, url)
    assert response is not None
    page_url = str(response.url)
    count = _try_feed(response)
    if count is not None:
        return DiscoveryResult(
            page_url,
            urlsplit(page_url).hostname or "Feed",
            [FeedCandidate(page_url, None, "page", count)],
        )

    soup = BeautifulSoup(response.content, "lxml", from_encoding=response.charset_encoding)
    result = DiscoveryResult(page_url, _site_name(soup, page_url))
    result.feeds = [v for c in _unique(_link_feeds(soup, page_url)) if (v := _verified(client, c))]
    if not result.feeds and probe_paths:
        root = f"{urlsplit(page_url).scheme}://{urlsplit(page_url).netloc}"
        probes = _unique([FeedCandidate(root + p, None, "path") for p in COMMON_PATHS])
        result.feeds = [v for c in probes if (v := _verified(client, c))]
        result.feeds = _unique(result.feeds)
    if not result.feeds:
        result.selectors = suggest_selectors(response.content, page_url, response.charset_encoding)
    return result


def snippet(result: DiscoveryResult) -> str | None:
    """A ready-to-paste ``sources.yaml`` list entry for the best finding, or ``None``."""
    entry: dict[str, object]
    if result.feeds:
        entry = {"name": result.name, "url": result.feeds[0].url, "type": "rss"}
    elif result.selectors:
        entry = {
            "name": result.name,
            "url": result.page_url,
            "type": "html",
            "selector": result.selectors[0].selector,
        }
    else:
        return None
    text = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=1000)
    return "".join(f"  {line}\n" for line in text.splitlines())
