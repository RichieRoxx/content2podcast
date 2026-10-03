import json
import os
import signal
import time as real_time
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from content2podcast.scheduler import (
    DaemonStatus,
    JobResult,
    Scheduler,
    check_health,
    install_signal_handlers,
    last_slot,
    local_tz,
    needs_catch_up,
    next_slot,
    parse_schedule_time,
)

BERLIN = ZoneInfo("Europe/Berlin")
AT = time(5, 30)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def berlin(*args) -> datetime:
    return datetime(*args, tzinfo=BERLIN)


# --- parsing and time zone ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"), [("05:30", time(5, 30)), ("00:00", time(0, 0)), ("23:59", time(23, 59))]
)
def test_parse_schedule_time(text, expected):
    assert parse_schedule_time(text) == expected


@pytest.mark.parametrize("text", ["5:30", "24:00", "12:60", "noon", "", "05:30:00"])
def test_parse_schedule_time_rejects_garbage(text):
    with pytest.raises(ValueError, match="HH:MM"):
        parse_schedule_time(text)


def test_local_tz_uses_the_tz_variable(caplog):
    assert local_tz({"TZ": "Europe/Berlin"}) == BERLIN
    assert local_tz({"TZ": ":Europe/Berlin"}) == BERLIN  # POSIX style with a leading colon
    assert local_tz({"TZ": "UTC"}) == ZoneInfo("UTC")
    system = local_tz({})
    assert system is not None
    with caplog.at_level("WARNING"):
        assert local_tz({"TZ": "Mars/Olympus"}) is not None
    assert any("Mars/Olympus" in r.message for r in caplog.records)


# --- slots -------------------------------------------------------------------------------


def test_next_slot_before_and_after_the_time_of_day():
    assert next_slot(berlin(2026, 10, 3, 4, 0), AT, BERLIN) == berlin(2026, 10, 3, 5, 30)
    assert next_slot(berlin(2026, 10, 3, 6, 0), AT, BERLIN) == berlin(2026, 10, 4, 5, 30)


def test_next_slot_is_strictly_after_now():
    assert next_slot(berlin(2026, 10, 3, 5, 30), AT, BERLIN) == berlin(2026, 10, 4, 5, 30)
    assert next_slot(berlin(2026, 10, 3, 5, 29, 59), AT, BERLIN) == berlin(2026, 10, 3, 5, 30)


def test_last_slot_is_at_or_before_now():
    assert last_slot(berlin(2026, 10, 3, 6, 0), AT, BERLIN) == berlin(2026, 10, 3, 5, 30)
    assert last_slot(berlin(2026, 10, 3, 5, 30), AT, BERLIN) == berlin(2026, 10, 3, 5, 30)
    assert last_slot(berlin(2026, 10, 3, 5, 0), AT, BERLIN) == berlin(2026, 10, 2, 5, 30)


def test_slots_work_across_time_zones_of_the_clock():
    # 03:30 UTC is 05:30 in Berlin during summer time
    assert next_slot(utc(2026, 10, 3, 3, 29), AT, BERLIN) == utc(2026, 10, 3, 3, 30)
    assert next_slot(utc(2026, 10, 3, 3, 30), AT, BERLIN) == utc(2026, 10, 4, 3, 30)


def test_the_slot_keeps_its_wall_clock_time_across_the_dst_change():
    # clocks go forward on 2026-03-29: the day between the two slots has only 23 hours
    before = next_slot(berlin(2026, 3, 27, 12, 0), AT, BERLIN)
    after = next_slot(before, AT, BERLIN)
    assert before == berlin(2026, 3, 28, 5, 30)
    assert after == berlin(2026, 3, 29, 5, 30)
    assert after.astimezone(UTC) - before.astimezone(UTC) == timedelta(hours=23)  # real time
    # clocks go back on 2026-10-25: 25 hours
    before = next_slot(berlin(2026, 10, 23, 12, 0), AT, BERLIN)
    after = next_slot(before, AT, BERLIN)
    assert (before, after) == (berlin(2026, 10, 24, 5, 30), berlin(2026, 10, 25, 5, 30))
    assert after.astimezone(UTC) - before.astimezone(UTC) == timedelta(hours=25)


