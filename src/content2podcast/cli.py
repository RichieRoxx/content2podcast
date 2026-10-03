"""Command line interface."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import typer

from content2podcast import __version__
from content2podcast.config import AppConfig, ConfigError, load_config
from content2podcast.logging_setup import setup_logging

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

    @property
    def config(self) -> AppConfig:
        if self._config is None:
            self._config = load_config(self.config_path)
        return self._config


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
def check(ctx: typer.Context) -> None:
    """Validate configuration and sources."""
    _stub(ctx, "check")


@app.command()
def run(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Do everything except publish.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Reprocess already seen articles.")
    ] = False,
) -> None:
    """Fetch new articles and generate episodes."""
    _stub(ctx, "run")


@app.command()
def script(ctx: typer.Context) -> None:
    """Generate dialogue scripts only."""
    _stub(ctx, "script")


@app.command()
def tts(ctx: typer.Context) -> None:
    """Synthesize audio from existing scripts."""
    _stub(ctx, "tts")


@feed_app.command("rebuild")
def feed_rebuild(ctx: typer.Context) -> None:
    """Rebuild the RSS feed from stored episodes."""
    _stub(ctx, "feed rebuild")


@sources_app.command("list")
def sources_list(ctx: typer.Context) -> None:
    """List configured sources."""
    _stub(ctx, "sources list")


@sources_app.command("baseline")
def sources_baseline(ctx: typer.Context) -> None:
    """Mark all current articles as seen without generating episodes."""
    _stub(ctx, "sources baseline")


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
