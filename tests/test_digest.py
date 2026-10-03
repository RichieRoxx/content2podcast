"""Daily digest mode (issue #38)."""

import json
import subprocess
from datetime import UTC, timedelta, timezone

import feedparser
import httpx
import pytest
import respx
from typer.testing import CliRunner

from content2podcast import repository as repo
from content2podcast.config import AppConfig
from content2podcast.layout import episode_work_dir
from content2podcast.pipeline import MAX_ATTEMPTS
from content2podcast.providers.llm.base import LLMError
from content2podcast.providers.llm.fake import FakeLLM
from content2podcast.providers.tts.fake import FakeTTS
from content2podcast.script.prompt import ArticleInput, build_prompts
from pipeline_helpers import (
    BLOG,
    FEED,
    NOW,
    baseline,
    canned,
    feed,
    make_pipeline,
    route_posts,
    url,
)

TZ_UTC = UTC


class Crash(BaseException):
    pass


class CrashingTTS(FakeTTS):
    def synthesize(self, text, voice, style):
        raise Crash


@pytest.fixture
def digest_env(env):
    env["config"].episode.mode = "daily_digest"
    env["config"].episode.max_articles = 2
    return env


def pending_articles(env, ages=((1, 4), (2, 3), (3, 2), (4, 1)), *, serve=None):
    """Baseline the source, then make the given posts pending: ``(post number, age in hours)``."""
    pipeline = make_pipeline(env, tz=TZ_UTC)
    baseline(env, pipeline)
    route_posts(*(serve if serve is not None else [n for n, _ in ages]))
    respx.get(FEED).mock(return_value=httpx.Response(200, content=feed((0, 24 * 30), *ages)))


def make(env, **kwargs):
    kwargs.setdefault("tz", TZ_UTC)
    return make_pipeline(env, **kwargs)


def digests(env, status="published"):
    return [e for e in repo.list_episodes(env["conn"], status) if e["mode"] == "daily_digest"]


# --- roles and show notes ----------------------------------------------------------------


@respx.mock
def test_newest_articles_are_discussed_and_the_rest_mentioned(digest_env):
    env = digest_env
    # posts 3 and 4 are the newest; 1 and 2 overflow. Only discussed pages are fetched.
    pending_articles(env, serve=[3, 4])
    llm = FakeLLM(canned("Der Tag in Python"))
    summary = make(env, llm=llm).run([BLOG])

    [result] = summary.published
    assert summary.exit_code == 0 and result.episode_title == "Der Tag in Python"
    conn = env["conn"]
    [episode] = digests(env)
    roles = {
        repo.get_article(conn, link["article_id"])["url"]: link["role"]
        for link in repo.episode_articles(conn, episode["id"])
    }
    assert roles == {
        url(4): "discussed",
        url(3): "discussed",
        url(2): "mentioned",
        url(1): "mentioned",
    }
    assert repo.list_articles(conn, "pending") == []  # mentioned ones are processed as well
    assert len(repo.list_articles(conn, "processed")) == 4
    assert episode["number"] == 1 and episode["mode"] == "daily_digest"


@respx.mock
def test_show_notes_list_discussed_sources_and_mentioned_articles(digest_env):
    env = digest_env
    pending_articles(env, serve=[3, 4])
    make(env).run([BLOG])
    parsed = feedparser.parse((env["config"].paths.output_dir / "feed.xml").read_bytes())
    [entry] = parsed.entries
    notes = entry.content[0].value
    assert "Quellen" in notes and "Außerdem neu" in notes
    discussed, mentioned = notes.split("Außerdem neu")
    assert "Beitrag 3" in discussed and "Beitrag 4" in discussed
    assert "Beitrag 1" in mentioned and "Beitrag 2" in mentioned
    assert url(2) in mentioned


@respx.mock
def test_script_sources_are_the_discussed_articles_only(digest_env):
    env = digest_env
    pending_articles(env, serve=[3, 4])
    make(env).run([BLOG])
    [episode] = digests(env)
    sources = json.loads(episode["script_json"])["sources"]
    assert [s["url"] for s in sources] == [url(4), url(3)]


@respx.mock
def test_all_articles_discussed_when_there_are_few(digest_env):
    env = digest_env
    env["config"].episode.max_articles = 10
    pending_articles(env, ages=((1, 3), (2, 2)))
    make(env).run([BLOG])
    [episode] = digests(env)
    assert {link["role"] for link in repo.episode_articles(env["conn"], episode["id"])} == {
        "discussed"
    }


