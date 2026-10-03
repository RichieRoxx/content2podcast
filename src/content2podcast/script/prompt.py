"""Prompt building: template lookup, word budget, article blocks and truncation."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path
from string import Template

from content2podcast.config import AppConfig, EpisodeConfig

SYSTEM_MARKER = "system"
USER_MARKER = "user"
_SECTION = re.compile(r"^<!--\s*(system|user)\s*-->[ \t]*$", re.MULTILINE)

#: Placeholders every template may use.
PLACEHOLDERS = (
    "podcast_title",
    "date_long",
    "host_name",
    "host_description",
    "expert_name",
    "expert_description",
    "target_words",
    "target_minutes",
    "styles",
    "articles",
)

_DE_DAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
_DE_MONTHS = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


class PromptError(Exception):
    """A prompt template is missing or malformed."""


@dataclass(frozen=True)
class ArticleInput:
    source: str  # name of the source, e.g. "heise"
    title: str
    text: str
    published: str | None = None  # ISO date or timestamp


@dataclass(frozen=True)
class Prompts:
    system: str
    user: str
    target_words: int
    target_minutes: int


# --- word budget -------------------------------------------------------------------------


def word_budget(article_words: int, episode: EpisodeConfig) -> int:
    """Target number of spoken words: ``article_words * length_factor``, clamped to
    ``min_minutes`` .. ``max_minutes`` at ``words_per_minute``."""
    wpm = episode.words_per_minute
    low, high = episode.min_minutes * wpm, episode.max_minutes * wpm
    return round(min(max(article_words * episode.length_factor, low), high))


def minutes_for(words: int, words_per_minute: int) -> int:
    """Whole minutes (at least 1) a text of ``words`` words takes to speak."""
    return max(1, round(words / words_per_minute))


# --- article blocks ----------------------------------------------------------------------


def truncate_text(text: str, max_chars: int) -> str:
    """Cut ``text`` to at most ``max_chars``, preferring a paragraph, then a sentence, then a
    word boundary (only if that keeps at least half of the allowed length)."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    floor = max_chars // 2
    paragraph = head.rfind("\n")
    if paragraph >= floor:
        return head[:paragraph].rstrip()
    sentence_ends = [m.end() for m in re.finditer(r"[.!?…][\"'“”»«)\]]*(?=\s)", head)]
    if sentence_ends and sentence_ends[-1] >= floor:
        return head[: sentence_ends[-1]].rstrip()
    word = max(head.rfind(" "), head.rfind("\n"))
    return (head[:word] if word >= floor else head).rstrip()


def _neutralize_tags(text: str) -> str:
    """Keep article text from opening or closing ``<article>`` blocks."""
    return re.sub(r"<(\s*/?\s*article)", lambda m: "&lt;" + m.group(1), text, flags=re.IGNORECASE)


def format_article(article: ArticleInput, max_chars: int) -> str:
    """``<article source=... title=... published=...>text</article>`` with truncated text."""
    attrs = f'source="{html.escape(article.source)}" title="{html.escape(article.title)}"'
    if article.published:
        attrs += f' published="{html.escape(article.published[:10])}"'
    text = _neutralize_tags(truncate_text(article.text, max_chars))
    return f"<article {attrs}>\n{text}\n</article>"


def format_date_long(day: date, language: str) -> str:
    """``Montag, 5. Oktober 2026`` (German) or ``Monday, October 5, 2026`` (English);
    other languages get the ISO date."""
    if language == "de":
        return f"{_DE_DAYS[day.weekday()]}, {day.day}. {_DE_MONTHS[day.month - 1]} {day.year}"
    if language == "en":
        return f"{day:%A}, {day:%B} {day.day}, {day.year}"
    return day.isoformat()


# --- templates ---------------------------------------------------------------------------


def _split_sections(raw: str, origin: str) -> tuple[str, str]:
    parts = _SECTION.split(raw)
    # parts: [preamble, name1, text1, name2, text2, ...]
    sections = {name: text.strip() for name, text in zip(parts[1::2], parts[2::2], strict=False)}
    missing = [n for n in (SYSTEM_MARKER, USER_MARKER) if not sections.get(n)]
    if missing:
        raise PromptError(
            f"Prompt template {origin} needs '<!-- system -->' and '<!-- user -->' sections "
            f"(missing: {', '.join(missing)})"
        )
    return sections[SYSTEM_MARKER], sections[USER_MARKER]


def language_code(language: str) -> str:
    """``de-DE`` -> ``de``."""
    return language.split("-")[0].split("_")[0].lower()


def load_template(mode: str, language: str, prompts_dir: Path | None = None) -> tuple[str, str]:
    """``(system, user)`` template text for ``<mode>_<language>.md``: from ``prompts_dir`` if it
    has the file, else from the packaged prompts."""
    filename = f"{mode}_{language_code(language)}.md"
    if prompts_dir is not None and (override := Path(prompts_dir) / filename).is_file():
        return _split_sections(override.read_text(encoding="utf-8"), str(override))
    packaged = resources.files("content2podcast").joinpath("prompts", filename)
    if packaged.is_file():
        return _split_sections(packaged.read_text(encoding="utf-8"), f"{filename} (packaged)")
    searched = f"{prompts_dir}, " if prompts_dir is not None else ""
    raise PromptError(f"No prompt template {filename!r} found (looked in: {searched}packaged)")


def _substitute(template: str, values: dict[str, str], origin: str) -> str:
    try:
        return Template(template).substitute(values)
    except KeyError as exc:
        raise PromptError(
            f"Prompt template {origin} uses unknown placeholder ${exc.args[0]} "
            f"(available: {', '.join('$' + p for p in PLACEHOLDERS)})"
        ) from None
    except ValueError as exc:
        raise PromptError(
            f"Prompt template {origin} has an invalid placeholder: {exc} "
            f"(write a literal dollar sign as $$)"
        ) from None


def build_prompts(
    config: AppConfig,
    articles: Sequence[ArticleInput],
    styles: Sequence[str],
    *,
    today: date,
    mode: str | None = None,
) -> Prompts:
    """Render the system and user prompt for ``articles``.

    The word budget follows the (truncated) article text, or ``episode.target_minutes`` for a
    daily digest; ``styles`` is the list allowed by the TTS provider. ``mode`` defaults to
    ``episode.mode``.
    """
    episode = config.episode
    mode = mode or episode.mode
    language = language_code(config.podcast.language)
    system_tpl, user_tpl = load_template(mode, config.podcast.language, episode.prompts_dir)

    blocks = [format_article(a, episode.max_chars_per_article) for a in articles]
    if mode == "daily_digest":  # a fixed length, independent of how much there is to read
        target_words = round(episode.target_minutes * episode.words_per_minute)
    else:
        words = sum(
            len(truncate_text(a.text, episode.max_chars_per_article).split()) for a in articles
        )
        target_words = word_budget(words, episode)
    roles = config.roles
    values = {
        "podcast_title": config.podcast.title,
        "date_long": format_date_long(today, language),
        "host_name": roles.host.name,
        "host_description": roles.host.description,
        "expert_name": roles.expert.name,
        "expert_description": roles.expert.description,
        "target_words": str(target_words),
        "target_minutes": str(minutes_for(target_words, episode.words_per_minute)),
        "styles": ", ".join(styles),
        "articles": "\n\n".join(blocks),
    }
    origin = f"{mode}_{language}.md"
    return Prompts(
        system=_substitute(system_tpl, values, origin),
        user=_substitute(user_tpl, values, origin),
        target_words=target_words,
        target_minutes=int(values["target_minutes"]),
    )
