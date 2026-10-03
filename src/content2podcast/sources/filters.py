"""``include`` / ``exclude`` regex filtering of article URLs (shared by all source types)."""

from __future__ import annotations

import re
from collections.abc import Iterable


def url_allowed(url: str, include: Iterable[str] = (), exclude: Iterable[str] = ()) -> bool:
    """True if ``url`` matches at least one ``include`` pattern (when any are given) and none of
    the ``exclude`` patterns. Patterns are searched anywhere in the URL."""
    include = list(include)
    if include and not any(re.search(p, url) for p in include):
        return False
    return not any(re.search(p, url) for p in exclude)