def test_a_slot_in_the_skipped_hour_moves_to_the_first_valid_time():
    # 02:30 does not exist on 2026-03-29 (02:00 -> 03:00): the run happens at 03:30 CEST
    at = time(2, 30)
    slot = next_slot(berlin(2026, 3, 28, 12, 0), at, BERLIN)
    assert slot.astimezone(UTC) == utc(2026, 3, 29, 1, 30)
    assert slot.astimezone(BERLIN).hour == 3
    # and exactly once per day: the day after is a normal 02:30 again
    assert next_slot(slot, at, BERLIN) == berlin(2026, 3, 30, 2, 30)


def test_a_slot_in_the_repeated_hour_runs_once_at_its_first_occurrence():
    at = time(2, 30)  # 02:30 happens twice on 2026-10-25 (CEST, then CET)
    first = next_slot(berlin(2026, 10, 24, 12, 0), at, BERLIN)
    assert first.astimezone(UTC) == utc(2026, 10, 25, 0, 30)  # the CEST one
    second = next_slot(first, at, BERLIN)
    assert second == berlin(2026, 10, 26, 2, 30)  # not the repeated 02:30 CET (01:30 UTC)
    assert last_slot(utc(2026, 10, 25, 1, 45), at, BERLIN) == first  # still the same day's slot


# --- catch up ----------------------------------------------------------------------------


def test_catch_up_decision():
    now = berlin(2026, 10, 3, 9, 0)
    assert needs_catch_up(None, now, AT, BERLIN) is True
    assert needs_catch_up(berlin(2026, 10, 2, 5, 31), now, AT, BERLIN) is True  # yesterday's run
    assert needs_catch_up(berlin(2026, 10, 3, 5, 29), now, AT, BERLIN) is True  # just before
    assert needs_catch_up(berlin(2026, 10, 3, 5, 30), now, AT, BERLIN) is False  # exactly the slot
    assert needs_catch_up(berlin(2026, 10, 3, 8, 0), now, AT, BERLIN) is False
    # before today's slot only yesterday's counts
    early = berlin(2026, 10, 3, 4, 0)
    assert needs_catch_up(berlin(2026, 10, 2, 6, 0), early, AT, BERLIN) is False


# --- the scheduler loop with an injected clock -------------------------------------------


class FakeTime:
    """A clock that only moves when the scheduler sleeps."""

    def __init__(self, start: datetime):
        self.now = start
        self.scheduler: Scheduler | None = None
        self.on_sleep = None
        self.sleeps: list[float] = []

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> bool:
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep()
        if self.scheduler and self.scheduler.stopping:
            return True
        self.now += timedelta(seconds=seconds)
        return False


def make(tmp_path, start, job, *, previous=None, at=AT, tz=UTC, **kwargs):
    fake = FakeTime(start)
    status_path = tmp_path / "status.json"
    if previous is not None:
        status_path.write_text(previous.to_json())
    scheduler = Scheduler(
        status_path, at, job, tz=tz, clock=fake.clock, sleep=fake.sleep,
        background_heartbeat=False, **kwargs,
    )  # fmt: skip
    fake.scheduler = scheduler
    return scheduler, fake, status_path


def read(status_path) -> dict:
    return json.loads(status_path.read_text())


def test_runs_at_the_slot_and_again_the_next_day(tmp_path):
    runs: list[datetime] = []
    holder = {}

    def job():
        runs.append(holder["fake"].now)
        if len(runs) == 2:
            holder["scheduler"].stop()
        return JobResult("ok")

    previous = DaemonStatus(last_success_at=utc(2026, 10, 2, 5, 31))  # no catch-up needed
    scheduler, fake, _ = make(tmp_path, utc(2026, 10, 3, 4, 0), job, previous=previous)
    holder.update(fake=fake, scheduler=scheduler)
    scheduler.serve()
    assert runs == [utc(2026, 10, 3, 5, 30), utc(2026, 10, 4, 5, 30)]


def test_catches_up_immediately_when_the_last_run_is_older_than_the_last_slot(tmp_path):
    runs = []
    holder = {}

    def job():
        runs.append(holder["fake"].now)
        holder["scheduler"].stop()
        return JobResult("ok")

    previous = DaemonStatus(last_success_at=utc(2026, 10, 1, 5, 31))
    scheduler, fake, _ = make(tmp_path, utc(2026, 10, 3, 9, 0), job, previous=previous)
    holder.update(fake=fake, scheduler=scheduler)
    scheduler.serve()
    assert runs == [utc(2026, 10, 3, 9, 0)]  # right at the start, not at the next slot


