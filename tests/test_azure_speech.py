import logging
import xml.etree.ElementTree as ET

import httpx
import pytest
import respx
from pydantic import SecretStr

from content2podcast import __version__
from content2podcast.config import ConfigError, Secrets, load_config
from content2podcast.providers.registry import (
    ProviderNotConfiguredError,
    build_tts,
    list_providers,
)
from content2podcast.providers.tts.azure_speech import (
    DEFAULT_STYLES,
    AzureSpeechOptions,
    AzureSpeechTTS,
    resolve_url,
)
from content2podcast.providers.tts.azure_ssml import MSTTS_NS, SSML_NS
from content2podcast.providers.tts.base import (
    AudioChunk,
    TTSError,
    TTSProvider,
    allowed_styles,
)

REGION_URL = "https://swedencentral.tts.speech.microsoft.com/cognitiveservices/v1"
KEY = "speech-key-123"
FOUNDRY_KEY = "foundry-key-456"
MIA = "de-DE-Mia:MAI-Voice-2.1"
KLAUS = "de-DE-Klaus:MAI-Voice-2.1"
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 50


def secrets(**kwargs):
    defaults = {"azure_speech_key": SecretStr(KEY)}
    return Secrets(**{**defaults, **kwargs})


def make(secrets_=None, **options) -> AzureSpeechTTS:
    return build_tts(AzureSpeechOptions(**options), secrets_ or secrets())


def audio(content=MP3):
    return httpx.Response(200, content=content, headers={"Content-Type": "audio/mpeg"})


def body_xml(route, index=0) -> ET.Element:
    return ET.fromstring(route.calls[index].request.content.decode("utf-8"))


# --- request -----------------------------------------------------------------------------


@respx.mock
def test_headers():
    route = respx.post(REGION_URL).mock(return_value=audio())
    make().synthesize("Hallo", MIA, "neutral")
    headers = route.calls[0].request.headers
    assert headers["ocp-apim-subscription-key"] == KEY
    assert headers["content-type"] == "application/ssml+xml"
    assert headers["x-microsoft-outputformat"] == "audio-24khz-96kbitrate-mono-mp3"
    assert headers["user-agent"].startswith(f"content2podcast/{__version__}")


@respx.mock
def test_ssml_body_with_style_and_text():
    route = respx.post(REGION_URL).mock(return_value=audio())
    make().synthesize("Das ist ja <unglaublich> & toll!", MIA, "excited")
    root = body_xml(route)
    assert root.tag == f"{{{SSML_NS}}}speak"
    assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "de-DE"
    voice = root.find(f"{{{SSML_NS}}}voice")
    assert voice.get("name") == MIA
    express = voice.find(f"{{{MSTTS_NS}}}express-as")
    assert express.get("style") == "excited"
    assert express.text == "Das ist ja <unglaublich> & toll!"


@respx.mock
def test_neutral_and_unknown_styles_are_plain_speech(caplog):
    route = respx.post(REGION_URL).mock(return_value=audio())
    tts = make()
    tts.synthesize("Hallo", MIA, "neutral")
    with caplog.at_level(logging.WARNING):
        tts.synthesize("Hallo", KLAUS, "furious")
    for index in (0, 1):
        assert b"express-as" not in route.calls[index].request.content
    assert any("furious" in r.message for r in caplog.records)


@respx.mock
def test_language_comes_from_the_voice_or_the_option():
    route = respx.post(REGION_URL).mock(return_value=audio())
    make().synthesize("Hi", "en-US-Jenny:MAI-Voice-2.1", "neutral")
    assert body_xml(route).get("{http://www.w3.org/XML/1998/namespace}lang") == "en-US"
    make(language="fr-FR").synthesize("Hi", MIA, "neutral")
    assert body_xml(route, 1).get("{http://www.w3.org/XML/1998/namespace}lang") == "fr-FR"


@respx.mock
def test_returns_audio_chunk_and_logs():
    respx.post(REGION_URL).mock(return_value=audio())
    chunk = make().synthesize("Hallo", MIA, "neutral")
    assert chunk == AudioChunk(data=MP3, ext="mp3")


@respx.mock
def test_other_output_format_is_sent_and_sets_the_extension():
    route = respx.post(REGION_URL).mock(return_value=audio(b"RIFF....WAVE"))
    chunk = make(output_format="riff-24khz-16bit-mono-pcm").synthesize("Hi", MIA, "neutral")
    assert route.calls[0].request.headers["x-microsoft-outputformat"] == "riff-24khz-16bit-mono-pcm"
    assert chunk.ext == "wav"


