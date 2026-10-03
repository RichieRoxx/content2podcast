"""Command line interface."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from content2podcast import __version__
from content2podcast import repository as repo
from content2podcast.audio import AudioError, EpisodeMetadata, assemble_episode, find_tools
from content2podcast.config import (
    AppConfig,
    ConfigError,
    Secrets,
    SourceConfig,
    SourcesConfig,
    load_config,
    load_secrets,
    load_sources,
    resolve_config_path,
)
from content2podcast.db import connect, db_path
from content2podcast.http import HttpError, make_client
from content2podcast.lock import RunLocked, run_lock
from content2podcast.logging_setup import setup_logging
from content2podcast.providers.llm.base import LLMError, LLMProvider
from content2podcast.providers.registry import ProviderNotConfiguredError, build_llm, build_tts
from content2podcast.providers.tts.base import NEUTRAL, TTSError, TTSOptions, allowed_styles
from content2podcast.script.adhoc import fetch_article
from content2podcast.script.dryrun import dry_run as run_dry_run
from content2podcast.script.dryrun import dry_run_root
from content2podcast.script.generator import ScriptArticle, generate_script
from content2podcast.script.models import ScriptError, load_script
from content2podcast.script.output import unique_dir, write_script_files
from content2podcast.script.prompt import PromptError
from content2podcast.slug import slugify
from content2podcast.sources.discovery import DiscoveryReport, SourceReport, discover
from content2podcast.sources.models import SourceError
from content2podcast.speech import group_by_segment, plan_script, synthesize_script

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_CONFIG_ERROR = 2

log = logging.getLogger(__name__)

app = typer.Typer(help="Turn blog and news articles into podcast episodes.", no_args_is_help=True)
feed_app = typer.Typer(help="Feed maintenance.", no_args_is_help=True)
sources_app = typer.Typer(help="Inspect configured sources.", no_args_is_help=True)
episodes_app = typer.Typer(help="Inspect generated episodes.", no_args_is_help=True)
app.add_typer(feed_app, name="feed")
app.add_typer(sources_app, name="sources")
app.add_typer(episodes_app, name="episodes")


@dataclass
class AppContext:
    """Shared CLI state. The config loads on first access (once), so ``--help`` never needs it."""

    config_path: Path | None = None
    _config: AppConfig | None = field(default=None, repr=False)
    _secrets: Secrets | None = field(default=None, repr=False)

    @property
    def config(self) -> AppConfig:
        if self._config is None:
            self._config = load_config(self.config_path)
        return self._config

    @property
    def secrets(self) -> Secrets:
        """Credentials from the environment and the ``.env`` next to the config file."""
        if self._secrets is None:
            self._secrets = load_secrets(resolve_config_path(self.config_path).parent / ".env")
        return self._secrets


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code)


def _load(ctx: typer.Context) -> AppConfig:
    """Return the config, turning a ``ConfigError`` into one message and exit code 2."""
    try:
        return ctx.obj.config
    except ConfigError as exc:
        raise _fail(str(exc), EXIT_CONFIG_ERROR) from None


def _stub(ctx: typer.Context, name: str) -> None:
    _load(ctx)
    typer.echo(f"{name}: not implemented yet")


SourceOption = Annotated[str | None, typer.Option("--source", help="Only this source (by name).")]


@dataclass
class Workspace:
    config: AppConfig
    sources: SourcesConfig
    conn: sqlite3.Connection


@contextmanager
def _workspace(ctx: typer.Context) -> Iterator[Workspace]:
    """Config, sources file and database; config problems end the command with exit code 2."""
    config = _load(ctx)
    try:
        sources = load_sources(config.paths.sources_file)
    except ConfigError as exc:
        raise _fail(str(exc), EXIT_CONFIG_ERROR) from None
    conn = connect(db_path(config.paths.data_dir))
    try:
        yield Workspace(config, sources, conn)
    finally:
        conn.close()


def _select_sources(workspace: Workspace, name: str | None) -> list[SourceConfig]:
    sources = workspace.sources.sources
    if name is None:
        return list(sources)
    selected = [s for s in sources if s.name == name]
    if not selected:
        known = ", ".join(s.name for s in sources) or "none"
        raise _fail(f"Unknown source {name!r}. Configured sources: {known}", EXIT_CONFIG_ERROR)
    return selected


def _run_discovery(workspace: Workspace, sources: list[SourceConfig]) -> DiscoveryReport:
    with make_client(workspace.config.http) as http:
        return discover(
            workspace.conn,
            sources,
            http,
            max_article_age_days=workspace.config.episode.max_article_age_days,
        )


def _print_summary(report: DiscoveryReport) -> None:
    typer.echo(
        f"Summary: {report.new} new, {report.baseline} baseline, "
        f"{report.skipped} skipped, {report.errors} error(s)"
    )


def _exit_if_all_failed(report: DiscoveryReport) -> None:
    """Exit 1 only if every checked source failed."""
    if report.sources and report.errors == len(report.sources):
        raise typer.Exit(EXIT_RUNTIME_ERROR)


def _print_check_line(source: SourceReport) -> None:
    if source.error:
        typer.echo(f"{source.name}: ERROR {source.error}")
    elif source.not_modified:
        typer.echo(f"{source.name}: not modified")
    elif source.baselined:
        typer.echo(f"{source.name}: baseline set ({source.baseline} existing articles)")
    else:
        typer.echo(f"{source.name}: {source.new} new, {source.skipped} skipped")
    for article in source.new_articles:
        published = (article.published_at or "no date")[:10]
        typer.echo(f"  - {article.title or '(no title)'} ({published})")
        typer.echo(f"    {article.url}")


def _checked_sources(workspace: Workspace, name: str | None) -> list[SourceConfig]:
    sources = _select_sources(workspace, name)
    if name is not None and not sources[0].enabled:
        raise _fail(f"Source {name!r} is disabled.", EXIT_CONFIG_ERROR)
    return sources


def _llm_and_styles(ctx: typer.Context, config: AppConfig) -> tuple[LLMProvider, list[str]]:
    """The configured LLM and the speaking styles the TTS provider supports for both voices."""
    try:
        secrets = ctx.obj.secrets
        llm = build_llm(config.llm, secrets)
        if config.tts is None:
            typer.echo(
                "Note: no tts provider configured; scripts will only use the 'neutral' style.",
                err=True,
            )
            return llm, [NEUTRAL]
        tts = build_tts(config.tts, secrets)
    except (ProviderNotConfiguredError, ConfigError) as exc:
        raise _fail(str(exc), EXIT_CONFIG_ERROR) from None
    return llm, allowed_styles(tts, config.roles.host.voice, config.roles.expert.voice)


def _speaker_names(config: AppConfig) -> dict[str, str]:
    return {"host": config.roles.host.name, "expert": config.roles.expert.name}


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"content2podcast {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Config file (default: $C2P_CONFIG or ./config.yaml)."),
    ] = None,
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose", "-v", count=True, help="Debug logging; repeat (-vv) to include libraries."
        ),
    ] = 0,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only warnings and errors.")] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Turn blog and news articles into podcast episodes."""
    if quiet and verbose:
        raise _fail("--quiet and --verbose cannot be combined.", EXIT_CONFIG_ERROR)
    setup_logging(verbose, quiet)
    ctx.obj = AppContext(config_path=config)


