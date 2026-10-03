"""SSML for Azure speech (MAI voices) with emotion styles.

Only constructs documented for MAI voices are emitted: ``speak`` > ``voice`` and, for a
supported style, ``mstts:express-as``. Never ``prosody``, ``break``, ``emphasis`` or
``styledegree``.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from xml.sax.saxutils import escape, quoteattr

from content2podcast.logging_setup import kv
from content2podcast.xmltext import strip_invalid_xml_chars

log = logging.getLogger(__name__)

SSML_NS = "http://www.w3.org/2001/10/synthesis"
MSTTS_NS = "http://www.w3.org/2001/mstts"
NEUTRAL = "neutral"

_TEXT_ENTITIES = {'"': "&quot;", "'": "&apos;"}


def build_ssml(
    text: str, voice: str, style: str | None, lang: str, allowed_styles: Collection[str]
) -> str:
    """SSML for one utterance.

    ``style`` is applied with ``mstts:express-as`` only if it is in ``allowed_styles``; a missing
    or ``neutral`` style gives plain speech, an unknown one logs a warning and also falls back to
    plain speech. Raises ``ValueError`` if no text is left after cleaning.
    """
    cleaned = strip_invalid_xml_chars(text).strip()
    if not cleaned:
        raise ValueError("SSML text is empty after removing invalid characters")
    body = escape(cleaned, _TEXT_ENTITIES)

    if style and style != NEUTRAL:
        if style in allowed_styles:
            body = f"<mstts:express-as style={quoteattr(style)}>{body}</mstts:express-as>"
        else:
            log.warning(
                "Unknown speaking style, using plain speech: %s", kv(style=style, voice=voice)
            )

    return (
        f'<speak version="1.0" xmlns="{SSML_NS}" xmlns:mstts="{MSTTS_NS}" '
        f"xml:lang={quoteattr(strip_invalid_xml_chars(lang))}>"
        f"<voice name={quoteattr(strip_invalid_xml_chars(voice))}>{body}</voice></speak>"
    )
