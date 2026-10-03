"""Draft episodes, resume after a crash and the transactional publish (issue #37)."""

import json
from datetime import timedelta

import feedparser
import httpx
import pytest
import respx

from content2podcast import repository as repo
from content2podcast.layout import episode_work_dir
from content2podcast.pipeline import MAX_ATTEMPTS
from content2podcast.providers.llm.base import LLMError
from content2podcast.providers.llm.fake import FakeLLM
from content2podcast.providers.tts.base import TTSError
from content2podcast.providers.tts.fake import FakeTTS
from pipeline_helpers import (
    BLOG,
    FEED,
    NOW,
    StubAssembler,
    baseline,
    canned,
    feed,
    make_pipeline,
    route_posts,
)


class Crash(BaseException):
    """Simulates the process dying: not an Exception, so no isolation code catches it."""


class CrashingTTS(FakeTTS):
    def synthesize(self, text, voice, style):
        raise Crash


class CrashingAssembler(StubAssembler):
    def __call__(self, *args, **kwargs):
        raise Crash


def one_new_article(env, number=1, age_hours=1):
    """Baseline the source, then make article ``number`` pending."""
    pipeline = make_pipeline(env)
    baseline(env, pipeline)
    route_posts(number)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (number, age_hours)))
    )


def draft_of(env):
    [draft] = repo.list_episodes(env["conn"], "draft")
    return draft


# --- crash after the script --------------------------------------------------------------


@respx.mock
def test_crash_after_the_script_does_not_call_the_llm_again(env):
    one_new_article(env)
    llm = FakeLLM(canned())
    with pytest.raises(Crash):
        make_pipeline(env, llm=llm, tts=CrashingTTS()).run([BLOG])
    assert len(llm.calls) == 1

    draft = draft_of(env)  # the script was stored before any TTS cost
    assert json.loads(draft["script_json"])["title"] == "Folge zum Artikel"
    assert draft["audio_file"].startswith("episodes/") and draft["number"] is None
    article = repo.list_articles(env["conn"], "pending")[0]
    assert article["attempts"] == 0  # a crash is not a failed attempt

    second_llm = FakeLLM(canned("Anderer Titel"))
    summary = make_pipeline(env, llm=second_llm).run([BLOG])
    assert second_llm.calls == []  # script reused
    [published] = summary.published
    assert published.guid == draft["guid"]  # same GUID
    episode = repo.get_episode_by_guid(env["conn"], draft["guid"])
    assert (episode["status"], episode["title"]) == ("published", "Folge zum Artikel")
    assert episode["audio_file"] == draft["audio_file"]


@respx.mock
def test_an_ordinary_failure_also_leaves_a_resumable_draft(env):
    one_new_article(env)
    tts = FakeTTS()
    tts.synthesize = lambda *a: (_ for _ in ()).throw(TTSError("quota"))  # type: ignore[method-assign]
    llm = FakeLLM(canned())
    summary = make_pipeline(env, llm=llm, tts=tts).run([BLOG])
    assert "quota" in summary.failed[0].error
    assert repo.list_articles(env["conn"], "pending")[0]["attempts"] == 1
    draft = draft_of(env)

    again = FakeLLM(canned())
    summary = make_pipeline(env, llm=again).run([BLOG])
    assert again.calls == [] and summary.published[0].guid == draft["guid"]


# --- crash after TTS ---------------------------------------------------------------------


@respx.mock
def test_crash_after_tts_does_not_call_tts_again(env):
    one_new_article(env)
    tts = FakeTTS()
    with pytest.raises(Crash):
        make_pipeline(env, tts=tts, assembler=CrashingAssembler()).run([BLOG])
    assert len(tts.calls) == 2  # both segments were synthesized
    draft = draft_of(env)
    cached = list(episode_work_dir(env["config"].paths.data_dir, draft["guid"]).glob("*.wav"))
    assert len(cached) == 2  # kept for the resume

    second_tts, second_llm = FakeTTS(), FakeLLM(canned())
    summary = make_pipeline(env, llm=second_llm, tts=second_tts).run([BLOG])
    assert second_tts.calls == [] and second_llm.calls == []
    assert summary.published[0].guid == draft["guid"]


