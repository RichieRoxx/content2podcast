"""Provider registry: maps a provider name to its options model and factory."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict
from pydantic_core import PydanticCustomError

if TYPE_CHECKING:
    from content2podcast.config import Secrets
    from content2podcast.providers.llm.base import LLMProvider
    from content2podcast.providers.tts.base import TTSProvider

P = TypeVar("P")


class ProviderOptions(BaseModel):
    """Base class of provider options models; ``provider`` selects the registered provider."""

    model_config = ConfigDict(extra="forbid")

    provider: str


class ProviderNotConfiguredError(ValueError):
    """Raised when a provider is needed but none is configured."""


@dataclass(frozen=True)
class _Entry(Generic[P]):
    options: type[ProviderOptions]
    factory: Callable[[Any, Secrets], P]


class Registry(Generic[P]):
    """Registered providers of one kind (``llm`` or ``tts``)."""

    def __init__(self, kind: str):
        self.kind = kind
        self._entries: dict[str, _Entry[P]] = {}

    def register(
        self, name: str, options: type[ProviderOptions]
    ) -> Callable[[Callable[[Any, Secrets], P]], Callable[[Any, Secrets], P]]:
        def decorator(factory: Callable[[Any, Secrets], P]) -> Callable[[Any, Secrets], P]:
            if name in self._entries:
                raise ValueError(f"{self.kind} provider {name!r} is already registered")
            self._entries[name] = _Entry(options, factory)
            return factory

        return decorator

    def names(self) -> list[str]:
        load_builtin_providers()
        return sorted(self._entries)

    def options_model(self, name: str) -> type[ProviderOptions]:
        return self._entry(name).options

    def _entry(self, name: str) -> _Entry[P]:
        load_builtin_providers()
        try:
            return self._entries[name]
        except KeyError:
            raise PydanticCustomError(
                "unknown_provider",
                "Unknown {kind} provider '{name}'. Available: {available}",
                {"kind": self.kind, "name": name, "available": ", ".join(self.names()) or "none"},
            ) from None

    def parse(self, raw: Any) -> ProviderOptions | None:
        """Validate a raw config mapping with the options model chosen by its ``provider`` key.

        Used as the validator of the ``llm`` / ``tts`` config fields (a discriminated union
        over the registered providers). Errors keep their field paths, e.g. ``tts.region``.
        """
        if raw is None:
            return None
        if isinstance(raw, ProviderOptions):
            return raw
        if not isinstance(raw, dict):
            raise PydanticCustomError("provider_type", "Expected a mapping with a 'provider' key")
        if "provider" not in raw:
            raise PydanticCustomError(
                "provider_missing",
                "Missing 'provider'. Available {kind} providers: {available}",
                {"kind": self.kind, "available": ", ".join(self.names()) or "none"},
            )
        return self._entry(raw["provider"]).options.model_validate(raw)

    def build(self, options: ProviderOptions | None, secrets: Secrets) -> P:
        if options is None:
            raise ProviderNotConfiguredError(
                f"No {self.kind} provider configured. Set '{self.kind}.provider' "
                f"(available: {', '.join(self.names()) or 'none'})."
            )
        return self._entry(options.provider).factory(options, secrets)


llm_registry: Registry[LLMProvider] = Registry("llm")
tts_registry: Registry[TTSProvider] = Registry("tts")


def register_llm(name: str, *, options: type[ProviderOptions]):
    """Decorator registering an LLM provider factory ``(options, secrets) -> LLMProvider``."""
    return llm_registry.register(name, options)


def register_tts(name: str, *, options: type[ProviderOptions]):
    """Decorator registering a TTS provider factory ``(options, secrets) -> TTSProvider``."""
    return tts_registry.register(name, options)


def build_llm(options: ProviderOptions | None, secrets: Secrets) -> LLMProvider:
    return llm_registry.build(options, secrets)


def build_tts(options: ProviderOptions | None, secrets: Secrets) -> TTSProvider:
    return tts_registry.build(options, secrets)


def list_providers() -> dict[str, list[str]]:
    """Registered provider names by kind."""
    return {"llm": llm_registry.names(), "tts": tts_registry.names()}


_builtins_loaded = False


def load_builtin_providers() -> None:
    """Import every module in ``providers.llm`` and ``providers.tts`` so they register."""
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    for kind in ("llm", "tts"):
        package = importlib.import_module(f"content2podcast.providers.{kind}")
        for module in pkgutil.iter_modules(package.__path__):
            importlib.import_module(f"{package.__name__}.{module.name}")
