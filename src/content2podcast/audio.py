"""Episode assembly with ffmpeg: decode, gaps, intro/outro, two-pass loudnorm, MP3 + ID3.

The ``*_command`` functions only build argument lists (testable without ffmpeg); the rest runs
them. Intermediate files live in a temporary directory inside the work directory and the final
file is written atomically.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from content2podcast.config import AudioConfig
from content2podcast.logging_setup import kv

log = logging.getLogger(__name__)

FFMPEG_BASE = ["-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
ERROR_TAIL_CHARS = 1500


class AudioError(Exception):
    """ffmpeg is missing or an audio step failed."""


@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str


@dataclass(frozen=True)
class EpisodeMetadata:
    """ID3 tags of the episode file."""

    title: str
    artist: str
    album: str
    date: str | None = None  # YYYY-MM-DD
    comment: str | None = None


@dataclass(frozen=True)
class AssemblyResult:
    path: Path
    duration_s: float
    size_bytes: int
    measured_lufs: float | None  # integrated loudness of the body before normalization


def find_tools() -> Tools:
    """Locate ``ffmpeg`` and ``ffprobe`` on ``PATH`` or raise a clear error."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    missing = [name for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not path]
    if missing:
        raise AudioError(
            f"{' and '.join(missing)} not found on PATH. Install ffmpeg "
            "(e.g. 'apt install ffmpeg'); the Docker image already contains it."
        )
    return Tools(ffmpeg, ffprobe)  # type: ignore[arg-type]


# --- command construction ----------------------------------------------------------------


def _pcm_filter(sample_rate: int) -> str:
    return f"aformat=sample_fmts=s16:sample_rates={sample_rate}:channel_layouts=mono"


def decode_command(ffmpeg: str, src: Path, dst: Path, sample_rate: int) -> list[str]:
    """Decode any audio file to mono 16-bit PCM WAV at ``sample_rate``."""
    return [
        ffmpeg, *FFMPEG_BASE, "-i", str(src), "-vn",
        "-af", _pcm_filter(sample_rate), "-c:a", "pcm_s16le", str(dst),
    ]  # fmt: skip


def silence_command(ffmpeg: str, dst: Path, duration_ms: int, sample_rate: int) -> list[str]:
    return [
        ffmpeg, *FFMPEG_BASE, "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
        "-t", f"{duration_ms / 1000:.3f}", "-c:a", "pcm_s16le", str(dst),
    ]  # fmt: skip


def concat_command(ffmpeg: str, list_file: Path, dst: Path) -> list[str]:
    return [ffmpeg, *FFMPEG_BASE, "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(dst)]  # fmt: skip


def concat_list(files: Sequence[Path]) -> str:
    """Content of a concat-demuxer list file (single quotes in paths are escaped)."""
    return "".join("file '" + str(f).replace("'", "'\\''") + "'\n" for f in files)


def _loudnorm_target(cfg: AudioConfig) -> str:
    return f"I={cfg.loudness_lufs:g}:TP={cfg.true_peak_db:g}:LRA={cfg.loudness_range:g}"


def measure_command(ffmpeg: str, src: Path, cfg: AudioConfig) -> list[str]:
    """Loudnorm pass 1: measure only (the JSON report is printed on stderr)."""
    filt = f"loudnorm={_loudnorm_target(cfg)}:print_format=json"
    return [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-i", str(src),
            "-af", filt, "-f", "null", "-"]  # fmt: skip


def encode_command(
    ffmpeg: str,
    src: Path,
    dst: Path,
    cfg: AudioConfig,
    meta: EpisodeMetadata,
    measured: dict[str, float] | None,
) -> list[str]:
    """Loudnorm pass 2 (with the measured values, ``linear=true``) and MP3 encoding with ID3.
    Without ``measured`` (silent input) the audio is only encoded."""
    filters = []
    if measured is not None:
        filters.append(
            f"loudnorm={_loudnorm_target(cfg)}"
            f":measured_I={measured['input_i']:g}:measured_TP={measured['input_tp']:g}"
            f":measured_LRA={measured['input_lra']:g}:measured_thresh={measured['input_thresh']:g}"
            f":offset={measured['target_offset']:g}:linear=true"
        )
    filters.append(f"aresample={cfg.sample_rate}")  # loudnorm works at 192 kHz internally
    tags = {
        "title": meta.title,
        "artist": meta.artist,
        "album": meta.album,
        "date": meta.date,
        "comment": meta.comment,
    }
    command = [
        ffmpeg, *FFMPEG_BASE, "-i", str(src), "-af", ",".join(filters),
        "-c:a", "libmp3lame", "-b:a", cfg.bitrate, "-ac", "1", "-ar", str(cfg.sample_rate),
        "-id3v2_version", "3", "-write_id3v1", "1",
    ]  # fmt: skip
    for key, value in tags.items():
        if value:
            command += ["-metadata", f"{key}={value}"]
    return [*command, "-f", "mp3", str(dst)]


def probe_duration_command(ffprobe: str, path: Path) -> list[str]:
    return [ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path)]  # fmt: skip