# --- crash after the feed was written, before the commit ---------------------------------


@respx.mock
def test_crash_after_feed_write_before_commit_gives_no_duplicate_item(env, monkeypatch):
    one_new_article(env)
    from content2podcast import pipeline as pipeline_module

    real_write_feed = pipeline_module.write_feed
    state = {"crash": True}

    def write_then_crash(*args, **kwargs):
        path = real_write_feed(*args, **kwargs)
        if state["crash"]:
            raise Crash
        return path

    monkeypatch.setattr(pipeline_module, "write_feed", write_then_crash)
    with pytest.raises(Crash):
        make_pipeline(env).run([BLOG])

    conn = env["conn"]
    draft = draft_of(env)  # rolled back: still a draft, no number consumed
    assert draft["number"] is None and draft["published_at"] is None
    assert repo.list_articles(conn, "pending") and not repo.list_articles(conn, "processed")
    assert (env["config"].paths.output_dir / "feed.xml").exists()  # the file got ahead of the DB

    state["crash"] = False
    summary = make_pipeline(env).run([BLOG])
    assert summary.published[0].guid == draft["guid"]
    parsed = feedparser.parse((env["config"].paths.output_dir / "feed.xml").read_bytes())
    assert [e.id for e in parsed.entries] == [draft["guid"]]  # exactly one item, same GUID
    episode = repo.get_episode_by_guid(conn, draft["guid"])
    assert (episode["status"], episode["number"]) == ("published", 1)
    assert len(list((env["config"].paths.output_dir / "episodes").glob("*.mp3"))) == 1
    assert len(repo.list_articles(conn, "processed")) == 1


@respx.mock
def test_a_feed_error_rolls_the_whole_publish_back(env, monkeypatch):
    one_new_article(env)
    from content2podcast import pipeline as pipeline_module

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline_module, "write_feed", broken)
    summary = make_pipeline(env).run([BLOG])
    assert "disk full" in summary.failed[0].error
    assert repo.list_episodes(env["conn"], "published") == []
    assert repo.list_articles(env["conn"], "processed") == []
    assert draft_of(env)["number"] is None


# --- numbers, work dirs, dates -----------------------------------------------------------


