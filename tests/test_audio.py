import json
import subprocess
from pathlib import Path

import pytest

from content2podcast import audio
from content2podcast.audio import (
    AudioError,
    EpisodeMetadata,
    Tools,
    assemble_episode,
    concat_command,
    concat_list,
    decode_command,
    encode_command,
    find_tools,
    measure_command,
    measure_loudness,
    parse_loudnorm_json,
    probe_duration,
    probe_duration_command,
    silence_command,
)
from content2podcast.config import AudioConfig

CFG = AudioConfig()
META = EpisodeMetadata(
    title="Folge 1: Äpfel & Birnen",
    artist="Mein Podcast",
    album="Mein Podcast",
    date="2026-10-03",
    comment="Quellen: https://example.com/a",
)
MEASURED = {
    "input_i": -23.5,
    "input_tp": -6.2,
    "input_lra": 3.4,
    "input_thresh": -34.1,
    "target_offset": 0.8,
}

# --- command construction (no ffmpeg needed) ---------------------------------------------


def test_decode_command_normalizes_to_mono_pcm_at_the_common_rate():
    cmd = decode_command("ffmpeg", Path("in.mp3"), Path("out.wav"), 44100)
    assert cmd[0] == "ffmpeg" and cmd[-1] == "out.wav"
    assert cmd[cmd.index("-i") + 1] == "in.mp3"
    assert "aformat=sample_fmts=s16:sample_rates=44100:channel_layouts=mono" in cmd
    assert cmd[cmd.index("-c:a") + 1] == "pcm_s16le"
    assert "-nostdin" in cmd and "-y" in cmd


def test_silence_command_uses_the_gap_in_seconds():
    cmd = silence_command("ffmpeg", Path("gap.wav"), 300, 44100)
    assert cmd[cmd.index("-t") + 1] == "0.300"
    assert "anullsrc=r=44100:cl=mono" in cmd


def test_concat_command_and_list_escaping():
    cmd = concat_command("ffmpeg", Path("list.txt"), Path("body.wav"))
    assert cmd[cmd.index("-f") + 1] == "concat" and cmd[cmd.index("-c") + 1] == "copy"
    assert "-safe" in cmd
    listing = concat_list([Path("/tmp/a.wav"), Path("/tmp/it's.wav")])
    assert listing == "file '/tmp/a.wav'\nfile '/tmp/it'\\''s.wav'\n"


def test_measure_command_targets_and_json():
    cmd = measure_command("ffmpeg", Path("body.wav"), CFG)
    filt = cmd[cmd.index("-af") + 1]
    assert filt == "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json"
    assert cmd[-3:] == ["-f", "null", "-"]


def test_encode_command_second_pass_uses_measured_values_and_linear_mode():
    cmd = encode_command("ffmpeg", Path("body.wav"), Path("out.mp3"), CFG, META, MEASURED)
    filt = cmd[cmd.index("-af") + 1]
    assert filt.startswith("loudnorm=I=-16:TP=-1.5:LRA=11:")
    for part in (
        "measured_I=-23.5",
        "measured_TP=-6.2",
        "measured_LRA=3.4",
        "measured_thresh=-34.1",
        "offset=0.8",
        "linear=true",
    ):
        assert part in filt
    assert filt.endswith(",aresample=44100")
    assert cmd[cmd.index("-c:a") + 1] == "libmp3lame"
    assert cmd[cmd.index("-b:a") + 1] == "128k"
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-id3v2_version") + 1] == "3"
    assert cmd[-3:] == ["-f", "mp3", "out.mp3"]


def test_encode_command_id3_tags_are_passed_as_separate_arguments():
    cmd = encode_command("ffmpeg", Path("b.wav"), Path("o.mp3"), CFG, META, MEASURED)
    tags = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-metadata"]
    assert tags == [
        "title=Folge 1: Äpfel & Birnen",
        "artist=Mein Podcast",
        "album=Mein Podcast",
        "date=2026-10-03",
        "comment=Quellen: https://example.com/a",
    ]


def test_encode_command_skips_empty_tags_and_loudnorm_for_silence():
    meta = EpisodeMetadata(title="T", artist="A", album="A")
    cmd = encode_command("ffmpeg", Path("b.wav"), Path("o.mp3"), CFG, meta, None)
    assert cmd[cmd.index("-af") + 1] == "aresample=44100"
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "-metadata"] == [
        "title=T",
        "artist=A",
        "album=A",
    ]


