"""Deterministic fake TTS for tests and dry runs: tiny silent WAV, records calls."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from content2podcast.config import Secrets
from content2podcast.providers.registry import ProviderOptions, register_tts
from content2podcast.providers.tts.base import AudioChunk, VoiceCapabilities

SAMPLE_RATE = 8000
SECONDS_PER_CHAR = 0.01


@dataclass
class TTSCall:
    text: str
    voice: str
    style: str


class FakeTTS:
    name = "fake"

    def __init__(
        self,
        styles: tuple[str, ...] = ("neutral", "cheerful", "serious"),
        voice_styles: dict[str, tuple[str, ...]] | None = None,
        max_chars_per_request: int = 1000,
        language: str = "de-DE",
    ):
        self.styles = styles
        self.voice_styles = voice_styles or {}
        self.max_chars_per_request = max_chars_per_request
        self.language = language
        self.calls: list[TTSCall] = []

    def capabilities(self, voice: str) -> VoiceCapabilities:
        return VoiceCapabilities(
            styles=tuple(self.voice_styles.get(voice, self.styles)),
            max_chars_per_request=self.max_chars_per_request,
            language=self.language,
        )

    def synthesize(self, text: str, voice: str, style: str) -> AudioChunk:
        self.calls.append(TTSCall(text, voice, style))
        frames = max(1, int(len(text) * SECONDS_PER_CHAR * SAMPLE_RATE))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(b"\x00\x00" * frames)
        return AudioChunk(data=buf.getvalue(), ext="wav")


class FakeTTSOptions(ProviderOptions):
    provider: Literal["fake"] = "fake"
    styles: list[str] = Field(default_factory=lambda: ["neutral", "cheerful", "serious"])
    voice_styles: dict[str, list[str]] = Field(default_factory=dict)
    max_chars_per_request: int = Field(1000, gt=0)
    language: str = "de-DE"


@register_tts("fake", options=FakeTTSOptions)
def build_fake_tts(options: FakeTTSOptions, secrets: Secrets) -> FakeTTS:
    return FakeTTS(
        styles=tuple(options.styles),
        voice_styles={v: tuple(s) for v, s in options.voice_styles.items()},
        max_chars_per_request=options.max_chars_per_request,
        language=options.language,
    )