def test_a_fresh_installation_runs_right_away(tmp_path):
    runs = []
    holder = {}

    def job():
        runs.append(holder["fake"].now)
        holder["scheduler"].stop()
        return JobResult("ok")

    scheduler, fake, _ = make(tmp_path, utc(2026, 10, 3, 14, 0), job)
    holder.update(fake=fake, scheduler=scheduler)
    scheduler.serve()
    assert runs == [utc(2026, 10, 3, 14, 0)]


def test_no_catch_up_when_the_slot_was_served(tmp_path):
    runs = []
    holder = {}

    def job():
        runs.append(holder["fake"].now)
        holder["scheduler"].stop()
        return JobResult("ok")

    previous = DaemonStatus(last_success_at=utc(2026, 10, 3, 5, 45))
    scheduler, fake, _ = make(tmp_path, utc(2026, 10, 3, 9, 0), job, previous=previous)
    holder.update(fake=fake, scheduler=scheduler)
    scheduler.serve()
    assert runs == [utc(2026, 10, 4, 5, 30)]


def test_a_failing_run_does_not_kill_the_daemon(tmp_path):
    outcomes = iter([RuntimeError("boom"), "ok"])
    holder = {}

    def job():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        holder["scheduler"].stop()
        return JobResult("ok")

    previous = DaemonStatus(last_success_at=utc(2026, 10, 2, 6, 0))
    scheduler, fake, path = make(tmp_path, utc(2026, 10, 3, 4, 0), job, previous=previous)
    holder.update(scheduler=scheduler)
    # observe the status after the first (failed) run
    seen = {}
    original = scheduler.run_job

    def spy():
        result = original()
        seen.setdefault("first", read(path))
        return result

    scheduler.run_job = spy
    scheduler.serve()
    first = seen["first"]
    assert first["last_status"] == "failed" and "RuntimeError: boom" in first["last_error"]
    assert first["last_success_at"] == "2026-10-02T06:00:00Z"  # a failure is not a success
    final = read(path)
    assert final["last_status"] == "ok" and final["last_error"] is None
    assert final["last_success_at"] == "2026-10-04T05:30:00Z"


def test_a_failed_result_and_a_locked_result_are_recorded_but_not_successes(tmp_path):
    results = iter([JobResult("failed", "3 episodes failed"), JobResult("locked", "busy")])
    holder = {}
    seen = []

    def job():
        result = next(results)
        if result.status == "locked":
            holder["scheduler"].stop()
        return result

    previous = DaemonStatus(last_success_at=utc(2026, 10, 2, 6, 0))
    scheduler, fake, path = make(tmp_path, utc(2026, 10, 3, 4, 0), job, previous=previous)
    holder["scheduler"] = scheduler
    fake.on_sleep = lambda: seen.append(read(path)["last_status"])
    scheduler.serve()
    assert "failed" in seen
    final = read(path)
    assert final["last_status"] == "locked" and final["last_error"] is None
    assert final["last_success_at"] == "2026-10-02T06:00:00Z"


def test_stop_while_waiting_ends_the_daemon_without_a_run(tmp_path):
    runs = []
    previous = DaemonStatus(last_success_at=utc(2026, 10, 3, 6, 0))
    scheduler, fake, path = make(
        tmp_path,
        utc(2026, 10, 3, 7, 0),
        lambda: runs.append(1) or JobResult("ok"),
        previous=previous,
    )
    sleeps = {"n": 0}

    def on_sleep():
        sleeps["n"] += 1
        if sleeps["n"] == 3:
            scheduler.stop()

    fake.on_sleep = on_sleep
    scheduler.serve()
    assert runs == [] and sleeps["n"] == 3
    final = read(path)
    assert final["state"] == "stopped" and final["next_run"] is None


