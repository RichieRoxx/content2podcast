import feedparser
import httpx
import pytest
import respx

from content2podcast import repository as repo
from content2podcast.layout import episode_work_dir
from content2podcast.pipeline import MAX_ATTEMPTS, RunSummary
from content2podcast.providers.llm.base import LLMError
from content2podcast.providers.llm.fake import FakeLLM
from content2podcast.providers.tts.base import TTSError
from content2podcast.providers.tts.fake import FakeTTS
from content2podcast.script.prompt import PromptError
from content2podcast.sources.discovery import DiscoveryReport
from pipeline_helpers import (
    BLOG,
    FEED,
    StubAssembler,
    articles_by_status,
    baseline,
    canned,
    feed,
    make_pipeline,
    route_posts,
    url,
)

# --- happy path --------------------------------------------------------------------------


@respx.mock
def test_n_pending_articles_become_n_episodes(env):
    pipeline = make_pipeline(env)
    baseline(env, pipeline)
    route_posts(1, 2, 3)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 3), (2, 2), (3, 1)))
    )
    summary = pipeline.run([BLOG])

    assert len(summary.published) == 3 and summary.failed == [] and summary.exit_code == 0
    assert {r.episode_title for r in summary.published} == {"Folge zum Artikel"}
    assert {r.title for r in summary.published} == {"Beitrag 1", "Beitrag 2", "Beitrag 3"}
    conn, config = env["conn"], env["config"]
    episodes = repo.list_episodes(conn, "published")
    assert sorted(e["number"] for e in episodes) == [1, 2, 3]
    assert all(e["audio_bytes"] == 103 and e["duration_s"] == 12.5 for e in episodes)
    for episode in episodes:
        assert (config.paths.output_dir / episode["audio_file"]).read_bytes().startswith(b"ID3")
        assert episode["audio_file"].startswith("episodes/2026-10-03-folge-zum-artikel-")
        [link] = repo.episode_articles(conn, episode["id"])
        assert link["role"] == "discussed"
    assert {a["status"] for a in repo.list_articles(conn, "processed")} == {"processed"}
    assert len(repo.list_articles(conn, "processed")) == 3
    assert repo.list_articles(conn, "pending") == []

    parsed = feedparser.parse((config.paths.output_dir / "feed.xml").read_bytes())
    assert len(parsed.entries) == 3 and not parsed.bozo
    assert parsed.entries[0].enclosures[0].href.startswith("https://nas.example.ts.net/episodes/")


@respx.mock
def test_oldest_articles_are_processed_first_and_get_the_lowest_numbers(env):
    llm = FakeLLM(
        lambda system, user, schema: canned("Folge " + user.split('title="')[1].split('"')[0])
    )
    pipeline = make_pipeline(env, llm=llm)
    baseline(env, pipeline)
    route_posts(1, 2, 3)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (3, 1), (1, 3), (2, 2)))
    )
    pipeline.run([BLOG])
    by_number = {e["number"]: e["title"] for e in repo.list_episodes(env["conn"])}
    assert by_number == {1: "Folge Beitrag 1", 2: "Folge Beitrag 2", 3: "Folge Beitrag 3"}


@respx.mock
def test_no_pending_articles_means_no_episode_and_exit_0(env):
    pipeline = make_pipeline(env)
    baseline(env, pipeline)
    summary = pipeline.run([BLOG])  # nothing new
    assert summary.results == [] and summary.exit_code == 0
    assert repo.list_episodes(env["conn"]) == []
    assert not (env["config"].paths.output_dir / "feed.xml").exists()


@respx.mock
def test_assembly_gets_segments_gaps_metadata_and_intro(env):
    config = env["config"]
    config.episode.gap_ms = 450
    config.episode.intro_file = env["tmp"] / "intro.mp3"
    config.podcast.author = "Erika"
    assembler = StubAssembler()
    pipeline = make_pipeline(env, assembler=assembler)
    baseline(env, pipeline)
    route_posts(1)
    respx.get(FEED).mock(return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 1))))
    pipeline.run([BLOG])
    [call] = assembler.calls
    assert [len(group) for group in call["segments"]] == [1, 1]  # two segments, one part each
    assert call["gap_ms"] == 450 and call["intro"] == env["tmp"] / "intro.mp3"
    meta = call["meta"]
    assert (meta.title, meta.artist, meta.album) == ("Folge zum Artikel", "Erika", "Mein Podcast")
    assert meta.date == "2026-10-03" and meta.comment == "Es geht um Paketmanager."