@app.command()
def check(ctx: typer.Context, source: SourceOption = None) -> None:
    """Check the sources for new articles and list them.

    Articles found are stored (new ones as pending); a source seen for the first time is only
    baselined. Exits with 1 only if every checked source failed.
    """
    with _workspace(ctx) as workspace:
        report = _run_discovery(workspace, _checked_sources(workspace, source))
    for source_report in report.sources:
        _print_check_line(source_report)
    _print_summary(report)
    _exit_if_all_failed(report)


def _dry_run(ctx: typer.Context) -> None:
    with _workspace(ctx) as workspace:
        config = workspace.config
        llm, styles = _llm_and_styles(ctx, config)
        enabled = [s for s in workspace.sources.sources if s.enabled]
        report = _run_discovery(workspace, enabled)
        for source_report in report.sources:
            _print_check_line(source_report)
        _print_summary(report)
        today = date.today()
        try:
            with make_client(config.http) as http:
                items = run_dry_run(workspace.conn, http, config, llm, styles, today=today)
        except PromptError as exc:
            raise _fail(str(exc), EXIT_CONFIG_ERROR) from None
    if not items:
        typer.echo("Dry run: no pending articles.")
        return
    written = [i for i in items if i.script is not None]
    typer.echo(
        f"Dry run: {len(written)} of {len(items)} script(s) written to "
        f"{dry_run_root(config, today)}"
    )
    for item in items:
        if item.script is not None:
            minutes = item.script.estimated_minutes(config.episode.words_per_minute)
            typer.echo(f"  - {item.title} ({item.script.word_count} words, ~{minutes:.1f} min)")
            typer.echo(f"    {item.directory}")
        else:
            typer.echo(f"  - FAILED {item.title}: {item.error}")
    if not written:
        raise typer.Exit(EXIT_RUNTIME_ERROR)


