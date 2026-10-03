import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from typer.testing import CliRunner

from content2podcast import scheduler as scheduler_module
from content2podcast.cli import app
from content2podcast.lock import run_lock
from content2podcast.scheduler import DaemonStatus, JobResult

runner = CliRunner()
FEED = "https://blog.example.com/feed.xml"
EMPTY_FEED = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith(("C2P_", "AZURE_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "paths:\n  data_dir: data\n  output_dir: public\nschedule:\n  time: '05:30'\n"
        'llm:\n  provider: fake\n  response: {"title": "T", "summary": "S", "segments": []}\n'
        "tts:\n  provider: fake\n"
    )
    (tmp_path / "sources.yaml").write_text(f"sources:\n  - name: Blog\n    url: {FEED}\n")
    return tmp_path


def invoke(*args):
    return runner.invoke(app, list(args))


def write_status(project, **kwargs):
    defaults = {
        "state": "idle",
        "heartbeat": datetime.now(UTC) - timedelta(seconds=20),
        "daemon_started": datetime.now(UTC) - timedelta(hours=2),
        "last_status": "ok",
        "last_success_at": datetime.now(UTC) - timedelta(hours=3),
    }
    (project / "data").mkdir(exist_ok=True)
    (project / "data" / "status.json").write_text(DaemonStatus(**{**defaults, **kwargs}).to_json())


# --- health ------------------------------------------------------------------------------


def test_health_without_a_status_file_is_unhealthy(project):
    result = invoke("health")
    assert result.exit_code == 1
    assert "unhealthy: no status file" in result.output


def test_health_of_a_running_daemon(project):
    write_status(project)
    result = invoke("health")
    assert result.exit_code == 0 and "healthy: ok" in result.output


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"heartbeat": datetime.now(UTC) - timedelta(minutes=10)}, "no heartbeat"),
        ({"last_success_at": datetime.now(UTC) - timedelta(hours=30)}, "last successful run"),
        ({"last_status": "failed", "last_error": "all episodes failed"}, "all episodes failed"),
        ({"state": "stopped"}, "has stopped"),
    ],
    ids=["stale-heartbeat", "old-run", "failed-run", "stopped"],
)
def test_health_failures(project, overrides, message):
    write_status(project, **overrides)
    result = invoke("health")
    assert result.exit_code == 1
    assert result.output.startswith("unhealthy:") and message in result.output


def test_health_with_a_corrupt_status_file(project):
    (project / "data").mkdir()
    (project / "data" / "status.json").write_text("{broken")
    result = invoke("health")
    assert result.exit_code == 1 and "Cannot read status file" in result.output


def test_health_needs_no_providers_or_database(project):
    (project / "config.yaml").write_text("paths:\n  data_dir: data\n")
    write_status(project)
    assert invoke("health").exit_code == 0
    assert not list((project / "data").glob("*.db"))


def test_health_config_error(project):
    (project / "config.yaml").write_text("episode:\n  mode: weekly\n")
    assert invoke("health").exit_code == 2


# --- daemon ------------------------------------------------------------------------------


class OneShotScheduler:
    """Stands in for the real loop: construct it like the real one, run the job once."""

    instances: list["OneShotScheduler"] = []

    def __init__(self, status_path, at, job, **kwargs):
        self.status_path, self.at, self.job = status_path, at, job
        self.tz = "Test/Zone"
        self.stopping = False
        self.result: JobResult | None = None
        OneShotScheduler.instances.append(self)

    def serve(self):
        self.result = self.job()


@pytest.fixture
def one_shot(monkeypatch):
    OneShotScheduler.instances = []
    monkeypatch.setattr("content2podcast.cli.Scheduler", OneShotScheduler)
    monkeypatch.setattr("content2podcast.cli.find_tools", lambda: None)
    return OneShotScheduler


