import io
import wave
from typing import Literal

import pytest
from pydantic import BaseModel

from content2podcast.config import ConfigError, Secrets, load_config
from content2podcast.providers import registry
from content2podcast.providers.llm.base import LLMError, LLMProvider, LLMRefusalError
from content2podcast.providers.llm.fake import FakeLLM, FakeLLMOptions
from content2podcast.providers.registry import (
    ProviderNotConfiguredError,
    ProviderOptions,
    build_llm,
    build_tts,
    list_providers,
    register_llm,
    register_tts,
)
from content2podcast.providers.tts.base import TTSProvider, allowed_styles
from content2podcast.providers.tts.fake import FakeTTS, FakeTTSOptions


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    import os

    for key in list(os.environ):
        if key.startswith(("C2P_", "AZURE_")):
            monkeypatch.delenv(key)
    registry.load_builtin_providers()
    # keep test registrations out of the global registries
    monkeypatch.setattr(registry.llm_registry, "_entries", dict(registry.llm_registry._entries))
    monkeypatch.setattr(registry.tts_registry, "_entries", dict(registry.tts_registry._entries))


def config_from(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path, env_file=tmp_path / ".env")


class Answer(BaseModel):
    text: str


# --- registry ----------------------------------------------------------------------------


def test_builtin_fakes_registered():
    assert "fake" in list_providers()["llm"]
    assert "fake" in list_providers()["tts"]


def test_register_and_build_custom_llm_provider():
    class EchoOptions(ProviderOptions):
        provider: Literal["echo"] = "echo"
        prefix: str = ">"

    class Echo:
        name = "echo"

        def __init__(self, prefix):
            self.prefix = prefix

        def generate_structured(self, system, user, schema):
            return schema(text=self.prefix + user)

    @register_llm("echo", options=EchoOptions)
    def build(options, secrets):
        return Echo(options.prefix)

    assert "echo" in list_providers()["llm"]
    llm = build_llm(EchoOptions(prefix="# "), Secrets())
    assert isinstance(llm, LLMProvider)
    assert llm.generate_structured("s", "hi", Answer).text == "# hi"


def test_duplicate_registration_rejected():
    with pytest.raises(ValueError, match="already registered"):
        register_llm("fake", options=FakeLLMOptions)(lambda o, s: None)


def test_build_without_configuration_is_readable_error():
    with pytest.raises(ProviderNotConfiguredError, match="No llm provider configured"):
        build_llm(None, Secrets())
    with pytest.raises(ProviderNotConfiguredError, match="tts.provider"):
        build_tts(None, Secrets())


# --- config: discriminated union ---------------------------------------------------------


def test_providers_unset_by_default(tmp_path):
    cfg = config_from(tmp_path, "")
    assert cfg.llm is None and cfg.tts is None


def test_discriminator_selects_options_model(tmp_path):
    cfg = config_from(
        tmp_path,
        "llm:\n  provider: fake\n  response: {text: hi}\n"
        "tts:\n  provider: fake\n  styles: [neutral, calm]\n",
    )
    assert isinstance(cfg.llm, FakeLLMOptions) and cfg.llm.response == {"text": "hi"}
    assert isinstance(cfg.tts, FakeTTSOptions) and cfg.tts.styles == ["neutral", "calm"]
    assert cfg.tts.model_dump()["styles"] == ["neutral", "calm"]


def test_newly_registered_provider_is_available_in_config(tmp_path):
    class OtherOptions(ProviderOptions):
        provider: Literal["other"] = "other"
        region: str

    register_tts("other", options=OtherOptions)(lambda o, s: FakeTTS())
    cfg = config_from(tmp_path, "tts:\n  provider: other\n  region: eu\n")
    assert isinstance(cfg.tts, OtherOptions) and cfg.tts.region == "eu"
    assert isinstance(build_tts(cfg.tts, Secrets()), TTSProvider)


