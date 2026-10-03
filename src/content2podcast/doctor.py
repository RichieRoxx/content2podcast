"""``podcast doctor``: is this installation ready to run? Read-only; nothing is created."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, SecretStr

from content2podcast.audio import AudioError, find_tools
from content2podcast.config import (
    AppConfig,
    ConfigError,
    Secrets,
    SourcesConfig,
    load_sources,
)
from content2podcast.db import db_path, latest_version
from content2podcast.providers.llm.base import LLMError, LLMProvider
from content2podcast.providers.registry import ProviderNotConfiguredError, build_llm, build_tts
from content2podcast.providers.tts.base import TTSError, TTSProvider, allowed_styles
from content2podcast.script.prompt import PromptError, load_template

Status = Literal["ok", "info", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


class _Ping(BaseModel):
    ok: bool


def ok(name: str, detail: str) -> Check:
    return Check(name, "ok", detail)


def fail(name: str, detail: str) -> Check:
    return Check(name, "fail", detail)


# --- offline checks ----------------------------------------------------------------------


def check_sources(config: AppConfig) -> tuple[Check, SourcesConfig | None]:
    try:
        sources = load_sources(config.paths.sources_file)
    except ConfigError as exc:
        return fail("sources", str(exc).replace("\n", " ")), None
    enabled = sum(s.enabled for s in sources.sources)
    status: Status = "ok" if enabled else "warn"
    detail = f"{len(sources.sources)} configured, {enabled} enabled ({config.paths.sources_file})"
    return Check("sources", status, detail), sources


def _mask(secret: SecretStr | None) -> str:
    """Only whether and how long, never the value."""
    value = secret.get_secret_value() if secret else ""
    return f"set ({len(value)} characters)" if value else "not set"


def check_secrets(secrets: Secrets) -> list[Check]:
    """Which credentials exist (values are never shown)."""
    key_set = secrets.azure_foundry_api_key is not None
    speech_key = secrets.azure_speech_key
    checks = [
        Check(
            "secret AZURE_FOUNDRY_BASE_URL",
            "ok" if secrets.azure_foundry_base_url else "info",
            secrets.azure_foundry_base_url or "not set",
        ),
        Check(
            "secret AZURE_FOUNDRY_API_KEY",
            "ok" if key_set else "info",
            _mask(secrets.azure_foundry_api_key),
        ),
    ]
    if speech_key is not None:
        speech = _mask(speech_key)
    elif key_set:
        speech = "not set (falls back to AZURE_FOUNDRY_API_KEY)"
    else:
        speech = "not set"
    checks.append(
        Check("secret AZURE_SPEECH_KEY", "ok" if speech_key or key_set else "info", speech)
    )
    checks.append(
        Check(
            "secret AZURE_SPEECH_REGION",
            "ok",
            f"{secrets.azure_speech_region}"
            + (
                f", endpoint {secrets.azure_speech_endpoint}"
                if secrets.azure_speech_endpoint
                else ""
            ),
        )
    )
    return checks


def check_providers(
    config: AppConfig, secrets: Secrets
) -> tuple[list[Check], LLMProvider | None, TTSProvider | None]:
    checks: list[Check] = []
    llm: LLMProvider | None = None
    tts: TTSProvider | None = None
    try:
        llm = build_llm(config.llm, secrets)
        model = getattr(config.llm, "model", None)
        checks.append(ok("llm provider", llm.name + (f", model {model}" if model else "")))
    except ProviderNotConfiguredError as exc:
        checks.append(fail("llm provider", str(exc)))
    try:
        tts = build_tts(config.tts, secrets)
    except ProviderNotConfiguredError as exc:
        checks.append(fail("tts provider", str(exc)))
    else:
        host, expert = config.roles.host, config.roles.expert
        styles = allowed_styles(tts, host.voice, expert.voice)
        checks.append(ok("tts provider", tts.name))
        checks.append(ok("voices", f"{host.name}: {host.voice}; {expert.name}: {expert.voice}"))
        checks.append(ok("speaking styles", ", ".join(styles)))
    return checks, llm, tts


def _first_line(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0] if lines else "(no output)"


def check_ffmpeg() -> list[Check]:
    try:
        tools = find_tools()
    except AudioError as exc:
        return [fail("ffmpeg", str(exc))]
    return [
        ok("ffmpeg", _first_line([tools.ffmpeg, "-version"])),
        ok("ffprobe", _first_line([tools.ffprobe, "-version"])),
    ]


def _writable(directory: Path) -> tuple[bool, str]:
    """Writable now, or creatable (the nearest existing parent is writable)."""
    if directory.exists():
        if not directory.is_dir():
            return False, "exists but is not a directory"
        try:
            with tempfile.NamedTemporaryFile(dir=directory):
                pass
        except OSError as exc:
            return False, f"not writable ({exc.strerror})"
        return True, "writable"
    parent = directory.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if parent.is_dir() and os.access(parent, os.W_OK):
        return True, "does not exist yet, will be created"
    return False, f"does not exist and cannot be created below {parent}"


def check_directories(config: AppConfig) -> list[Check]:
    checks = []
    for label, directory in (
        ("data dir", config.paths.data_dir),
        ("output dir", config.paths.output_dir),
    ):
        good, message = _writable(directory)
        checks.append(Check(label, "ok" if good else "fail", f"{directory}: {message}"))
    return checks


def check_database(config: AppConfig) -> Check:
    """Schema version of an existing database, read-only (a missing one is fine)."""
    path = db_path(config.paths.data_dir)
    if not path.is_file():
        return ok("database", f"not created yet ({path})")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fail("database", f"cannot open {path}: {exc}")
    latest = latest_version()
    if version > latest:
        return fail("database", f"schema v{version} is newer than this program (v{latest})")
    if version < latest:
        return Check(
            "database", "warn", f"schema v{version}, v{latest} will be applied on the next run"
        )
    return ok("database", f"schema v{version} (current), {path}")


def check_files(config: AppConfig) -> list[Check]:
    checks: list[Check] = []
    for label, path in (
        ("cover image", config.podcast.cover_image),
        ("intro file", config.episode.intro_file),
        ("outro file", config.episode.outro_file),
    ):
        if path is not None:
            checks.append(
                ok(label, str(path)) if path.is_file() else fail(label, f"not found: {path}")
            )
    try:
        load_template(config.episode.mode, config.podcast.language, config.episode.prompts_dir)
        checks.append(ok("prompt template", f"{config.episode.mode} / {config.podcast.language}"))
    except PromptError as exc:
        checks.append(fail("prompt template", str(exc)))
    return checks


# --- online checks -----------------------------------------------------------------------


def check_online(
    config: AppConfig, llm: LLMProvider | None, tts: TTSProvider | None
) -> list[Check]:
    """One minimal request per provider (costs a few tokens and a second of speech)."""
    checks: list[Check] = []
    if llm is not None:
        try:
            llm.generate_structured("Answer with ok=true.", "ping", _Ping)
            checks.append(ok("llm request", f"{llm.name} answered"))
        except LLMError as exc:
            checks.append(fail("llm request", str(exc)))
    if tts is not None:
        try:
            chunk = tts.synthesize("Test.", config.roles.host.voice, "neutral")
            checks.append(
                ok("tts request", f"{tts.name} returned {len(chunk.data)} bytes of {chunk.ext}")
            )
        except TTSError as exc:
            checks.append(fail("tts request", str(exc)))
    return checks


# --- all together ------------------------------------------------------------------------


def run_doctor(
    load_config: Callable[[], AppConfig],
    load_secrets: Callable[[], Secrets],
    *,
    config_description: str,
    online: bool = False,
) -> list[Check]:
    """Run every check; a broken configuration still gets the checks that do not depend on it."""
    checks: list[Check] = []
    config: AppConfig | None
    try:
        config = load_config()
        paths = f"data_dir={config.paths.data_dir}, output_dir={config.paths.output_dir}"
        checks.append(ok("config", f"{config_description}; {paths}"))
    except ConfigError as exc:
        config = None
        checks.append(fail("config", str(exc).replace("\n", " ")))
    checks.extend(check_ffmpeg())
    if config is None:
        return checks

    source_check, _ = check_sources(config)
    checks.append(source_check)
    try:
        secrets = load_secrets()
    except ConfigError as exc:
        checks.append(fail("secrets", str(exc).replace("\n", " ")))
        return checks
    checks.extend(check_secrets(secrets))
    provider_checks, llm, tts = check_providers(config, secrets)
    checks.extend(provider_checks)
    checks.extend(check_directories(config))
    checks.append(check_database(config))
    checks.extend(check_files(config))
    if online:
        checks.extend(check_online(config, llm, tts))
    return checks
