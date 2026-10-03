"""TTS provider interface and style helper."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import Field

from content2podcast.providers.registry import ProviderOptions

NEUTRAL = "neutral"


class TTSError(Exception):
    """The TTS call failed."""


class TTSOptions(ProviderOptions):
    """Options every TTS provider shares (used by the provider-agnostic synthesis)."""

    concurrency: int = Field(3, ge=1)  # parallel requests
    max_chars_per_episode: int = Field(30000, gt=0)  # cost guard, checked before any request


@dataclass(frozen=True)
class VoiceCapabilities:
    styles: tuple[str, ...]
    max_chars_per_request: int
    language: str


@dataclass(frozen=True)
class AudioChunk:
    data: bytes
    ext: str  # file extension without dot, e.g. "mp3" or "wav"


@runtime_checkable
class TTSProvider(Protocol):
    name: str

    def capabilities(self, voice: str) -> VoiceCapabilities: ...

    def synthesize(self, text: str, voice: str, style: str) -> AudioChunk: ...


def allowed_styles(tts: TTSProvider, host_voice: str, expert_voice: str) -> list[str]:
    """Styles both voices support, always including ``neutral`` (listed first).

    This is the single source of truth for the LLM schema enum and the prompt.
    """
    host = tts.capabilities(host_voice).styles
    expert = set(tts.capabilities(expert_voice).styles)
    shared = [s for s in host if s in expert and s != NEUTRAL]
    return [NEUTRAL, *dict.fromkeys(shared)]
