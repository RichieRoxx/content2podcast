import logging
import xml.etree.ElementTree as ET

import pytest

from content2podcast.providers.tts.azure_ssml import (
    MSTTS_NS,
    SSML_NS,
    build_ssml,
    strip_invalid_xml_chars,
)

VOICE = "de-DE-Mia:MAI-Voice-2.1"
STYLES = ["neutral", "excited", "serious"]


def parse(ssml: str) -> ET.Element:
    return ET.fromstring(ssml)  # raises if not well-formed


def voice_element(root: ET.Element) -> ET.Element:
    return root.find(f"{{{SSML_NS}}}voice")


def spoken_text(ssml: str) -> str:
    return "".join(voice_element(parse(ssml)).itertext())


def test_matches_the_documented_shape_exactly():
    ssml = build_ssml("Das ist ja unglaublich!", VOICE, "excited", "de-DE", STYLES)
    assert ssml == (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xmlns:mstts="http://www.w3.org/2001/mstts" xml:lang="de-DE">'
        '<voice name="de-DE-Mia:MAI-Voice-2.1">'
        '<mstts:express-as style="excited">Das ist ja unglaublich!</mstts:express-as>'
        "</voice></speak>"
    )


def test_valid_style_uses_express_as():
    root = parse(build_ssml("Hallo", VOICE, "serious", "de-DE", STYLES))
    assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "de-DE"
    voice = voice_element(root)
    assert voice.get("name") == VOICE
    [express] = voice.findall(f"{{{MSTTS_NS}}}express-as")
    assert express.get("style") == "serious" and express.text == "Hallo"


@pytest.mark.parametrize("style", [None, "", "neutral"])
def test_missing_or_neutral_style_has_no_express_as(style, caplog):
    with caplog.at_level(logging.WARNING):
        ssml = build_ssml("Hallo", VOICE, style, "de-DE", STYLES)
    assert "express-as" not in ssml
    assert voice_element(parse(ssml)).text == "Hallo"
    assert not caplog.records  # neutral is normal, not a warning


def test_unknown_style_falls_back_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        ssml = build_ssml("Hallo", VOICE, "furious", "de-DE", STYLES)
    assert "express-as" not in ssml and spoken_text(ssml) == "Hallo"
    assert any("furious" in r.message and VOICE in r.message for r in caplog.records)


def test_style_not_in_whitelist_even_if_otherwise_valid():
    assert "express-as" not in build_ssml("Hi", VOICE, "excited", "de-DE", ["neutral"])
    assert "express-as" not in build_ssml("Hi", VOICE, "excited", "de-DE", [])


def test_escaping_of_special_characters():
    text = "a < b > c & d \" e ' f"
    ssml = build_ssml(text, VOICE, "excited", "de-DE", STYLES)
    for entity in ("&lt;", "&gt;", "&amp;", "&quot;", "&apos;"):
        assert entity in ssml
    assert " < " not in ssml and " > " not in ssml and " & " not in ssml
    assert spoken_text(ssml) == text  # round trip through a real XML parser


def test_markup_in_text_cannot_inject_elements():
    text = "</voice></speak><prosody rate='fast'>x</prosody>"
    ssml = build_ssml(text, VOICE, None, "de-DE", STYLES)
    root = parse(ssml)
    assert [el.tag for el in root.iter()] == [f"{{{SSML_NS}}}speak", f"{{{SSML_NS}}}voice"]
    assert spoken_text(ssml) == text


def test_attributes_are_quoted_safely():
    evil_voice = 'x" onload="alert(1)'
    evil_lang = "de-DE'><injected/>"
    ssml = build_ssml("Hi", evil_voice, "excited", evil_lang, ["excited"])
    root = parse(ssml)
    assert voice_element(root).get("name") == evil_voice
    assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == evil_lang
    assert "injected" not in [el.tag for el in root.iter()]


def test_style_attribute_is_quoted_safely():
    style = 'a"b'
    root = parse(build_ssml("Hi", VOICE, style, "de-DE", [style]))
    express = voice_element(root).find(f"{{{MSTTS_NS}}}express-as")
    assert express.get("style") == style


def test_control_characters_are_removed():
    dirty = "a\x00b\x01c\x0bd\x0ce\x1ff￾g￿h\ud800i"
    ssml = build_ssml(dirty, VOICE, None, "de-DE", STYLES)
    assert spoken_text(ssml) == "abcdefghi"


def test_valid_whitespace_and_unicode_survive():
    text = "Grüße,\tOhr\nzwei Zeilen – 日本語 😀"
    assert spoken_text(build_ssml(text, VOICE, None, "de-DE", STYLES)) == text


def test_outer_whitespace_is_trimmed():
    assert spoken_text(build_ssml("  \n Hallo \t ", VOICE, None, "de-DE", STYLES)) == "Hallo"


@pytest.mark.parametrize("text", ["", "   ", "\x00\x01", " \x0b "])
def test_empty_text_is_rejected(text):
    with pytest.raises(ValueError, match="empty"):
        build_ssml(text, VOICE, None, "de-DE", STYLES)


@pytest.mark.parametrize("style", [None, "neutral", "excited", "furious"])
def test_never_emits_undocumented_elements(style):
    ssml = build_ssml("Test", VOICE, style, "de-DE", STYLES)
    for forbidden in ("prosody", "<break", "emphasis", "styledegree", "<?xml"):
        assert forbidden not in ssml


def test_strip_invalid_xml_chars_keeps_tab_lf_cr():
    assert strip_invalid_xml_chars("a\tb\nc\rd\x00") == "a\tb\nc\rd"
