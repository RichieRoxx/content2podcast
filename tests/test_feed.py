import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import feedparser
import pytest

from content2podcast import repository as repo
from content2podcast.config import AppConfig
from content2podcast.db import connect
from content2podcast.feed import (
    ATOM,
    CONTENT,
    ITUNES,
    FeedEpisode,
    FeedSource,
    build_feed,
    format_duration,
    load_feed_episodes,
    show_notes_html,
    write_feed,
)

NOW = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)


def config(**overrides) -> AppConfig:
    cfg = AppConfig()
    cfg.podcast.title = "Mein Podcast"
    cfg.podcast.description = "Artikel zum Hören"
    cfg.podcast.author = "Erika Muster"
    cfg.feed.base_url = "https://nas.example.ts.net:8443/"
    for key, value in overrides.items():
        setattr(cfg.podcast, key, value)
    return cfg


def episode(n=1, **kwargs) -> FeedEpisode:
    defaults = {
        "guid": f"guid-{n}",
        "title": f"Folge {n}",
        "summary": f"Zusammenfassung {n}.",
        "audio_file": f"episodes/2026-10-0{n}-folge-{n}-guid{n}.mp3",
        "audio_bytes": 1_000_000 + n,
        "duration_s": 62.5 + n,
        "published_at": f"2026-10-0{n}T05:30:00Z",
        "number": n,
        "sources": [
            FeedSource(f"Artikel {n}", f"https://blog.example.com/{n}", "Blog", "discussed")
        ],
    }
    return FeedEpisode(**{**defaults, **kwargs})


def root_of(data: bytes) -> ET.Element:
    return ET.fromstring(data)


def ns(prefix: str, tag: str) -> str:
    uri = {"itunes": ITUNES, "content": CONTENT, "atom": ATOM}[prefix]
    return f"{{{uri}}}{tag}"


def channel_of(data: bytes) -> ET.Element:
    return root_of(data).find("channel")


# --- structure ---------------------------------------------------------------------------


def test_declaration_and_encoding():
    data = build_feed(config(), [episode()], now=NOW)
    assert data.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n<rss ')
    assert root_of(data).get("version") == "2.0"
    data.decode("utf-8")  # valid UTF-8


def test_channel_tags():
    cfg = config(explicit=False, language="de-DE", category="Technology")
    channel = channel_of(build_feed(cfg, [episode()], cover_relpath="cover.jpg", now=NOW))
    text = lambda tag: channel.findtext(tag)  # noqa: E731
    assert text("title") == "Mein Podcast"
    assert text("link") == "https://nas.example.ts.net:8443/"
    assert text("description") == "Artikel zum Hören"
    assert text("language") == "de-DE"
    assert text("generator").startswith("content2podcast ")
    assert parsedate_to_datetime(text("lastBuildDate")) == NOW
    atom_link = channel.find(ns("atom", "link"))
    assert atom_link.attrib == {
        "href": "https://nas.example.ts.net:8443/feed.xml",
        "rel": "self",
        "type": "application/rss+xml",
    }
    assert text(ns("itunes", "author")) == "Erika Muster"
    assert channel.find(ns("itunes", "owner")).findtext(ns("itunes", "name")) == "Erika Muster"
    assert channel.find(ns("itunes", "category")).get("text") == "Technology"
    assert text(ns("itunes", "explicit")) == "false"
    assert text(ns("itunes", "type")) == "episodic"
    cover = "https://nas.example.ts.net:8443/cover.jpg"
    assert channel.find(ns("itunes", "image")).get("href") == cover
    image = channel.find("image")
    assert (image.findtext("url"), image.findtext("title")) == (cover, "Mein Podcast")


def test_explicit_true_and_no_cover():
    channel = channel_of(build_feed(config(explicit=True), [], now=NOW))
    assert channel.findtext(ns("itunes", "explicit")) == "true"
    assert channel.find(ns("itunes", "image")) is None and channel.find("image") is None


def test_item_tags():
    channel = channel_of(build_feed(config(), [episode(1)], now=NOW))
    [item] = channel.findall("item")
    assert item.findtext("title") == "Folge 1"
    assert item.findtext("description") == "Zusammenfassung 1."
    assert item.findtext("link") == "https://blog.example.com/1"
    assert item.findtext(ns("itunes", "duration")) == "0:01:04"  # 63.5 s
    assert item.findtext(ns("itunes", "episode")) == "1"
    assert item.findtext(ns("itunes", "explicit")) == "false"
    guid = item.find("guid")
    assert (guid.text, guid.get("isPermaLink")) == ("guid-1", "false")
    assert parsedate_to_datetime(item.findtext("pubDate")) == datetime(
        2026, 10, 1, 5, 30, tzinfo=UTC
    )


