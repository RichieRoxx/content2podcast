import pytest
from typer.testing import CliRunner

from content2podcast import cli
from content2podcast.cli import app
from content2podcast.config import ConfigError, load_config, parse_overrides

runner = CliRunner()


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("C2P_"):
            monkeypatch.delenv(key)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "podcast:\n  title: From File\n  author: File Author\n"
        "feed:\n  base_url: http://file.example:8080\n"
        "paths:\n  data_dir: data\n  output_dir: public\n  sources_file: sources.yaml\n",
        encoding="utf-8",
    )
    (tmp_path / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    return path


# --- parse_overrides -------------------------------------------------------------------------


def test_parse_nested_keys_and_yaml_values():
    parsed = parse_overrides(
        [
            "feed.base_url=http://x:8080",
            "episode.max_articles=5",
            "podcast.explicit=true",
            "episode.max_episodes_per_run=null",
            "feed.retention.max_age_days=14",
            "podcast.title=Hello: World",
            "llm.provider=fake",
        ]
    )
    assert parsed == {
        "feed": {"base_url": "http://x:8080", "retention": {"max_age_days": 14}},
        "episode": {"max_articles": 5, "max_episodes_per_run": None},
        "podcast": {"explicit": True, "title": "Hello: World"},
        "llm": {"provider": "fake"},
    }


def test_later_overrides_win_and_values_may_contain_equals():
    assert parse_overrides(["a.b=1", "a.b=2"]) == {"a": {"b": 2}}
    assert parse_overrides(["feed.base_url=http://x/?a=b"]) == {
        "feed": {"base_url": "http://x/?a=b"}
    }


@pytest.mark.parametrize("item", ["novalue", "=x", "a..b=1", "a b=1", "a.=1", "1a=2"])
def test_invalid_syntax_is_rejected(item):
    with pytest.raises(ConfigError, match="--set"):
        parse_overrides([item])


def test_conflicting_overrides_are_rejected():
    with pytest.raises(ConfigError, match="Conflicting"):
        parse_overrides(["feed=1", "feed.base_url=x"])
    with pytest.raises(ConfigError, match="Conflicting"):
        parse_overrides(["feed.base_url=x", "feed=1"])


# --- precedence ------------------------------------------------------------------------------


def test_override_beats_environment_and_file_and_keeps_siblings(config_file, monkeypatch):
    monkeypatch.setenv("C2P_FEED__BASE_URL", "http://env.example:8080")
    monkeypatch.setenv("C2P_PODCAST__AUTHOR", "Env Author")
    config = load_config(
        config_file, overrides=parse_overrides(["feed.base_url=http://cli.example:8080"])
    )
    assert config.feed.base_url == "http://cli.example:8080"  # CLI > env > file
    assert config.podcast.author == "Env Author"  # untouched keys keep their layers
    assert config.podcast.title == "From File"


def test_override_of_provider_options_merges_with_the_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("llm:\n  provider: azure_foundry\n  model: big\n", encoding="utf-8")
    config = load_config(path, overrides=parse_overrides(["llm.model=small"]))
    assert config.llm.provider == "azure_foundry" and config.llm.model == "small"


def test_cli_set_beats_environment(config_file, monkeypatch):
    monkeypatch.setenv("C2P_FEED__BASE_URL", "http://env.example:8080")
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_file),
            "--set",
            "feed.base_url=http://cli.example:8080",
            "feed",
            "rebuild",
        ],
    )
    assert result.exit_code == 0, result.output
    feed = (config_file.parent / "public" / "feed.xml").read_text(encoding="utf-8")
    assert "cli.example" in feed and "env.example" not in feed


def test_cli_set_can_be_repeated(config_file):
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_file),
            "--set",
            "podcast.title=A",
            "--set",
            "podcast.title=B",
            "feed",
            "rebuild",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "<title>B</title>" in (config_file.parent / "public" / "feed.xml").read_text()


def test_malformed_set_exits_with_2(config_file):
    result = runner.invoke(app, ["--config", str(config_file), "--set", "oops", "feed", "rebuild"])
    assert result.exit_code == 2
    assert "KEY.PATH=VALUE" in result.output


def test_unknown_or_invalid_override_is_a_config_error(config_file):
    unknown = runner.invoke(
        app, ["--config", str(config_file), "--set", "feed.nope=1", "feed", "rebuild"]
    )
    assert unknown.exit_code == 2 and "nope" in unknown.output
    invalid = runner.invoke(
        app, ["--config", str(config_file), "--set", "episode.max_articles=x", "feed", "rebuild"]
    )
    assert invalid.exit_code == 2 and "max_articles" in invalid.output


def test_help_documents_set():
    result = runner.invoke(app, ["--help"])
    assert "--set" in result.output and "KEY=VALUE" in result.output


# --- runtime errors --------------------------------------------------------------------------


def boom(*args, **kwargs):
    raise RuntimeError("disk exploded")


def test_unexpected_error_is_one_line_with_exit_code_1(config_file, monkeypatch):
    monkeypatch.setattr(cli, "write_feed", boom)
    result = runner.invoke(app, ["--config", str(config_file), "feed", "rebuild"])
    assert result.exit_code == 1
    assert "Error: RuntimeError: disk exploded" in result.output
    assert "-v" in result.output
    assert "Traceback" not in result.output


def test_verbose_prints_the_traceback(config_file, monkeypatch, capsys):
    monkeypatch.setattr(cli, "write_feed", boom)
    result = runner.invoke(app, ["--config", str(config_file), "-v", "feed", "rebuild"])
    assert result.exit_code == 1
    assert "Error: RuntimeError: disk exploded" in result.output
    assert "Traceback" in capsys.readouterr().err + result.output


def test_other_exit_codes_are_untouched(config_file, tmp_path):
    missing = runner.invoke(app, ["--config", str(tmp_path / "nope.yaml"), "feed", "rebuild"])
    assert missing.exit_code == 2
    usage = runner.invoke(app, ["--config", str(config_file), "no-such-command"])
    assert usage.exit_code == 2


def test_held_run_lock_still_exits_with_3(config_file, monkeypatch):
    from content2podcast.lock import RunLocked

    def locked(*args, **kwargs):
        raise RunLocked("held")

    monkeypatch.setattr(cli, "run_lock", locked)
    result = runner.invoke(app, ["--config", str(config_file), "run"])
    assert result.exit_code == 3, result.output