def test_encode_command_uses_configured_bitrate_and_targets():
    cfg = AudioConfig(loudness_lufs=-18, true_peak_db=-2, loudness_range=7, bitrate="96k")
    cmd = encode_command("ffmpeg", Path("b.wav"), Path("o.mp3"), cfg, META, MEASURED)
    assert cmd[cmd.index("-b:a") + 1] == "96k"
    assert "loudnorm=I=-18:TP=-2:LRA=7:" in cmd[cmd.index("-af") + 1]


def test_probe_command():
    cmd = probe_duration_command("ffprobe", Path("x.mp3"))
    assert cmd[0] == "ffprobe" and cmd[-1] == "x.mp3" and "format=duration" in cmd


SAMPLE_STDERR = """[Parsed_loudnorm_0 @ 0x55d] 
{
	"input_i" : "-23.41",
	"input_tp" : "-6.20",
	"input_lra" : "3.40",
	"input_thresh" : "-34.00",
	"output_i" : "-16.12",
	"output_tp" : "-1.50",
	"output_lra" : "2.10",
	"output_thresh" : "-26.55",
	"normalization_type" : "dynamic",
	"target_offset" : "0.12"
}
"""


def test_parse_loudnorm_json():
    parsed = parse_loudnorm_json("size=N/A time=00:00:05.00\n" + SAMPLE_STDERR)
    assert parsed == {
        "input_i": -23.41,
        "input_tp": -6.2,
        "input_lra": 3.4,
        "input_thresh": -34.0,
        "target_offset": 0.12,
    }


def test_parse_loudnorm_json_handles_silence_and_garbage():
    silent = SAMPLE_STDERR.replace('"-23.41"', '"-inf"')
    assert parse_loudnorm_json(silent)["input_i"] == float("-inf")
    with pytest.raises(AudioError, match="loudnorm measurement"):
        parse_loudnorm_json("no json here")


def test_find_tools_reports_missing_binaries(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda name: None)
    with pytest.raises(AudioError) as exc:
        find_tools()
    assert "ffmpeg and ffprobe not found on PATH" in str(exc.value)
    monkeypatch.setattr(
        audio.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
    )
    with pytest.raises(AudioError, match="^ffprobe not found"):
        find_tools()


def test_run_turns_failures_into_audio_errors():
    with pytest.raises(AudioError, match="exit 3.*boom"):
        audio.run(["sh", "-c", "echo boom >&2; exit 3"])
    with pytest.raises(AudioError, match="Cannot run"):
        audio.run(["/nonexistent/ffmpeg"])


def test_assemble_without_audio_or_with_missing_files(monkeypatch, tmp_path):
    monkeypatch.setattr(audio, "find_tools", lambda: Tools("ffmpeg", "ffprobe"))
    kwargs = {"cfg": CFG, "gap_ms": 300, "meta": META}
    with pytest.raises(AudioError, match="No audio"):
        assemble_episode([], tmp_path / "o.mp3", tmp_path / "w", **kwargs)
    with pytest.raises(AudioError, match="not found"):
        assemble_episode([[tmp_path / "missing.wav"]], tmp_path / "o.mp3", tmp_path / "w", **kwargs)


# --- real ffmpeg --------------------------------------------------------------------------