# --- URL: region vs endpoint -------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "secret_kwargs", "expected"),
    [
        ({}, {}, REGION_URL),
        (
            {"region": "westeurope"},
            {},
            "https://westeurope.tts.speech.microsoft.com/cognitiveservices/v1",
        ),
        (
            {},
            {"azure_speech_region": "eastus"},
            "https://eastus.tts.speech.microsoft.com/cognitiveservices/v1",
        ),
        (  # the option wins over the environment
            {"region": "westeurope"},
            {"azure_speech_region": "eastus"},
            "https://westeurope.tts.speech.microsoft.com/cognitiveservices/v1",
        ),
        (  # a bare custom domain gets the custom-domain path
            {"endpoint": "https://my-res.cognitiveservices.azure.com"},
            {},
            "https://my-res.cognitiveservices.azure.com/tts/cognitiveservices/v1",
        ),
        (
            {"endpoint": "https://my-res.cognitiveservices.azure.com/"},
            {},
            "https://my-res.cognitiveservices.azure.com/tts/cognitiveservices/v1",
        ),
        (  # an endpoint with a path is used as given
            {"endpoint": "https://proxy.example.com/speech/v1"},
            {},
            "https://proxy.example.com/speech/v1",
        ),
        (  # endpoint from the environment, option wins
            {},
            {"azure_speech_endpoint": "https://env.cognitiveservices.azure.com"},
            "https://env.cognitiveservices.azure.com/tts/cognitiveservices/v1",
        ),
        (
            {"endpoint": "https://a.example.com/x"},
            {"azure_speech_endpoint": "https://env.example.com/y"},
            "https://a.example.com/x",
        ),
    ],
)
def test_url_resolution(options, secret_kwargs, expected):
    assert resolve_url(AzureSpeechOptions(**options), secrets(**secret_kwargs)) == expected


@respx.mock
def test_requests_go_to_the_endpoint_when_configured():
    url = "https://my-res.cognitiveservices.azure.com/tts/cognitiveservices/v1"
    route = respx.post(url).mock(return_value=audio())
    make(endpoint="https://my-res.cognitiveservices.azure.com").synthesize("Hi", MIA, "neutral")
    assert route.called


# --- key fallback ------------------------------------------------------------------------


@respx.mock
def test_foundry_key_is_the_fallback():
    route = respx.post(REGION_URL).mock(return_value=audio())
    only_foundry = Secrets(azure_foundry_api_key=SecretStr(FOUNDRY_KEY))
    make(only_foundry).synthesize("Hi", MIA, "neutral")
    assert route.calls[0].request.headers["ocp-apim-subscription-key"] == FOUNDRY_KEY


@respx.mock
def test_speech_key_wins_over_foundry_key():
    route = respx.post(REGION_URL).mock(return_value=audio())
    both = Secrets(azure_speech_key=SecretStr(KEY), azure_foundry_api_key=SecretStr(FOUNDRY_KEY))
    make(both).synthesize("Hi", MIA, "neutral")
    assert route.calls[0].request.headers["ocp-apim-subscription-key"] == KEY


def test_no_key_is_a_clear_error():
    with pytest.raises(
        ProviderNotConfiguredError, match="AZURE_SPEECH_KEY or AZURE_FOUNDRY_API_KEY"
    ):
        build_tts(AzureSpeechOptions(), Secrets())


@respx.mock
def test_key_never_appears_in_logs_errors_or_repr(caplog):
    respx.post(REGION_URL).mock(return_value=httpx.Response(401, text="denied"))
    tts = make()
    with caplog.at_level(logging.DEBUG), pytest.raises(TTSError) as exc:
        tts.synthesize("Hi", MIA, "neutral")
    assert KEY not in caplog.text and KEY not in str(exc.value)
    assert KEY not in repr(tts) and KEY not in repr(tts.options)


# --- retries and errors ------------------------------------------------------------------


@respx.mock
def test_429_with_retry_after_is_retried():
    route = respx.post(REGION_URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "0"}), audio()]
    )
    assert make().synthesize("Hi", MIA, "neutral").data == MP3
    assert route.call_count == 2


@respx.mock
def test_5xx_is_retried_then_reported():
    route = respx.post(REGION_URL).mock(
        side_effect=[httpx.Response(503, headers={"Retry-After": "0"})] * 4
    )
    with pytest.raises(TTSError, match="HTTP 503"):
        make(max_retries=3).synthesize("Hi", MIA, "neutral")
    assert route.call_count == 4


