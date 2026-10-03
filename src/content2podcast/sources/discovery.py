"""Discovery service: turns the configured sources into article rows with the right status.

Every source is fetched independently; a failing source is logged, recorded in
``sources.last_error`` and never stops the others. A new source is *baselined* on its first
successful check: everything it lists is stored as ``baseline`` so a whole archive never ends up
in an episode.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from content2podcast import repository as repo
from content2podcast.config import SourceConfig
from content2podcast.sources.filters import url_allowed
from content2podcast.sources.html import fetch_html
from content2podcast.sources.llm_links import LinkSelector
from content2podcast.sources.models import DiscoveredArticle, FetchResult
from content2podcast.sources.rss import fetch_rss

log = logging.getLogger(__name__)

MAX_ERROR_CHARS = 500


@dataclass
class SourceReport:
    name: str
    new: int = 0  # stored as pending
    baseline: int = 0  # stored as baseline (first successful check)
    baselined: bool = False  # this check set the source's baseline (possibly with 0 articles)
    skipped: int = 0  # too old, stored as skipped
    known: int = 0  # already in the database (also via another source)
    not_modified: bool = False
    error: str | None = None
    new_articles: list[DiscoveredArticle] = field(default_factory=list)  # the pending ones


@dataclass
class DiscoveryReport:
    sources: list[SourceReport] = field(default_factory=list)

    @property
    def new(self) -> int:
        return sum(s.new for s in self.sources)

    @property
    def baseline(self) -> int:
        return sum(s.baseline for s in self.sources)

    @property
    def skipped(self) -> int:
        return sum(s.skipped for s in self.sources)

    @property
    def errors(self) -> int:
        return sum(s.error is not None for s in self.sources)


def _sync_sources(conn: sqlite3.Connection, sources: Sequence[SourceConfig]) -> dict[str, int]:
    """Insert new sources and update url/type of existing ones. Sources that were removed from
    the configuration stay in the database (history); they are simply not fetched."""
    return {s.name: repo.upsert_source(conn, s.name, s.url, s.type) for s in sources}


def _link_picker(
    source: SourceConfig,
    row: sqlite3.Row,
    selector: LinkSelector | None,
    llm_links_default: bool,
):
    """The LLM link picker for a selector-less HTML source, if enabled for it."""
    wanted = source.llm_links if source.llm_links is not None else llm_links_default
    if source.selector or not wanted or selector is None:
        return None

    def pick(html: bytes, page_url: str, encoding: str | None) -> list[DiscoveredArticle]:
        return selector.select(
            row["id"],
            html,
            page_url,
            include=source.include,
            exclude=source.exclude,
            same_site=source.same_site,
            encoding=encoding,
        )

    return pick


def _fetch(
    source: SourceConfig,
    row: sqlite3.Row,
    http: httpx.Client,
    link_selector: LinkSelector | None = None,
    llm_links_default: bool = False,
) -> FetchResult:
    if source.type == "html":
        return fetch_html(
            http,
            source.url,
            selector=source.selector,
            include=source.include,
            exclude=source.exclude,
            same_site=source.same_site,
            link_picker=_link_picker(source, row, link_selector, llm_links_default),
        )
    # Validators are only worth sending once the baseline exists.
    use_validators = row["baseline_at"] is not None
    result = fetch_rss(
        http,
        source.url,
        etag=row["etag"] if use_validators else None,
        last_modified=row["last_modified"] if use_validators else None,
    )
    result.articles = [
        a for a in result.articles if url_allowed(a.url, source.include, source.exclude)
    ]
    return result


def _is_too_old(article: DiscoveredArticle, cutoff: datetime) -> bool:
    if article.published_at is None:
        return False  # age unknown: treat as new
    return datetime.fromisoformat(article.published_at.replace("Z", "+00:00")) < cutoff


def _store(
    conn: sqlite3.Connection,
    source_id: int,
    articles: Sequence[DiscoveredArticle],
    report: SourceReport,
    *,
    baselining: bool,
    cutoff: datetime,
) -> None:
    for article in articles:
        if baselining:
            status = "baseline"
        elif _is_too_old(article, cutoff):
            status = "skipped"
        else:
            status = "pending"
        article_id = repo.insert_article_if_new(
            conn,
            source_id,
            article.url,
            title=article.title,
            feed_summary=article.summary,
            published_at=article.published_at,
            status=status,
        )
        if article_id is None:
            report.known += 1
        elif status == "baseline":
            report.baseline += 1
        elif status == "skipped":
            report.skipped += 1
        else:
            report.new += 1
            report.new_articles.append(article)


def discover(
    conn: sqlite3.Connection,
    sources: Sequence[SourceConfig],
    http: httpx.Client,
    *,
    max_article_age_days: int = 7,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    link_selector: LinkSelector | None = None,
    llm_links_default: bool = False,
) -> DiscoveryReport:
    """Check all enabled sources and store the articles they list.

    * unknown article of a source without baseline (first successful check) -> ``baseline``
    * unknown article otherwise -> ``pending``, or ``skipped`` if published more than
      ``max_article_age_days`` ago
    * articles are keyed by normalized URL, so the same article from two sources is one row

    HTML sources without ``selector`` use ``link_selector`` (LLM based) when enabled for the
    source (``llm_links``) or by default (``llm_links_default``).
    """
    source_ids = _sync_sources(conn, sources)
    cutoff = now() - timedelta(days=max_article_age_days)
    report = DiscoveryReport()

    for source in sources:
        if not source.enabled:
            log.debug("Source %s is disabled, skipping", source.name)
            continue
        source_id = source_ids[source.name]
        source_report = SourceReport(source.name)
        report.sources.append(source_report)
        try:
            row = repo.get_source(conn, source.name)
            baselining = row["baseline_at"] is None
            result = _fetch(source, row, http, link_selector, llm_links_default)
            if result.not_modified:
                source_report.not_modified = True
            else:
                _store(
                    conn,
                    source_id,
                    result.articles,
                    source_report,
                    baselining=baselining,
                    cutoff=cutoff,
                )
                if baselining:
                    repo.set_baseline(conn, source_id)
                    source_report.baselined = True
            repo.mark_source_checked(
                conn, source_id, etag=result.etag, last_modified=result.last_modified
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
            log.error("Source %s failed: %s", source.name, message)
            log.debug("Traceback for source %s", source.name, exc_info=True)
            source_report.error = message
            repo.mark_source_checked(conn, source_id, error=message)
    return report
