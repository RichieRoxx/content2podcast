"""Typed configuration with file + environment layering.

Precedence (highest first): CLI overrides > environment (``C2P_`` prefix, ``__`` nesting)
> ``.env`` > ``config.yaml`` > defaults.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainValidator,
    SecretStr,
    SerializeAsAny,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from content2podcast.providers.registry import ProviderOptions, llm_registry, tts_registry

CONFIG_ENV_VAR = "C2P_CONFIG"
DEFAULT_CONFIG_FILE = "config.yaml"
ENV_PREFIX = "C2P_"


class ConfigError(Exception):
    """A configuration problem, with a human-readable message (no stack trace needed)."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PodcastConfig(_Model):
    title: str = "content2podcast"
    description: str = "Articles turned into podcast episodes."
    author: str = "content2podcast"
    language: str = "de-DE"
    cover_image: Path | None = None
    category: str = "Technology"
    explicit: bool = False


class PathsConfig(_Model):
    data_dir: Path = Path("data")
    output_dir: Path = Path("public")
    sources_file: Path = Path("sources.yaml")


class RetentionConfig(_Model):
    max_age_days: int | None = Field(30, gt=0)
    max_episodes: int | None = Field(None, gt=0)


class FeedConfig(_Model):
    base_url: str = "http://localhost:8080"
    retention: RetentionConfig = RetentionConfig()


class EpisodeConfig(_Model):
    mode: Literal["per_article", "daily_digest"] = "per_article"
    min_minutes: float = Field(3, gt=0)
    max_minutes: float = Field(10, gt=0)
    length_factor: float = Field(1.0, gt=0)
    words_per_minute: int = Field(140, gt=0)
    target_minutes: float = Field(15, gt=0)
    max_articles: int = Field(10, gt=0)
    max_article_age_days: int = Field(7, gt=0)
    max_chars_per_article: int = Field(8000, gt=0)
    min_chars_per_article: int = Field(300, gt=0)  # shorter extractions fall back to the summary
    max_episodes_per_run: int | None = Field(None, gt=0)
    gap_ms: int = Field(300, ge=0)
    intro_file: Path | None = None
    outro_file: Path | None = None

    @model_validator(mode="after")
    def _check_minutes(self) -> EpisodeConfig:
        if self.min_minutes > self.max_minutes:
            raise ValueError("min_minutes must not exceed max_minutes")
        return self


class RoleConfig(_Model):
    name: str
    voice: str
    description: str = ""


class RolesConfig(_Model):
    host: RoleConfig = RoleConfig(
        name="Mia", voice="de-DE-Mia:MAI-Voice-2.1", description="Curious, friendly host."
    )
    expert: RoleConfig = RoleConfig(
        name="Klaus", voice="de-DE-Klaus:MAI-Voice-2.1", description="Knowledgeable expert."
    )


class AudioConfig(_Model):
    loudness_lufs: float = -16.0
    true_peak_db: float = -1.5
    bitrate: str = "128k"


class HttpConfig(_Model):
    user_agent: str | None = None  # None: content2podcast/<version> (+repo URL)
    connect_timeout: float = Field(10.0, gt=0)
    read_timeout: float = Field(30.0, gt=0)


class ScheduleConfig(_Model):
    time: str = "05:30"

    @field_validator("time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("must be HH:MM (24h)")
        return v


# Fields holding filesystem paths that are resolved against the config directory.
_PATH_FIELDS: tuple[tuple[str, str], ...] = (
    ("paths", "data_dir"),
    ("paths", "output_dir"),
    ("paths", "sources_file"),
    ("podcast", "cover_image"),
    ("episode", "intro_file"),
    ("episode", "outro_file"),
)


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="ignore",  # .env also carries unrelated keys (e.g. AZURE_*); see _check_top_level
        yaml_file=None,
        env_file=None,
        env_file_encoding="utf-8",
    )

    podcast: PodcastConfig = PodcastConfig()
    paths: PathsConfig = PathsConfig()
    feed: FeedConfig = FeedConfig()
    episode: EpisodeConfig = EpisodeConfig()
    roles: RolesConfig = RolesConfig()
    audio: AudioConfig = AudioConfig()
    http: HttpConfig = HttpConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    # Discriminated on ``provider``: validated against the options model of the registered
    # provider, see content2podcast.providers. None = not configured.
    llm: Annotated[SerializeAsAny[ProviderOptions] | None, PlainValidator(llm_registry.parse)] = (
        None
    )
    tts: Annotated[SerializeAsAny[ProviderOptions] | None, PlainValidator(tts_registry.parse)] = (
        None
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
        )


