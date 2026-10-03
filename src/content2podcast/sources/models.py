"""Types shared by the source fetchers."""

from __future__ import annotations

from dataclasses import dataclass, field


class SourceError(Exception):
    """A source could not be fetched or understood."""


@dataclass(frozen=True)
class DiscoveredArticle:
    url: str
    title: str | None = None
    summary: str | None = None
    published_at: str | None = None  # UTC ISO-8601, ``2026-01-31T05:30:00Z``


@dataclass
class FetchResult:
    articles: list[DiscoveredArticle] = field(default_factory=list)
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