def test_stop_during_a_run_lets_it_finish_and_then_exits(tmp_path):
    holder = {}
    finished = []

    def job():
        holder["scheduler"].stop()  # e.g. SIGTERM arrives mid-run; the run still completes
        finished.append(True)
        return JobResult("ok")

    scheduler, fake, path = make(tmp_path, utc(2026, 10, 3, 14, 0), job)
    holder["scheduler"] = scheduler
    scheduler.serve()
    assert finished == [True]
    assert fake.sleeps == []  # it did not wait for the next slot
    final = read(path)
    assert final["last_status"] == "ok" and final["state"] == "stopped"


def test_the_heartbeat_is_refreshed_while_waiting(tmp_path):
    holder = {}
    beats = []
    previous = DaemonStatus(last_success_at=utc(2026, 10, 3, 5, 31))
    scheduler, fake, path = make(
        tmp_path, utc(2026, 10, 3, 6, 0), lambda: JobResult("ok"), previous=previous,
        heartbeat_interval=timedelta(seconds=60),
    )  # fmt: skip
    holder["scheduler"] = scheduler

    def on_sleep():
        beats.append(read(path)["heartbeat"])
        if len(beats) == 4:
            scheduler.stop()

    fake.on_sleep = on_sleep
    scheduler.serve()
    assert fake.sleeps[:3] == [60.0, 60.0, 60.0]  # sleeps in heartbeat-sized steps
    assert beats == [
        "2026-10-03T06:00:00Z",
        "2026-10-03T06:01:00Z",
        "2026-10-03T06:02:00Z",
        "2026-10-03T06:03:00Z",
    ]


def test_the_last_sleep_is_only_as_long_as_needed(tmp_path):
    holder = {}
    previous = DaemonStatus(last_success_at=utc(2026, 10, 3, 5, 31))

    def job():
        holder["scheduler"].stop()
        return JobResult("ok")

    scheduler, fake, _ = make(tmp_path, utc(2026, 10, 3, 5, 29, 30), job, previous=previous)
    holder["scheduler"] = scheduler
    scheduler.serve()
    assert fake.sleeps == [30.0]  # 30 s to the slot, not a full heartbeat interval


def test_status_file_content_and_history_across_restarts(tmp_path):
    holder = {}
    scheduler, fake, path = make(
        tmp_path, utc(2026, 10, 3, 14, 0), lambda: holder["scheduler"].stop() or JobResult("ok")
    )
    holder["scheduler"] = scheduler
    scheduler.serve()
    data = read(path)
    assert data["pid"] == os.getpid() and data["daemon_started"] == "2026-10-03T14:00:00Z"
    assert data["last_run_started"] == data["last_run_finished"] == "2026-10-03T14:00:00Z"
    assert data["last_success_at"] == "2026-10-03T14:00:00Z" and data["state"] == "stopped"

    again = Scheduler(path, AT, lambda: JobResult("ok"), tz=UTC, background_heartbeat=False)
    assert again.status.last_success_at == utc(2026, 10, 3, 14, 0)  # history survives restarts
    assert again.status.last_status == "ok"


def test_an_unreadable_status_file_is_ignored_at_start(tmp_path, caplog):
    path = tmp_path / "status.json"
    path.write_text("{not json")
    with caplog.at_level("WARNING"):
        scheduler = Scheduler(path, AT, lambda: JobResult("ok"), tz=UTC, background_heartbeat=False)
    assert scheduler.status.last_success_at is None
    assert any("unreadable status file" in r.message for r in caplog.records)


def test_the_status_is_written_atomically(tmp_path):
    holder = {}
    scheduler, fake, path = make(
        tmp_path, utc(2026, 10, 3, 14, 0), lambda: holder["scheduler"].stop() or JobResult("ok")
    )
    holder["scheduler"] = scheduler
    scheduler.serve()
    assert not list(tmp_path.glob("*.part"))


# --- background heartbeat during a long run ----------------------------------------------


def test_the_heartbeat_continues_while_a_run_is_in_progress(tmp_path):
    path = tmp_path / "status.json"
    holder = {}
    observed = {}

    def job():
        first = read(path)["heartbeat"]
        deadline = real_time.monotonic() + 5
        while real_time.monotonic() < deadline:
            if read(path)["heartbeat"] != first:
                observed["advanced"] = True
                break
            real_time.sleep(0.01)
        holder["scheduler"].stop()
        return JobResult("ok")

    scheduler = Scheduler(
        path, AT, job, tz=UTC,
        clock=lambda: datetime.now(UTC) + timedelta(seconds=real_time.monotonic() * 100),
        heartbeat_interval=timedelta(milliseconds=20),
    )  # fmt: skip
    holder["scheduler"] = scheduler
    scheduler.serve()
    assert observed.get("advanced") is True


