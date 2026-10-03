"""Article-link extraction for HTML sources without a CSS selector.

The overview page is reduced to a compact list of candidate links (same site, with text,
de-duplicated, capped, each with the heading it sits under); the LLM answers with the indexes of
the links that lead to articles. The answer is cached per source and candidate list, so an
unchanged page costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from content2podcast import repository as repo
from content2podcast.providers.llm.base import LLMProvider
from content2podcast.sources.filters import url_allowed
from content2podcast.sources.html import _IGNORED_HREF, _same_site, _title
from content2podcast.sources.models import DiscoveredArticle, SourceError
from content2podcast.sources.normalize import normalize_url

log = logging.getLogger(__name__)

PROMPT_VERSION = 1
MIN_TEXT_CHARS = 10  # shorter link texts are navigation ("Home", "More"), not article titles
MAX_TEXT_CHARS = 160
MAX_CONTEXT_CHARS = 80
_SKIPPED_PARENTS = ("nav", "footer", "aside")
_HEADINGS = ["h1", "h2", "h3", "h4"]

SYSTEM_PROMPT = (
    "You help to find the articles on a website's overview page. You get a numbered list of "
    "links of the page: link text, address and, where available, the heading the link appears "
    "under. Select the links that lead to individual articles, blog posts or news items. Leave "
    "out navigation, category and tag pages, authors, login, search, imprint, privacy, "
    "advertising and links to the overview itself. Answer with the indexes of the selected "
    "links."
)


class LinkSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_indexes: list[int] = Field(
        description="Indexes of the links that lead to individual articles."
    )


@dataclass(frozen=True)
class Candidate:
    url: str
    text: str
    context: str | None = None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def collect_candidates(
    html: bytes | str,
    page_url: str,
    *,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    same_site: bool = True,
    max_candidates: int = 150,
    encoding: str | None = None,
) -> list[Candidate]:
    """Links that could be articles, in page order: same site (unless ``same_site`` is off),
    with a text of at least ``MIN_TEXT_CHARS`` characters, outside nav/footer/aside, passing the
    include/exclude filters, de-duplicated by normalized URL and not pointing to the page itself.
    At most ``max_candidates``."""
    soup = BeautifulSoup(html, "lxml", from_encoding=encoding if isinstance(html, bytes) else None)
    page_key = normalize_url(page_url)
    seen = {page_key}
    candidates: list[Candidate] = []
    for link in soup.select("a[href]"):
        href = (link.get("href") or "").strip()
        if not href or _IGNORED_HREF.match(href):
            continue
        if link.find_parent(_SKIPPED_PARENTS) is not None:
            continue
        url = urljoin(page_url, href)
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        if same_site and not _same_site(url, page_url):
            continue
        if not url_allowed(url, include, exclude):
            continue
        text = _title(link) or ""
        if len(text) < MIN_TEXT_CHARS:
            continue
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        heading = link.find_previous(_HEADINGS)
        context = None
        if heading is not None and heading is not link and link not in heading.descendants:
            context = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)) or None
        candidates.append(
            Candidate(
                url,
                _clip(text, MAX_TEXT_CHARS),
                _clip(context, MAX_CONTEXT_CHARS) if context else None,
            )
        )
        if len(candidates) >= max_candidates:
            break
    return candidates


def render_candidates(candidates: list[Candidate], page_url: str) -> str:
    """The numbered list sent to the LLM; addresses on the page's own site are shortened to the
    path."""
    lines = []
    for index, c in enumerate(candidates):
        parts = urlsplit(c.url)
        address = (
            parts.path + (f"?{parts.query}" if parts.query else "")
            if _same_site(c.url, page_url)
            else c.url
        )
        line = f"[{index}] {c.text} | {address or '/'}"
        if c.context and c.context != c.text:
            line += f" | under: {c.context}"
        lines.append(line)
    return f"Overview page: {page_url}\n\nLinks:\n" + "\n".join(lines)


def candidates_hash(candidates: list[Candidate]) -> str:
    payload = json.dumps(
        [PROMPT_VERSION, [[c.url, c.text, c.context] for c in candidates]], ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def map_indexes(candidates: list[Candidate], indexes: Iterable[int]) -> list[Candidate]:
    """The chosen candidates in page order; out-of-range and repeated indexes are ignored."""
    chosen = sorted({i for i in indexes if 0 <= i < len(candidates)})
    return [candidates[i] for i in chosen]


class LinkSelector:
    """Picks the article links of an overview page with an LLM, caching per source.

    ``llm_factory`` is called at most once and only when a call is actually needed, so a
    configuration without a (usable) LLM only fails for sources that need one.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        llm_factory: Callable[[], LLMProvider],
        *,
        max_candidates: int = 150,
    ):
        self.conn = conn
        self._llm_factory = llm_factory
        self._llm: LLMProvider | None = None
        self.max_candidates = max_candidates

    def _get_llm(self) -> LLMProvider:
        if self._llm is None:
            self._llm = self._llm_factory()
        return self._llm

    def select(
        self,
        source_id: int,
        html: bytes | str,
        page_url: str,
        *,
        include: Iterable[str] = (),
        exclude: Iterable[str] = (),
        same_site: bool = True,
        encoding: str | None = None,
    ) -> list[DiscoveredArticle]:
        candidates = collect_candidates(
            html,
            page_url,
            include=include,
            exclude=exclude,
            same_site=same_site,
            max_candidates=self.max_candidates,
            encoding=encoding,
        )
        if not candidates:
            return []
        digest = candidates_hash(candidates)
        by_url = {c.url: c for c in candidates}
        cached = repo.get_link_selection(self.conn, source_id, digest)
        if cached is not None:
            log.debug("Link selection cached: %s", page_url)
            chosen = [by_url[u] for u in cached if u in by_url]
        else:
            try:
                answer = self._get_llm().generate_structured(
                    SYSTEM_PROMPT, render_candidates(candidates, page_url), LinkSelection
                )
            except SourceError:
                raise
            except Exception as exc:
                raise SourceError(f"LLM link selection failed: {exc}") from exc
            chosen = map_indexes(candidates, answer.article_indexes)
            repo.set_link_selection(self.conn, source_id, digest, [c.url for c in chosen])
        return [DiscoveredArticle(url=c.url, title=c.text) for c in chosen]
