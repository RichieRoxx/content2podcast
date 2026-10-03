"""TTS provider ``azure_speech``: Azure AI Speech REST API with MAI voices.

Configuration (``tts:`` section)::

    tts:
      provider: azure_speech
      region: swedencentral            # optional, else AZURE_SPEECH_REGION
      endpoint: null                   # optional override, else AZURE_SPEECH_ENDPOINT

Credentials come from the environment / ``.env``: ``AZURE_SPEECH_KEY``, falling back to
``AZURE_FOUNDRY_API_KEY`` (one Foundry key for LLM and TTS).

Endpoint rules: by default ``https://<region>.tts.speech.microsoft.com/cognitiveservices/v1``.
An ``endpoint`` that is only a host (a custom domain such as
``https://<name>.cognitiveservices.azure.com``) gets the path ``/tts/cognitiveservices/v1``
appended; an ``endpoint`` with a path is used as given.
"""

from __future__ import annotations

import logging
import re
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, field_validator

from content2podcast.config import HttpConfig, Secrets
from content2podcast.http import (
    HttpError,
    default_user_agent,
    loggable_url,
    make_client,
    request_with_retry,
)
from content2podcast.logging_setup import kv
from content2podcast.providers.registry import (
    ProviderNotConfiguredError,
    ProviderOptions,
    register_tts,
)
from content2podcast.providers.tts.azure_ssml import build_ssml
from content2podcast.providers.tts.base import AudioChunk, TTSError, VoiceCapabilities

log = logging.getLogger(__name__)

DEFAULT_STYLES = (
    "angry",
    "confused",
    "determined",
    "disgusted",
    "embarrassed",
    "excited",
    "fearful",
    "happy",
    "hopeful",
    "jealous",
    "joyful",
    "neutral",
    "regretful",
    "relieved",
    "sad",
    "shouting",
    "softvoice",
    "surprised",
    "whispering",
)
DEFAULT_OUTPUT_FORMAT = "audio-24khz-96kbitrate-mono-mp3"
DEFAULT_LANGUAGE = "de-DE"
CUSTOM_DOMAIN_PATH = "/tts/cognitiveservices/v1"
_VOICE_LANGUAGE = re.compile(r"^([a-z]{2,3}-[A-Z]{2,4})-")


def extension_for(output_format: str) -> str:
    """File extension of the audio Azure returns for ``output_format``."""
    fmt = output_format.lower()
    if "mp3" in fmt:
        return "mp3"
    if fmt.startswith("riff"):
        return "wav"
    if "ogg" in fmt:
        return "ogg"
    raise ValueError(f"unsupported output format {output_format!r} (need an mp3, riff or ogg one)")


def _looks_like_mp3(data: bytes) -> bool:
    """ID3 tag or an MPEG audio frame sync."""
    return data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)


class AzureSpeechOptions(ProviderOptions):
    provider: Literal["azure_speech"] = "azure_speech"
    region: str | None = None  # default: AZURE_SPEECH_REGION (swedencentral)
    endpoint: str | None = None  # default: AZURE_SPEECH_ENDPOINT, else the regional URL
    output_format: str = DEFAULT_OUTPUT_FORMAT
    language: str | None = None  # default: taken from the voice name (de-DE-Mia:... -> de-DE)
    styles: list[str] = Field(default_factory=lambda: list(DEFAULT_STYLES))
    max_chars_per_request: int = Field(2000, gt=0)
    timeout_s: float = Field(60.0, gt=0)
    max_retries: int = Field(3, ge=0)

    @field_validator("output_format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        extension_for(value)
        return value


def resolve_url(options: AzureSpeechOptions, secrets: Secrets) -> str:
    endpoint = options.endpoint or secrets.azure_speech_endpoint
    if not endpoint:
        region = options.region or secrets.azure_speech_region
        return f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    parts = urlsplit(endpoint)
    if parts.path in ("", "/"):
        return endpoint.rstrip("/") + CUSTOM_DOMAIN_PATH
    return endpoint


class AzureSpeechTTS:
    name = "azure_speech"

    def __init__(
        self, options: AzureSpeechOptions, url: str, api_key: str, client: httpx.Client
    ) -> None:
        self.options = options
        self.url = url
        self._api_key = api_key
        self._client = client

    def __repr__(self) -> str:
        return f"AzureSpeechTTS(url={loggable_url(self.url)!r})"

    def capabilities(self, voice: str) -> VoiceCapabilities:
        match = _VOICE_LANGUAGE.match(voice)
        language = self.options.language or (match.group(1) if match else DEFAULT_LANGUAGE)
        return VoiceCapabilities(
            styles=tuple(self.options.styles),
            max_chars_per_request=self.options.max_chars_per_request,
            language=language,
        )

    def synthesize(self, text: str, voice: str, style: str) -> AudioChunk:
        caps = self.capabilities(voice)
        if len(text) > caps.max_chars_per_request:
            raise TTSError(
                f"Text of {len(text)} characters exceeds max_chars_per_request "
                f"({caps.max_chars_per_request}); split it first"
            )
        try:
            ssml = build_ssml(text, voice, style, caps.language, caps.styles)
        except ValueError as exc:
            raise TTSError(str(exc)) from None

        headers = {
            "Ocp-Apim-Subscription-Key": self._api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": self.options.output_format,
            "User-Agent": default_user_agent(),
        }
        try:
            response = request_with_retry(
                self._client,
                "POST",
                self.url,
                max_attempts=self.options.max_retries + 1,
                headers=headers,
                content=ssml.encode("utf-8"),
            )
        except HttpError as exc:
            detail = f": {exc.body}" if exc.body else ""
            if exc.status is None:
                raise TTSError(f"Azure Speech request failed ({exc})") from exc
            raise TTSError(f"Azure Speech returned HTTP {exc.status}{detail}") from exc

        ext = extension_for(self.options.output_format)
        data = response.content
        if not data:
            raise TTSError("Azure Speech returned no audio")
        if ext == "mp3" and not _looks_like_mp3(data):
            raise TTSError("Azure Speech returned data that is not MP3 audio")
        log.info(
            "TTS synthesized: %s", kv(voice=voice, style=style, chars=len(text), bytes=len(data))
        )
        return AudioChunk(data=data, ext=ext)


@register_tts("azure_speech", options=AzureSpeechOptions)
def build_azure_speech(options: AzureSpeechOptions, secrets: Secrets) -> AzureSpeechTTS:
    key = secrets.effective_speech_key
    if key is None:
        raise ProviderNotConfiguredError(
            "tts provider azure_speech needs AZURE_SPEECH_KEY or AZURE_FOUNDRY_API_KEY "
            "(environment or .env)"
        )
    client = make_client(HttpConfig(read_timeout=options.timeout_s))
    return AzureSpeechTTS(options, resolve_url(options, secrets), key.get_secret_value(), client)
