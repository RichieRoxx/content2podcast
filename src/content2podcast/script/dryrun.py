"""Dry run: scripts for pending articles without TTS, feed or status changes."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

from content2podcast import repository as repo
from content2podcast.config import AppConfig
from content2podcast.extract import ensure_content
from content2podcast.providers.llm.base import LLMError, LLMProvider
from content2podcast.script.generator import ScriptArticle, generate_script
from content2podcast.script.models import PodcastScript, ScriptError
from content2podcast.script.output import unique_dir, write_script_files
from content2podcast.slug import slugify

log = logging.getLogger(__name__)

DRY_RUN_DIR = "dry-run"


@dataclass
class DryRunItem:
    article_id: int
    title: str
    directory: Path | None = None
    script: PodcastScript | None = None
    error: str | None = None


def dry_run_root(config: AppConfig, today: date) -> Path:
    return config.paths.data_dir / DRY_RUN_DIR / today.isoformat()


def dry_run(
    conn: sqlite3.Connection,
    http: httpx.Client,
    config: AppConfig,
    llm: LLMProvider,
    styles: list[str],
    *,
    today: date,
) -> list[DryRunItem]:
    """One script per pending article (``per_article`` mode) written to
    ``data_dir/dry-run/<date>/<slug>/``.

    Extracted article text is cached in the database like in a real run, but no article is marked
    processed. A failing article is reported and does not stop the others.
    """
    episode = config.episode
    articles = repo.list_pending_articles(conn, episode.max_episodes_per_run)
    root = dry_run_root(config, today)
    names = {"host": config.roles.host.name, "expert": config.roles.expert.name}
    items: list[DryRunItem] = []

    for row in articles:
        title = row["title"] or row["url"]
        item = DryRunItem(row["id"], title)
        items.append(item)
        content = ensure_content(conn, http, row, min_chars=episode.min_chars_per_article)
        if content.text is None:
            item.error = f"no article text: {content.error}"
            continue
        article = ScriptArticle(
            url=row["url"],
            title=title,
            text=content.text,
            source=row["source_name"],
            published=row["published_at"],
        )
        try:
            item.script = generate_script(llm, [article], config, styles, today=today)
        except (LLMError, ScriptError) as exc:
            item.error = str(exc)
            log.error("Script generation failed for %s: %s", row["url"], exc)
            continue
        item.directory = unique_dir(root, slugify(item.script.title, "article"), str(row["id"]))
        write_script_files(item.script, item.directory, names)
    return items