@respx.mock
def test_prompt_uses_the_digest_template_and_a_fixed_length(digest_env):
    env = digest_env
    pending_articles(env, serve=[3, 4])
    llm = FakeLLM(canned())
    make(env, llm=llm).run([BLOG])
    call = llm.calls[0]
    assert "tägliche Überblicksfolge" in call.system and "Übergänge" in call.system
    assert "etwa 15 Minuten" in call.user and "ungefähr 2100 gesprochene Wörter" in call.user
    assert call.user.count("<article ") == 2  # only the discussed ones are given to the model
    assert 'title="Beitrag 4"' in call.user and 'title="Beitrag 3"' in call.user


def test_digest_word_budget_does_not_depend_on_the_article_length():
    config = AppConfig()
    config.episode.mode = "daily_digest"
    tiny = [ArticleInput(source="s", title="t", text="wenig Text")]
    huge = [ArticleInput(source="s", title="t", text="wort " * 50_000)]
    for articles in (tiny, huge):
        prompts = build_prompts(config, articles, ["neutral"], today=NOW.date())
        assert (prompts.target_words, prompts.target_minutes) == (2100, 15)
    config.episode.target_minutes = 8
    assert build_prompts(config, tiny, ["neutral"], today=NOW.date()).target_words == 1120
    config.episode.mode = "per_article"  # the per-article budget is unchanged
    assert build_prompts(config, tiny, ["neutral"], today=NOW.date()).target_words == 420


# --- one per day -------------------------------------------------------------------------


@respx.mock
def test_a_second_run_on_the_same_day_does_nothing(digest_env):
    env = digest_env
    pending_articles(env, ages=((1, 3), (2, 2)))
    make(env).run([BLOG])
    assert len(digests(env)) == 1

    # new articles arrive later the same day
    route_posts(5)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 3), (2, 2), (5, 0.5)))
    )
    llm = FakeLLM(canned("Zweiter Versuch"))
    summary = make(env, llm=llm).run([BLOG])
    assert summary.results == [] and llm.calls == []
    assert any("already published today" in note for note in summary.notes)
    assert len(digests(env)) == 1
    assert [a["url"] for a in repo.list_articles(env["conn"], "pending")] == [url(5)]


@respx.mock
def test_force_creates_another_digest_the_same_day(digest_env):
    env = digest_env
    pending_articles(env, ages=((1, 3), (2, 2)))
    make(env).run([BLOG])
    route_posts(5)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 3), (2, 2), (5, 0.5)))
    )
    summary = make(env, llm=FakeLLM(canned("Zweiter Versuch"))).run([BLOG], force=True)
    assert len(summary.published) == 1 and summary.notes == []
    assert [e["number"] for e in digests(env)] == [2, 1]  # newest first
    assert repo.list_articles(env["conn"], "pending") == []


@respx.mock
def test_the_next_day_allows_a_new_digest(digest_env):
    env = digest_env
    pending_articles(env, ages=((1, 3),))
    make(env).run([BLOG])
    route_posts(6)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 3), (6, 0.5)))
    )
    tomorrow = make(env, now=lambda: NOW + timedelta(days=1))
    # the feed ages are relative to NOW; the article is "new" and recent enough
    summary = tomorrow.run([BLOG])
    assert len(summary.published) == 1 and len(digests(env)) == 2


@pytest.mark.parametrize(
    ("tz", "blocked"),
    [
        (TZ_UTC, False),  # 02 Oct 12:00 UTC and 03 Oct 06:00 UTC are different days
        (timezone(timedelta(hours=14)), True),  # 03 Oct 02:00 and 03 Oct 20:00: the same day
    ],
)
@respx.mock
def test_the_calendar_day_is_the_local_one(digest_env, tz, blocked):
    env = digest_env
    pending_articles(env, ages=((1, 2),))
    previous = env["conn"]
    episode_id = repo.create_episode(
        previous, guid="earlier", mode="daily_digest", title="Früher", audio_file="episodes/a.mp3"
    )
    repo.publish_draft_episode(
        previous, episode_id, audio_file="episodes/a.mp3", audio_bytes=1, duration_s=1
    )
    previous.execute(
        "UPDATE episodes SET published_at = '2026-10-02T12:00:00Z' WHERE id = ?", (episode_id,)
    )
    previous.commit()
    summary = make(env, tz=tz).run([BLOG])
    assert (summary.results == []) is blocked


@respx.mock
def test_without_pending_articles_nothing_happens_and_no_note(digest_env):
    env = digest_env
    pipeline = make(env)
    baseline(env, pipeline)
    summary = pipeline.run([BLOG])
    assert summary.results == [] and summary.notes == [] and summary.exit_code == 0


@respx.mock
def test_max_episodes_per_run_does_not_limit_a_digest(digest_env):
    env = digest_env
    env["config"].episode.max_episodes_per_run = 1
    env["config"].episode.max_articles = 10
    pending_articles(env, ages=((1, 3), (2, 2), (3, 1)))
    summary = make(env).run([BLOG])
    assert len(summary.published) == 1
    assert repo.list_articles(env["conn"], "pending") == []


