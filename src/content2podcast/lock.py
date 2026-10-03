"""Run lock: prevents the timer/daemon and a manual run from executing concurrently."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

log = logging.getLogger(__name__)

LOCK_FILENAME = "run.lock"
EXIT_LOCKED = 3


class RunLocked(Exception):
    """Another run holds the lock."""

    exit_code = EXIT_LOCKED


@contextmanager
def run_lock(data_dir: Path | str) -> Iterator[None]:
    """Hold the run lock without blocking; raise :class:`RunLocked` if it is already held."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(data_dir / LOCK_FILENAME, timeout=0)
    try:
        lock.acquire()
    except Timeout:
        log.warning("Another run is already in progress (lock: %s)", lock.lock_file)
        raise RunLocked(f"another run is already in progress ({lock.lock_file})") from None
    try:
        yield
    finally:
        lock.release()