def test_enclosure_url_length_and_type():
    channel = channel_of(build_feed(config(), [episode(2)], now=NOW))
    enclosure = channel.find("item/enclosure")
    assert enclosure.attrib == {
        "url": "https://nas.example.ts.net:8443/episodes/2026-10-02-folge-2-guid2.mp3",
        "length": "1000002",
        "type": "audio/mpeg",
    }


def test_enclosure_url_is_encoded_and_base_url_with_path_works():
    cfg = config()
    cfg.feed.base_url = "http://h/podcast"
    ep = episode(1, audio_file="episodes/a b#1.mp3")
    enclosure = channel_of(build_feed(cfg, [ep], now=NOW)).find("item/enclosure")
    assert enclosure.get("url") == "http://h/podcast/episodes/a%20b%231.mp3"


def test_episode_number_is_optional():
    channel = channel_of(build_feed(config(), [episode(1, number=None)], now=NOW))
    assert channel.find("item").find(ns("itunes", "episode")) is None


def test_items_keep_the_given_newest_first_order():
    episodes = [episode(3), episode(2), episode(1)]
    titles = [
        i.findtext("title")
        for i in channel_of(build_feed(config(), episodes, now=NOW)).findall("item")
    ]
    assert titles == ["Folge 3", "Folge 2", "Folge 1"]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0:00:00"),
        (59.4, "0:00:59"),
        (59.5, "0:01:00"),
        (62.5, "0:01:03"),
        (3600, "1:00:00"),
        (3725, "1:02:05"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


# --- show notes --------------------------------------------------------------------------


def test_show_notes_list_sources_and_mentions_separately_in_german():
    ep = episode(
        1,
        sources=[
            FeedSource("Hauptartikel", "https://a.example.com/x?a=1&b=2", "heise", "discussed"),
            FeedSource("Nebenbei <neu>", "https://b.example.com/y", "golem", "mentioned"),
        ],
    )
    notes = show_notes_html(ep, "de")
    assert notes.startswith("<p>Zusammenfassung 1.</p>")
    link = '<a href="https://a.example.com/x?a=1&amp;b=2">Hauptartikel</a>'
    assert f"<h3>Quellen</h3><ul><li>{link} (heise)</li></ul>" in notes
    assert "<h3>Außerdem neu</h3>" in notes
    assert "Nebenbei &lt;neu&gt;" in notes and "(golem)" in notes
    assert notes.index("Quellen") < notes.index("Außerdem neu")


def test_show_notes_english_and_without_sources():
    assert "<h3>Sources</h3>" in show_notes_html(episode(1), "en")
    assert "<h3>Sources</h3>" in show_notes_html(episode(1), "fr")  # unknown -> English
    bare = show_notes_html(episode(1, sources=[]), "de")
    assert bare == "<p>Zusammenfassung 1.</p>"


def test_content_encoded_is_html_inside_the_xml():
    channel = channel_of(build_feed(config(), [episode(1)], now=NOW))
    html = channel.find("item").findtext(ns("content", "encoded"))
    assert '<a href="https://blog.example.com/1">Artikel 1</a> (Blog)' in html


# --- special characters ------------------------------------------------------------------

NASTY = "Äpfel & Birnen <b>\"fett\"</b> 'quote' 😀 ]]> &amp;"


def test_special_characters_round_trip_through_xml_and_feedparser():
    ep = episode(1, title=NASTY, summary=NASTY)
    data = build_feed(config(title=NASTY, description=NASTY, author=NASTY), [ep], now=NOW)
    channel = channel_of(data)
    assert channel.findtext("title") == NASTY
    assert channel.find("item").findtext("title") == NASTY
    assert channel.find("item").findtext("description") == NASTY
    parsed = feedparser.parse(data)
    assert not parsed.bozo
    assert parsed.feed.title == NASTY
    assert parsed.entries[0].title == NASTY


def test_invalid_xml_characters_are_removed():
    ep = episode(1, title="Titel\x00mit\x0bKontroll\x1fzeichen", summary="a\x08b")
    data = build_feed(config(), [ep], now=NOW)
    item = channel_of(data).find("item")  # parses at all: the document is well-formed
    assert item.findtext("title") == "TitelmitKontrollzeichen"
    assert item.findtext("description") == "ab"


# --- parsers -----------------------------------------------------------------------------


def test_feedparser_reads_the_feed():
    cfg = config()
    data = build_feed(cfg, [episode(2), episode(1)], cover_relpath="cover.png", now=NOW)
    parsed = feedparser.parse(data)
    assert not parsed.bozo and parsed.version == "rss20"
    assert parsed.feed.language == "de-DE"
    assert [e.title for e in parsed.entries] == ["Folge 2", "Folge 1"]
    first = parsed.entries[0]
    assert first.id == "guid-2" and first.guidislink is False
    [enclosure] = first.enclosures
    assert enclosure.href.endswith("/2026-10-02-folge-2-guid2.mp3")
    assert (enclosure.length, enclosure.type) == ("1000002", "audio/mpeg")
    assert first.itunes_duration == "0:01:05"  # 64.5 s, rounded half up
    assert first.published_parsed[:5] == (2026, 10, 2, 5, 30)
    assert "Quellen" in first.content[0].value
    assert parsed.feed.image.href == "https://nas.example.ts.net:8443/cover.png"


def test_empty_feed_is_valid():
    data = build_feed(config(), [], now=NOW)
    parsed = feedparser.parse(data)
    assert not parsed.bozo and parsed.version == "rss20" and parsed.entries == []
    channel = channel_of(data)
    assert channel.findtext("title") == "Mein Podcast" and channel.find("item") is None


# --- database and writing ----------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "db.sqlite3")
    yield c
    c.close()


def publish(conn, source_id, n, *, published="2026-10-0{n}T05:30:00Z", status="published"):
    article = repo.insert_article_if_new(
        conn, source_id, f"https://blog.example.com/{n}", title=f"Artikel {n}"
    )
    mention = repo.insert_article_if_new(
        conn, source_id, f"https://blog.example.com/m{n}", title=f"Erwähnt {n}"
    )
    eid = repo.create_episode(
        conn,
        guid=f"guid-{n}",
        mode="per_article",
        title=f"Folge {n}",
        summary=f"Zusammenfassung {n}.",
        number=n,
        articles=[(mention, "mentioned"), (article, "discussed")],
    )
    repo.publish_episode(
        conn, eid, audio_file=f"episodes/e{n}.mp3", audio_bytes=1000 * n, duration_s=60.0 * n
    )
    conn.execute(
        "UPDATE episodes SET published_at = ?, status = ? WHERE id = ?",
        (published.format(n=n), status, eid),
    )
    conn.commit()
    return eid


def test_load_feed_episodes_newest_first_published_only(conn):
    source = repo.upsert_source(conn, "Blog", "https://blog.example.com/feed", "rss")
    publish(conn, source, 1)
    publish(conn, source, 3)
    publish(conn, source, 2)
    publish(conn, source, 4, status="pruned")
    draft = repo.create_episode(conn, guid="draft", mode="per_article", title="Entwurf")
    assert draft
    episodes = load_feed_episodes(conn)
    assert [e.title for e in episodes] == ["Folge 3", "Folge 2", "Folge 1"]
    first = episodes[0]
    assert (first.audio_file, first.audio_bytes, first.duration_s, first.number) == (
        "episodes/e3.mp3",
        3000,
        180.0,
        3,
    )
    assert [(s.title, s.role, s.source_name) for s in first.sources] == [
        ("Artikel 3", "discussed", "Blog"),
        ("Erwähnt 3", "mentioned", "Blog"),
    ]


def test_write_feed_writes_atomically_with_cover(tmp_path, conn):
    source = repo.upsert_source(conn, "Blog", "https://blog.example.com/feed", "rss")
    publish(conn, source, 1)
    cover = tmp_path / "pic.png"
    cover.write_bytes(b"png")
    cfg = config()
    cfg.paths.output_dir = tmp_path / "public"
    cfg.podcast.cover_image = cover
    path = write_feed(cfg, conn, now=NOW)
    assert path == tmp_path / "public" / "feed.xml"
    assert (tmp_path / "public" / "cover.png").read_bytes() == b"png"
    parsed = feedparser.parse(path.read_bytes())
    assert [e.title for e in parsed.entries] == ["Folge 1"]
    assert parsed.feed.image.href.endswith("/cover.png")
    assert not list((tmp_path / "public").glob("*.part"))


def test_write_feed_without_episodes_or_cover(tmp_path, conn):
    cfg = config()
    cfg.paths.output_dir = tmp_path / "public"
    path = write_feed(cfg, conn, now=NOW)
    parsed = feedparser.parse(path.read_bytes())
    assert not parsed.bozo and parsed.entries == []
    assert not (tmp_path / "public" / "cover.png").exists()


def test_write_feed_replaces_the_previous_feed(tmp_path, conn):
    cfg = config()
    cfg.paths.output_dir = tmp_path / "public"
    write_feed(cfg, conn, now=NOW)
    source = repo.upsert_source(conn, "Blog", "https://blog.example.com/feed", "rss")
    publish(conn, source, 1)
    write_feed(cfg, conn, now=NOW)
    assert len(feedparser.parse((tmp_path / "public" / "feed.xml").read_bytes()).entries) == 1


def test_published_episode_without_audio_data_is_skipped(conn, caplog):
    eid = repo.create_episode(conn, guid="x", mode="per_article", title="Kaputt")
    conn.execute(
        "UPDATE episodes SET status = 'published', published_at = ? WHERE id = ?",
        ("2026-10-01T00:00:00Z", eid),
    )
    conn.commit()
    assert load_feed_episodes(conn) == []
    assert any("without audio data" in r.message for r in caplog.records)