@app.command()
def run(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Discover, extract and write scripts to data_dir/dry-run/; no TTS, no feed, "
            "articles stay pending.",
        ),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Reprocess already seen articles.")
    ] = False,
) -> None:
    """Fetch new articles and generate episodes."""
    config = _load(ctx)
    try:
        with run_lock(config.paths.data_dir):
            if dry_run:
                _dry_run(ctx)
            else:
                typer.echo("run: not implemented yet")
    except RunLocked as exc:
        raise _fail(f"Cannot start: {exc}", RunLocked.exit_code) from None


MODES = ("per_article", "daily_digest")


@app.command()
def script(
    ctx: typer.Context,
    urls: Annotated[list[str], typer.Argument(help="Article URLs.")],
    mode: Annotated[
        str | None,
        typer.Option(
            "--mode", help="per_article (one script per URL) or daily_digest (one script)."
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("-o", "--output", help="Output directory (default: data_dir/adhoc/<date>)."),
    ] = None,
) -> None:
    """Write scripts for article URLs without touching the database (for prompt tuning)."""
    config = _load(ctx)
    mode = mode or config.episode.mode
    if mode not in MODES:
        raise _fail(f"Unknown mode {mode!r}. Use one of: {', '.join(MODES)}", EXIT_CONFIG_ERROR)
    llm, styles = _llm_and_styles(ctx, config)
    today = date.today()
    base = output or config.paths.data_dir / "adhoc" / today.isoformat()

    articles: list[ScriptArticle] = []
    failures = 0
    with make_client(config.http) as http:
        for url in urls:
            try:
                articles.append(fetch_article(http, url))
            except (HttpError, SourceError) as exc:
                failures += 1
                typer.echo(f"FAILED {url}: {exc}", err=True)

    groups = [[a] for a in articles] if mode == "per_article" else ([articles] if articles else [])
    written = 0
    for group in groups:
        try:
            result = generate_script(llm, group, config, styles, today=today, mode=mode)
        except PromptError as exc:
            raise _fail(str(exc), EXIT_CONFIG_ERROR) from None
        except (LLMError, ScriptError) as exc:
            failures += 1
            typer.echo(f"FAILED {group[0].url}: {exc}", err=True)
            continue
        directory = unique_dir(base, slugify(result.title), str(written + 1))
        write_script_files(result, directory, _speaker_names(config))
        minutes = result.estimated_minutes(config.episode.words_per_minute)
        typer.echo(f"{result.title} ({result.word_count} words, ~{minutes:.1f} min)")
        typer.echo(f"  {directory}")
        written += 1
    if not written:
        raise typer.Exit(EXIT_RUNTIME_ERROR)


@app.command()
def tts(
    ctx: typer.Context,
    script_file: Annotated[Path, typer.Argument(help="script.json to voice.")],
    output: Annotated[
        Path | None,
        typer.Option("-o", "--output", help="MP3 to write (default: next to the script)."),
    ] = None,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir", help="Cache for synthesized parts (default: data_dir/tts-work)."
        ),
    ] = None,
) -> None:
    """Voice an existing script and assemble the MP3 (no database, no feed)."""
    config = _load(ctx)
    try:
        script = load_script(script_file)
    except ScriptError as exc:
        raise _fail(str(exc), EXIT_CONFIG_ERROR) from None
    try:
        provider = build_tts(config.tts, ctx.obj.secrets)
    except (ProviderNotConfiguredError, ConfigError) as exc:
        raise _fail(str(exc), EXIT_CONFIG_ERROR) from None
    try:
        find_tools()  # fail before any (paid) synthesis request if ffmpeg is missing
    except AudioError as exc:
        raise _fail(str(exc), EXIT_RUNTIME_ERROR) from None

    work_dir = work_dir or config.paths.data_dir / "tts-work"
    output = output or script_file.with_suffix(".mp3")
    options = (
        config.tts if isinstance(config.tts, TTSOptions) else TTSOptions(provider=provider.name)
    )
    parts = plan_script(script, provider, config.roles)
    chars = sum(len(p.text) for p in parts)
    try:
        paths = synthesize_script(script, provider, config.roles, work_dir, options, parts=parts)
        result = assemble_episode(
            group_by_segment(parts, paths),
            output,
            work_dir,
            cfg=config.audio,
            gap_ms=config.episode.gap_ms,
            meta=EpisodeMetadata(
                title=script.title,
                artist=config.podcast.author,
                album=config.podcast.title,
                date=date.today().isoformat(),
                comment=script.summary,
            ),
            intro=config.episode.intro_file,
            outro=config.episode.outro_file,
        )
    except (TTSError, AudioError) as exc:
        raise _fail(str(exc), EXIT_RUNTIME_ERROR) from None
    typer.echo(f"Wrote {result.path}")
    typer.echo(
        f"  duration {result.duration_s:.1f} s, {chars} characters in {len(parts)} part(s), "
        f"{result.size_bytes / 1024:.0f} KiB"
    )


