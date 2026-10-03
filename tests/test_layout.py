import logging
from datetime import date
from pathlib import Path

import pytest

from content2podcast.layout import (
    CoverResult,
    episode_relpath,
    feed_url,
    place_episode,
    public_url,
    short_guid,
    sync_cover,
)
from content2podcast.slug import slugify

# --- slugs -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Über Äpfel & Birnen", "ueber-aepfel-birnen"),
        ("Größe, Maße und Füße", "groesse-masse-und-fuesse"),
        ("ÖL-Preise: ÄHNLICH?", "oel-preise-aehnlich"),
        ("  --Hallo,   Welt!!  ", "hallo-welt"),
        ("Python 3.12 ist da", "python-3-12-ist-da"),
        ("Café déjà vu", "cafe-deja-vu"),
        ("日本語", "script"),  # nothing ASCII left -> fallback
        ("", "script"),
        ("***", "script"),
    ],
)
def test_slugify(text, expected):
    assert slugify(text) == expected


def test_slugify_fallback_and_length_limit():
    assert slugify("!!!", fallback="episode") == "episode"
    long = slugify("wort " * 50)
    assert len(long) <= 60 and not long.endswith("-")
    short = slugify("alpha beta gamma delta", max_length=11)
    assert short == "alpha-beta" and len(short) <= 11  # cut at 11, trailing dash removed


# --- episode paths -----------------------------------------------------------------------


def test_short_guid():
    assert short_guid("3F2504E0-4F89-11D3-9A0C-0305E82C3301") == "3f2504e0"
    assert short_guid("ab") == "ab"
    assert short_guid("---")  # nothing usable: derived from a hash instead
    assert len(short_guid("---")) == 8


def test_episode_relpath_layout():
    rel = episode_relpath(date(2026, 10, 3), "Über Äpfel & Birnen", "3f2504e0-4f89-11d3")
    assert rel == "episodes/2026-10-03-ueber-aepfel-birnen-3f2504e0.mp3"


def test_episode_relpath_accepts_iso_timestamps_and_limits_the_slug():
    rel = episode_relpath("2026-10-03T05:30:00Z", "x " * 100, "abcdef012345")
    assert rel.startswith("episodes/2026-10-03-x-x-") and rel.endswith("-abcdef01.mp3")
    slug = rel.removeprefix("episodes/2026-10-03-").removesuffix("-abcdef01.mp3")
    assert len(slug) <= 50 and not slug.endswith("-")


def test_episode_relpath_without_usable_title():
    assert episode_relpath(date(2026, 1, 2), "???", "g1") == "episodes/2026-01-02-episode-g1.mp3"


# --- URLs --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "rel", "expected"),
    [
        ("http://nas.local:8080", "feed.xml", "http://nas.local:8080/feed.xml"),
        ("http://nas.local:8080/", "feed.xml", "http://nas.local:8080/feed.xml"),
        ("http://nas.local:8080//", "/feed.xml", "http://nas.local:8080/feed.xml"),
        (
            "https://host.ts.net/podcast/",
            "episodes/a.mp3",
            "https://host.ts.net/podcast/episodes/a.mp3",
        ),
        (
            "https://host.ts.net/podcast",
            "episodes/a.mp3",
            "https://host.ts.net/podcast/episodes/a.mp3",
        ),
        ("http://h", "episodes/a b.mp3", "http://h/episodes/a%20b.mp3"),
        ("http://h", "episodes/ä#?.mp3", "http://h/episodes/%C3%A4%23%3F.mp3"),
    ],
)
def test_public_url(base, rel, expected):
    assert public_url(base, rel) == expected


def test_feed_url():
    assert feed_url("http://h:8080/") == "http://h:8080/feed.xml"


# --- placing files -----------------------------------------------------------------------


def test_place_episode_creates_directories_and_is_atomic(tmp_path):
    src = tmp_path / "work" / "out.mp3"
    src.parent.mkdir()
    src.write_bytes(b"audio-1")
    out = tmp_path / "public"
    target = place_episode(src, out, "episodes/2026-10-03-x-abc.mp3")
    assert target == out / "episodes" / "2026-10-03-x-abc.mp3"
    assert target.read_bytes() == b"audio-1" and src.exists()  # copied, not moved
    assert not list(out.rglob("*.part"))


def test_place_episode_replaces_an_existing_file(tmp_path):
    src = tmp_path / "a.mp3"
    out = tmp_path / "public"
    src.write_bytes(b"new")
    (out / "episodes").mkdir(parents=True)
    (out / "episodes" / "e.mp3").write_bytes(b"old")
    place_episode(src, out, "episodes/e.mp3")
    assert (out / "episodes" / "e.mp3").read_bytes() == b"new"


def test_failed_copy_leaves_no_part_file_and_keeps_the_old_file(tmp_path):
    out = tmp_path / "public"
    (out / "episodes").mkdir(parents=True)
    (out / "episodes" / "e.mp3").write_bytes(b"old")
    with pytest.raises(FileNotFoundError):
        place_episode(tmp_path / "missing.mp3", out, "episodes/e.mp3")
    assert (out / "episodes" / "e.mp3").read_bytes() == b"old"
    assert not list(out.rglob("*.part"))


# --- cover -------------------------------------------------------------------------------


def test_no_cover_image(tmp_path):
    assert sync_cover(None, tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_cover_is_copied_only_when_changed(tmp_path):
    cover = tmp_path / "src" / "my picture.png"
    cover.parent.mkdir()
    cover.write_bytes(b"png-1")
    out = tmp_path / "public"
    assert sync_cover(cover, out) == CoverResult("cover.png", copied=True)
    assert (out / "cover.png").read_bytes() == b"png-1"
    mtime = (out / "cover.png").stat().st_mtime_ns

    assert sync_cover(cover, out) == CoverResult("cover.png", copied=False)
    assert (out / "cover.png").stat().st_mtime_ns == mtime  # untouched

    cover.write_bytes(b"png-2")
    assert sync_cover(cover, out) == CoverResult("cover.png", copied=True)
    assert (out / "cover.png").read_bytes() == b"png-2"


def test_cover_with_a_new_extension_replaces_the_old_one(tmp_path):
    out = tmp_path / "public"
    png = tmp_path / "a.PNG"
    png.write_bytes(b"1")
    sync_cover(png, out)
    jpg = tmp_path / "b.jpg"
    jpg.write_bytes(b"2")
    assert sync_cover(jpg, out).relpath == "cover.jpg"
    assert sorted(p.name for p in out.iterdir()) == ["cover.jpg"]


def test_cover_extension_is_lowercased_and_unusual_types_warn(tmp_path, caplog):
    out = tmp_path / "public"
    upper = tmp_path / "x.JPG"
    upper.write_bytes(b"1")
    assert sync_cover(upper, out).relpath == "cover.jpg"
    gif = tmp_path / "x.gif"
    gif.write_bytes(b"1")
    with caplog.at_level(logging.WARNING):
        assert sync_cover(gif, out).relpath == "cover.gif"
    assert any("jpg or png" in r.message for r in caplog.records)


def test_missing_cover_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="podcast.cover_image"):
        sync_cover(Path(tmp_path / "nope.jpg"), tmp_path / "public")
