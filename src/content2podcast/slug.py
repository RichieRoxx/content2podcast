"""Slugs for file and directory names."""

from __future__ import annotations

import re
import unicodedata

SLUG_MAX = 60
_GERMAN = (
    ("ß", "ss"),
    ("ä", "ae"),
    ("ö", "oe"),
    ("ü", "ue"),
    ("Ä", "Ae"),
    ("Ö", "Oe"),
    ("Ü", "Ue"),
)


def slugify(text: str, fallback: str = "script", max_length: int = SLUG_MAX) -> str:
    """Lowercase ASCII slug, German umlauts and ``ß`` transliterated
    (``Über Äpfel & Birnen`` -> ``ueber-aepfel-birnen``), cut at ``max_length`` without a
    trailing dash. ``fallback`` is returned if nothing usable is left."""
    for char, repl in _GERMAN:
        text = text.replace(char, repl)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_length].strip("-")
    return slug or fallback