def tone(path: Path, seconds: float, *, rate=44100, channels=1, freq=440, volume=0.5) -> Path:
    layout = "stereo" if channels == 2 else "mono"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate={rate}",
            "-af", f"volume={volume},aformat=channel_layouts={layout}", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def tags_of(path: Path) -> dict[str, str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return {k.lower(): v for k, v in json.loads(out.stdout)["format"]["tags"].items()}


@pytest.fixture
def tools():
    return find_tools()


@pytest.mark.ffmpeg
def test_assembles_segments_with_gaps_at_the_target_loudness(tmp_path, tools):
    a = tone(tmp_path / "a.wav", 2.0, rate=22050, volume=0.1)
    b = tone(tmp_path / "b.wav", 2.0, rate=24000, freq=660, volume=0.8)
    c = tone(tmp_path / "c.wav", 1.5, rate=44100, freq=330)
    out = tmp_path / "out" / "episode.mp3"
    result = assemble_episode(
        [[a], [b], [c]], out, tmp_path / "work", cfg=CFG, gap_ms=500, meta=META
    )
    assert out.is_file() and result.path == out and result.size_bytes == out.stat().st_size
    expected = 2.0 + 2.0 + 1.5 + 2 * 0.5  # two gaps between three segments
    assert result.duration_s == pytest.approx(expected, abs=0.2)
    assert probe_duration(tools, out) == pytest.approx(result.duration_s)
    loudness = measure_loudness(tools, out, CFG)
    assert loudness["input_i"] == pytest.approx(-16.0, abs=1.0)
    assert loudness["input_tp"] <= -1.0  # true peak stays under the ceiling (plus MP3 slack)
    assert not list((tmp_path / "out").glob("*.part"))
    assert not list((tmp_path / "work").glob("assemble-*"))  # temp files cleaned up


@pytest.mark.ffmpeg
def test_parts_of_one_segment_get_no_gap(tmp_path):
    p1, p2 = tone(tmp_path / "1.wav", 2.0), tone(tmp_path / "2.wav", 2.0, freq=550)
    split = assemble_episode(
        [[p1, p2]], tmp_path / "split.mp3", tmp_path / "w", cfg=CFG, gap_ms=1000, meta=META
    )
    separate = assemble_episode(
        [[p1], [p2]], tmp_path / "sep.mp3", tmp_path / "w", cfg=CFG, gap_ms=1000, meta=META
    )
    assert split.duration_s == pytest.approx(4.0, abs=0.2)
    assert separate.duration_s == pytest.approx(5.0, abs=0.2)


@pytest.mark.ffmpeg
def test_zero_gap(tmp_path):
    a, b = tone(tmp_path / "a.wav", 2.0), tone(tmp_path / "b.wav", 2.0)
    result = assemble_episode(
        [[a], [b]], tmp_path / "o.mp3", tmp_path / "w", cfg=CFG, gap_ms=0, meta=META
    )
    assert result.duration_s == pytest.approx(4.0, abs=0.2)


@pytest.mark.ffmpeg
def test_intro_and_outro_with_other_rates_channels_and_formats(tmp_path):
    body = tone(tmp_path / "body.wav", 3.0, rate=24000)
    intro = tone(tmp_path / "intro.wav", 1.0, rate=48000, channels=2, freq=880)
    outro_wav = tone(tmp_path / "outro.wav", 1.5, rate=16000, freq=220)
    outro = tmp_path / "outro.mp3"  # a different container/codec as well
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(outro_wav), str(outro)],
        check=True,
    )
    result = assemble_episode(
        [[body]], tmp_path / "o.mp3", tmp_path / "w", cfg=CFG, gap_ms=300, meta=META,
        intro=intro, outro=outro,
    )  # fmt: skip
    assert result.duration_s == pytest.approx(1.0 + 3.0 + 1.5, abs=0.25)


@pytest.mark.ffmpeg
def test_id3_tags_and_mp3_properties(tmp_path, tools):
    a = tone(tmp_path / "a.wav", 3.0, channels=2)
    out = tmp_path / "o.mp3"
    assemble_episode(
        [[a]], out, tmp_path / "w", cfg=AudioConfig(bitrate="96k"), gap_ms=0, meta=META
    )
    tags = tags_of(out)
    assert tags["title"] == "Folge 1: Äpfel & Birnen"
    assert tags["artist"] == "Mein Podcast" and tags["album"] == "Mein Podcast"
    assert tags["date"].startswith("2026")  # ID3v2.3 may keep only the year, newer ffmpeg all
    assert tags["comment"] == "Quellen: https://example.com/a"
    info = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-of", "json", "-show_entries",
             "stream=codec_name,channels,sample_rate,bit_rate", str(out)],
            capture_output=True, text=True, check=True,
        ).stdout
    )["streams"][0]  # fmt: skip
    assert (info["codec_name"], info["channels"], info["sample_rate"]) == ("mp3", 1, "44100")
    assert int(info["bit_rate"]) == pytest.approx(96000, rel=0.1)


@pytest.mark.ffmpeg
def test_silent_audio_is_encoded_without_normalization(tmp_path, caplog):
    silent = tmp_path / "silent.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "anullsrc=r=24000:cl=mono", "-t", "2", str(silent)],
        check=True,
    )  # fmt: skip
    result = assemble_episode(
        [[silent]], tmp_path / "o.mp3", tmp_path / "w", cfg=CFG, gap_ms=0, meta=META
    )
    assert result.duration_s == pytest.approx(2.0, abs=0.2)
    assert any("silent" in r.message for r in caplog.records)


@pytest.mark.ffmpeg
def test_broken_input_gives_an_audio_error_and_leaves_no_output(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"this is not audio")
    out = tmp_path / "o.mp3"
    with pytest.raises(AudioError, match="ffmpeg failed"):
        assemble_episode([[bad]], out, tmp_path / "w", cfg=CFG, gap_ms=0, meta=META)
    assert not out.exists() and not list(tmp_path.glob("*.part"))
