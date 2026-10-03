"""The complete run: discovery -> extraction -> script -> speech -> assembly -> publish ->
retention. Every article is processed in isolation: one failing article never stops the run."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo

import httpx

from content2podcast import repository as repo
from content2podcast.audio import AssemblyResult, EpisodeMetadata, assemble_episode
from content2podcast.config import AppConfig, SourceConfig
from content2podcast.extract import ensure_content
from content2podcast.feed import write_feed
from content2podcast.layout import episode_relpath, episode_work_dir, place_episode
from content2podcast.logging_setup import kv
from content2podcast.providers.llm.base import LLMProvider
from content2podcast.providers.tts.base import TTSOptions, TTSProvider
from content2podcast.retention import apply_retention
from content2podcast.script.generator import ScriptArticle, generate_script
from content2podcast.script.models import PodcastScript
from content2podcast.script.prompt import PromptError
from content2podcast.sources.discovery import DiscoveryReport, discover
from content2podcast.speech import group_by_segment, plan_script, synthesize_script

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

Assembler = Callable[..., AssemblyResult]


@dataclass
class EpisodeResult:
    article_id: int
    url: str
    title: str
    guid: str | None = None  # set once the episode is published
    episode_title: str | None = None  # title of the published episode (from the script)
    error: str | None = None
    status: str = "published"  # article status after the attempt: "failed" once attempts ran out


@dataclass
class RunSummary:
    discovery: DiscoveryReport
    results: list[EpisodeResult] = field(default_factory=list)
    pruned: int = 0
    duration_s: float = 0.0
    notes: list[str] = field(default_factory=list)  # e.g. why a digest was not created

    @property
    def published(self) -> list[EpisodeResult]:
        return [r for r in self.results if r.error is None]

    @property
    def failed(self) -> list[EpisodeResult]:
        return [r for r in self.results if r.error is not None]

    @property
    def exit_code(self) -> int:
        """0 if nothing was attempted or at least one episode was published, else 1."""
        return 1 if self.results and not self.published else 0


@dataclass
class Pipeline:
    """Everything a run needs; the assembler and the clock are injectable for tests."""

    config: AppConfig
    conn: sqlite3.Connection
    http: httpx.Client
    llm: LLMProvider
    tts: TTSProvider
    styles: Sequence[str]
    assemble: Assembler = assemble_episode
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    new_guid: Callable[[], str] = lambda: str(uuid.uuid4())
    tz: tzinfo | None = None  # "calendar day" for the digest guard; None = the system time zone

    # --- one article ---------------------------------------------------------------------

    def process_article(self, row: sqlite3.Row) -> EpisodeResult:
        """Turn one pending article into a published episode, or record the failure."""
        title = row["title"] or row["url"]
        result = EpisodeResult(row["id"], row["url"], title)
        try:
            content = ensure_content(
                self.conn,
                self.http,
                row,
                min_chars=self.config.episode.min_chars_per_article,
                max_attempts=MAX_ATTEMPTS,
            )
            if content.text is None:  # the attempt was already counted by ensure_content
                result.error = f"no article text: {content.error}"
                result.status = content.status
                log.warning(
                    "Article skipped: %s", kv(id=row["id"], url=row["url"], error=content.error)
                )
                return result
            result.guid, result.episode_title = self._build_and_publish(row, title, content.text)
        except PromptError:
            raise  # a broken template would fail every article; stop the run instead
        except Exception as exc:
            result.guid = result.episode_title = None
            result.error = f"{type(exc).__name__}: {exc}"
            result.status = repo.record_article_failure(
                self.conn, row["id"], result.error, max_attempts=MAX_ATTEMPTS
            )
            log.error(
                "Episode failed: %s",
                kv(id=row["id"], url=row["url"], error=result.error, status=result.status),
            )
            log.debug("Traceback for article %s", row["id"], exc_info=True)
        return result

    def _draft(
        self, row: sqlite3.Row, title: str, text: str, today: date
    ) -> tuple[int, str, str, PodcastScript]:
        """The episode draft for this article: an existing one is resumed (script reused, no LLM
        call); otherwise the script is generated and the draft is stored *before* any TTS cost.
        Returns ``(episode id, guid, planned relative MP3 path, script)``."""
        existing = repo.find_draft_for_article(self.conn, row["id"])
        if existing is not None and existing["script_json"] and existing["audio_file"]:
            try:
                script = PodcastScript.model_validate_json(existing["script_json"])
            except ValueError:
                log.warning(
                    "Discarding draft with unreadable script: %s", kv(guid=existing["guid"])
                )
                self._discard_draft(existing)
            else:
                log.info("Resuming draft episode: %s", kv(guid=existing["guid"], id=row["id"]))
                return existing["id"], existing["guid"], existing["audio_file"], script
        elif existing is not None:
            self._discard_draft(existing)

        config = self.config
        article = ScriptArticle(
            url=row["url"],
            title=title,
            text=text,
            source=row["source_name"],
            published=row["published_at"],
        )
        script = generate_script(self.llm, [article], config, self.styles, today=today)
        guid = self.new_guid()
        relpath = episode_relpath(today, script.title, guid)  # fixed now: a resume reuses it
        episode_id = repo.create_episode(
            self.conn,
            guid=guid,
            mode="per_article",
            title=script.title,
            summary=script.summary,
            script=script.model_dump(),
            audio_file=relpath,
            articles=[(row["id"], "discussed")],
        )
        return episode_id, guid, relpath, script

    def _discard_draft(self, draft: sqlite3.Row) -> None:
        repo.delete_episode(self.conn, draft["id"])
        shutil.rmtree(
            episode_work_dir(self.config.paths.data_dir, draft["guid"]), ignore_errors=True
        )

    def _build_and_publish(self, row: sqlite3.Row, title: str, text: str) -> tuple[str, str]:
        episode_id, guid, relpath, script = self._draft(row, title, text, self.now().date())
        self._finish(episode_id, guid, relpath, script)
        return guid, script.title

    def _finish(self, episode_id: int, guid: str, relpath: str, script: PodcastScript) -> None:
        """Synthesize, assemble and publish a draft (shared by both episode modes)."""
        config = self.config
        now = self.now()
        work_dir = episode_work_dir(config.paths.data_dir, guid)
        options = (
            config.tts if isinstance(config.tts, TTSOptions) else TTSOptions(provider=self.tts.name)
        )
        parts = plan_script(script, self.tts, config.roles)
        paths = synthesize_script(script, self.tts, config.roles, work_dir, options, parts=parts)
        mp3 = work_dir / "episode.mp3"
        assembled = self.assemble(
            group_by_segment(parts, paths),
            mp3,
            work_dir,
            cfg=config.audio,
            gap_ms=config.episode.gap_ms,
            meta=EpisodeMetadata(
                title=script.title,
                artist=config.podcast.author,
                album=config.podcast.title,
                date=now.date().isoformat(),
                comment=script.summary,
            ),
            intro=config.episode.intro_file,
            outro=config.episode.outro_file,
        )

        place_episode(mp3, config.paths.output_dir, relpath)
        number = repo.publish_draft_episode(
            self.conn,
            episode_id,
            audio_file=relpath,
            audio_bytes=assembled.size_bytes,
            duration_s=assembled.duration_s,
            # same transaction: if the feed cannot be written, nothing is published
            before_commit=lambda: write_feed(config, self.conn, now=now),
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        log.info(
            "Episode published: %s",
            kv(
                guid=guid,
                number=number,
                title=script.title,
                duration=f"{assembled.duration_s:.0f}s",
            ),
        )

    # --- daily digest --------------------------------------------------------------------

    def _digest_published_today(self) -> bool:
        last = repo.latest_published_at(self.conn, "daily_digest")
        if last is None:
            return False
        published = datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone(self.tz)
        return published.date() == self.now().astimezone(self.tz).date()

    def process_digest(
        self, pending: Sequence[sqlite3.Row], *, force: bool
    ) -> EpisodeResult | None:
        """One episode for all pending articles: the newest ``episode.max_articles`` are
        ``discussed``, the rest ``mentioned`` (show notes only). A draft from an interrupted run
        is continued. At most one digest is published per calendar day unless ``force``.
        Returns None if there was nothing to do."""
        draft = repo.latest_draft(self.conn, "daily_digest")
        if draft is None:
            if not pending:
                return None
            if not force and self._digest_published_today():
                return None
        today = self.now().date()
        ids = [r["id"] for r in pending]
        result = EpisodeResult(ids[0] if ids else 0, "", f"daily digest ({len(pending)} articles)")
        discussed_ids: list[int] = []
        try:
            if draft is not None and draft["script_json"] and draft["audio_file"]:
                episode_id, guid, relpath = draft["id"], draft["guid"], draft["audio_file"]
                script = PodcastScript.model_validate_json(draft["script_json"])
                discussed_ids = [
                    link["article_id"]
                    for link in repo.episode_articles(self.conn, episode_id)
                    if link["role"] == "discussed"
                ]
                log.info("Resuming draft digest: %s", kv(guid=guid))
            else:
                if draft is not None:
                    self._discard_draft(draft)
                built = self._build_digest_draft(pending, today, discussed_ids)
                if isinstance(built, EpisodeResult):
                    return built
                episode_id, guid, relpath, script = built
            result.url = script.sources[0].url if script.sources else ""
            self._finish(episode_id, guid, relpath, script)
            result.guid, result.episode_title = guid, script.title
        except PromptError:
            raise
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.status = "pending"
            # only articles the model was given count; unreadable ones were counted on extraction
            for article_id in discussed_ids:
                result.status = repo.record_article_failure(
                    self.conn, article_id, result.error, max_attempts=MAX_ATTEMPTS
                )
            log.error("Digest failed: %s", kv(articles=len(discussed_ids), error=result.error))
            log.debug("Traceback for the digest", exc_info=True)
        return result

    def _build_digest_draft(
        self, pending: Sequence[sqlite3.Row], today: date, discussed_ids: list[int]
    ) -> tuple[int, str, str, PodcastScript] | EpisodeResult:
        """Pick the articles, fetch their text, generate the script and store the draft. The ids
        of the articles given to the model are appended to ``discussed_ids`` (so a failure can
        be booked against them). Returns an ``EpisodeResult`` (failure) if none of the articles
        has usable text."""
        episode = self.config.episode
        newest_first = sorted(
            pending, key=lambda r: r["published_at"] or r["discovered_at"], reverse=True
        )
        chosen, mentioned = (
            newest_first[: episode.max_articles],
            newest_first[episode.max_articles :],
        )

        articles: list[ScriptArticle] = []
        discussed: list[sqlite3.Row] = []
        errors: list[str] = []
        for row in chosen:
            content = ensure_content(
                self.conn,
                self.http,
                row,
                min_chars=episode.min_chars_per_article,
                max_attempts=MAX_ATTEMPTS,
            )
            if content.text is None:  # the attempt was counted by ensure_content
                errors.append(f"{row['url']}: {content.error}")
                continue
            discussed.append(row)
            discussed_ids.append(row["id"])
            articles.append(
                ScriptArticle(
                    url=row["url"],
                    title=row["title"] or row["url"],
                    text=content.text,
                    source=row["source_name"],
                    published=row["published_at"],
                )
            )
        if not articles:
            return EpisodeResult(
                pending[0]["id"],
                pending[0]["url"],
                f"daily digest ({len(pending)} articles)",
                error="no article text: " + "; ".join(errors),
            )

        script = generate_script(
            self.llm, articles, self.config, self.styles, today=today, mode="daily_digest"
        )
        guid = self.new_guid()
        relpath = episode_relpath(today, script.title, guid)
        episode_id = repo.create_episode(
            self.conn,
            guid=guid,
            mode="daily_digest",
            title=script.title,
            summary=script.summary,
            script=script.model_dump(),
            audio_file=relpath,
            articles=[(r["id"], "discussed") for r in discussed]
            + [(r["id"], "mentioned") for r in mentioned],
        )
        return episode_id, guid, relpath, script

    # --- the run -------------------------------------------------------------------------

    def run(self, sources: Sequence[SourceConfig], *, force: bool = False) -> RunSummary:
        """Discover, then process the pending articles (oldest first, at most
        ``episode.max_episodes_per_run``), then apply the retention policy.

        ``force`` first puts ``failed`` and ``skipped`` articles back to ``pending``.
        """
        started = time.monotonic()
        config = self.config
        if force:
            requeued = repo.requeue_articles(self.conn)
            log.info("Requeued failed and skipped articles: %s", kv(count=requeued))
        for draft in repo.stale_drafts(self.conn):
            log.info("Discarding stale draft episode: %s", kv(guid=draft["guid"]))
            self._discard_draft(draft)
        discovery = discover(
            self.conn,
            [s for s in sources if s.enabled],
            self.http,
            max_article_age_days=config.episode.max_article_age_days,
            now=self.now,
        )
        summary = RunSummary(discovery)

        if config.episode.mode == "daily_digest":
            pending = repo.list_pending_articles(self.conn)
            result = self.process_digest(pending, force=force)
            if result is not None:
                summary.results.append(result)
            elif pending:
                summary.notes.append(
                    "A daily digest was already published today (use --force for another one)"
                )
        else:
            pending = repo.list_pending_articles(self.conn, config.episode.max_episodes_per_run)
            for row in pending:
                summary.results.append(self.process_article(row))

        report = apply_retention(
            self.conn,
            config.paths.output_dir,
            config.paths.data_dir,
            config.feed.retention,
            now=self.now(),
        )
        summary.pruned = len(report.pruned)
        if report.pruned:
            write_feed(config, self.conn, now=self.now())

        summary.duration_s = time.monotonic() - started
        log.info(
            "Run finished: %s",
            kv(
                published=len(summary.published),
                failed=len(summary.failed),
                pruned=summary.pruned,
                sources_failed=discovery.errors,
                duration=f"{summary.duration_s:.1f}s",
            ),
        )
        return summary
