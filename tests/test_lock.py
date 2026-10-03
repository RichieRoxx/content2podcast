import pytest
from typer.testing import CliRunner

from content2podcast.cli import app
from content2podcast.lock import EXIT_LOCKED, RunLocked, run_lock


def test_second_holder_is_rejected(tmp_path):
    with run_lock(tmp_path), pytest.raises(RunLocked) as exc, run_lock(tmp_path):
        pass
    assert exc.value.exit_code == EXIT_LOCKED == 3


def test_lock_released_after_use(tmp_path):
    with run_lock(tmp_path):
        pass
    with run_lock(tmp_path):
        pass


def test_lock_released_after_exception(tmp_path):
    with pytest.raises(ValueError), run_lock(tmp_path):
        raise ValueError
    with run_lock(tmp_path):
        pass


def test_rejection_logs_warning(tmp_path, caplog):
    with run_lock(tmp_path), pytest.raises(RunLocked), run_lock(tmp_path):
        pass
    assert any("already in progress" in r.message for r in caplog.records)


def test_cli_run_exits_3_when_locked(tmp_path, monkeypatch):
    for var in ("NO_COLOR",):
        monkeypatch.setenv(var, "1")
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("paths:\n  data_dir: data\n")
    runner = CliRunner()
    with run_lock(tmp_path / "data"):
        result = runner.invoke(app, ["--config", str(cfg), "run"])
    assert result.exit_code == 3
    assert "already in progress" in result.output
    # the lock is released again: the next run gets past it (and stops at the missing sources)
    after = runner.invoke(app, ["--config", str(cfg), "run"])
    assert after.exit_code == 2 and "Sources file not found" in after.output
