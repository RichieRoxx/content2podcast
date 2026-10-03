import logging
import threading
import time

import pytest

from content2podcast.config import RolesConfig
from content2podcast.providers.tts.base import TTSError, TTSOptions
from content2podcast.providers.tts.fake import FakeTTS, FakeTTSOptions
from content2podcast.script.models import PodcastScript, Segment
from content2podcast.speech import (
    CostGuardError,
    SpeechPart,
    cache_key,
    plan_script,
    split_text,
    synthesize_script,
)

ROLES = RolesConfig()
HOST, EXPERT = ROLES.host.voice, ROLES.expert.voice


def script(*segments: tuple[str, str, str]) -> PodcastScript:
    return PodcastScript(
        title="T",
        summary="S",
        segments=[Segment(speaker=sp, style=st, text=tx) for sp, st, tx in segments],
    )


BASIC = script(
    ("host", "neutral", "Hallo und willkommen."),
    ("expert", "cheerful", "Schön, dabei zu sein."),
    ("host", "serious", "Dann los."),
)


def options(**kwargs) -> TTSOptions:
    return FakeTTSOptions(**kwargs)


# --- splitting ---------------------------------------------------------------------------


def test_short_text_is_not_split():
    assert split_text("Ein Satz.", 100) == ["Ein Satz."]
    assert split_text("   ", 100) == []


def test_split_at_sentence_boundaries_packs_greedily():
    text = "Eins ist hier. Zwei ist da! Drei kommt dann? Vier endet…"
    assert split_text(text, 30) == ["Eins ist hier. Zwei ist da!", "Drei kommt dann? Vier endet…"]
    assert split_text(text, 16) == [
        "Eins ist hier.",
        "Zwei ist da!",
        "Drei kommt dann?",
        "Vier endet…",
    ]


def test_every_piece_respects_the_limit_and_nothing_is_lost():
    text = " ".join(f"Satz Nummer {i} steht hier." for i in range(40))
    for limit in (40, 80, 200):
        pieces = split_text(text, limit)
        assert all(0 < len(p) <= limit for p in pieces)
        assert " ".join(pieces).split() == text.split()


def test_overlong_sentence_is_split_at_whitespace_then_anywhere():
    sentence = "wort " * 30  # no sentence end at all
    pieces = split_text(sentence, 22)
    assert all(len(p) <= 22 for p in pieces) and " ".join(pieces).split() == sentence.split()
    pieces = split_text("x" * 50, 20)
    assert pieces == ["x" * 20, "x" * 20, "x" * 10]


# --- plan --------------------------------------------------------------------------------


def test_plan_maps_speakers_to_voices_in_order():
    parts = plan_script(BASIC, FakeTTS(), ROLES)
    assert [(p.segment_index, p.voice, p.style, p.text) for p in parts] == [
        (0, HOST, "neutral", "Hallo und willkommen."),
        (1, EXPERT, "cheerful", "Schön, dabei zu sein."),
        (2, HOST, "serious", "Dann los."),
    ]


def test_unsupported_style_falls_back_to_neutral_with_one_warning(caplog):
    tts = FakeTTS(voice_styles={HOST: ("neutral", "cheerful"), EXPERT: ("neutral",)})
    spoken = script(
        ("host", "serious", "A."), ("host", "serious", "B."), ("expert", "cheerful", "C.")
    )
    with caplog.at_level(logging.WARNING):
        parts = plan_script(spoken, tts, ROLES)
    assert [p.style for p in parts] == ["neutral", "neutral", "neutral"]
    warnings = [r.message for r in caplog.records if "not supported" in r.message]
    assert len(warnings) == 2  # (host, serious) once, (expert, cheerful) once
    assert any("serious" in w for w in warnings) and any("cheerful" in w for w in warnings)


def test_neutral_is_always_allowed_even_if_not_listed(caplog):
    tts = FakeTTS(styles=("cheerful",))
    with caplog.at_level(logging.WARNING):
        parts = plan_script(script(("host", "neutral", "A.")), tts, ROLES)
    assert parts[0].style == "neutral" and not caplog.records