@respx.mock
def test_episode_numbers_are_sequential_and_not_consumed_by_failures(env):
    pipeline = make_pipeline(env)
    baseline(env, pipeline)
    route_posts(1, 2, 3)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 3), (2, 2), (3, 1)))
    )
    stub = StubAssembler()
    calls = {"n": 0}

    class FailsOnTheSecondEpisode:
        def __call__(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("assembly hiccup")
            return stub(*args, **kwargs)

    summary = make_pipeline(env, assembler=FailsOnTheSecondEpisode()).run([BLOG])
    assert len(summary.published) == 2 and len(summary.failed) == 1
    numbers = sorted(e["number"] for e in repo.list_episodes(env["conn"], "published"))
    assert numbers == [1, 2]  # no gap although the second article failed
    assert draft_of(env)["number"] is None

    summary = make_pipeline(env).run([BLOG])  # the failed article is retried and completes
    assert len(summary.published) == 1
    assert sorted(e["number"] for e in repo.list_episodes(env["conn"], "published")) == [1, 2, 3]


@respx.mock
def test_work_dir_exists_after_a_crash_and_is_removed_after_the_commit(env):
    one_new_article(env)
    with pytest.raises(Crash):
        make_pipeline(env, assembler=CrashingAssembler()).run([BLOG])
    guid = draft_of(env)["guid"]
    work = episode_work_dir(env["config"].paths.data_dir, guid)
    assert work.is_dir()
    make_pipeline(env).run([BLOG])
    assert not work.exists()


@respx.mock
def test_a_resume_on_another_day_reuses_the_planned_file_name(env):
    one_new_article(env)
    with pytest.raises(Crash):
        make_pipeline(env, tts=CrashingTTS()).run([BLOG])
    draft = draft_of(env)
    assert "2026-10-03" in draft["audio_file"]
    tomorrow = make_pipeline(env, now=lambda: NOW + timedelta(days=1))
    summary = tomorrow.run([BLOG])
    assert summary.published
    episode = repo.get_episode_by_guid(env["conn"], draft["guid"])
    assert episode["audio_file"] == draft["audio_file"]  # still the day-one name
    files = list((env["config"].paths.output_dir / "episodes").glob("*.mp3"))
    assert [f.name for f in files] == [draft["audio_file"].split("/")[-1]]


# --- stale and unusable drafts -----------------------------------------------------------


@respx.mock
def test_stale_drafts_are_discarded_with_their_work_dir(env):
    one_new_article(env)
    with pytest.raises(Crash):
        make_pipeline(env, tts=CrashingTTS()).run([BLOG])
    draft = draft_of(env)
    work = episode_work_dir(env["config"].paths.data_dir, draft["guid"])
    work.mkdir(parents=True, exist_ok=True)
    (work / "x.wav").write_bytes(b"x")
    # the article got processed by other means: the draft cannot be continued
    article = repo.list_articles(env["conn"], "pending")[0]
    repo.set_article_status(env["conn"], article["id"], "processed")

    summary = make_pipeline(env).run([BLOG])
    assert summary.results == []
    assert repo.list_episodes(env["conn"], "draft") == [] and not work.exists()


@respx.mock
def test_failed_articles_lose_their_draft_on_the_next_run(env):
    one_new_article(env)
    llm = FakeLLM(canned())
    broken_tts = FakeTTS()
    broken_tts.synthesize = lambda *a: (_ for _ in ()).throw(TTSError("always"))  # type: ignore[method-assign]
    for _ in range(MAX_ATTEMPTS):
        make_pipeline(env, llm=llm, tts=broken_tts).run([BLOG])
    assert repo.list_articles(env["conn"], "failed")
    assert len(repo.list_episodes(env["conn"], "draft")) == 1  # not yet cleaned
    make_pipeline(env).run([BLOG])
    assert repo.list_episodes(env["conn"], "draft") == []


@respx.mock
def test_a_draft_with_an_unreadable_script_is_replaced(env):
    one_new_article(env)
    with pytest.raises(Crash):
        make_pipeline(env, tts=CrashingTTS()).run([BLOG])
    draft = draft_of(env)
    env["conn"].execute("UPDATE episodes SET script_json = 'not json' WHERE id = ?", (draft["id"],))
    env["conn"].commit()
    llm = FakeLLM(canned("Neuer Anlauf"))
    summary = make_pipeline(env, llm=llm).run([BLOG])
    assert len(llm.calls) == 1  # regenerated
    episode = repo.get_episode_by_guid(env["conn"], summary.published[0].guid)
    assert episode["title"] == "Neuer Anlauf" and episode["guid"] != draft["guid"]
    assert repo.list_episodes(env["conn"], "draft") == []


@respx.mock
def test_a_draft_without_a_planned_file_is_replaced(env):
    one_new_article(env)
    with pytest.raises(Crash):
        make_pipeline(env, tts=CrashingTTS()).run([BLOG])
    draft = draft_of(env)
    env["conn"].execute("UPDATE episodes SET audio_file = NULL WHERE id = ?", (draft["id"],))
    env["conn"].commit()
    llm = FakeLLM(canned())
    summary = make_pipeline(env, llm=llm).run([BLOG])
    assert len(llm.calls) == 1 and summary.published[0].guid != draft["guid"]


# --- repository --------------------------------------------------------------------------


def seeded_draft(env, *, mentioned=False):
    conn = env["conn"]
    source = repo.upsert_source(conn, "Blog", "https://blog.example.com/feed", "rss")
    main = repo.insert_article_if_new(conn, source, "https://blog.example.com/main", title="Main")
    links = [(main, "discussed")]
    side = None
    if mentioned:
        side = repo.insert_article_if_new(conn, source, "https://blog.example.com/side", title="S")
        links.append((side, "mentioned"))
    episode_id = repo.create_episode(
        conn,
        guid="g1",
        mode="per_article",
        title="T",
        script={"x": 1},
        audio_file="episodes/a.mp3",
        articles=links,
    )
    return episode_id, main, side


def test_publish_draft_episode_updates_everything_in_one_go(env):
    episode_id, main, side = seeded_draft(env, mentioned=True)
    conn = env["conn"]
    number = repo.publish_draft_episode(
        conn, episode_id, audio_file="episodes/a.mp3", audio_bytes=10, duration_s=2.5
    )
    episode = repo.get_episode(conn, episode_id)
    assert number == 1 and episode["status"] == "published" and episode["published_at"]
    assert (episode["audio_bytes"], episode["duration_s"]) == (10, 2.5)
    assert repo.get_article(conn, main)["status"] == "processed"
    assert repo.get_article(conn, side)["status"] == "pending"  # only mentioned


def test_publish_draft_episode_rolls_back_when_before_commit_raises(env):
    episode_id, main, _ = seeded_draft(env)
    conn = env["conn"]

    def boom():
        raise RuntimeError("feed failed")

    with pytest.raises(RuntimeError, match="feed failed"):
        repo.publish_draft_episode(
            conn, episode_id, audio_file="episodes/a.mp3", audio_bytes=1, duration_s=1,
            before_commit=boom,
        )  # fmt: skip
    assert repo.get_episode(conn, episode_id)["status"] == "draft"
    assert repo.get_episode(conn, episode_id)["number"] is None
    assert repo.get_article(conn, main)["status"] == "pending"


def test_before_commit_sees_the_uncommitted_state(env):
    episode_id, _, _ = seeded_draft(env)
    seen = {}
    repo.publish_draft_episode(
        env["conn"], episode_id, audio_file="episodes/a.mp3", audio_bytes=1, duration_s=1,
        before_commit=lambda: seen.update(
            published=[e["guid"] for e in repo.list_episodes(env["conn"], "published")]
        ),
    )  # fmt: skip
    assert seen["published"] == ["g1"]


def test_publishing_a_non_draft_is_refused(env):
    episode_id, main, _ = seeded_draft(env)
    kwargs = {"audio_file": "episodes/a.mp3", "audio_bytes": 1, "duration_s": 1}
    repo.publish_draft_episode(env["conn"], episode_id, **kwargs)
    with pytest.raises(ValueError, match="not a draft"):
        repo.publish_draft_episode(env["conn"], episode_id, **kwargs)
    assert repo.get_episode(env["conn"], episode_id)["number"] == 1  # unchanged


def test_find_draft_and_stale_drafts(env):
    episode_id, main, _ = seeded_draft(env)
    conn = env["conn"]
    assert repo.find_draft_for_article(conn, main)["id"] == episode_id
    assert repo.stale_drafts(conn) == []  # its article is pending: continuable
    repo.set_article_status(conn, main, "failed")
    assert [d["id"] for d in repo.stale_drafts(conn)] == [episode_id]
    orphan = repo.create_episode(conn, guid="orphan", mode="per_article", title="O")
    assert orphan in [d["id"] for d in repo.stale_drafts(conn)]  # no discussed article at all


def test_published_episodes_are_never_stale(env):
    episode_id, main, _ = seeded_draft(env)
    repo.publish_draft_episode(
        env["conn"], episode_id, audio_file="episodes/a.mp3", audio_bytes=1, duration_s=1
    )
    assert repo.stale_drafts(env["conn"]) == []
    assert repo.find_draft_for_article(env["conn"], main) is None


# --- unchanged behavior around it --------------------------------------------------------


@respx.mock
def test_llm_failure_before_a_draft_exists_leaves_no_draft(env):
    one_new_article(env)
    summary = make_pipeline(env, llm=FakeLLM(error=LLMError("no"))).run([BLOG])
    assert summary.failed and repo.list_episodes(env["conn"]) == []
