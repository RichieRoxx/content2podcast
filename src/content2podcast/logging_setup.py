"""Logging setup: journald-friendly under systemd, plain timestamped lines otherwise."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from typing import Any, TextIO

PACKAGE_LOGGER = "content2podcast"
NOISY_LOGGERS = ("httpx", "httpcore", "openai")

# syslog priorities, see sd-daemon(3)
_SYSLOG_PRIORITY = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}


def kv(**fields: Any) -> str:
    """Format fields as ``key=value`` pairs; values with whitespace or quotes are JSON-quoted."""
    parts = []
    for key, value in fields.items():
        text = str(value)
        if not text or any(c.isspace() or c in "\"'=" for c in text):
            text = json.dumps(text, ensure_ascii=False)
        parts.append(f"{key}={text}")
    return " ".join(parts)


class _PlainFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")


class JournalFormatter(logging.Formatter):
    """``<N>logger: message`` with the syslog priority; journald adds the timestamp."""

    def __init__(self) -> None:
        super().__init__("%(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"<{_SYSLOG_PRIORITY.get(record.levelno, 6)}>"
        # prefix every line so multi-line messages and tracebacks keep their priority
        return "\n".join(prefix + line for line in super().format(record).splitlines())


def level_for(verbosity: int, quiet: bool) -> int:
    if quiet:
        return logging.WARNING
    return logging.DEBUG if verbosity >= 1 else logging.INFO


def setup_logging(
    verbosity: int = 0,
    quiet: bool = False,
    *,
    env: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> None:
    """Configure the root logger. Third-party loggers stay at WARNING unless ``verbosity >= 2``."""
    env = os.environ if env is None else env
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JournalFormatter() if env.get("JOURNAL_STREAM") else _PlainFormatter())
    handler._c2p = True  # type: ignore[attr-defined]  # marks our handler for idempotent re-setup

    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, "_c2p", False)]:
        root.removeHandler(old)
    root.addHandler(handler)

    level = level_for(verbosity, quiet)
    if verbosity >= 2 and not quiet:
        root.setLevel(logging.DEBUG)
    else:
        root.setLevel(logging.WARNING)
        logging.getLogger(PACKAGE_LOGGER).setLevel(level)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET if verbosity >= 2 else logging.WARNING)
