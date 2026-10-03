import pytest
from typer.testing import CliRunner

from content2podcast import __version__
from content2podcast.cli import app

runner = CliRunner()

COMMANDS = [
    ["check"],
    ["run"],
    ["script"],
    ["tts"],
    ["feed"],
    ["feed", "rebuild"],
    ["sources"],
    ["sources", "list"],
    ["sources", "baseline"],
    ["episodes"],
    ["episodes", "list"],
    ["doctor"],
    ["daemon"],
    ["health"],
]


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch, request):
    import os

    for key in list(os.environ):
        if key.startswith("C2P_"):
            monkeypatch.delenv(key)
    # CI sets color variables; Rich would then split option names with ANSI codes
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_root_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("check", "run", "script", "tts", "feed", "sources", "episodes", "doctor"):
        assert name in result.output
    assert "--config" in result.output and "--verbose" in result.output


@pytest.mark.parametrize("cmd", COMMANDS, ids=" ".join)
def test_every_command_has_help(cmd):
    result = runner.invoke(app, [*cmd, "--help"])
    assert result.exit_code == 0, result.output


def test_run_options_in_help():
    out = runner.invoke(app, ["run", "--help"]).output
    assert "--dry-run" in out and "--force" in out


def test_commands_accept_global_options(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("podcast:\n  title: T\n")
    result = runner.invoke(app, ["--config", str(cfg), "-vv", "episodes", "list"])
    assert result.exit_code == 0


def test_config_error_is_one_message_and_exit_2(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("episode:\n  mode: weekly\n")
    result = runner.invoke(app, ["--config", str(cfg), "check"])
    assert result.exit_code == 2
    assert "episode.mode" in result.output
    assert "Traceback" not in result.output


def test_missing_config_is_exit_2(tmp_path):
    result = runner.invoke(app, ["--config", str(tmp_path / "nope.yaml"), "check"])
    assert result.exit_code == 2
    assert "not found" in result.output


def test_help_works_even_with_broken_config(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("episode:\n  mode: weekly\n")
    result = runner.invoke(app, ["--config", str(cfg), "check", "--help"])
    assert result.exit_code == 0


def test_quiet_and_verbose_conflict():
    result = runner.invoke(app, ["-q", "-v", "check"])
    assert result.exit_code == 2