# --- failures ----------------------------------------------------------------------------


@respx.mock
def test_a_failed_digest_counts_an_attempt_for_the_discussed_articles_only(digest_env):
    env = digest_env
    pending_articles(env, serve=[3, 4])
    summary = make(env, llm=FakeLLM(error=LLMError("model down"))).run([BLOG])
    assert summary.exit_code == 1 and "model down" in summary.failed[0].error
    assert summary.failed[0].status == "pending"
    attempts = {a["url"]: a["attempts"] for a in repo.list_articles(env["conn"])}
    assert attempts[url(3)] == 1 and attempts[url(4)] == 1
    assert attempts[url(1)] == 0 and attempts[url(2)] == 0  # never looked at
    assert digests(env) == [] and digests(env, "draft") == []  # no script, no draft


@respx.mock
def test_articles_fail_for_good_after_three_digest_attempts(digest_env):
    env = digest_env
    pending_articles(env, serve=[1, 2, 3, 4])
    for _ in range(MAX_ATTEMPTS):
        make(env, llm=FakeLLM(error=LLMError("down"))).run([BLOG])
    statuses = {a["url"]: a["status"] for a in repo.list_articles(env["conn"])}
    assert statuses[url(3)] == statuses[url(4)] == "failed"
    assert statuses[url(1)] == statuses[url(2)] == "pending"  # never given to the model

    # the next digest is built from what is left
    summary = make(env, llm=FakeLLM(canned("Der Rest"))).run([BLOG])
    assert [r.episode_title for r in summary.published] == ["Der Rest"]
    statuses = {a["url"]: a["status"] for a in repo.list_articles(env["conn"])}
    assert statuses[url(1)] == statuses[url(2)] == "processed"
    assert statuses[url(3)] == statuses[url(4)] == "failed"


@respx.mock
def test_articles_without_text_are_dropped_from_the_digest(digest_env):
    env = digest_env
    pending_articles(env, ages=((1, 3), (2, 2)), serve=[1])
    respx.get(url(2)).mock(return_value=httpx.Response(403))
    llm = FakeLLM(canned())
    summary = make(env, llm=llm).run([BLOG])
    assert len(summary.published) == 1
    assert llm.calls[0].user.count("<article ") == 1
    [episode] = digests(env)
    roles = {
        repo.get_article(env["conn"], link["article_id"])["url"]: link["role"]
        for link in repo.episode_articles(env["conn"], episode["id"])
    }
    assert roles == {url(1): "discussed"}
    unreadable = next(a for a in repo.list_articles(env["conn"]) if a["url"] == url(2))
    assert (unreadable["status"], unreadable["attempts"]) == ("pending", 1)


@respx.mock
def test_no_usable_text_at_all_means_no_llm_call(digest_env):
    env = digest_env
    pending_articles(env, ages=((1, 3), (2, 2)), serve=[])
    respx.get(url(1)).mock(return_value=httpx.Response(403))
    respx.get(url(2)).mock(return_value=httpx.Response(403))
    llm = FakeLLM(canned())
    summary = make(env, llm=llm).run([BLOG])
    assert llm.calls == [] and summary.exit_code == 1
    assert "no article text" in summary.failed[0].error


# --- resume ------------------------------------------------------------------------------


@respx.mock
def test_an_interrupted_digest_is_continued_with_the_same_articles(digest_env):
    env = digest_env
    pending_articles(env, serve=[3, 4])
    llm = FakeLLM(canned("Digest"))
    with pytest.raises(Crash):
        make(env, llm=llm, tts=CrashingTTS()).run([BLOG])
    assert len(llm.calls) == 1
    [draft] = digests(env, "draft")
    links = {
        repo.get_article(env["conn"], link["article_id"])["url"]: link["role"]
        for link in repo.episode_articles(env["conn"], draft["id"])
    }
    assert links[url(4)] == links[url(3)] == "discussed" and links[url(1)] == "mentioned"

    # another article shows up before the resume: it must not join the draft
    route_posts(7)
    respx.get(FEED).mock(
        return_value=httpx.Response(
            200, content=feed((0, 24 * 30), (1, 4), (2, 3), (3, 2), (4, 1), (7, 0.2))
        )
    )
    second_llm = FakeLLM(canned("Anders"))
    summary = make(env, llm=second_llm).run([BLOG])
    assert second_llm.calls == []
    assert summary.published[0].guid == draft["guid"]
    assert [a["url"] for a in repo.list_articles(env["conn"], "pending")] == [url(7)]
    assert digests(env)[0]["title"] == "Digest"
    assert not episode_work_dir(env["config"].paths.data_dir, draft["guid"]).exists()


