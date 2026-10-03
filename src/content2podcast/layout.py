"""Static output layout: ``feed.xml``, ``cover.<ext>`` and ``episodes/<date>-<slug>-<id>.mp3``.

Everything is plain files, so any web server can serve the output directory.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

from content2podcast.slug import slugify

log = logging.getLogger(__name__)

FEED_FILENAME = "feed.xml"
EPISODES_DIR = "episodes"
COVER_STEM = "cover"
SHORT_GUID_LENGTH = 8
EPISODE_SLUG_MAX = 50
COVER_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def short_guid(guid: str) -> str:
    """The first characters of a guid that are safe in a file name."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", guid).lower()
    return (
        cleaned[:SHORT_GUID_LENGTH] or hashlib.sha256(guid.encode()).hexdigest()[:SHORT_GUID_LENGTH]
    )


def episode_relpath(published: date | str, title: str, guid: str) -> str:
    """``episodes/<YYYY-MM-DD>-<slug>-<short-guid>.mp3`` (relative to the output directory)."""
    day = published.isoformat() if isinstance(published, date) else str(published)[:10]
    slug = slugify(title, "episode", EPISODE_SLUG_MAX)
    return f"{EPISODES_DIR}/{day}-{slug}-{short_guid(guid)}.mp3"


def public_url(base_url: str, relpath: str) -> str:
    """``base_url`` (with or without trailing slash) plus the URL-encoded relative path."""
    return base_url.rstrip("/") + "/" + quote(relpath.lstrip("/"), safe="/")


def feed_url(base_url: str) -> str:
    return public_url(base_url, FEED_FILENAME)


def place_file(src: Path, dest: Path) -> Path:
    """Copy ``src`` to ``dest`` atomically: to a temp file in the target directory, then
    ``os.replace``. Parent directories are created."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def write_atomic(dest: Path, data: bytes) -> Path:
    """Write ``data`` to ``dest`` atomically (temp file in the same directory + ``os.replace``)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def place_episode(src: Path, output_dir: Path, relpath: str) -> Path:
    """Put a finished MP3 at ``output_dir/relpath``."""
    return place_file(src, output_dir / relpath)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class CoverResult:
    relpath: str  # e.g. "cover.jpg", relative to the output directory
    copied: bool  # False if the file in the output directory was already identical


def sync_cover(cover_image: Path | None, output_dir: Path) -> CoverResult | None:
    """Copy ``podcast.cover_image`` to ``cover.<ext>`` in the output directory, only if the
    content changed. Returns None without a cover image."""
    if cover_image is None:
        return None
    if not cover_image.is_file():
        raise FileNotFoundError(f"podcast.cover_image not found: {cover_image}")
    extension = cover_image.suffix.lower()
    if extension not in COVER_EXTENSIONS:
        log.warning("Cover image is not a jpg or png, podcast apps may reject it: %s", cover_image)
    relpath = f"{COVER_STEM}{extension}"
    target = output_dir / relpath
    if target.is_file() and _digest(target) == _digest(cover_image):
        return CoverResult(relpath, copied=False)
    place_file(cover_image, target)
    # a cover with another extension from an earlier run would be stale
    for old in output_dir.glob(f"{COVER_STEM}.*"):
        if old != target and old.suffix.lower() in COVER_EXTENSIONS:
            old.unlink()
    return CoverResult(relpath, copied=True)
