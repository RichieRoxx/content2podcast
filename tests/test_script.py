import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from content2podcast.script.models import (
    PodcastScript,
    ScriptError,
    Segment,
    SourceRef,
    build_llm_schema,
    clean_segments,
    load_script,
    save_script,
    validate_script,
)
from content2podcast.script.render import render_markdown

FIXTURES = Path(__file__).parent / "fixtures" / "script"
NAMES = {"host": "Mia", "expert": "Klaus"}
STYLES = ["neutral", "cheerful", "serious"]


# --- LLM schema --------------------------------------------------------------------------


def _walk(node):
    """All nested JSON-schema objects."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_style_enum_reflects_given_styles():
    schema = build_llm_schema(["neutral", "calm"]).model_json_schema()
    assert schema["$defs"]["ScriptSegment"]["properties"]["style"]["enum"] == ["neutral", "calm"]
    other = build_llm_schema(STYLES).model_json_schema()
    assert other["$defs"]["ScriptSegment"]["properties"]["style"]["enum"] == STYLES


def test_duplicate_styles_are_collapsed_and_empty_is_rejected():
    schema = build_llm_schema(["neutral", "neutral", "calm"]).model_json_schema()
    assert schema["$defs"]["ScriptSegment"]["properties"]["style"]["enum"] == ["neutral", "calm"]
    with pytest.raises(ValueError):
        build_llm_schema([])


def test_json_schema_is_strict_mode_compatible():
    schema = build_llm_schema(STYLES).model_json_schema()
    objects = [n for n in _walk(schema) if n.get("type") == "object"]
    assert len(objects) == 2  # script and segment
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])  # every field required
    assert set(schema["properties"]) == {"title", "summary", "segments"}  # no sources
    segment = schema["$defs"]["ScriptSegment"]["properties"]
    assert segment["speaker"]["enum"] == ["host", "expert"]


def test_llm_schema_validates_and_rejects():
    model = build_llm_schema(STYLES)
    ok = model.model_validate(
        {
            "title": "T",
            "summary": "S",
            "segments": [{"speaker": "host", "style": "cheerful", "text": "Hi"}],
        }
    )
    assert ok.segments[0].style == "cheerful"
    with pytest.raises(ValidationError):  # style not in the enum
        model.model_validate(
            {
                "title": "T",
                "summary": "S",
                "segments": [{"speaker": "host", "style": "angry", "text": "Hi"}],
            }
        )
    with pytest.raises(ValidationError):  # additional property
        model.model_validate({"title": "T", "summary": "S", "segments": [], "sources": []})


def test_from_llm_fills_sources_from_the_caller():
    model = build_llm_schema(STYLES)
    output = model.model_validate(
        {
            "title": " T ",
            "summary": "S",
            "segments": [{"speaker": "expert", "style": "neutral", "text": "  Hi  "}],
        }
    )
    sources = [SourceRef(title="A", url="https://example.com/a")]
    script = PodcastScript.from_llm(output, sources)
    assert script.title == "T" and script.segments[0].text == "Hi"
    assert script.sources == sources


# --- model and helpers -------------------------------------------------------------------


def test_segment_model_rejects_unknown_speaker_and_extra_fields():
    with pytest.raises(ValidationError):
        Segment(speaker="narrator", style="neutral", text="x")
    with pytest.raises(ValidationError):
        Segment(speaker="host", style="neutral", text="x", volume=3)


def test_counts_and_duration():
    script = load_script(FIXTURES / "script.json")
    assert script.word_count == 6 + 11 + 9
    assert script.char_count == sum(len(s.text) for s in script.segments)
    assert script.estimated_minutes(words_per_minute=130) == pytest.approx(0.2)


def test_clean_segments_trims_drops_empty_and_replaces_unknown_styles():
    segments = [
        Segment(speaker="host", style="cheerful", text="  Hallo \n  Welt  "),
        Segment(speaker="expert", style="neutral", text="   "),
        Segment(speaker="expert", style="angry", text="Ja."),
    ]
    cleaned = clean_segments(segments, STYLES)
    assert [(s.speaker, s.style, s.text) for s in cleaned] == [
        ("host", "cheerful", "Hallo Welt"),
        ("expert", "neutral", "Ja."),
    ]
    assert clean_segments(segments)[1].style == "angry"  # no whitelist: styles untouched


def test_validate_script_reports_problems():
    good = load_script(FIXTURES / "script.json")
    assert validate_script(good, STYLES) == []
    assert validate_script(good, ["neutral"]) == [
        "segment 1 uses unknown style 'cheerful'",
        "segment 3 uses unknown style 'serious'",
    ]
    empty = PodcastScript(title=" ", summary="", segments=[])
    assert validate_script(empty) == ["title is empty", "script has no segments"]
    one_voice = PodcastScript(
        title="T", summary="S", segments=[Segment(speaker="host", style="neutral", text="x")]
    )
    assert validate_script(one_voice) == ["dialogue needs both a host and an expert segment"]


# --- load / save -------------------------------------------------------------------------


def test_load_save_round_trip(tmp_path):
    script = load_script(FIXTURES / "script.json")
    target = tmp_path / "nested" / "script.json"
    save_script(script, target)
    assert load_script(target) == script
    raw = target.read_text(encoding="utf-8")
    assert "Lock-Dateien" in raw and "\\u" not in raw  # real UTF-8, not escapes
    assert raw.endswith("\n")
    assert list(json.loads(raw)) == ["title", "summary", "segments", "sources"]
    assert not list(tmp_path.rglob("*.tmp"))


def test_saved_file_matches_the_documented_layout(tmp_path):
    script = PodcastScript(
        title="T",
        summary="S",
        segments=[Segment(speaker="host", style="neutral", text="Hi")],
        sources=[SourceRef(title="A", url="https://example.com")],
    )
    save_script(script, tmp_path / "s.json")
    assert json.loads((tmp_path / "s.json").read_text()) == {
        "title": "T",
        "summary": "S",
        "segments": [{"speaker": "host", "style": "neutral", "text": "Hi"}],
        "sources": [{"title": "A", "url": "https://example.com"}],
    }


def test_sources_are_optional_when_loading(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"title": "T", "summary": "S", "segments": []}')
    assert load_script(path).sources == []


def test_load_errors_are_readable(tmp_path):
    with pytest.raises(ScriptError, match="not found"):
        load_script(tmp_path / "missing.json")
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{nope")
    with pytest.raises(ScriptError, match="Cannot read"):
        load_script(bad_json)
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"title": "T", "summary": "S", "segments": [{"speaker": "x"}]}')
    with pytest.raises(ScriptError) as exc:
        load_script(invalid)
    assert "segments.0.speaker" in str(exc.value) and str(invalid) in str(exc.value)


# --- markdown ----------------------------------------------------------------------------


def test_markdown_snapshot():
    script = load_script(FIXTURES / "script.json")
    expected = (FIXTURES / "expected.md").read_text(encoding="utf-8")
    assert render_markdown(script, NAMES) == expected


def test_markdown_defaults_and_no_sources():
    script = PodcastScript(
        title="T",
        summary="S",
        segments=[Segment(speaker="expert", style="calm", text="Hi")],
    )
    assert render_markdown(script) == "# T\n\nS\n\n## Dialogue\n\n**Expert** *(calm)*: Hi\n"
