"""Writing ``script.json`` / ``script.md`` into per-script directories."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from content2podcast.script.models import PodcastScript, save_script
from content2podcast.script.render import render_markdown

SLUG_MAX = 60


def slugify(text: str, fallback: str = "script") -> str:
    """Lowercase ASCII slug (``Über Äpfel & Birnen`` -> ``ueber-aepfel-birnen``)."""
    text = text.replace("ß", "ss")
    for char, repl in (
        ("ä", "ae"),
        ("ö", "oe"),
        ("ü", "ue"),
        ("Ä", "Ae"),
        ("Ö", "Oe"),
        ("Ü", "Ue"),
    ):
        text = text.replace(char, repl)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:SLUG_MAX].strip("-")
    return slug or fallback


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
