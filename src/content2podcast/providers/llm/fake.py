"""Deterministic fake LLM for tests and dry runs: returns a canned answer, records calls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from content2podcast.config import Secrets
from content2podcast.providers.llm.base import LLMError, T
from content2podcast.providers.registry import ProviderOptions, register_llm


@dataclass
class LLMCall:
    system: str
    user: str
    schema: type


class FakeLLM:
    """``response`` is a mapping/model validated against the requested schema, or a callable
    ``(system, user, schema) -> mapping/model``. ``error`` makes every call raise it."""

    name = "fake"

    def __init__(
        self,
        response: Any | Callable[[str, str, type], Any] = None,
        error: Exception | None = None,
    ):
        self.response = response
        self.error = error
        self.calls: list[LLMCall] = []

    def generate_structured(self, system: str, user: str, schema: type[T]) -> T:
        self.calls.append(LLMCall(system, user, schema))
        if self.error is not None:
            raise self.error
        raw = self.response(system, user, schema) if callable(self.response) else self.response
        if raw is None:
            raise LLMError("FakeLLM has no canned response")
        if isinstance(raw, schema):
            return raw
        data = raw.model_dump() if hasattr(raw, "model_dump") else raw
        return schema.model_validate(data)


class FakeLLMOptions(ProviderOptions):
    provider: Literal["fake"] = "fake"
    response: dict[str, Any] | None = None


@register_llm("fake", options=FakeLLMOptions)
def build_fake_llm(options: FakeLLMOptions, secrets: Secrets) -> FakeLLM:
    return FakeLLM(response=options.response)
