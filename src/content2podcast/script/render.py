"""Human-readable Markdown rendering of a script."""

from __future__ import annotations

import re
from collections.abc import Mapping

from content2podcast.script.models import PodcastScript

DEFAULT_NAMES = {"host": "Host", "expert": "Expert"}


def _link_text(text: str) -> str:
    return re.sub(r"([\\\[\]])", r"\\\1", text)


def _link_url(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def render_markdown(script: PodcastScript, speaker_names: Mapping[str, str] | None = None) -> str:
    """Markdown with title, summary, sources and the dialogue (speaker name and style tag per
    segment). ``speaker_names`` maps ``host`` / ``expert`` to display names."""
    names = {**DEFAULT_NAMES, **(speaker_names or {})}
    lines = [f"# {script.title}", "", script.summary, ""]
    if script.sources:
        lines += ["## Sources", ""]
        lines += [f"- [{_link_text(s.title)}]({_link_url(s.url)})" for s in script.sources]
        lines.append("")
    lines += ["## Dialogue", ""]
    for segment in script.segments:
        lines += [f"**{names[segment.speaker]}** *({segment.style})*: {segment.text}", ""]
    return "\n".join(lines).rstrip() + "\n"
