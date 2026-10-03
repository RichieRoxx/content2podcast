from pathlib import Path

import pytest

from content2podcast.config import (
    ConfigError,
    load_config,
    load_secrets,
    load_sources,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith(("C2P_", "AZURE_")):
            monkeypatch.delenv(key)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults(tmp_path):
    cfg = load_config(None, env_file=tmp_path / ".env")
    assert cfg.podcast.language == "de-DE"
    assert cfg.episode.mode == "per_article"
    assert cfg.episode.words_per_minute == 140
    assert cfg.feed.retention.max_age_days == 30
    assert cfg.feed.retention.max_episodes is None
    assert cfg.roles.host.voice == "de-DE-Mia:MAI-Voice-2.1"
    assert cfg.schedule.time == "05:30"


def test_yaml_loading(tmp_path):
    path = write(
        tmp_path / "config.yaml", "podcast:\n  title: Hello\nepisode:\n  max_articles: 3\n"
    )
    cfg = load_config(path, env_file=tmp_path / ".env")
    assert cfg.podcast.title == "Hello"
    assert cfg.episode.max_articles == 3
    assert cfg.episode.min_minutes == 3  # default preserved


def test_env_overrides_nested_field(tmp_path, monkeypatch):
    path = write(tmp_path / "config.yaml", "feed:\n  base_url: http://yaml\n")
    monkeypatch.setenv("C2P_FEED__BASE_URL", "http://env")
    cfg = load_config(path, env_file=tmp_path / ".env")
    assert cfg.feed.base_url == "http://env"


def test_dotenv_between_env_and_yaml(tmp_path, monkeypatch):
    path = write(tmp_path / "config.yaml", "feed:\n  base_url: http://yaml\npodcast:\n  title: Y\n")
    write(tmp_path / ".env", "C2P_FEED__BASE_URL=http://dotenv\nC2P_PODCAST__TITLE=D\n")
    monkeypatch.setenv("C2P_PODCAST__TITLE", "E")
    cfg = load_config(path)
    assert cfg.feed.base_url == "http://dotenv"  # .env > yaml
    assert cfg.podcast.title == "E"  # env > .env


def test_dotenv_with_secrets_does_not_break_config(tmp_path):
    write(tmp_path / ".env", "AZURE_FOUNDRY_API_KEY=abc\nC2P_PODCAST__TITLE=D\n")
    assert load_config(env_file=tmp_path / ".env").podcast.title == "D"


def test_cli_overrides_win(tmp_path, monkeypatch):
    monkeypatch.setenv("C2P_FEED__BASE_URL", "http://env")
    cfg = load_config(env_file=tmp_path / ".env", overrides={"feed": {"base_url": "http://cli"}})
    assert cfg.feed.base_url == "http://cli"


def test_config_path_precedence(tmp_path, monkeypatch):
    a = write(tmp_path / "a.yaml", "podcast:\n  title: A\n")
    b = write(tmp_path / "b.yaml", "podcast:\n  title: B\n")
    monkeypatch.setenv("C2P_CONFIG", str(b))
    assert load_config(env_file=tmp_path / ".env").podcast.title == "B"
    assert load_config(a, env_file=tmp_path / ".env").podcast.title == "A"


def test_relative_paths_resolved_against_config_dir(tmp_path):
    sub = tmp_path / "conf"
    sub.mkdir()
    path = write(
        sub / "config.yaml",
        "paths:\n  data_dir: d\n  output_dir: /abs/out\npodcast:\n  cover_image: c.jpg\n",
    )
    cfg = load_config(path)
    assert cfg.paths.data_dir == (sub / "d").resolve()
    assert cfg.paths.output_dir == Path("/abs/out")
    assert cfg.paths.sources_file == (sub / "sources.yaml").resolve()
    assert cfg.podcast.cover_image == (sub / "c.jpg").resolve()


def test_invalid_value_gives_readable_error(tmp_path):
    path = write(tmp_path / "config.yaml", "episode:\n  mode: weekly\nschedule:\n  time: 25:00\n")
    with pytest.raises(ConfigError) as exc:
        load_config(path, env_file=tmp_path / ".env")
    msg = str(exc.value)
    assert str(path) in msg
    assert "episode.mode" in msg
    assert "schedule.time" in msg
    assert "Traceback" not in msg


def test_unknown_key_rejected(tmp_path):
    path = write(tmp_path / "config.yaml", "episode:\n  max_articels: 3\n")
    with pytest.raises(ConfigError, match="episode.max_articels"):
        load_config(path, env_file=tmp_path / ".env")


def test_missing_explicit_config_is_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_min_minutes_must_not_exceed_max(tmp_path):
    path = write(tmp_path / "config.yaml", "episode:\n  min_minutes: 20\n")
    with pytest.raises(ConfigError, match="min_minutes"):
        load_config(path, env_file=tmp_path / ".env")


def test_secrets_never_leak_in_repr(monkeypatch):
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "super-secret-key")
    secrets = load_secrets()
    assert "super-secret-key" not in repr(secrets)
    assert "super-secret-key" not in str(secrets)
    assert secrets.effective_speech_key.get_secret_value() == "super-secret-key"
    assert secrets.azure_speech_region == "swedencentral"


