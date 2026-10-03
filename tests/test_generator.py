import logging
from datetime import date

import pytest

from content2podcast.config import AppConfig
from content2podcast.providers.llm.base import LLMRefusalError
from content2podcast.providers.llm.fake import FakeLLM
from content2podcast.script.generator import ScriptArticle, generate_script
from content2podcast.script.models import ScriptError

STYLES = ["neutral", "cheerful"]
TODAY = date(2026, 10, 5)


def article(words=900, **kwargs):
    defaults = {
        "url": "https://blog.example.com/a",
        "title": "Der Artikel",
        "source": "blog.example.com",
        "published": "2026-09-29",
    }
    return ScriptArticle(text=" ".join(["wort"] * words), **{**defaults, **kwargs})


def response(words_per_segment=450, **overrides):
    data = {
        "title": "Folgentitel",
        "summary": "Kurz gesagt.",
        "segments": [
            {
                "speaker": "host",
                "style": "cheerful",
                "text": " ".join(["hallo"] * words_per_segment),
            },
            {"speaker": "expert", "style": "neutral", "text": " ".join(["ja"] * words_per_segment)},
        ],
    }
    return {**data, **overrides}


def test_sources_come_from_the_articles_not_the_llm():
    llm = FakeLLM(response())
    script = generate_script(
        llm,
        [article(), article(url="https://x.example.org/b", title="Zweiter")],
        AppConfig(),
        STYLES,
        today=TODAY,
    )
    assert [(s.title, s.url) for s in script.sources] == [
        ("Der Artikel", "https://blog.example.com/a"),
        ("Zweiter", "https://x.example.org/b"),
    ]
    assert script.title == "Folgentitel" and len(script.segments) == 2


def test_prompt_and_schema_reach_the_llm():
    llm = FakeLLM(response())
    generate_script(llm, [article()], AppConfig(), STYLES, today=TODAY)
    call = llm.calls[0]
    assert "Montag, 5. Oktober 2026" in call.user
    assert 'source="blog.example.com" title="Der Artikel" published="2026-09-29"' in call.user
    assert "neutral, cheerful" in call.user
    assert "„content2podcast“" in call.system
    style_enum = call.schema.model_json_schema()["$defs"]["ScriptSegment"]["properties"]["style"]
    assert style_enum["enum"] == STYLES


def test_segments_are_cleaned():
    data = response()
    data["segments"].append({"speaker": "host", "style": "neutral", "text": "   "})
    data["segments"][0]["text"] = "  Hallo \n  Welt  "
    script = generate_script(FakeLLM(data), [article()], AppConfig(), STYLES, today=TODAY)
    assert len(script.segments) == 2  # the empty one is dropped
    assert script.segments[0].text == "Hallo Welt"


@pytest.mark.parametrize(
    ("segments", "message"),
    [
        ([], "no segments"),
        (
            [{"speaker": "host", "style": "neutral", "text": "Nur ich."}],
            "both a host and an expert",
        ),
        ([{"speaker": "host", "style": "neutral", "text": "   "}], "no segments"),
    ],
)
def test_unusable_scripts_raise(segments, message):
    llm = FakeLLM(response(segments=segments))
    with pytest.raises(ScriptError, match=message):
        generate_script(llm, [article()], AppConfig(), STYLES, today=TODAY)


def test_llm_errors_propagate():
    llm = FakeLLM(error=LLMRefusalError("nein"))
    with pytest.raises(LLMRefusalError):
        generate_script(llm, [article()], AppConfig(), STYLES, today=TODAY)


def test_no_articles_is_a_programming_error():
    with pytest.raises(ValueError):
        generate_script(FakeLLM(response()), [], AppConfig(), STYLES, today=TODAY)


def test_length_deviation_over_30_percent_warns(caplog):
    # budget is 900 words; the script has 20
    with caplog.at_level(logging.WARNING):
        generate_script(FakeLLM(response(10)), [article(900)], AppConfig(), STYLES, today=TODAY)
    message = next(r.message for r in caplog.records if "deviates" in r.message)
    assert "words=20" in message and "target=900" in message and "deviation=-98%" in message


def test_length_within_tolerance_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        generate_script(FakeLLM(response(400)), [article(900)], AppConfig(), STYLES, today=TODAY)
    assert not [r for r in caplog.records if "deviates" in r.message]  # 800 vs 900: -11 %


def test_budget_floor_applies_to_short_articles(caplog):
    # a 100-word article still gets the 3 minute floor (420 words): 400 words are fine
    with caplog.at_level(logging.WARNING):
        generate_script(FakeLLM(response(200)), [article(100)], AppConfig(), STYLES, today=TODAY)
    assert not [r for r in caplog.records if "deviates" in r.message]