@respx.mock
def test_max_retries_zero_means_a_single_attempt():
    route = respx.post(REGION_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(TTSError, match="HTTP 429"):
        make(max_retries=0).synthesize("Hi", MIA, "neutral")
    assert route.call_count == 1


@respx.mock
def test_400_raises_tts_error_with_body_snippet_and_is_not_retried():
    route = respx.post(REGION_URL).mock(
        return_value=httpx.Response(400, text="Invalid voice name 'x'" + " " * 10 + "y" * 500)
    )
    with pytest.raises(TTSError) as exc:
        make().synthesize("Hi", MIA, "neutral")
    assert route.call_count == 1
    message = str(exc.value)
    assert "HTTP 400" in message and "Invalid voice name" in message
    assert len(message) < 400  # the body is a snippet, not everything


@respx.mock
def test_transport_error_is_a_tts_error():
    respx.post(REGION_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(TTSError, match="request failed"):
        make(max_retries=0).synthesize("Hi", MIA, "neutral")


@respx.mock
def test_empty_audio_is_an_error():
    respx.post(REGION_URL).mock(return_value=audio(b""))
    with pytest.raises(TTSError, match="no audio"):
        make().synthesize("Hi", MIA, "neutral")


@respx.mock
def test_non_mp3_data_is_an_error():
    respx.post(REGION_URL).mock(return_value=audio(b"<html>error page</html>"))
    with pytest.raises(TTSError, match="not MP3"):
        make().synthesize("Hi", MIA, "neutral")


@respx.mock
def test_frame_sync_without_id3_tag_is_valid_mp3():
    respx.post(REGION_URL).mock(return_value=audio(b"\xff\xfb\x90\x00" * 20))
    assert make().synthesize("Hi", MIA, "neutral").ext == "mp3"


def test_text_longer_than_the_limit_is_rejected_without_a_request():
    with respx.mock:
        route = respx.post(REGION_URL).mock(return_value=audio())
        with pytest.raises(TTSError, match="exceeds max_chars_per_request"):
            make(max_chars_per_request=10).synthesize("x" * 11, MIA, "neutral")
        assert not route.called


def test_empty_text_is_a_tts_error():
    with pytest.raises(TTSError, match="empty"):
        make().synthesize("  \x00 ", MIA, "neutral")


# --- capabilities, registration, configuration -------------------------------------------


def test_capabilities_default_styles_and_language():
    caps = make().capabilities(MIA)
    assert caps.styles == DEFAULT_STYLES
    assert set(DEFAULT_STYLES) == {
        "angry", "confused", "determined", "disgusted", "embarrassed", "excited", "fearful",
        "happy", "hopeful", "jealous", "joyful", "neutral", "regretful", "relieved", "sad",
        "shouting", "softvoice", "surprised", "whispering",
    }  # fmt: skip
    assert (caps.language, caps.max_chars_per_request) == ("de-DE", 2000)
    assert make().capabilities("custom-voice").language == "de-DE"


def test_capabilities_use_configured_styles_and_allowed_styles_intersects_them():
    tts = make(styles=["excited", "sad", "whispering"])
    assert tts.capabilities(KLAUS).styles == ("excited", "sad", "whispering")
    assert allowed_styles(tts, MIA, KLAUS) == ["neutral", "excited", "sad", "whispering"]
    assert allowed_styles(make(), MIA, KLAUS)[0] == "neutral"


def test_registered_and_a_tts_provider():
    assert "azure_speech" in list_providers()["tts"]
    assert isinstance(make(), TTSProvider)


def test_config_selects_options_model_with_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("tts:\n  provider: azure_speech\n  region: westeurope\n", encoding="utf-8")
    cfg = load_config(path, env_file=tmp_path / ".env")
    assert isinstance(cfg.tts, AzureSpeechOptions)
    assert (cfg.tts.region, cfg.tts.output_format, cfg.tts.max_retries) == (
        "westeurope",
        "audio-24khz-96kbitrate-mono-mp3",
        3,
    )


def test_config_rejects_unknown_output_format_and_options(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "tts:\n  provider: azure_speech\n  output_format: flac\n  speed: 2\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path, env_file=tmp_path / ".env")
    assert "tts.output_format" in str(exc.value) and "tts.speed" in str(exc.value)
