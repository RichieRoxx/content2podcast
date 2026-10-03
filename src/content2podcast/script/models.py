"""Script data model, the strict LLM output schema and ``script.json`` load/save."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, field_validator

NEUTRAL = "neutral"
Speaker = Literal["host", "expert"]
SPEAKERS: tuple[str, ...] = ("host", "expert")


class ScriptError(Exception):
    """A script file or LLM result is unusable."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Segment(_Model):
    speaker: Speaker
    style: str
    text: str

    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


class SourceRef(_Model):
    title: str
    url: str


class PodcastScript(_Model):
    """The on-disk ``script.json``. ``sources`` come from the database, never from the LLM."""

    title: str
    summary: str
    segments: list[Segment]
    sources: list[SourceRef] = Field(default_factory=list)

    @property
    def word_count(self) -> int:
        return sum(len(s.text.split()) for s in self.segments)

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.segments)

    def estimated_minutes(self, words_per_minute: int) -> float:
        return self.word_count / words_per_minute

    @classmethod
    def from_llm(cls, output: BaseModel, sources: Iterable[SourceRef] = ()) -> PodcastScript:
        """Build the script from a validated LLM result (see :func:`build_llm_schema`)."""
        data = output.model_dump()
        return cls(
            title=data["title"].strip(),
            summary=data["summary"].strip(),
            segments=[Segment(**s) for s in data["segments"]],
            sources=list(sources),
        )


# --- LLM schema --------------------------------------------------------------------------


def build_llm_schema(styles: Sequence[str]) -> type[BaseModel]:
    """Pydantic model for the LLM's structured output with ``style`` restricted to ``styles``.

    The JSON schema is strict-mode compatible: every field is required and no object accepts
    additional properties. ``sources`` are deliberately absent (filled from the database).
    """
    unique = list(dict.fromkeys(styles))
    if not unique:
        raise ValueError("at least one style is required")
    style_type = Literal[tuple(unique)]  # type: ignore[valid-type]

    segment = create_model(
        "ScriptSegment",
        __config__=ConfigDict(extra="forbid"),
        speaker=(Speaker, Field(description="Who speaks: the host or the expert.")),
        style=(style_type, Field(description="Delivery style for this segment.")),
        text=(
            str,
            Field(description="The spoken text, without speaker label or stage directions."),
        ),
    )
    return create_model(
        "LLMScript",
        __config__=ConfigDict(extra="forbid"),
        title=(str, Field(description="Episode title.")),
        summary=(str, Field(description="Episode summary in 2 to 4 sentences.")),
        segments=(list[segment], Field(description="The dialogue, in spoken order.")),  # type: ignore[valid-type]
    )


# --- helpers -----------------------------------------------------------------------------


def clean_segments(
    segments: Iterable[Segment], allowed_styles: Sequence[str] | None = None
) -> list[Segment]:
    """Collapse whitespace in texts and drop empty segments. If ``allowed_styles`` is given,
    styles outside it are replaced with ``neutral``."""
    allowed = set(allowed_styles) if allowed_styles is not None else None
    cleaned: list[Segment] = []
    for segment in segments:
        text = " ".join(segment.text.split())
        if not text:
            continue
        style = segment.style
        if allowed is not None and style not in allowed:
            style = NEUTRAL
        cleaned.append(Segment(speaker=segment.speaker, style=style, text=text))
    return cleaned


def validate_script(
    script: PodcastScript, allowed_styles: Sequence[str] | None = None
) -> list[str]:
    """Problems that make a script unusable for synthesis (empty list = fine)."""
    problems: list[str] = []
    if not script.title.strip():
        problems.append("title is empty")
    if not script.segments:
        problems.append("script has no segments")
    for index, segment in enumerate(script.segments, start=1):
        if not segment.text.strip():
            problems.append(f"segment {index} has no text")
        if allowed_styles is not None and segment.style not in allowed_styles:
            problems.append(f"segment {index} uses unknown style {segment.style!r}")
    if script.segments and {s.speaker for s in script.segments} != set(SPEAKERS):
        problems.append("dialogue needs both a host and an expert segment")
    return problems


# --- load / save -------------------------------------------------------------------------


def save_script(script: PodcastScript, path: Path | str) -> None:
    """Write ``script.json`` (UTF-8, indented) atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(script.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def load_script(path: Path | str) -> PodcastScript:
    """Read and validate ``script.json``; problems are reported as :class:`ScriptError`."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ScriptError(f"Script file not found: {path}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ScriptError(f"Cannot read {path}: {exc}") from None
    try:
        return PodcastScript.model_validate(data)
    except ValidationError as exc:
        lines = [f"Invalid script ({path}):"]
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "<root>"
            lines.append(f"  {loc}: {err['msg']}")
        raise ScriptError("\n".join(lines)) from None
