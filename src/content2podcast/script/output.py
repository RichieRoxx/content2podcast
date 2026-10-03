"""Writing ``script.json`` / ``script.md`` into per-script directories."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from content2podcast.script.models import PodcastScript, save_script
from content2podcast.script.render import render_markdown


def unique_dir(base: Path, slug: str, suffix: str) -> Path:
    """``base/slug``, or ``base/slug-suffix`` if that already exists."""
    target = base / slug
    return base / f"{slug}-{suffix}" if target.exists() else target


def write_script_files(
    script: PodcastScript, directory: Path, speaker_names: Mapping[str, str] | None = None
) -> tuple[Path, Path]:
    """Write ``script.json`` and ``script.md`` into ``directory`` and return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    json_path, md_path = directory / "script.json", directory / "script.md"
    save_script(script, json_path)
    md_path.write_text(render_markdown(script, speaker_names), encoding="utf-8")
    return json_path, md_path
