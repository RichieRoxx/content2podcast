"""Command line interface."""

import typer

from content2podcast import __version__

app = typer.Typer(help="Turn blog and news articles into podcast episodes.", no_args_is_help=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"content2podcast {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Turn blog and news articles into podcast episodes."""
