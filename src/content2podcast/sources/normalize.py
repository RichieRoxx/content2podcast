"""URL normalization: a stable dedup key (``articles.url_norm``).

Only the key is normalized; the original URL is kept for fetching.
"""

from __future__ import annotations

import contextlib
import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "igshid",
        "_hsenc",
        "_hsmi",
        "mkt_tok",
    }
)
TRACKING_PREFIXES = ("utm_",)

_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")


def _is_tracking(name: str) -> bool:
    name = name.lower()
    return name in TRACKING_PARAMS or name.startswith(TRACKING_PREFIXES)


def _normalize_percent(match: re.Match[str]) -> str:
    char = chr(int(match.group(1), 16))
    return char if char in _UNRESERVED else f"%{match.group(1).upper()}"


def _normalize_path(path: str) -> str:
    # Encode non-ASCII/space/control characters (keep reserved ones and existing escapes),
    # then decode escaped unreserved characters and upper-case the remaining hex digits.
    path = quote(path, safe="%/:@!$&'()*+,;=~")
    path = _PERCENT.sub(_normalize_percent, path)
    return path.rstrip("/") or "/"


def _normalize_host(host: str) -> str:
    host = host.lower().rstrip(".")
    host = host.removeprefix("www.")
    with contextlib.suppress(UnicodeError):  # keep hosts that are not valid IDNA as they are
        host = host.encode("idna").decode("ascii")
    return f"[{host}]" if ":" in host else host


def normalize_url(url: str) -> str:
    """Return the dedup key for ``url``.

    ``http`` becomes ``https``; host is lower-cased without ``www.``; default port and fragment
    are dropped; tracking parameters are removed and the rest sorted; percent-encoding is
    canonicalized; a trailing slash is removed (except for the root path).
    """
    parts = urlsplit(url.strip())
    original_scheme = parts.scheme.lower()
    scheme = "https" if original_scheme == "http" else original_scheme
    host = _normalize_host(parts.hostname or "")
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and port not in {_DEFAULT_PORTS.get(original_scheme), _DEFAULT_PORTS.get(scheme)}:
        host = f"{host}:{port}"
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)
    )
    return urlunsplit((scheme, host, _normalize_path(parts.path), urlencode(query), ""))
