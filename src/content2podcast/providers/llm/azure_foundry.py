"""LLM provider ``azure_foundry``: Azure AI Foundry through the OpenAI-compatible v1 endpoint.

Configuration (``llm:`` section) and secrets (environment)::

    llm:
      provider: azure_foundry
      model: gpt-5.4            # deployment name
      reasoning_effort: low     # optional: none | minimal | low | medium | high

``AZURE_FOUNDRY_BASE_URL`` (e.g. ``https://<resource>.openai.azure.com/openai/v1/``) and
``AZURE_FOUNDRY_API_KEY`` come from the environment / ``.env``.

``temperature`` is never sent: reasoning models (gpt-5 family) may reject it.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import openai
from openai import OpenAI
from pydantic import Field, ValidationError

from content2podcast.config import Secrets
from content2podcast.logging_setup import kv
from content2podcast.providers.llm.base import LLMError, LLMRefusalError, T
from content2podcast.providers.registry import (
    ProviderNotConfiguredError,
    ProviderOptions,
    register_llm,
)

log = logging.getLogger(__name__)

PARSE_ATTEMPTS = 2  # one extra attempt if the answer cannot be parsed


class AzureFoundryOptions(ProviderOptions):
    provider: Literal["azure_foundry"] = "azure_foundry"
    model: str = "gpt-5.4"
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    # reasoning tokens count against this limit, so keep generous headroom
    max_completion_tokens: int | None = Field(16000, gt=0)
    timeout_s: float = Field(120.0, gt=0)
    max_retries: int = Field(2, ge=0)  # SDK-level retries for 429 / 5xx / connection errors


class AzureFoundryLLM:
    name = "azure_foundry"

    def __init__(self, options: AzureFoundryOptions, client: OpenAI):
        self.options = options
        self._client = client

    def __repr__(self) -> str:
        return f"AzureFoundryLLM(model={self.options.model!r})"

    def _request_args(self, system: str, user: str, schema: type[T]) -> dict:
        args: dict = {
            "model": self.options.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": schema,
        }
        if self.options.max_completion_tokens is not None:
            args["max_completion_tokens"] = self.options.max_completion_tokens
        if self.options.reasoning_effort is not None:
            args["reasoning_effort"] = self.options.reasoning_effort
        return args  # no temperature, on purpose

    def _log_usage(self, completion) -> None:
        usage = completion.usage
        if usage is None:
            return
        details = getattr(usage, "completion_tokens_details", None)
        log.info(
            "LLM usage: %s",
            kv(
                model=self.options.model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                reasoning_tokens=getattr(details, "reasoning_tokens", None) or 0,
                total_tokens=usage.total_tokens,
            ),
        )

    def generate_structured(self, system: str, user: str, schema: type[T]) -> T:
        args = self._request_args(system, user, schema)
        last_problem = "no answer"
        for attempt in range(1, PARSE_ATTEMPTS + 1):
            try:
                completion = self._client.chat.completions.parse(**args)
            except openai.LengthFinishReasonError:
                raise LLMError(
                    "The model hit max_completion_tokens before finishing; raise "
                    "'llm.max_completion_tokens' (reasoning tokens count against it)"
                ) from None
            except openai.ContentFilterFinishReasonError:
                raise LLMRefusalError("The response was blocked by the content filter") from None
            except ValidationError as exc:
                last_problem = f"answer does not match the schema ({exc.error_count()} errors)"
                log.warning("LLM answer unusable: %s", kv(attempt=attempt, problem=last_problem))
                continue
            except openai.APIStatusError as exc:
                raise LLMError(
                    f"LLM request failed with HTTP {exc.status_code}: {exc.message}"
                ) from exc
            except openai.APIError as exc:
                raise LLMError(f"LLM request failed: {type(exc).__name__}: {exc}") from exc

            self._log_usage(completion)
            if not completion.choices:
                raise LLMError("The LLM returned no choices")
            message = completion.choices[0].message
            if message.refusal:
                raise LLMRefusalError(message.refusal)
            if message.parsed is not None:
                return message.parsed
            last_problem = "empty or unparsable answer"
            log.warning("LLM answer unusable: %s", kv(attempt=attempt, problem=last_problem))
        raise LLMError(f"The LLM did not return a usable structured answer ({last_problem})")


def _client(
    options: AzureFoundryOptions, secrets: Secrets, http_client: Any | None = None
) -> OpenAI:
    """The SDK client; ``http_client`` lets tests plug in a mock transport."""
    missing = [
        env
        for env, value in (
            ("AZURE_FOUNDRY_BASE_URL", secrets.azure_foundry_base_url),
            ("AZURE_FOUNDRY_API_KEY", secrets.azure_foundry_api_key),
        )
        if not value
    ]
    if missing:
        raise ProviderNotConfiguredError(
            f"llm provider azure_foundry needs {' and '.join(missing)} (environment or .env)"
        )
    base_url = secrets.azure_foundry_base_url.rstrip("/") + "/"
    return OpenAI(
        base_url=base_url,
        api_key=secrets.azure_foundry_api_key.get_secret_value(),
        timeout=options.timeout_s,
        max_retries=options.max_retries,
        http_client=http_client,
    )


@register_llm("azure_foundry", options=AzureFoundryOptions)
def build_azure_foundry(options: AzureFoundryOptions, secrets: Secrets) -> AzureFoundryLLM:
    return AzureFoundryLLM(options, _client(options, secrets))