# --- running -----------------------------------------------------------------------------


def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a command; a failure becomes an :class:`AudioError` with the tail of stderr."""
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "").strip()[-ERROR_TAIL_CHARS:]
        raise AudioError(
            f"{Path(command[0]).name} failed (exit {exc.returncode}): {tail}"
        ) from None
    except OSError as exc:
        raise AudioError(f"Cannot run {command[0]}: {exc}") from None


def parse_loudnorm_json(stderr: str) -> dict[str, float]:
    """The measurement block printed by ``loudnorm=print_format=json``."""
    blocks = re.findall(r"\{[^{}]*\}", stderr, flags=re.DOTALL)
    for block in reversed(blocks):
        try:
            raw = json.loads(block)
            return {
                key: float(raw[key])
                for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
            }
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    raise AudioError("Could not read the loudnorm measurement from ffmpeg's output")


def measure_loudness(tools: Tools, src: Path, cfg: AudioConfig) -> dict[str, float]:
    """Integrated loudness, true peak and range of ``src`` (loudnorm pass 1)."""
    return parse_loudnorm_json(run(measure_command(tools.ffmpeg, src, cfg)).stderr)


def probe_duration(tools: Tools, path: Path) -> float:
    out = run(probe_duration_command(tools.ffprobe, path)).stdout.strip()
    try:
        return float(out)
    except ValueError:
        raise AudioError(f"ffprobe could not determine the duration of {path}") from None


def assemble_episode(
    segments: Sequence[Sequence[Path]],
    output: Path,
    work_dir: Path,
    *,
    cfg: AudioConfig,
    gap_ms: int,
    meta: EpisodeMetadata,
    intro: Path | None = None,
    outro: Path | None = None,
) -> AssemblyResult:
    """Build the finished MP3.

    ``segments`` holds the audio files of every spoken segment (several if a segment was split);
    ``gap_ms`` of silence separates segments, not the parts of one segment. ``intro`` / ``outro``
    may be any audio format. The body is loudness-normalized in two passes before the MP3 is
    encoded; ``output`` appears atomically.
    """
    tools = find_tools()
    parts = [p for segment in segments for p in segment]
    if not parts:
        raise AudioError("No audio to assemble")
    for path in [*parts, *(p for p in (intro, outro) if p)]:
        if not path.is_file():
            raise AudioError(f"Audio file not found: {path}")

    work_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_dir, prefix="assemble-") as tmp_name:
        tmp = Path(tmp_name)
        decoded: dict[Path, Path] = {}

        def decode(path: Path) -> Path:
            if path not in decoded:
                target = tmp / f"pcm-{len(decoded):04d}.wav"
                run(decode_command(tools.ffmpeg, path, target, cfg.sample_rate))
                decoded[path] = target
            return decoded[path]

        gap: Path | None = None
        if gap_ms > 0:
            gap = tmp / "gap.wav"
            run(silence_command(tools.ffmpeg, gap, gap_ms, cfg.sample_rate))

        sequence: list[Path] = []
        if intro:
            sequence.append(decode(intro))
        for index, segment in enumerate(segments):
            if index and gap:
                sequence.append(gap)
            sequence.extend(decode(p) for p in segment)
        if outro:
            sequence.append(decode(outro))

        list_file, body = tmp / "list.txt", tmp / "body.wav"
        list_file.write_text(concat_list(sequence), encoding="utf-8")
        run(concat_command(tools.ffmpeg, list_file, body))

        measured: dict[str, float] | None = measure_loudness(tools, body, cfg)
        measured_lufs = measured["input_i"]
        if measured_lufs == float("-inf") or measured_lufs < -70:  # silence: nothing to normalize
            log.warning("Audio is silent, skipping loudness normalization")
            measured = None

        part_file = output.with_name(output.name + ".part")
        try:
            run(encode_command(tools.ffmpeg, body, part_file, cfg, meta, measured))
            os.replace(part_file, output)
        finally:
            part_file.unlink(missing_ok=True)

    duration = probe_duration(tools, output)
    log.info(
        "Episode assembled: %s",
        kv(file=output.name, duration=f"{duration:.1f}s", measured_lufs=measured_lufs),
    )
    return AssemblyResult(output, duration, output.stat().st_size, measured_lufs)