@respx.mock
def test_a_draft_digest_is_continued_even_though_one_was_published_today(digest_env):
    env = digest_env
    pending_articles(env, ages=((1, 3),))
    with pytest.raises(Crash):
        make(env, tts=CrashingTTS()).run([BLOG])
    conn = env["conn"]
    earlier = repo.create_episode(
        conn, guid="e0", mode="daily_digest", title="Heute früh", audio_file="episodes/x.mp3"
    )
    repo.publish_draft_episode(
        conn, earlier, audio_file="episodes/x.mp3", audio_bytes=1, duration_s=1
    )
    summary = make(env).run([BLOG])
    assert len(summary.published) == 1  # the guard only blocks *new* digests


@respx.mock
def test_stale_digest_drafts_are_discarded(digest_env):
    env = digest_env
    pending_articles(env, ages=((1, 3),))
    with pytest.raises(Crash):
        make(env, tts=CrashingTTS()).run([BLOG])
    [draft] = digests(env, "draft")
    article = repo.list_articles(env["conn"], "pending")[0]
    repo.set_article_status(env["conn"], article["id"], "processed")  # handled elsewhere
    make(env).run([BLOG])
    assert digests(env, "draft") == []
    assert not episode_work_dir(env["config"].paths.data_dir, draft["guid"]).exists()


# --- pipeline mode switch ----------------------------------------------------------------


@respx.mock
def test_per_article_mode_is_unaffected(env):
    pipeline = make_pipeline(env, tz=TZ_UTC)
    baseline(env, pipeline)
    route_posts(1, 2)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 3), (2, 2)))
    )
    summary = pipeline.run([BLOG])
    assert len(summary.published) == 2 and summary.notes == []
    assert {e["mode"] for e in repo.list_episodes(env["conn"])} == {"per_article"}


# --- repository --------------------------------------------------------------------------


def test_latest_published_at_and_latest_draft(env):
    conn = env["conn"]
    assert repo.latest_published_at(conn, "daily_digest") is None
    assert repo.latest_draft(conn, "daily_digest") is None
    first = repo.create_episode(conn, guid="a", mode="daily_digest", title="A", audio_file="x")
    assert repo.latest_draft(conn, "daily_digest")["id"] == first
    assert repo.latest_draft(conn, "per_article") is None
    repo.publish_draft_episode(conn, first, audio_file="x", audio_bytes=1, duration_s=1)
    conn.execute("UPDATE episodes SET published_at = '2026-10-01T10:00:00Z' WHERE id = ?", (first,))
    repo.prune_episode(conn, first)  # pruned digests still count as published
    conn.commit()
    assert repo.latest_published_at(conn, "daily_digest") == "2026-10-01T10:00:00Z"
    assert repo.latest_published_at(conn, "per_article") is None


# --- CLI ---------------------------------------------------------------------------------


@pytest.mark.ffmpeg
@respx.mock
def test_cli_run_reports_the_one_per_day_guard(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from email.utils import format_datetime

    from content2podcast.cli import app

    for key in ("NO_COLOR",):
        monkeypatch.setenv(key, "1")
    monkeypatch.chdir(tmp_path)
    canned_json = json.dumps(canned("Der Tag"))
    (tmp_path / "config.yaml").write_text(
        "paths:\n  data_dir: data\n  output_dir: public\n"
        "episode:\n  mode: daily_digest\n"
        f"llm:\n  provider: fake\n  response: {canned_json}\ntts:\n  provider: fake\n"
    )
    (tmp_path / "sources.yaml").write_text(f"sources:\n  - name: Blog\n    url: {FEED}\n")
    recent = format_datetime(datetime.now(UTC) - timedelta(hours=1))

    def rss(*posts):
        items = "".join(
            f"<item><title>P{n}</title><link>{url(n)}</link><pubDate>{recent}</pubDate></item>"
            for n in posts
        )
        head = '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        return f"{head}{items}</channel></rss>".encode()

    from pipeline_helpers import ARTICLE_HTML

    blog = respx.get(FEED).mock(return_value=httpx.Response(200, content=rss()))
    for n in (1, 2):
        respx.get(url(n)).mock(return_value=httpx.Response(200, content=ARTICLE_HTML))
    runner = CliRunner()
    runner.invoke(app, ["run"])  # baseline
    blog.mock(return_value=httpx.Response(200, content=rss(1)))
    first = runner.invoke(app, ["run"])
    assert first.exit_code == 0 and "Published Der Tag" in first.output
    blog.mock(return_value=httpx.Response(200, content=rss(1, 2)))
    second = runner.invoke(app, ["run"])
    assert second.exit_code == 0
    assert "Note: A daily digest was already published today" in second.output
    forced = runner.invoke(app, ["run", "--force"])
    assert forced.exit_code == 0 and "Published Der Tag" in forced.output
    assert (
        subprocess.run(
            ["ffprobe", "-v", "error", str(next((tmp_path / "public/episodes").glob("*.mp3")))]
        ).returncode
        == 0
    )