def test_long_segments_are_split_and_keep_their_segment_index():
    tts = FakeTTS(max_chars_per_request=30)
    long_segment = script(
        ("host", "cheerful", "Erster Satz hier. Zweiter Satz da. Dritter Satz dort.")
    )
    parts = plan_script(long_segment, tts, ROLES)
    assert len(parts) == 3 and {p.segment_index for p in parts} == {0}
    assert all(len(p.text) <= 30 and p.style == "cheerful" for p in parts)


# --- synthesis, cache --------------------------------------------------------------------


def test_synthesizes_all_parts_in_order(tmp_path):
    tts = FakeTTS()
    paths = synthesize_script(BASIC, tts, ROLES, tmp_path, options())
    assert len(paths) == 3 and all(p.exists() and p.suffix == ".wav" for p in paths)
    assert {c.text for c in tts.calls} == {
        "Hallo und willkommen.",
        "Schön, dabei zu sein.",
        "Dann los.",
    }
    for path, part in zip(paths, plan_script(BASIC, FakeTTS(), ROLES), strict=True):
        assert path.read_bytes() == FakeTTS().synthesize(part.text, part.voice, part.style).data


def test_cache_files_are_named_by_hash_of_provider_voice_style_and_text(tmp_path):
    tts = FakeTTS()
    paths = synthesize_script(
        script(("host", "cheerful", "Hallo.")), tts, ROLES, tmp_path, options()
    )
    expected = cache_key("fake", SpeechPart(0, HOST, "cheerful", "Hallo."))
    assert paths[0] == tmp_path / f"{expected}.wav"
    assert len(expected) == 64


def test_cache_key_depends_on_every_ingredient():
    base = SpeechPart(0, "v", "s", "t")
    keys = {
        cache_key("p", base),
        cache_key("q", base),
        cache_key("p", SpeechPart(0, "w", "s", "t")),
        cache_key("p", SpeechPart(0, "v", "x", "t")),
        cache_key("p", SpeechPart(0, "v", "s", "u")),
    }
    assert len(keys) == 5
    assert cache_key("p", base) == cache_key("p", SpeechPart(7, "v", "s", "t"))  # index irrelevant


def test_cache_hits_avoid_calls(tmp_path):
    first = FakeTTS()
    synthesize_script(BASIC, first, ROLES, tmp_path, options())
    assert len(first.calls) == 3
    second = FakeTTS()
    paths = synthesize_script(BASIC, second, ROLES, tmp_path, options())
    assert second.calls == [] and len(paths) == 3


def test_changed_text_style_or_voice_is_a_cache_miss(tmp_path):
    synthesize_script(BASIC, FakeTTS(), ROLES, tmp_path, options())
    tts = FakeTTS()
    changed = script(
        ("host", "neutral", "Hallo und willkommen!"),  # text
        ("expert", "serious", "Schön, dabei zu sein."),  # style
        ("expert", "serious", "Dann los."),  # voice (host -> expert)
    )
    synthesize_script(changed, tts, ROLES, tmp_path, options())
    assert len(tts.calls) == 3


def test_resume_after_a_crash_only_synthesizes_the_rest(tmp_path):
    class Flaky(FakeTTS):
        def synthesize(self, text, voice, style):
            if text == "Dann los.":
                raise TTSError("boom")
            return super().synthesize(text, voice, style)

    with pytest.raises(TTSError, match="boom"):
        synthesize_script(BASIC, Flaky(), ROLES, tmp_path, options(concurrency=1))
    retry = FakeTTS()
    synthesize_script(BASIC, retry, ROLES, tmp_path, options())
    assert [c.text for c in retry.calls] == ["Dann los."]  # the other two were cached