def test_env_selects_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("C2P_TTS__PROVIDER", "fake")
    monkeypatch.setenv("C2P_TTS__MAX_CHARS_PER_REQUEST", "42")
    cfg = config_from(tmp_path, "")
    assert isinstance(cfg.tts, FakeTTSOptions) and cfg.tts.max_chars_per_request == 42


def test_unknown_provider_lists_available(tmp_path):
    with pytest.raises(ConfigError) as exc:
        config_from(tmp_path, "llm:\n  provider: nope\n")
    msg = str(exc.value)
    assert "llm: Unknown llm provider 'nope'" in msg
    assert "Available: fake" in msg


def test_missing_provider_key(tmp_path):
    with pytest.raises(ConfigError, match=r"llm: Missing 'provider'.*fake"):
        config_from(tmp_path, "llm:\n  response: {}\n")


def test_option_errors_keep_field_path(tmp_path):
    with pytest.raises(ConfigError) as exc:
        config_from(tmp_path, "tts:\n  provider: fake\n  max_chars_per_request: 0\n  bogus: 1\n")
    msg = str(exc.value)
    assert "tts.max_chars_per_request" in msg
    assert "tts.bogus" in msg


# --- allowed_styles ----------------------------------------------------------------------


def test_allowed_styles_is_intersection_with_neutral_first():
    tts = FakeTTS(
        voice_styles={
            "mia": ("cheerful", "serious", "whisper", "neutral"),
            "klaus": ("serious", "cheerful", "sad"),
        }
    )
    assert allowed_styles(tts, "mia", "klaus") == ["neutral", "cheerful", "serious"]


def test_allowed_styles_always_includes_neutral():
    tts = FakeTTS(voice_styles={"a": ("cheerful",), "b": ("sad",)})
    assert allowed_styles(tts, "a", "b") == ["neutral"]


def test_allowed_styles_same_voice_and_duplicates():
    tts = FakeTTS(styles=("neutral", "calm", "calm"))
    assert allowed_styles(tts, "x", "x") == ["neutral", "calm"]


# --- fakes -------------------------------------------------------------------------------


def test_fake_llm_returns_validated_schema_and_records_calls():
    llm = FakeLLM(response={"text": "canned"})
    assert isinstance(llm, LLMProvider)
    result = llm.generate_structured("sys", "usr", Answer)
    assert result == Answer(text="canned")
    assert [(c.system, c.user, c.schema) for c in llm.calls] == [("sys", "usr", Answer)]


def test_fake_llm_callable_model_and_errors():
    assert (
        FakeLLM(lambda s, u, schema: Answer(text=u)).generate_structured("s", "u", Answer).text
        == "u"
    )
    with pytest.raises(LLMRefusalError):
        FakeLLM(error=LLMRefusalError("no")).generate_structured("s", "u", Answer)
    with pytest.raises(LLMError, match="no canned response"):
        FakeLLM().generate_structured("s", "u", Answer)


def test_fake_tts_produces_tiny_valid_wav_and_records_calls():
    tts = FakeTTS()
    assert isinstance(tts, TTSProvider)
    chunk = tts.synthesize("Hallo Welt", "mia", "cheerful")
    assert chunk.ext == "wav"
    with wave.open(io.BytesIO(chunk.data)) as wav:
        assert wav.getnframes() > 0
    assert len(chunk.data) < 10_000
    assert chunk.data == tts.synthesize("Hallo Welt", "mia", "cheerful").data  # deterministic
    assert (tts.calls[0].text, tts.calls[0].voice, tts.calls[0].style) == (
        "Hallo Welt",
        "mia",
        "cheerful",
    )
    caps = tts.capabilities("mia")
    assert (caps.language, caps.max_chars_per_request) == ("de-DE", 1000)


def test_factories_build_fakes_from_options():
    llm = build_llm(FakeLLMOptions(response={"text": "x"}), Secrets())
    assert llm.generate_structured("s", "u", Answer).text == "x"
    tts = build_tts(FakeTTSOptions(voice_styles={"v": ["calm"]}), Secrets())
    assert tts.capabilities("v").styles == ("calm",)
