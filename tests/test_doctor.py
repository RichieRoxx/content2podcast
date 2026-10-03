import sqlite3

import pytest
from typer.testing import CliRunner

from content2podcast import doctor as doctor_module
from content2podcast.audio import AudioError
from content2podcast.cli import app
from content2podcast.db import connect, db_path, latest_version
from content2podcast.providers.llm.base import LLMError
from content2podcast.providers.llm.fake import FakeLLM
from content2podcast.providers.tts.base import TTSError
from content2podcast.providers.tts.fake import FakeTTS

runner = CliRunner()
SECRET = "super-secret-key-1234567890"


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith(("C2P_", "AZURE_")):
            monkeypatch.delenv(key)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path)
    (tmp_path / "sources.yaml").write_text(
        "sources:\n  - name: Blog\n    url: https://blog.example.com/feed.xml\n"
        "  - name: Paused\n    url: https://paused.example.com/feed.xml\n    enabled: false\n"
    )
    return tmp_path


def write_config(root, *, llm=True, tts=True, extra=""):
    text = "paths:\n  data_dir: data\n  output_dir: public\n"
    if llm:
        text += "llm:\n  provider: fake\n"
    if tts:
        text += "tts:\n  provider: fake\n"
    (root / "config.yaml").write_text(text + extra, encoding="utf-8")


def doctor(*args):
    return runner.invoke(app, ["doctor", *args])


def line(result, name):
    return next(ln for ln in result.output.splitlines() if f"] {name}:" in ln)


# --- success -----------------------------------------------------------------------------


@pytest.mark.ffmpeg
def test_a_healthy_installation_passes(project):
    result = doctor()
    assert result.exit_code == 0, result.output
    assert "[FAIL]" not in result.output
    assert line(result, "config").startswith("[ ok ]") and "data_dir=" in line(result, "config")
    assert "2 configured, 1 enabled" in line(result, "sources")
    assert "fake" in line(result, "llm provider") and "fake" in line(result, "tts provider")
    assert "Mia: de-DE-Mia" in line(result, "voices") and "Klaus: de-DE-Klaus" in line(
        result, "voices"
    )
    assert line(result, "speaking styles").endswith("neutral, cheerful, serious")
    assert "ffmpeg version" in line(result, "ffmpeg") and "ffprobe version" in line(
        result, "ffprobe"
    )
    assert "will be created" in line(result, "data dir")
    assert "not created yet" in line(result, "database")
    assert "prompt template" in result.output
    assert result.output.rstrip().endswith("0 failed")


@pytest.mark.ffmpeg
def test_doctor_is_read_only(project):
    doctor()
    assert not (project / "data").exists() and not (project / "public").exists()


@pytest.mark.ffmpeg
def test_secrets_are_masked_and_the_fallback_is_explained(project):
    (project / ".env").write_text(
        f"AZURE_FOUNDRY_API_KEY={SECRET}\nAZURE_FOUNDRY_BASE_URL=https://res.openai.azure.com/openai/v1/\n"
    )
    result = doctor()
    assert SECRET not in result.output and SECRET[:8] not in result.output
    assert f"set ({len(SECRET)} characters)" in line(result, "secret AZURE_FOUNDRY_API_KEY")
    assert "https://res.openai.azure.com/openai/v1/" in line(
        result, "secret AZURE_FOUNDRY_BASE_URL"
    )
    assert "falls back to AZURE_FOUNDRY_API_KEY" in line(result, "secret AZURE_SPEECH_KEY")
    assert "swedencentral" in line(result, "secret AZURE_SPEECH_REGION")


def test_unset_secrets_are_informational_not_failures(project):
    result = doctor()
    assert line(result, "secret AZURE_FOUNDRY_API_KEY").startswith("[info]")
    assert "not set" in line(result, "secret AZURE_FOUNDRY_API_KEY")


# --- failures ----------------------------------------------------------------------------


def test_an_invalid_config_fails_but_still_checks_ffmpeg(project):
    (project / "config.yaml").write_text("episode:\n  mode: weekly\n")
    result = doctor()
    assert result.exit_code == 1
    assert line(result, "config").startswith("[FAIL]") and "episode.mode" in line(result, "config")
    assert "ffmpeg" in result.output  # independent of the config
    assert "sources" not in result.output  # the dependent checks were not attempted
    assert result.output.rstrip().endswith("failed")


def test_missing_and_invalid_sources_files_fail(project):
    (project / "sources.yaml").unlink()
    result = doctor()
    assert result.exit_code == 1 and "Sources file not found" in line(result, "sources")
    (project / "sources.yaml").write_text(
        "sources:\n  - name: A\n    url: https://a\n  - name: A\n    url: https://b\n"
    )
    assert "duplicate source name" in line(doctor(), "sources")


def test_no_enabled_source_is_only_a_warning(project):
    (project / "sources.yaml").write_text(
        "sources:\n  - name: A\n    url: https://a\n    enabled: false\n"
    )
    result = doctor()
    assert line(result, "sources").startswith("[warn]")
    assert "1 warning(s)" in result.output


def test_unconfigured_providers_fail(project):
    write_config(project, llm=False, tts=False)
    result = doctor()
    assert result.exit_code == 1
    assert "No llm provider configured" in line(result, "llm provider")
    assert "No tts provider configured" in line(result, "tts provider")


def test_provider_without_credentials_names_the_missing_variables(project):
    (project / "config.yaml").write_text(
        "paths:\n  data_dir: data\n"
        "llm:\n  provider: azure_foundry\ntts:\n  provider: azure_speech\n"
    )
    result = doctor()
    assert result.exit_code == 1
    assert "AZURE_FOUNDRY_BASE_URL and AZURE_FOUNDRY_API_KEY" in line(result, "llm provider")
    assert "AZURE_SPEECH_KEY or AZURE_FOUNDRY_API_KEY" in line(result, "tts provider")


