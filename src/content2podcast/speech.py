"""Provider-agnostic synthesis of a whole script: splitting, caching, concurrency, cost guard."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from content2podcast.config import RolesConfig
from content2podcast.logging_setup import kv
from content2podcast.providers.tts.base import NEUTRAL, TTSError, TTSOptions, TTSProvider
from content2podcast.script.models import PodcastScript

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


class CostGuardError(TTSError):
    """The script is longer than ``tts.max_chars_per_episode``."""


@dataclass(frozen=True)
class SpeechPart:
    """One synthesis request: a (piece of a) segment with its voice and validated style."""

    segment_index: int  # index in ``script.segments``; parts of one segment share it
    voice: str
    style: str
    text: str


def split_text(text: str, max_chars: int) -> list[str]:
    """Split ``text`` into pieces of at most ``max_chars``, at sentence boundaries where
    possible (then at whitespace, as a last resort anywhere)."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        while len(sentence) > max_chars:  # a single sentence that is too long
            cut = sentence.rfind(" ", 0, max_chars + 1)
            cut = cut if cut > 0 else max_chars
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        pieces.append(current)
    return pieces


def plan_script(script: PodcastScript, tts: TTSProvider, roles: RolesConfig) -> list[SpeechPart]:
    """The synthesis requests for ``script``, in spoken order.

    The voice comes from ``roles``; a style the voice does not support falls back to ``neutral``
    with a warning; long segments are split to respect ``max_chars_per_request``.
    """
    parts: list[SpeechPart] = []
    warned: set[tuple[str, str]] = set()
    for index, segment in enumerate(script.segments):
        voice = getattr(roles, segment.speaker).voice
        caps = tts.capabilities(voice)
        style = segment.style
        if style != NEUTRAL and style not in caps.styles:
            if (voice, style) not in warned:
                warned.add((voice, style))
                log.warning(
                    "Style not supported by the voice, using neutral: %s",
                    kv(voice=voice, style=style),
                )
            style = NEUTRAL
        parts.extend(
            SpeechPart(index, voice, style, piece)
            for piece in split_text(segment.text, caps.max_chars_per_request)
        )
    return parts


def cache_key(provider: str, part: SpeechPart) -> str:
    """Hash identifying the audio of ``part``: same provider, voice, style and text."""
    raw = "\0".join((provider, part.voice, part.style, part.text))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cached(work_dir: Path, key: str) -> Path | None:
    for path in work_dir.glob(f"{key}.*"):
        if path.suffix != ".part" and path.stat().st_size > 0:
            return path
    return None


def _synthesize_part(tts: TTSProvider, work_dir: Path, key: str, part: SpeechPart) -> Path:
    if (cached := _cached(work_dir, key)) is not None:
        return cached
    chunk = tts.synthesize(part.text, part.voice, part.style)
    target = work_dir / f"{key}.{chunk.ext}"
    tmp = work_dir / f"{key}.{chunk.ext}.part"
    tmp.write_bytes(chunk.data)
    os.replace(tmp, target)  # a crash never leaves a half-written file under the final name
    return target


def synthesize_script(
    script: PodcastScript,
    tts: TTSProvider,
    roles: RolesConfig,
    work_dir: Path,
    cfg: TTSOptions,
) -> list[Path]:
    """Audio files for ``script`` in spoken order, aligned with :func:`plan_script`.

    * files are cached in ``work_dir`` by hash of provider, voice, style and text, so a rerun
      after a crash (or a repeated sentence) costs nothing
    * at most ``cfg.concurrency`` requests run at once; the order of the result is preserved
    * if the script is longer than ``cfg.max_chars_per_episode`` characters, a
      :class:`CostGuardError` is raised *before* any request
    """
    parts = plan_script(script, tts, roles)
    total_chars = sum(len(p.text) for p in parts)
    if total_chars > cfg.max_chars_per_episode:
        raise CostGuardError(
            f"Script has {total_chars} characters, more than tts.max_chars_per_episode "
            f"({cfg.max_chars_per_episode}); shorten the episode or raise the limit"
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    keys = [cache_key(tts.name, part) for part in parts]
    unique: dict[str, SpeechPart] = dict(zip(keys, parts, strict=True))  # identical parts once
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        results = list(
            pool.map(lambda item: _synthesize_part(tts, work_dir, item[0], item[1]), unique.items())
        )
    paths = dict(zip(unique, results, strict=True))
    log.info(
        "Speech ready: %s",
        kv(parts=len(parts), requests=len(unique), chars=total_chars, concurrency=cfg.concurrency),
    )
    return [paths[key] for key in keys]