class Secrets(BaseSettings):
    """Credentials from the environment / ``.env``. ``SecretStr`` keeps them out of repr/logs."""

    model_config = SettingsConfigDict(env_file=None, env_file_encoding="utf-8", extra="ignore")

    azure_foundry_base_url: str | None = None
    azure_foundry_api_key: SecretStr | None = None
    azure_speech_key: SecretStr | None = None
    azure_speech_region: str = "swedencentral"
    azure_speech_endpoint: str | None = None

    @property
    def effective_speech_key(self) -> SecretStr | None:
        """Speech key, falling back to the Foundry key."""
        return self.azure_speech_key or self.azure_foundry_api_key


def resolve_config_path(cli_path: Path | str | None = None) -> Path:
    """``--config`` > ``C2P_CONFIG`` > ``./config.yaml``."""
    if cli_path is not None:
        return Path(cli_path)
    if env := os.environ.get(CONFIG_ENV_VAR):
        return Path(env)
    return Path(DEFAULT_CONFIG_FILE)


def _format_errors(exc: ValidationError, source: str) -> str:
    lines = [f"Invalid configuration ({source}):"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def _resolve_paths(cfg: AppConfig, base: Path) -> AppConfig:
    for section, name in _PATH_FIELDS:
        sub = getattr(cfg, section)
        value = getattr(sub, name)
        if value is not None and not value.is_absolute():
            setattr(sub, name, (base / value).resolve())
    return cfg


def _check_top_level(path: Path) -> None:
    """Reject unknown top-level keys in the YAML file (``extra="ignore"`` would hide typos)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Cannot parse {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"Invalid configuration ({path}): top level must be a mapping")
    unknown = sorted(set(data) - set(AppConfig.model_fields))
    if unknown:
        raise ConfigError(
            f"Invalid configuration ({path}):\n  unknown top-level key(s): {', '.join(unknown)}"
        )


def load_config(
    config_path: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
    env_file: Path | str | None = None,
) -> AppConfig:
    """Load the layered configuration.

    ``config_path`` follows :func:`resolve_config_path`. A missing default config file is fine
    (defaults + environment apply); a missing explicitly requested one is an error. The ``.env``
    file defaults to ``.env`` next to the config file. Relative paths resolve against the config
    file's directory.
    """
    explicit = config_path is not None or CONFIG_ENV_VAR in os.environ
    path = resolve_config_path(config_path)
    if explicit and not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    base = path.parent.resolve()
    has_file = path.is_file()
    dotenv = Path(env_file) if env_file is not None else base / ".env"

    if has_file:
        _check_top_level(path)

    class _Loaded(AppConfig):
        model_config = SettingsConfigDict(
            yaml_file=str(path) if has_file else None,
            env_file=str(dotenv),
        )

    try:
        cfg = _Loaded(**(overrides or {}))
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc, str(path) if has_file else "environment")) from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"Cannot parse {path}: {exc}") from None
    return _resolve_paths(cfg, base)


def load_secrets(env_file: Path | str | None = None) -> Secrets:
    """Load credentials from the environment, with an optional ``.env`` file."""
    try:
        return Secrets(_env_file=env_file)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc, "environment")) from None


class SourceConfig(_Model):
    name: str = Field(min_length=1)
    url: str
    type: Literal["rss", "html"] = "rss"
    selector: str | None = None
    include: list[str] = []
    exclude: list[str] = []
    same_site: bool = True  # html: only keep links on the page's own site
    enabled: bool = True

    @field_validator("include", "exclude")
    @classmethod
    def _check_regex(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from None
        return patterns


class SourcesConfig(_Model):
    sources: list[SourceConfig] = []

    @model_validator(mode="after")
    def _unique_names(self) -> SourcesConfig:
        seen: set[str] = set()
        for source in self.sources:
            if source.name in seen:
                raise ValueError(f"duplicate source name: {source.name!r}")
            seen.add(source.name)
        return self


def load_sources(path: Path | str) -> SourcesConfig:
    """Load and validate ``sources.yaml``."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"Sources file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Cannot parse {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"Invalid sources file ({path}): top level must be a mapping")
    try:
        return SourcesConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc, str(path))) from None
