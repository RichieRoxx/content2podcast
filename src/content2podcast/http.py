"""Shared HTTP client factory and a retrying request helper."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import httpx

from content2podcast import __version__
from content2podcast.config import HttpConfig
from content2podcast.logging_setup import kv

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
BODY_SNIPPET_CHARS = 200


def default_user_agent() -> str:
    return f"content2podcast/{__version__} (+https://github.com/RichieRoxx/content2podcast)"


class HttpError(Exception):
    """A failed request: HTTP error status (``status`` set) or transport failure (``None``)."""

    def __init__(self, message: str, *, url: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.url = url
        self.status = status
        self.body = body[:BODY_SNIPPET_CHARS]


def make_client(
    config: HttpConfig | None = None, *, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """Create an ``httpx.Client`` with the configured User-Agent and timeouts, following
    redirects."""
    config = config or HttpConfig()
    return httpx.Client(
        headers={"User-Agent": config.user_agent or default_user_agent()},
        timeout=httpx.Timeout(
            connect=config.connect_timeout,
            read=config.read_timeout,
            write=config.read_timeout,
            pool=config.connect_timeout,
        ),
        follow_redirects=True,
        transport=transport,
    )


def parse_retry_after(
    value: str | None, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP date), or None."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now()).total_seconds())


def loggable_url(url: str) -> str:
    """URL without query/fragment, so keys passed as parameters never reach the logs."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    max_retry_after: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    **kwargs,
) -> httpx.Response:
    """Send a request, retrying 429/5xx and transport errors with exponential backoff + jitter.

    ``Retry-After`` is honoured (capped at ``max_retry_after``). Other 4xx raise ``HttpError``
    immediately; so does the last failed attempt. Returns the successful response.
    """
    for attempt in range(1, max_attempts + 1):
        retry_after: float | None = None
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            reason = type(exc).__name__
            failure = HttpError(f"{method} {loggable_url(url)} failed: {reason}", url=url)
            failure.__cause__ = exc
        else:
            if response.status_code < 400:
                return response
            error = HttpError(
                f"{method} {loggable_url(url)} returned HTTP {response.status_code}",
                url=url,
                status=response.status_code,
                body=response.text,
            )
            if response.status_code not in RETRY_STATUSES:
                raise error
            reason = f"HTTP {response.status_code}"
            failure = error
            retry_after = parse_retry_after(response.headers.get("Retry-After"), now=now)

        if attempt == max_attempts:
            raise failure
        if retry_after is not None:
            delay = min(retry_after, max_retry_after)
        else:
            delay = min(max_delay, base_delay * 2 ** (attempt - 1)) * (0.5 + rand() / 2)
        log.warning(
            "Retrying request: %s",
            kv(
                attempt=attempt,
                max_attempts=max_attempts,
                method=method,
                url=loggable_url(url),
                reason=reason,
                delay=f"{delay:.1f}s",
            ),
        )
        sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
