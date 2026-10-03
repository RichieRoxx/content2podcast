"""Script generation: prompt -> LLM -> validated ``PodcastScript``. Provider-agnostic."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from content2podcast.config import AppConfig
from content2podcast.logging_setup import kv
from content2podcast.providers.llm.base import LLMProvider
from content2podcast.script.models import (
    PodcastScript,
    ScriptError,
    SourceRef,
    build_llm_schema,
    clean_segments,
    validate_script,
)
from content2podcast.script.prompt import ArticleInput, build_prompts

log = logging.getLogger(__name__)

LENGTH_TOLERANCE = 0.30  # warn when the script deviates more than this from the word budget


@dataclass(frozen=True)
class ScriptArticle:
    """An article as the generator needs it (from the database or fetched ad hoc)."""

    url: str
    title: str
    text: str
    source: str  # source name, e.g. "heise"
    published: str | None = None

    def as_input(self) -> ArticleInput:
        return ArticleInput(
            source=self.source, title=self.title, text=self.text, published=self.published
        )

    def as_ref(self) -> SourceRef:
        return SourceRef(title=self.title, url=self.url)


def generate_script(
    llm: LLMProvider,
    articles: Sequence[ScriptArticle],
    config: AppConfig,
    styles: Sequence[str],
    *,
    today: date | None = None,
    mode: str | None = None,
) -> PodcastScript:
    """Render the prompt, call the LLM and return the validated script.

    ``sources`` are taken from ``articles`` (never from the model). Raises ``ScriptError`` if the
    result is unusable; a length more than 30 % off the word budget only logs a warning.
    """
    if not articles:
        raise ValueError("at least one article is required")
    prompts = build_prompts(
        config, [a.as_input() for a in articles], styles, today=today or date.today(), mode=mode
    )
    output = llm.generate_structured(prompts.system, prompts.user, build_llm_schema(styles))

    script = PodcastScript.from_llm(output, [a.as_ref() for a in articles])
    script.segments = clean_segments(script.segments, styles)
    if problems := validate_script(script, styles):
        raise ScriptError("The LLM produced an unusable script: " + "; ".join(problems))

    deviation = script.word_count / prompts.target_words - 1
    if abs(deviation) > LENGTH_TOLERANCE:
        log.warning(
            "Script length deviates from the budget: %s",
            kv(
                title=script.title,
                words=script.word_count,
                target=prompts.target_words,
                deviation=f"{deviation:+.0%}",
            ),
        )
    return script