@respx.mock
def test_script_is_stored_with_the_episode_and_sources_come_from_the_database(env):
    import json

    pipeline = make_pipeline(env)
    baseline(env, pipeline)
    route_posts(1)
    respx.get(FEED).mock(return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 1))))
    pipeline.run([BLOG])
    [episode] = repo.list_episodes(env["conn"], "published")
    script = json.loads(episode["script_json"])
    assert script["sources"] == [{"title": "Beitrag 1", "url": url(1)}]
    assert [s["speaker"] for s in script["segments"]] == ["host", "expert"]


# --- failure isolation -------------------------------------------------------------------


@respx.mock
def test_a_failing_article_does_not_block_the_others(env):
    llm = FakeLLM(
        lambda system, user, schema: (
            (_ for _ in ()).throw(LLMError("model overloaded"))
            if 'title="Beitrag 2"' in user
            else canned()
        )
    )
    pipeline = make_pipeline(env, llm=llm)
    baseline(env, pipeline)
    route_posts(1, 2, 3)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 3), (2, 2), (3, 1)))
    )
    summary = pipeline.run([BLOG])
    assert [r.url for r in summary.published] == [url(1), url(3)]
    [failure] = summary.failed
    assert (
        failure.url == url(2)
        and "model overloaded" in failure.error
        and failure.status == "pending"
    )
    assert summary.exit_code == 0  # at least one episode was published
    assert articles_by_status(env["conn"])[url(2)] == "pending"
    row = next(a for a in repo.list_articles(env["conn"]) if a["url"] == url(2))
    assert (row["attempts"], "model overloaded" in row["last_error"]) == (1, True)


@respx.mock
def test_extraction_failure_is_counted_once(env):
    pipeline = make_pipeline(env)
    baseline(env, pipeline)
    respx.get(url(1)).mock(return_value=httpx.Response(403))
    respx.get(FEED).mock(return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 1))))
    summary = pipeline.run([BLOG])
    assert summary.exit_code == 1 and "no article text" in summary.failed[0].error
    row = next(a for a in repo.list_articles(env["conn"]) if a["url"] == url(1))
    assert row["attempts"] == 1


@respx.mock
def test_after_three_attempts_the_article_is_failed_and_no_longer_tried(env):
    pipeline = make_pipeline(env, llm=FakeLLM(error=LLMError("nope")))
    baseline(env, pipeline)
    route_posts(1)
    respx.get(FEED).mock(return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 1))))
    statuses = [pipeline.run([BLOG]).results[0].status for _ in range(MAX_ATTEMPTS)]
    assert statuses == ["pending", "pending", "failed"]
    assert articles_by_status(env["conn"])[url(1)] == "failed"
    assert pipeline.run([BLOG]).results == []  # not retried any more


@respx.mock
def test_exit_code_is_1_when_every_attempted_episode_failed(env):
    pipeline = make_pipeline(env, llm=FakeLLM(error=LLMError("down")))
    baseline(env, pipeline)
    route_posts(1, 2)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 2), (2, 1)))
    )
    summary = pipeline.run([BLOG])
    assert len(summary.failed) == 2 and summary.published == [] and summary.exit_code == 1


@respx.mock
def test_tts_and_assembly_failures_are_isolated_too(env):
    class FlakyTTS(FakeTTS):
        def synthesize(self, text, voice, style):
            raise TTSError("quota exceeded")

    pipeline = make_pipeline(env, tts=FlakyTTS())
    baseline(env, pipeline)
    route_posts(1)
    respx.get(FEED).mock(return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 1))))
    summary = pipeline.run([BLOG])
    assert "TTSError: quota exceeded" in summary.failed[0].error

    exploding = make_pipeline(env, assembler=StubAssembler(fail_for={"Folge zum Artikel"}))
    summary = exploding.run([BLOG])
    assert "assembly exploded" in summary.failed[0].error
    assert repo.list_episodes(env["conn"], "published") == []  # nothing half-published
    assert [e["status"] for e in repo.list_episodes(env["conn"])] == ["draft"]  # resumable


@respx.mock
def test_a_broken_prompt_template_stops_the_run_without_burning_attempts(env, tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "per_article_de.md").write_text(
        "<!-- system -->\nHallo $unknown\n<!-- user -->\nU\n", encoding="utf-8"
    )
    env["config"].episode.prompts_dir = prompts
    pipeline = make_pipeline(env)
    baseline(env, pipeline)
    route_posts(1, 2)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 2), (2, 1)))
    )
    with pytest.raises(PromptError, match="unknown"):
        pipeline.run([BLOG])
    assert {a["attempts"] for a in repo.list_articles(env["conn"], "pending")} == {0}


