"""Shared helpers for the pipeline tests (fake feeds, stub assembler, pipeline factory)."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import respx

from content2podcast import repository as repo
from content2podcast.audio import AssemblyResult
from content2podcast.config import SourceConfig
from content2podcast.pipeline import Pipeline
from content2podcast.providers.llm.fake import FakeLLM
from content2podcast.providers.tts.base import allowed_styles
from content2podcast.providers.tts.fake import FakeTTS

FIXTURES = Path(__file__).parent / "fixtures" / "articles"
ARTICLE_HTML = (FIXTURES / "article.html").read_bytes()
NOW = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
FEED = "https://blog.example.com/feed.xml"
BLOG = SourceConfig(name="Blog", url=FEED)


def url(n: int) -> str:
    return f"https://blog.example.com/posts/{n}"


def feed(*items: tuple[int, int]) -> bytes:
    """Items as ``(post number, age in hours)``."""
    body = "".join(
        f"<item><title>Beitrag {n}</title><link>{url(n)}</link>"
        f"<pubDate>{format_datetime(NOW - timedelta(hours=age))}</pubDate></item>"
        for n, age in items
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'
    ).encode()


def canned(title="Folge zum Artikel"):
    return {
        "title": title,
        "summary": "Es geht um Paketmanager.",
        "segments": [
            {"speaker": "host", "style": "neutral", "text": "Hallo und willkommen."},
            {"speaker": "expert", "style": "cheerful", "text": "Schön, dabei zu sein."},
        ],
    }


class StubAssembler:
    """Replaces ffmpeg: writes a small fake MP3 and records how it was called."""

    def __init__(self, fail_for: set[str] | None = None):
        self.calls: list[dict] = []
        self.fail_for = fail_for or set()

    def __call__(self, segments, output, work_dir, *, cfg, gap_ms, meta, intro=None, outro=None):
        self.calls.append(
            {"segments": segments, "output": output, "gap_ms": gap_ms, "meta": meta, "intro": intro}
        )
        if meta.title in self.fail_for:
            raise RuntimeError("assembly exploded")
        data = b"ID3" + b"\x00" * 100
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        return AssemblyResult(output, duration_s=12.5, size_bytes=len(data), measured_lufs=-20.0)


def make_pipeline(env, llm=None, tts=None, assembler=None, **kwargs) -> Pipeline:
    tts = tts or FakeTTS()
    config = env["config"]
    kwargs.setdefault("now", lambda: NOW)
    return Pipeline(
        config,
        env["conn"],
        env["http"],
        llm or FakeLLM(canned()),
        tts,
        allowed_styles(tts, config.roles.host.voice, config.roles.expert.voice),
        assemble=assembler or StubAssembler(),
        **kwargs,
    )


def baseline(env, pipeline):
    """First run: the source is baselined with one old post."""
    respx.get(FEED).mock(return_value=httpx.Response(200, content=feed((0, 24 * 30))))
    summary = pipeline.run([BLOG])
    assert summary.results == []
    return summary


def route_posts(*numbers, status=200):
    for n in numbers:
        respx.get(url(n)).mock(return_value=httpx.Response(status, content=ARTICLE_HTML))


def articles_by_status(conn):
    return {a["url"]: a["status"] for a in repo.list_articles(conn)}