def test_provider_with_credentials_shows_the_model(project):
    (project / "config.yaml").write_text(
        "paths:\n  data_dir: data\nllm:\n  provider: azure_foundry\n  model: my-gpt\n"
        "tts:\n  provider: azure_speech\n"
    )
    (project / ".env").write_text(
        "AZURE_FOUNDRY_BASE_URL=https://res.openai.azure.com/openai/v1/\n"
        f"AZURE_FOUNDRY_API_KEY={SECRET}\n"
    )
    result = doctor()
    assert "azure_foundry, model my-gpt" in line(result, "llm provider")
    assert line(result, "tts provider").startswith("[ ok ]")
    assert "excited" in line(result, "speaking styles")  # the default MAI-Voice styles
    assert SECRET not in result.output


def test_ffmpeg_missing_fails(project, monkeypatch):
    def missing():
        raise AudioError("ffmpeg and ffprobe not found on PATH. Install ffmpeg")

    monkeypatch.setattr(doctor_module, "find_tools", missing)
    result = doctor()
    assert result.exit_code == 1 and "not found on PATH" in line(result, "ffmpeg")


def test_a_directory_that_is_a_file_fails(project):
    (project / "data").write_text("oops, a file")
    result = doctor()
    assert result.exit_code == 1
    assert "exists but is not a directory" in line(result, "data dir")
    assert line(result, "output dir").startswith("[ ok ]")


def test_an_unwritable_directory_fails(project, monkeypatch):
    import tempfile

    (project / "public").mkdir()

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", refuse)
    result = doctor()
    assert "not writable" in line(result, "output dir")


def test_a_directory_below_a_file_cannot_be_created(project):
    (project / "blocker").write_text("file")
    (project / "config.yaml").write_text(
        "paths:\n  data_dir: blocker/data\nllm:\n  provider: fake\ntts:\n  provider: fake\n"
    )
    result = doctor()
    assert "does not exist and cannot be created below" in line(result, "data dir")
    assert result.exit_code == 1


def test_missing_cover_and_unknown_prompt_language_fail(project):
    write_config(project, extra="podcast:\n  cover_image: nope.png\n  language: fr-FR\n")
    result = doctor()
    assert "not found" in line(result, "cover image")
    assert "per_article_fr.md" in line(result, "prompt template")
    assert result.exit_code == 1


# --- database ----------------------------------------------------------------------------


def test_database_schema_version_is_reported(project):
    connect(db_path(project / "data")).close()
    result = doctor()
    assert f"schema v{latest_version()} (current)" in line(result, "database")


def test_an_outdated_schema_is_a_warning_and_a_newer_one_a_failure(project):
    path = db_path(project / "data")
    connect(path).close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    result = doctor()
    assert line(result, "database").startswith("[warn]") and "will be applied" in line(
        result, "database"
    )
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 0  # not migrated

    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {latest_version() + 5}")
    conn.commit()
    conn.close()
    result = doctor()
    assert line(result, "database").startswith("[FAIL]") and "newer than this program" in line(
        result, "database"
    )
    assert result.exit_code == 1


def test_a_corrupt_database_fails(project):
    path = db_path(project / "data")
    path.parent.mkdir()
    path.write_bytes(b"this is not a sqlite database" * 50)
    result = doctor()
    assert line(result, "database").startswith("[FAIL]")


# --- online ------------------------------------------------------------------------------


def patch_providers(monkeypatch, llm, tts):
    monkeypatch.setattr(doctor_module, "build_llm", lambda cfg, secrets: llm)
    monkeypatch.setattr(doctor_module, "build_tts", lambda cfg, secrets: tts)


@pytest.mark.ffmpeg
def test_online_checks_send_one_request_each(project, monkeypatch):
    llm, tts = FakeLLM({"ok": True}), FakeTTS()
    patch_providers(monkeypatch, llm, tts)
    result = doctor("--online")
    assert result.exit_code == 0, result.output
    assert line(result, "llm request").startswith("[ ok ]")
    assert "bytes of wav" in line(result, "tts request")
    assert len(llm.calls) == 1 and len(tts.calls) == 1
    assert tts.calls[0].voice == "de-DE-Mia:MAI-Voice-2.1"


def test_without_online_no_request_is_made(project, monkeypatch):
    llm, tts = FakeLLM({"ok": True}), FakeTTS()
    patch_providers(monkeypatch, llm, tts)
    result = doctor()
    assert "llm request" not in result.output and "tts request" not in result.output
    assert llm.calls == [] and tts.calls == []


def test_online_failures_fail_the_doctor(project, monkeypatch):
    class BrokenTTS(FakeTTS):
        def synthesize(self, text, voice, style):
            raise TTSError("401 Unauthorized")

    patch_providers(monkeypatch, FakeLLM(error=LLMError("model not found")), BrokenTTS())
    result = doctor("--online")
    assert result.exit_code == 1
    assert "model not found" in line(result, "llm request")
    assert "401 Unauthorized" in line(result, "tts request")


def test_help_mentions_the_cost_of_the_online_checks():
    result = runner.invoke(app, ["doctor", "--help"])
    assert "--online" in result.output and "costs" in result.output.replace("\n", " ")


def test_every_output_line_has_a_known_status_label(project):
    lines = [ln for ln in doctor().output.splitlines() if ln and not ln.startswith("Result:")]
    assert lines and all(ln[:6] in {"[ ok ]", "[info]", "[warn]", "[FAIL]"} for ln in lines)