def test_identical_parts_are_synthesized_once(tmp_path):
    repeated = script(
        ("host", "neutral", "Ja."), ("expert", "neutral", "Ja."), ("host", "neutral", "Ja.")
    )
    tts = FakeTTS()
    paths = synthesize_script(repeated, tts, ROLES, tmp_path, options())
    assert len(paths) == 3 and paths[0] == paths[2]
    assert [(c.voice, c.text) for c in tts.calls].count((HOST, "Ja.")) == 1
    assert len(tts.calls) == 2  # host "Ja." and expert "Ja."


def test_empty_or_partial_cache_files_are_not_trusted(tmp_path):
    part = SpeechPart(0, HOST, "neutral", "Hallo.")
    key = cache_key("fake", part)
    (tmp_path / f"{key}.wav").write_bytes(b"")  # empty file from a crash
    (tmp_path / f"{key}.wav.part").write_bytes(b"half")  # leftover temp file
    tts = FakeTTS()
    [path] = synthesize_script(
        script(("host", "neutral", "Hallo.")), tts, ROLES, tmp_path, options()
    )
    assert len(tts.calls) == 1 and path.stat().st_size > 0
    assert not list(tmp_path.glob("*.part"))  # the stale temp file is gone


def test_no_temp_files_remain(tmp_path):
    synthesize_script(BASIC, FakeTTS(), ROLES, tmp_path, options())
    assert not list(tmp_path.glob("*.part"))


# --- concurrency -------------------------------------------------------------------------


class SlowTTS(FakeTTS):
    """First texts are the slowest, so unordered collection would show up."""

    def __init__(self, delays):
        super().__init__()
        self.delays = delays
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def synthesize(self, text, voice, style):
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.delays.get(text, 0))
            return super().synthesize(text, voice, style)
        finally:
            with self._lock:
                self.in_flight -= 1


def test_order_is_preserved_under_concurrency_and_the_limit_is_respected(tmp_path):
    texts = [f"Satz {i}." for i in range(8)]
    spoken = script(
        *[("host" if i % 2 == 0 else "expert", "neutral", t) for i, t in enumerate(texts)]
    )
    tts = SlowTTS({t: 0.05 * (8 - i) for i, t in enumerate(texts)})  # reversed finishing order
    paths = synthesize_script(spoken, tts, ROLES, tmp_path, options(concurrency=3))
    assert 1 < tts.max_in_flight <= 3
    expected = [
        FakeTTS().synthesize(t, HOST if i % 2 == 0 else EXPERT, "neutral").data
        for i, t in enumerate(texts)
    ]
    assert [p.read_bytes() for p in paths] == expected


def test_concurrency_one_is_serial(tmp_path):
    spoken = script(*[("host", "neutral", f"Satz {i}.") for i in range(4)])
    tts = SlowTTS({})
    synthesize_script(spoken, tts, ROLES, tmp_path, options(concurrency=1))
    assert tts.max_in_flight == 1


# --- cost guard --------------------------------------------------------------------------


def test_cost_guard_fails_before_any_request(tmp_path):
    tts = FakeTTS()
    chars = sum(len(s.text) for s in BASIC.segments)
    with pytest.raises(CostGuardError, match="max_chars_per_episode") as exc:
        synthesize_script(BASIC, tts, ROLES, tmp_path, options(max_chars_per_episode=chars - 1))
    assert tts.calls == [] and str(chars) in str(exc.value)
    assert isinstance(exc.value, TTSError)
    assert not list(tmp_path.iterdir())


def test_cost_guard_allows_exactly_the_limit(tmp_path):
    chars = sum(len(s.text) for s in BASIC.segments)
    assert (
        len(
            synthesize_script(
                BASIC, FakeTTS(), ROLES, tmp_path, options(max_chars_per_episode=chars)
            )
        )
        == 3
    )


def test_default_limits():
    cfg = FakeTTSOptions()
    assert (cfg.concurrency, cfg.max_chars_per_episode) == (3, 30000)
    with pytest.raises(ValueError):
        FakeTTSOptions(concurrency=0)