@respx.mock
def test_a_failing_source_does_not_stop_processing_of_pending_articles(env):
    pipeline = make_pipeline(env)
    baseline(env, pipeline)
    route_posts(1)
    respx.get(FEED).mock(return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 1))))
    pipeline_offline = make_pipeline(env, llm=FakeLLM(error=LLMError("x")))
    pipeline_offline.run([BLOG])  # the article stays pending (1 attempt)
    respx.get(FEED).mock(return_value=httpx.Response(403))  # now the source is down
    summary = pipeline.run([BLOG])
    assert summary.discovery.errors == 1
    assert len(summary.published) == 1 and summary.published[0].url == url(1)


# --- limits, force, retention ------------------------------------------------------------


@respx.mock
def test_max_episodes_per_run_limits_the_work_and_leaves_the_rest_pending(env):
    env["config"].episode.max_episodes_per_run = 1
    pipeline = make_pipeline(env)
    baseline(env, pipeline)
    route_posts(1, 2)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (2, 1), (1, 5)))
    )
    first = pipeline.run([BLOG])
    assert [r.url for r in first.published] == [url(1)]  # the oldest first
    assert articles_by_status(env["conn"])[url(2)] == "pending"
    second = pipeline.run([BLOG])
    assert [r.url for r in second.published] == [url(2)]


@respx.mock
def test_force_requeues_failed_and_skipped_articles(env):
    llm = FakeLLM(error=LLMError("down"))
    pipeline = make_pipeline(env, llm=llm)
    baseline(env, pipeline)
    route_posts(1)
    respx.get(FEED).mock(return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 1))))
    for _ in range(MAX_ATTEMPTS):
        pipeline.run([BLOG])
    assert articles_by_status(env["conn"])[url(1)] == "failed"

    llm.error = None
    llm.response = canned()
    assert pipeline.run([BLOG]).results == []  # without --force it stays failed
    summary = pipeline.run([BLOG], force=True)
    assert len(summary.published) == 1
    assert articles_by_status(env["conn"])[url(1)] == "processed"


@respx.mock
def test_force_does_not_touch_baseline_or_processed_articles(env):
    pipeline = make_pipeline(env)
    baseline(env, pipeline)
    summary = pipeline.run([BLOG], force=True)
    assert summary.results == []
    assert set(articles_by_status(env["conn"]).values()) == {"baseline"}


@respx.mock
def test_retention_runs_at_the_end_and_the_feed_is_rewritten(env):
    config, conn = env["config"], env["conn"]
    config.feed.retention.max_episodes = 2
    pipeline = make_pipeline(env)
    baseline(env, pipeline)
    route_posts(1, 2, 3)
    respx.get(FEED).mock(
        return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 3), (2, 2), (3, 1)))
    )
    summary = pipeline.run([BLOG])
    assert summary.pruned == 1
    assert [e["status"] for e in repo.list_episodes(conn)] == ["published", "published", "pruned"]
    parsed = feedparser.parse((config.paths.output_dir / "feed.xml").read_bytes())
    assert len(parsed.entries) == 2
    pruned = next(e for e in repo.list_episodes(conn) if e["status"] == "pruned")
    assert not (config.paths.output_dir / pruned["audio_file"]).exists()


@respx.mock
def test_work_directories_of_published_episodes_are_cleaned_up(env):
    pipeline = make_pipeline(env, new_guid=lambda: "fixed-guid")
    baseline(env, pipeline)
    route_posts(1)
    respx.get(FEED).mock(return_value=httpx.Response(200, content=feed((0, 24 * 30), (1, 1))))
    pipeline.run([BLOG])
    assert not episode_work_dir(env["config"].paths.data_dir, "fixed-guid").exists()


def test_run_summary_exit_code():
    ok = RunSummary(DiscoveryReport())
    assert ok.exit_code == 0
    from content2podcast.pipeline import EpisodeResult

    ok.results = [EpisodeResult(1, "u", "t", error="x"), EpisodeResult(2, "u", "t", guid="g")]
    assert ok.exit_code == 0 and len(ok.published) == 1 and len(ok.failed) == 1
    ok.results = [EpisodeResult(1, "u", "t", error="x")]
    assert ok.exit_code == 1