@feed_app.command("rebuild")
def feed_rebuild(ctx: typer.Context) -> None:
    """Rebuild the RSS feed from stored episodes."""
    _stub(ctx, "feed rebuild")


def _table(rows: list[list[str]], header: list[str]) -> list[str]:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    return [
        "  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=True)).rstrip()
        for r in [header, *rows]
    ]


def _source_state(source: SourceConfig | None) -> str:
    if source is None:
        return "removed"  # in the database but no longer configured
    return "enabled" if source.enabled else "disabled"


@sources_app.command("list")
def sources_list(ctx: typer.Context) -> None:
    """List sources: state, last check and article counts by status."""
    with _workspace(ctx) as workspace:
        rows_by_name = {r["name"]: r for r in repo.list_sources(workspace.conn)}
        counts = repo.article_counts(workspace.conn)
    configured = {s.name: s for s in workspace.sources.sources}

    rows: list[list[str]] = []
    errors: list[str] = []
    for name in [*configured, *sorted(set(rows_by_name) - set(configured))]:
        source, row = configured.get(name), rows_by_name.get(name)
        state = _source_state(source)
        by_status = counts.get(row["id"], {}) if row else {}
        rows.append(
            [
                name,
                source.type if source else row["type"],
                state,
                (row["baseline_at"] if row else None) or "no",
                (row["last_checked_at"] if row else None) or "never",
                (row["last_success_at"] if row else None) or "never",
                " ".join(f"{k}={v}" for k, v in sorted(by_status.items())) or "-",
            ]
        )
        if row and row["last_error"]:
            errors.append(f"{name}: {row['last_error']}")
    header = ["NAME", "TYPE", "STATE", "BASELINE", "LAST CHECK", "LAST SUCCESS", "ARTICLES"]
    for line in _table(rows, header):
        typer.echo(line)
    for error in errors:
        typer.echo(f"last error - {error}")


@sources_app.command("baseline")
def sources_baseline(
    ctx: typer.Context,
    source: SourceOption = None,
    reset: Annotated[
        bool,
        typer.Option(
            "--reset",
            help="Re-baseline sources that already have a baseline (e.g. after a selector "
            "change); their pending articles become baseline.",
        ),
    ] = False,
) -> None:
    """Mark everything currently visible in sources without a baseline as seen.

    Nothing is turned into an episode for these articles.
    """
    with _workspace(ctx) as workspace:
        candidates = [s for s in _checked_sources(workspace, source) if s.enabled]
        targets: list[SourceConfig] = []
        for candidate in candidates:
            row = repo.get_source(workspace.conn, candidate.name)
            has_baseline = row is not None and row["baseline_at"] is not None
            if has_baseline and not reset:
                typer.echo(f"{candidate.name}: already baselined (use --reset to redo)")
                continue
            if reset and row is not None:
                repo.reset_baseline(workspace.conn, row["id"])
            targets.append(candidate)
        report = _run_discovery(workspace, targets) if targets else DiscoveryReport()
    for source_report in report.sources:
        if source_report.error:
            typer.echo(f"{source_report.name}: ERROR {source_report.error}")
        else:
            known = f", {source_report.known} already known" if source_report.known else ""
            typer.echo(
                f"{source_report.name}: baseline set ({source_report.baseline} articles{known})"
            )
    _exit_if_all_failed(report)


@episodes_app.command("list")
def episodes_list(ctx: typer.Context) -> None:
    """List generated episodes."""
    _stub(ctx, "episodes list")


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Diagnose the environment (ffmpeg, credentials, connectivity)."""
    _stub(ctx, "doctor")


@app.command()
def daemon(ctx: typer.Context) -> None:
    """Run on the configured schedule."""
    _stub(ctx, "daemon")


@app.command()
def health(ctx: typer.Context) -> None:
    """Exit 0 if the last run was healthy (for container health checks)."""
    _stub(ctx, "health")