# --- signals -----------------------------------------------------------------------------


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signals_make_the_scheduler_stop_and_handlers_are_restored(tmp_path, signum):
    scheduler = Scheduler(
        tmp_path / "s.json", AT, lambda: JobResult("ok"), tz=UTC, background_heartbeat=False
    )
    before = signal.getsignal(signum)
    restore = install_signal_handlers(scheduler)
    try:
        assert not scheduler.stopping
        os.kill(os.getpid(), signum)
        assert scheduler.stopping
    finally:
        restore()
    assert signal.getsignal(signum) == before


# --- health ------------------------------------------------------------------------------

NOW = utc(2026, 10, 3, 12, 0)


def status(**kwargs) -> DaemonStatus:
    defaults = {
        "state": "idle",
        "heartbeat": NOW - timedelta(seconds=30),
        "daemon_started": NOW - timedelta(hours=2),
        "last_status": "ok",
        "last_success_at": NOW - timedelta(hours=6),
    }
    return DaemonStatus(**{**defaults, **kwargs})


def test_a_healthy_daemon():
    assert check_health(status(), NOW) == (True, "ok")


def test_no_status_file_means_unhealthy():
    healthy, message = check_health(None, NOW)
    assert not healthy and "not started" in message


def test_a_stopped_daemon_is_unhealthy():
    assert check_health(status(state="stopped"), NOW)[0] is False


@pytest.mark.parametrize(
    ("age", "healthy"), [(timedelta(minutes=4), True), (timedelta(minutes=6), False)]
)
def test_the_heartbeat_must_be_fresh(age, healthy):
    ok, message = check_health(status(heartbeat=NOW - age), NOW)
    assert ok is healthy
    assert healthy or "no heartbeat" in message


def test_a_missing_heartbeat_is_unhealthy():
    assert "never" in check_health(status(heartbeat=None), NOW)[1]


def test_a_failed_last_run_is_unhealthy_and_explained():
    ok, message = check_health(status(last_status="failed", last_error="3 episode(s) failed"), NOW)
    assert not ok and "3 episode(s) failed" in message


@pytest.mark.parametrize(("hours", "healthy"), [(25, True), (26, True), (27, False)])
def test_the_last_successful_run_must_not_be_too_old(hours, healthy):
    ok, message = check_health(status(last_success_at=NOW - timedelta(hours=hours)), NOW)
    assert ok is healthy
    assert healthy or "last successful run" in message


def test_a_daemon_that_never_succeeded_gets_a_grace_period():
    young = status(last_success_at=None, last_status=None, daemon_started=NOW - timedelta(hours=3))
    assert check_health(young, NOW)[0] is True
    old = status(last_success_at=None, last_status=None, daemon_started=NOW - timedelta(hours=30))
    ok, message = check_health(old, NOW)
    assert not ok and "without a run" in message


def test_a_locked_last_run_is_not_a_failure():
    assert check_health(status(last_status="locked"), NOW)[0] is True


# --- status file format ------------------------------------------------------------------


def test_status_round_trip(tmp_path):
    original = status(next_run=NOW + timedelta(hours=1), pid=42, last_error="x")
    path = tmp_path / "s.json"
    path.write_text(original.to_json())
    assert DaemonStatus.from_file(path) == original


def test_status_reading_is_tolerant_and_strict_where_it_matters(tmp_path):
    path = tmp_path / "s.json"
    assert DaemonStatus.from_file(path) is None
    path.write_text('{"state": "idle", "future_field": 1}')
    assert DaemonStatus.from_file(path).state == "idle"  # unknown keys are ignored
    for content, message in (
        ("{nope", "Cannot read status file"),
        ("[1, 2]", "not a JSON object"),
        ('{"heartbeat": "yesterday-ish"}', "Cannot read status file"),
    ):
        path.write_text(content)
        with pytest.raises(ValueError, match=message):
            DaemonStatus.from_file(path)
