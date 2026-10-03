"""Retention: keep disk usage and feed size bounded by pruning old episodes."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from content2podcast import repository as repo
from content2podcast.config import RetentionConfig
from content2podcast.layout import episode_work_dir
from content2podcast.logging_setup import kv

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrunedEpisode:
    guid: str
    title: str
    reason: str  # "age", "count" or "age+count"
    file_deleted: bool  # False if the MP3 was already gone (or its path was unsafe)


@dataclass
class RetentionReport:
    pruned: list[PrunedEpisode] = field(default_factory=list)
    work_dirs_removed: int = 0


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _delete_audio(output_dir: Path, audio_file: str | None) -> bool:
    """Delete ``output_dir/audio_file``; a missing file is fine. Paths that would leave the
    output directory are never touched."""
    if not audio_file:
        return False
    root = output_dir.resolve()
    target = (root / audio_file).resolve()
    if not target.is_relative_to(root):
        log.warning("Not deleting audio outside the output directory: %s", kv(file=audio_file))
        return False
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True


def apply_retention(
    conn: sqlite3.Connection,
    output_dir: Path,
    data_dir: Path,
    retention: RetentionConfig,
    *,
    now: datetime | None = None,
) -> RetentionReport:
    """Prune published episodes older than ``max_age_days`` and/or beyond the newest
    ``max_episodes``: the MP3 is deleted (a missing file is tolerated) and the episode marked
    ``pruned``. Articles stay ``processed``; the feed only lists ``published`` episodes.
    Scratch directories of published and pruned episodes are removed as well.
    """
    now = now or datetime.now(UTC)
    report = RetentionReport()
    published = repo.list_episodes(conn, "published")  # newest first
    cutoff = now - timedelta(days=retention.max_age_days) if retention.max_age_days else None

    for position, episode in enumerate(published):
        too_old = (
            cutoff is not None
            and episode["published_at"] is not None
            and _parse_utc(episode["published_at"]) < cutoff
        )
        beyond_count = retention.max_episodes is not None and position >= retention.max_episodes
        if not (too_old or beyond_count):
            continue
        reason = "+".join(r for r, hit in (("age", too_old), ("count", beyond_count)) if hit)
        deleted = _delete_audio(output_dir, episode["audio_file"])
        repo.prune_episode(conn, episode["id"])
        report.pruned.append(PrunedEpisode(episode["guid"], episode["title"], reason, deleted))
        log.info(
            "Episode pruned: %s", kv(guid=episode["guid"], reason=reason, file_deleted=deleted)
        )

    for status in ("published", "pruned"):
        for episode in repo.list_episodes(conn, status):
            work_dir = episode_work_dir(data_dir, episode["guid"])
            if work_dir.is_dir():
                shutil.rmtree(work_dir, ignore_errors=True)
                report.work_dirs_removed += 1
    return report
