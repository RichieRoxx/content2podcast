"""Built-in scheduler for ``podcast daemon`` and the health check for ``podcast health``.

The daemon runs the pipeline once per day at ``schedule.time`` (wall-clock time in the local
time zone, ``TZ`` in containers), catches up on start if a slot was missed (like systemd's
``Persistent=true``) and keeps a status file in the data directory that ``podcast health`` reads.
All time handling goes through an injectable clock so it can be tested exactly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from content2podcast.layout import write_atomic
from content2podcast.logging_setup import kv

log = logging.getLogger(__name__)

STATUS_FILENAME = "status.json"
HEARTBEAT_INTERVAL = timedelta(seconds=60)
HEARTBEAT_MAX_AGE = timedelta(minutes=5)  # health: the daemon must have reported this recently
MAX_RUN_AGE = timedelta(hours=26)  # health: a daily run plus some slack

JobStatus = Literal["ok", "failed", "locked"]


@dataclass(frozen=True)
class JobResult:
    status: JobStatus
    detail: str | None = None


def _now_utc() -> datetime:
    return datetime.now(UTC)


# --- time zone and slots -----------------------------------------------------------------


def parse_schedule_time(value: str) -> time:
    """``"05:30"`` -> ``time(5, 30)``."""
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value)
    if not match:
        raise ValueError(f"schedule time must be HH:MM (24h), got {value!r}")
    return time(int(match.group(1)), int(match.group(2)))


def local_tz(env: dict[str, str] | None = None) -> tzinfo:
    """The ``TZ`` environment variable (an IANA name like ``Europe/Berlin``) if set and valid,
    else the system's local time zone."""
    name = (env if env is not None else os.environ).get("TZ")
    if name:
        try:
            return ZoneInfo(name.lstrip(":"))
        except (ZoneInfoNotFoundError, ValueError, OSError):
            log.warning("Unknown time zone in TZ, using the system time zone: %s", kv(TZ=name))
    return datetime.now().astimezone().tzinfo or UTC


def _slot(day, at: time, tz: tzinfo) -> datetime:
    """The slot of ``day`` as an aware datetime. A wall-clock time that does not exist (DST
    forward jump) moves to the first valid time after it; an ambiguous one (DST backward jump)
    uses its first occurrence, so there is exactly one slot per day."""
    naive = datetime.combine(day, at, tzinfo=tz)
    return naive.astimezone(UTC).astimezone(tz)


def next_slot(now: datetime, at: time, tz: tzinfo) -> datetime:
    """The first slot strictly after ``now``."""
    local = now.astimezone(tz)
    candidate = _slot(local.date(), at, tz)
    if candidate <= now:
        candidate = _slot(local.date() + timedelta(days=1), at, tz)
    return candidate


def last_slot(now: datetime, at: time, tz: tzinfo) -> datetime:
    """The most recent slot at or before ``now``."""
    local = now.astimezone(tz)
    candidate = _slot(local.date(), at, tz)
    if candidate > now:
        candidate = _slot(local.date() - timedelta(days=1), at, tz)
    return candidate


def needs_catch_up(last_success: datetime | None, now: datetime, at: time, tz: tzinfo) -> bool:
    """True if the last successful run is older than the most recent slot (or there was none)."""
    return last_success is None or last_success < last_slot(now, at, tz)


