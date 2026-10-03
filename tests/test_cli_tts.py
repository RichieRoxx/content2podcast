import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from content2podcast.audio import AudioError
from content2podcast.cli import app
from content2podcast.providers.tts.fake import FakeTTS

runner = CliRunner()
SCRIPT = Path(__file__).parent / "fixtures" / "script" / "script.json"
TEXTS = [s["text"] for s in json.loads(SCRIPT.read_text(encoding="utf-8"))["segments"]]
CHARS = sum(len(t) for t in TEXTS)


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith(("C2P_", "AZURE_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path)
    shutil.copy(SCRIPT, tmp_path / "script.json")
    return tmp_path


def write_config(root: Path, tts: str = "tts:\n  provider: fake\n", extra: str = ""):
    (root / "config.yaml").write_text(
        "paths:\n  data_dir: data\npodcast:\n  title: Mein Podcast\n  author: Erika\n"
        f"episode:\n  gap_ms: 300\n{extra}" + tts,
        encoding="utf-8",
    )


def invoke(*args):
    return runner.invoke(app, list(args))


def probe(path: Path, entries: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", "-show_entries", entries, str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(out.stdout)


# --- errors that need no ffmpeg ----------------------------------------------------------


def test_missing_script_file_is_a_readable_error(project):
    result = invoke("tts", "nope.json")
    assert result.exit_code == 2
    assert "Script file not found: nope.json" in result.output
    assert "Traceback" not in result.output


def test_invalid_json_and_invalid_content_are_readable_errors(project):
    (project / "bad.json").write_text("{nope")
    result = invoke("tts", "bad.json")
    assert result.exit_code == 2 and "Cannot read bad.json" in result.output

    (project / "invalid.json").write_text('{"title": "T", "summary": "S", "segments": [{}]}')
    result = invoke("tts", "invalid.json")
    assert result.exit_code == 2 and "segments.0.speaker" in result.output


def test_no_tts_provider_configured_is_a_config_error(project):
    write_config(project, tts="")
    result = invoke("tts", "script.json")
    assert result.exit_code == 2 and "No tts provider configured" in result.output


def test_missing_ffmpeg_fails_before_any_synthesis(project, monkeypatch):
    tts = FakeTTS()
    monkeypatch.setattr("content2podcast.cli.build_tts", lambda cfg, secrets: tts)

    def no_ffmpeg():
        raise AudioError("ffmpeg and ffprobe not found on PATH. Install ffmpeg")

    monkeypatch.setattr("content2podcast.cli.find_tools", no_ffmpeg)
    result = invoke("tts", "script.json")
    assert result.exit_code == 1 and "not found on PATH" in result.output
    assert tts.calls == []  # nothing was synthesized (and paid for)


@pytest.mark.ffmpeg
def test_cost_guard_is_reported_without_synthesizing(project, monkeypatch):
    tts = FakeTTS()
    monkeypatch.setattr("content2podcast.cli.build_tts", lambda cfg, secrets: tts)
    write_config(project, tts=f"tts:\n  provider: fake\n  max_chars_per_episode: {CHARS - 1}\n")
    result = invoke("tts", "script.json")
    assert result.exit_code == 1 and "max_chars_per_episode" in result.output
    assert tts.calls == []


# --- with ffmpeg -------------------------------------------------------------------------


@pytest.mark.ffmpeg
def test_voices_the_script_and_writes_an_mp3(project):
    result = invoke("tts", "script.json")
    assert result.exit_code == 0, result.output
    mp3 = project / "script.mp3"  # default: next to the script
    assert mp3.is_file()
    assert "Wrote script.mp3" in result.output
    assert f"{CHARS} characters in 3 part(s)" in result.output
    # the fake TTS speaks 10 ms per character; segments are separated by 300 ms gaps
    expected = CHARS * 0.01 + 2 * 0.3
    duration = float(probe(mp3, "format=duration")["format"]["duration"])
    assert duration == pytest.approx(expected, abs=0.25)
    assert f"duration {duration:.1f} s" in result.output
    tags = probe(mp3, "format_tags")["format"]["tags"]
    assert tags["title"] == "Python-Pakete installieren schneller"
    assert tags["artist"] == "Erika" and tags["album"] == "Mein Podcast"


@pytest.mark.ffmpeg
def test_output_and_work_dir_options(project):
    out = project / "out" / "folge.mp3"
    work = project / "my-work"
    result = invoke("tts", "script.json", "-o", str(out), "--work-dir", str(work))
    assert result.exit_code == 0, result.output
    assert out.is_file() and not (project / "script.mp3").exists()
    assert len(list(work.glob("*.wav"))) == 3  # cached parts
    assert not (project / "data" / "tts-work").exists()


@pytest.mark.ffmpeg
def test_default_work_dir_is_below_data_dir_and_reruns_use_the_cache(project, monkeypatch):
    first = FakeTTS()
    monkeypatch.setattr("content2podcast.cli.build_tts", lambda cfg, secrets: first)
    assert invoke("tts", "script.json").exit_code == 0
    assert len(first.calls) == 3
    assert len(list((project / "data" / "tts-work").glob("*.wav"))) == 3

    second = FakeTTS()
    monkeypatch.setattr("content2podcast.cli.build_tts", lambda cfg, secrets: second)
    assert invoke("tts", "script.json").exit_code == 0
    assert second.calls == []


@pytest.mark.ffmpeg
def test_intro_file_from_the_config_is_used(project):
    intro = project / "intro.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "sine=frequency=880:duration=2:sample_rate=48000", str(intro)],
        check=True,
    )  # fmt: skip
    write_config(project, extra="  intro_file: intro.wav\n")
    assert invoke("tts", "script.json").exit_code == 0
    duration = float(probe(project / "script.mp3", "format=duration")["format"]["duration"])
    assert duration == pytest.approx(2.0 + CHARS * 0.01 + 2 * 0.3, abs=0.3)


@pytest.mark.ffmpeg
def test_does_not_touch_a_database_or_feed(project):
    assert invoke("tts", "script.json").exit_code == 0
    assert not list((project / "data").glob("*.db"))