@respx.mock
def test_daemon_runs_the_pipeline_on_schedule(project, one_shot):
    route = respx.get(FEED).mock(return_value=httpx.Response(200, content=EMPTY_FEED))
    result = invoke("daemon")
    assert result.exit_code == 0, result.output
    assert "Daemon started: daily at 05:30 (Test/Zone)" in result.output
    assert "Run finished: 0 published, 0 failed" in result.output
    assert result.output.rstrip().endswith("Daemon stopped")
    [instance] = one_shot.instances
    assert instance.result == JobResult("ok")
    assert instance.at.hour == 5 and instance.at.minute == 30
    assert instance.status_path == project / "data" / "status.json"
    assert route.called


def test_a_failing_run_is_reported_to_the_scheduler_not_raised(project, one_shot):
    (project / "config.yaml").write_text(
        "paths:\n  data_dir: data\nllm:\n  provider: fake\n"  # no tts: the run cannot start
    )
    result = invoke("daemon")
    assert result.exit_code == 0  # the daemon itself survives
    [instance] = one_shot.instances
    assert instance.result.status == "failed" and "setup failed" in instance.result.detail
    assert "No tts provider configured" in result.output


@respx.mock
def test_a_locked_run_is_reported_as_locked(project, one_shot):
    respx.get(FEED).mock(return_value=httpx.Response(200, content=EMPTY_FEED))
    (project / "data").mkdir()
    with run_lock(project / "data"):
        invoke("daemon")
    [instance] = one_shot.instances
    assert instance.result.status == "locked"


@respx.mock
def test_all_episodes_failing_makes_the_run_failed(project, one_shot, monkeypatch):
    from content2podcast.providers.llm.base import LLMError
    from content2podcast.providers.llm.fake import FakeLLM

    monkeypatch.setattr(
        "content2podcast.cli.build_llm", lambda cfg, secrets: FakeLLM(error=LLMError("down"))
    )
    from email.utils import format_datetime

    recent = format_datetime(datetime.now(UTC) - timedelta(hours=1))
    page = "https://blog.example.com/posts/1"
    feed_with_item = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        f"<item><title>A</title><link>{page}</link><pubDate>{recent}</pubDate></item>"
        "</channel></rss>"
    ).encode()
    route = respx.get(FEED).mock(return_value=httpx.Response(200, content=EMPTY_FEED))
    respx.get(page).mock(
        return_value=httpx.Response(200, text="<html><body><p>x</p></body></html>")
    )
    invoke("daemon")  # baseline
    route.mock(return_value=httpx.Response(200, content=feed_with_item))
    one_shot.instances.clear()
    invoke("daemon")
    [instance] = one_shot.instances
    assert instance.result.status == "failed" and "1 episode(s) failed" in instance.result.detail


def test_daemon_config_errors_end_it_immediately(project, one_shot):
    (project / "config.yaml").write_text("schedule:\n  time: tomorrow\n")
    result = invoke("daemon")
    assert result.exit_code == 2 and "schedule.time" in result.output
    assert one_shot.instances == []


def test_daemon_uses_the_real_scheduler_and_stops_on_a_signal(project, monkeypatch):
    """The wiring with the real loop: SIGTERM ends it cleanly and the status says so."""
    import os
    import signal
    import threading

    monkeypatch.setattr("content2podcast.cli.find_tools", lambda: None)
    started = threading.Event()
    original = scheduler_module.Scheduler.run_job

    def run_job_then_signal(self):
        result = original(self)
        started.set()
        os.kill(os.getpid(), signal.SIGTERM)  # as `docker stop` would
        return result

    monkeypatch.setattr(scheduler_module.Scheduler, "run_job", run_job_then_signal)
    with respx.mock:
        respx.get(FEED).mock(return_value=httpx.Response(200, content=EMPTY_FEED))
        result = invoke("daemon")
    assert result.exit_code == 0 and started.is_set()
    status = json.loads((project / "data" / "status.json").read_text())
    assert status["state"] == "stopped" and status["last_status"] == "ok"
    assert status["last_success_at"] is not None


def test_help_texts():
    assert "every day" in invoke("daemon", "--help").output
    assert "container" in invoke("health", "--help").output
