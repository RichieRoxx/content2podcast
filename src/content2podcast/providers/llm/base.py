"""LLM provider interface."""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The LLM call failed."""


class LLMRefusalError(LLMError):
    """The model refused to answer (content policy)."""


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def generate_structured(self, system: str, user: str, schema: type[T]) -> T:
        """Return the model's answer validated against the pydantic model ``schema``."""
        ...