def test_speech_key_overrides_foundry_key(monkeypatch):
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "f")
    monkeypatch.setenv("AZURE_SPEECH_KEY", "s")
    assert load_secrets().effective_speech_key.get_secret_value() == "s"


def test_secrets_from_dotenv(tmp_path):
    env = write(tmp_path / ".env", "AZURE_SPEECH_KEY=k\nAZURE_SPEECH_REGION=westeurope\n")
    secrets = load_secrets(env)
    assert secrets.azure_speech_region == "westeurope"
    assert secrets.azure_speech_key.get_secret_value() == "k"


def test_sources_loading(tmp_path):
    path = write(
        tmp_path / "sources.yaml",
        "sources:\n"
        "  - name: A\n    url: https://a/feed\n"
        "  - name: B\n    url: https://b\n    type: html\n    selector: a\n",
    )
    sources = load_sources(path).sources
    assert [s.name for s in sources] == ["A", "B"]
    assert sources[0].type == "rss" and sources[0].enabled


def test_duplicate_source_names_rejected(tmp_path):
    path = write(
        tmp_path / "sources.yaml",
        "sources:\n  - name: A\n    url: https://a\n  - name: A\n    url: https://b\n",
    )
    with pytest.raises(ConfigError, match="duplicate source name"):
        load_sources(path)


def test_invalid_regex_rejected(tmp_path):
    path = write(
        tmp_path / "sources.yaml",
        "sources:\n  - name: A\n    url: https://a\n    include: ['(']\n",
    )
    with pytest.raises(ConfigError, match="invalid regex"):
        load_sources(path)


def test_example_files_load(tmp_path):
    cfg = load_config(ROOT / "config.example.yaml", env_file=tmp_path / ".env")
    assert cfg.podcast.title == "My Daily Podcast"
    sources = load_sources(ROOT / "sources.example.yaml")
    assert {s.type for s in sources.sources} == {"rss", "html"}
    secrets = load_secrets(ROOT / ".env.example")
    assert secrets.azure_speech_region == "swedencentral"


def test_unknown_top_level_key_rejected(tmp_path):
    path = write(tmp_path / "config.yaml", "podcats:\n  title: x\n")
    with pytest.raises(ConfigError, match="podcats"):
        load_config(path, env_file=tmp_path / ".env")


def test_unreadable_config_files_give_a_readable_error(tmp_path):
    from unittest import mock

    import pydantic_settings

    path = write(tmp_path / "config.yaml", "podcast:\n  title: x\n")
    denied = PermissionError(13, "Permission denied")
    with mock.patch.object(
        pydantic_settings.DotEnvSettingsSource, "_read_env_files", side_effect=denied
    ):
        with pytest.raises(ConfigError) as exc:
            load_config(path)
        message = str(exc.value)
        assert "Permission denied" in message and "may read" in message
        assert str(tmp_path / ".env") in message and str(path) in message
        assert "Traceback" not in message
        with pytest.raises(ConfigError, match="Permission denied.*may read"):
            load_secrets(tmp_path / ".env")