# --- status file -------------------------------------------------------------------------


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass
class DaemonStatus:
    state: str = "starting"  # starting, idle, running, stopped
    pid: int | None = None
    daemon_started: datetime | None = None
    heartbeat: datetime | None = None
    last_run_started: datetime | None = None
    last_run_finished: datetime | None = None
    last_status: str | None = None  # ok, failed, locked
    last_error: str | None = None
    last_success_at: datetime | None = None
    next_run: datetime | None = None

    def to_json(self) -> str:
        data: dict[str, Any] = {
            key: _iso(value) if isinstance(value, datetime) else value
            for key, value in self.__dict__.items()
        }
        return json.dumps(data, indent=2) + "\n"

    @classmethod
    def from_file(cls, path: Path) -> DaemonStatus | None:
        """The status, ``None`` if the file does not exist; unreadable content raises
        ``ValueError``."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read status file {path}: {exc}") from None
        if not isinstance(raw, dict):
            raise ValueError(f"Cannot read status file {path}: not a JSON object")
        status = cls()
        try:
            for key, value in raw.items():
                if key not in status.__dict__:
                    continue
                if key in {
                    "daemon_started",
                    "heartbeat",
                    "last_run_started",
                    "last_run_finished",
                    "last_success_at",
                    "next_run",
                }:
                    value = _parse(value)
                setattr(status, key, value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Cannot read status file {path}: {exc}") from None
        return status


def check_health(
    status: DaemonStatus | None,
    now: datetime,
    *,
    max_heartbeat_age: timedelta = HEARTBEAT_MAX_AGE,
    max_run_age: timedelta = MAX_RUN_AGE,
) -> tuple[bool, str]:
    """Is the daemon alive and doing its job? Returns ``(healthy, message)``."""
    if status is None:
        return False, "no status file: the daemon has not started"
    if status.state == "stopped":
        return False, "the daemon has stopped"
    if status.heartbeat is None or now - status.heartbeat > max_heartbeat_age:
        age = (
            "never"
            if status.heartbeat is None
            else f"{(now - status.heartbeat).total_seconds():.0f}s ago"
        )
        return False, f"no heartbeat (last: {age})"
    if status.last_status == "failed":
        return False, f"the last run failed: {status.last_error or 'unknown error'}"
    reference = status.last_success_at or status.daemon_started
    if reference is None:
        return False, "the daemon has not completed a run yet"
    if now - reference > max_run_age:
        hours = (now - reference).total_seconds() / 3600
        what = "last successful run" if status.last_success_at else "daemon start without a run"
        return (
            False,
            f"{what} is {hours:.0f} h old (limit {max_run_age.total_seconds() / 3600:.0f} h)",
        )
    return True, "ok"


# --- the scheduler -----------------------------------------------------------------------


class Scheduler:
    """Runs ``job`` once per day. ``clock`` and ``sleep`` are injectable: ``sleep(seconds)``
    must return early (and return True) when a stop was requested."""

    def __init__(
        self,
        status_path: Path,
        at: time,
        job: Callable[[], JobResult],
        *,
        tz: tzinfo | None = None,
        clock: Callable[[], datetime] = _now_utc,
        sleep: Callable[[float], bool] | None = None,
        heartbeat_interval: timedelta = HEARTBEAT_INTERVAL,
        background_heartbeat: bool = True,
    ) -> None:
        self.status_path = status_path
        self.at = at
        self.job = job
        self.tz = tz or local_tz()
        self.clock = clock
        self.heartbeat_interval = heartbeat_interval
        self._stop = threading.Event()
        self._sleep = sleep or (lambda seconds: self._stop.wait(seconds))
        self._lock = threading.Lock()
        self._background = background_heartbeat
        self._thread_beating = False  # True while the heartbeat thread keeps the status fresh
        self.status = DaemonStatus(pid=os.getpid())
        previous = None
        try:
            previous = DaemonStatus.from_file(status_path)
        except ValueError:
            log.warning("Ignoring an unreadable status file: %s", kv(file=status_path))
        if previous is not None:  # keep the history across restarts
            for key in ("last_run_started", "last_run_finished", "last_status", "last_error"):
                setattr(self.status, key, getattr(previous, key))
            self.status.last_success_at = previous.last_success_at

    # -- control --

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        """Ask the daemon to finish: a running job is not interrupted but should check
        :attr:`stopping` between episodes."""
        self._stop.set()

    # -- status --

    def _write(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self.status, key, value)
            self.status.heartbeat = self.clock()
            write_atomic(self.status_path, self.status.to_json().encode("utf-8"))

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval.total_seconds()):
            self._write()

    # -- the loop --

    def run_job(self) -> JobResult:
        """One run of the job with status bookkeeping; the daemon survives any failure."""
        started = self.clock()
        self._write(state="running", last_run_started=started)
        try:
            result = self.job()
        except Exception as exc:
            log.exception("Run crashed")
            result = JobResult("failed", f"{type(exc).__name__}: {exc}")
        finished = self.clock()
        changes: dict[str, Any] = {
            "state": "idle",
            "last_run_finished": finished,
            "last_status": result.status,
            "last_error": result.detail if result.status == "failed" else None,
        }
        if result.status == "ok":
            changes["last_success_at"] = finished
        self._write(**changes)
        log.info("Run finished: %s", kv(status=result.status, detail=result.detail))
        return result

    def _wait_until(self, moment: datetime) -> None:
        """Sleep until ``moment`` in heartbeat-sized steps; returns early on stop."""
        while not self.stopping:
            remaining = (moment - self.clock()).total_seconds()
            if remaining <= 0:
                return
            if not self._thread_beating:
                self._write()
            if self._sleep(min(remaining, self.heartbeat_interval.total_seconds())):
                return

    def serve(self) -> None:
        """Run until :meth:`stop` is called."""
        now = self.clock()
        self._write(state="starting", daemon_started=now, pid=os.getpid())
        thread = None
        if self._background:
            self._thread_beating = True
            thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            thread.start()
        try:
            if needs_catch_up(self.status.last_success_at, now, self.at, self.tz):
                log.info("Catching up on a missed run")
                self.run_job()
            while not self.stopping:
                upcoming = next_slot(self.clock(), self.at, self.tz)
                self._write(state="idle", next_run=upcoming)
                log.info("Next run: %s", kv(at=upcoming.astimezone(self.tz).isoformat()))
                self._wait_until(upcoming)
                if self.stopping:
                    break
                self.run_job()
        finally:
            self._stop.set()
            self._thread_beating = False
            if thread is not None:
                thread.join(timeout=5)
            self._write(state="stopped", next_run=None)


def install_signal_handlers(scheduler: Scheduler) -> Callable[[], None]:
    """SIGTERM and SIGINT make the scheduler stop after the current episode. Returns a function
    that restores the previous handlers. Must be called from the main thread."""
    previous = {}

    def handler(signum: int, frame: Any) -> None:
        log.info("Stop requested: %s", kv(signal=signal.Signals(signum).name))
        scheduler.stop()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, handler)

    def restore() -> None:
        for signum, old in previous.items():
            signal.signal(signum, old)

    return restore
