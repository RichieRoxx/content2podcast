"""Podcast RSS 2.0 feed (with iTunes, content and atom namespaces) generated from the database."""

from __future__ import annotations

import html
import logging
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path

from content2podcast import __version__
from content2podcast import repository as repo
from content2podcast.config import AppConfig
from content2podcast.layout import FEED_FILENAME, feed_url, public_url, sync_cover, write_atomic
from content2podcast.xmltext import strip_invalid_xml_chars

log = logging.getLogger(__name__)

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
ATOM = "http://www.w3.org/2005/Atom"
for prefix, uri in (("itunes", ITUNES), ("content", CONTENT), ("atom", ATOM)):
    ET.register_namespace(prefix, uri)

AUDIO_TYPE = "audio/mpeg"
# Labels of the show notes by language code; unknown languages use English.
LABELS = {
    "de": {"sources": "Quellen", "also": "Außerdem neu"},
    "en": {"sources": "Sources", "also": "Also new"},
}


@dataclass(frozen=True)
class FeedSource:
    title: str
    url: str
    source_name: str
    role: str  # "discussed" or "mentioned"


@dataclass(frozen=True)
class FeedEpisode:
    guid: str
    title: str
    summary: str
    audio_file: str  # relative to the output directory, e.g. "episodes/2026-10-03-x-abc.mp3"
    audio_bytes: int
    duration_s: float
    published_at: str  # UTC ISO-8601
    number: int | None = None
    sources: list[FeedSource] = field(default_factory=list)


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _clean(text: str | None) -> str:
    return strip_invalid_xml_chars(text or "").strip()


def _text(parent: ET.Element, tag: str, text: str, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, tag, attrs)
    element.text = text
    return element


def _rfc2822(moment: datetime) -> str:
    return format_datetime(moment.astimezone(UTC), usegmt=True)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def format_duration(seconds: float) -> str:
    """``H:MM:SS`` for ``itunes:duration``."""
    total = int(seconds + 0.5)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def show_notes_html(episode: FeedEpisode, language: str) -> str:
    """HTML for ``content:encoded``: the summary and the attributed source articles; articles
    that were only mentioned are listed separately."""
    labels = LABELS.get(language, LABELS["en"])

    def item(source: FeedSource) -> str:
        link = f'<a href="{html.escape(source.url, quote=True)}">{html.escape(source.title)}</a>'
        return f"<li>{link} ({html.escape(source.source_name)})</li>"

    parts = [f"<p>{html.escape(episode.summary)}</p>"]
    discussed = [s for s in episode.sources if s.role != "mentioned"]
    mentioned = [s for s in episode.sources if s.role == "mentioned"]
    if discussed:
        parts.append(f"<h3>{labels['sources']}</h3><ul>{''.join(item(s) for s in discussed)}</ul>")
    if mentioned:
        parts.append(f"<h3>{labels['also']}</h3><ul>{''.join(item(s) for s in mentioned)}</ul>")
    return "".join(parts)


def build_feed(
    config: AppConfig,
    episodes: Sequence[FeedEpisode],
    *,
    cover_relpath: str | None = None,
    now: datetime | None = None,
) -> bytes:
    """The feed as UTF-8 XML bytes (with XML declaration). ``episodes`` must already be ordered
    newest first."""
    podcast, base_url = config.podcast, config.feed.base_url
    language = podcast.language.split("-")[0].lower()
    explicit = "true" if podcast.explicit else "false"
    now = now or datetime.now(UTC)

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    _text(channel, "title", _clean(podcast.title))
    _text(channel, "link", base_url)
    _text(channel, "description", _clean(podcast.description))
    _text(channel, "language", podcast.language)
    ET.SubElement(
        channel,
        _q(ATOM, "link"),
        {"href": feed_url(base_url), "rel": "self", "type": "application/rss+xml"},
    )
    _text(channel, "lastBuildDate", _rfc2822(now))
    _text(channel, "generator", f"content2podcast {__version__}")
    _text(channel, _q(ITUNES, "author"), _clean(podcast.author))
    owner = ET.SubElement(channel, _q(ITUNES, "owner"))
    _text(owner, _q(ITUNES, "name"), _clean(podcast.author))
    if cover_relpath:
        cover = public_url(base_url, cover_relpath)
        ET.SubElement(channel, _q(ITUNES, "image"), {"href": cover})
        image = ET.SubElement(channel, "image")
        _text(image, "url", cover)
        _text(image, "title", _clean(podcast.title))
        _text(image, "link", base_url)
    ET.SubElement(channel, _q(ITUNES, "category"), {"text": _clean(podcast.category)})
    _text(channel, _q(ITUNES, "explicit"), explicit)
    _text(channel, _q(ITUNES, "type"), "episodic")

    for episode in episodes:
        item = ET.SubElement(channel, "item")
        _text(item, "title", _clean(episode.title))
        _text(item, "description", _clean(episode.summary))
        _text(
            item,
            _q(CONTENT, "encoded"),
            strip_invalid_xml_chars(show_notes_html(episode, language)),
        )
        first = next((s for s in episode.sources if s.role != "mentioned"), None)
        if first is not None:
            _text(item, "link", first.url)
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": public_url(base_url, episode.audio_file),
                "length": str(episode.audio_bytes),
                "type": AUDIO_TYPE,
            },
        )
        _text(item, "guid", episode.guid, isPermaLink="false")
        _text(item, "pubDate", _rfc2822(_parse_utc(episode.published_at)))
        _text(item, _q(ITUNES, "duration"), format_duration(episode.duration_s))
        if episode.number is not None:
            _text(item, _q(ITUNES, "episode"), str(episode.number))
        _text(item, _q(ITUNES, "explicit"), explicit)

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    declaration = b'<?xml version="1.0" encoding="UTF-8"?>\n'
    return declaration + ET.tostring(rss, encoding="utf-8") + b"\n"


def load_feed_episodes(conn: sqlite3.Connection) -> list[FeedEpisode]:
    """Published episodes, newest first, with their source articles."""
    episodes: list[FeedEpisode] = []
    for row in repo.list_episodes(conn, "published"):
        if not row["audio_file"] or row["audio_bytes"] is None or not row["published_at"]:
            log.warning("Skipping published episode without audio data: guid=%s", row["guid"])
            continue
        sources = [
            FeedSource(s["title"] or s["url"], s["url"], s["source_name"], s["role"])
            for s in repo.episode_sources(conn, row["id"])
        ]
        episodes.append(
            FeedEpisode(
                guid=row["guid"],
                title=row["title"],
                summary=row["summary"] or "",
                audio_file=row["audio_file"],
                audio_bytes=row["audio_bytes"],
                duration_s=row["duration_s"] or 0.0,
                published_at=row["published_at"],
                number=row["number"],
                sources=sources,
            )
        )
    return episodes


def write_feed(config: AppConfig, conn: sqlite3.Connection, *, now: datetime | None = None) -> Path:
    """Copy the cover (if it changed), build the feed from the database and write
    ``<output_dir>/feed.xml`` atomically. Returns the feed path."""
    output_dir = config.paths.output_dir
    cover = sync_cover(config.podcast.cover_image, output_dir)
    data = build_feed(
        config,
        load_feed_episodes(conn),
        cover_relpath=cover.relpath if cover else None,
        now=now,
    )
    return write_atomic(output_dir / FEED_FILENAME, data)
