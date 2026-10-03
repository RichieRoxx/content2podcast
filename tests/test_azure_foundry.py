import json
import logging

import httpx2
import pytest
from pydantic import SecretStr

from content2podcast.config import Secrets, load_config
from content2podcast.providers.llm.azure_foundry import (
    AzureFoundryLLM,
    AzureFoundryOptions,
    _client,
)
from content2podcast.providers.llm.base import LLMError, LLMProvider, LLMRefusalError
from content2podcast.providers.registry import (
    ProviderNotConfiguredError,
    build_llm,
    list_providers,
)
from content2podcast.script.models import build_llm_schema

BASE = "https://res.openai.azure.com/openai/v1"
URL = f"{BASE}/chat/completions"
KEY = "super-secret-key-123"
STYLES = ["neutral", "cheerful", "serious"]
SCRIPT = {
    "title": "Titel",
    "summary": "Zusammenfassung.",
    "segments": [
        {"speaker": "host", "style": "cheerful", "text": "Hallo!"},
        {"speaker": "expert", "style": "neutral", "text": "Guten Tag."},
    ],
}


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr("openai._base_client.time.sleep", lambda seconds: None)


@pytest.fixture
def schema():
    return build_llm_schema(STYLES)


def secrets(base_url=BASE + "/"):
    return Secrets(azure_foundry_base_url=base_url, azure_foundry_api_key=SecretStr(KEY))


class FakeAzure:
    """Mock transport for the SDK's HTTP client: answers from a queue (the last answer repeats)
    and records every request. (The openai 3.x SDK uses ``httpx2``, which respx cannot patch.)"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def body(self, index=0) -> dict:
        return json.loads(self.requests[index].content)


def make(azure: FakeAzure, base_url=BASE + "/", **options) -> AzureFoundryLLM:
    http_client = httpx2.Client(transport=httpx2.MockTransport(azure))
    opts = AzureFoundryOptions(**options)
    return AzureFoundryLLM(opts, _client(opts, secrets(base_url), http_client=http_client))


def completion(content=None, *, refusal=None, finish_reason="stop", usage=True):
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-5.4",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(content) if isinstance(content, dict) else content,
                    "refusal": refusal,
                },
            }
        ],
    }
    if usage:
        body["usage"] = {
            "prompt_tokens": 1200,
            "completion_tokens": 800,
            "total_tokens": 2000,
            "completion_tokens_details": {"reasoning_tokens": 300},
        }
    return httpx2.Response(200, json=body)


def ok(**kwargs):
    return completion(SCRIPT, **kwargs)


def test_request_has_no_temperature_and_carries_schema_with_style_enum(schema):
    azure = FakeAzure(ok())
    make(azure).generate_structured("SYS", "USER", schema)
    body = azure.body()
    assert "temperature" not in body
    assert body["model"] == "gpt-5.4"
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert body["max_completion_tokens"] == 16000
    assert "reasoning_effort" not in body
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    schema_json = json.dumps(fmt["json_schema"]["schema"])
    assert '"enum": ["neutral", "cheerful", "serious"]' in schema_json
    assert '"enum": ["host", "expert"]' in schema_json
    assert "sources" not in schema_json


def test_base_url_and_auth_header(schema):
    azure = FakeAzure(ok())
    make(azure).generate_structured("s", "u", schema)
    request = azure.requests[0]
    assert str(request.url) == URL
    assert request.method == "POST"
    assert request.headers["authorization"] == f"Bearer {KEY}"


def test_base_url_without_trailing_slash_works(schema):
    azure = FakeAzure(ok())
    make(azure, base_url=BASE).generate_structured("s", "u", schema)
    assert str(azure.requests[0].url) == URL


def test_returns_parsed_instance_of_the_schema(schema):
    result = make(FakeAzure(ok())).generate_structured("s", "u", schema)
    assert isinstance(result, schema)
    assert result.title == "Titel" and result.segments[0].style == "cheerful"


def test_options_are_sent(schema):
    azure = FakeAzure(ok())
    make(
        azure, model="my-deployment", reasoning_effort="low", max_completion_tokens=5000
    ).generate_structured("s", "u", schema)
    body = azure.body()
    assert (body["model"], body["reasoning_effort"], body["max_completion_tokens"]) == (
        "my-deployment",
        "low",
        5000,
    )
    assert "temperature" not in body


def test_refusal_raises_refusal_error(schema):
    azure = FakeAzure(completion(None, refusal="Dazu kann ich nichts sagen."))
    with pytest.raises(LLMRefusalError, match="Dazu kann ich nichts sagen"):
        make(azure).generate_structured("s", "u", schema)


def test_content_filter_is_a_refusal(schema):
    azure = FakeAzure(completion(None, finish_reason="content_filter"))
    with pytest.raises(LLMRefusalError, match="content filter"):
        make(azure).generate_structured("s", "u", schema)


def test_one_extra_attempt_when_the_answer_cannot_be_parsed(schema, caplog):
    azure = FakeAzure(completion("not json"), ok())
    with caplog.at_level(logging.WARNING):
        result = make(azure).generate_structured("s", "u", schema)
    assert result.title == "Titel" and azure.call_count == 2
    assert any("unusable" in r.message for r in caplog.records)


def test_schema_violation_counts_as_parse_failure(schema):
    bad = {**SCRIPT, "segments": [{"speaker": "host", "style": "angry", "text": "x"}]}
    azure = FakeAzure(completion(bad), ok())
    assert make(azure).generate_structured("s", "u", schema).title == "Titel"
    assert azure.call_count == 2


def test_two_unusable_answers_raise_llm_error(schema):
    azure = FakeAzure(completion("still not json"))
    with pytest.raises(LLMError, match="usable structured answer"):
        make(azure).generate_structured("s", "u", schema)
    assert azure.call_count == 2  # not more


def test_empty_content_without_refusal_is_an_error(schema):
    with pytest.raises(LLMError):
        make(FakeAzure(completion(None))).generate_structured("s", "u", schema)


def test_no_choices_is_an_error(schema):
    body = {"id": "x", "object": "chat.completion", "created": 1, "model": "m", "choices": []}
    with pytest.raises(LLMError, match="no choices"):
        make(FakeAzure(httpx2.Response(200, json=body))).generate_structured("s", "u", schema)


def test_length_limit_gives_an_actionable_error(schema):
    azure = FakeAzure(completion('{"title": "abge', finish_reason="length"))
    with pytest.raises(LLMError, match="max_completion_tokens"):
        make(azure).generate_structured("s", "u", schema)


def test_429_is_retried(schema):
    azure = FakeAzure(
        httpx2.Response(429, headers={"retry-after": "1"}, json={"error": {"message": "slow"}}),
        ok(),
    )
    assert make(azure).generate_structured("s", "u", schema).title == "Titel"
    assert azure.call_count == 2


def test_5xx_is_retried_then_reported(schema):
    azure = FakeAzure(httpx2.Response(503, json={"error": {"message": "x"}}))
    with pytest.raises(LLMError, match="HTTP 503"):
        make(azure, max_retries=2).generate_structured("s", "u", schema)
    assert azure.call_count == 3  # first try + 2 retries


def test_max_retries_zero_means_no_retry(schema):
    azure = FakeAzure(httpx2.Response(429, json={"error": {"message": "x"}}))
    with pytest.raises(LLMError, match="HTTP 429"):
        make(azure, max_retries=0).generate_structured("s", "u", schema)
    assert azure.call_count == 1


def test_auth_error_is_reported_with_status_and_not_retried(schema):
    azure = FakeAzure(httpx2.Response(401, json={"error": {"message": "Access denied"}}))
    with pytest.raises(LLMError, match="HTTP 401"):
        make(azure).generate_structured("s", "u", schema)
    assert azure.call_count == 1


def test_connection_error_is_an_llm_error(schema):
    azure = FakeAzure(httpx2.ConnectError("down"))
    with pytest.raises(LLMError, match="LLM request failed"):
        make(azure, max_retries=0).generate_structured("s", "u", schema)


def test_usage_is_logged(schema, caplog):
    with caplog.at_level(logging.INFO):
        make(FakeAzure(ok())).generate_structured("s", "u", schema)
    line = next(r.message for r in caplog.records if "LLM usage" in r.message)
    for part in (
        "model=gpt-5.4",
        "prompt_tokens=1200",
        "completion_tokens=800",
        "reasoning_tokens=300",
        "total_tokens=2000",
    ):
        assert part in line


def test_missing_usage_is_tolerated(schema):
    assert make(FakeAzure(ok(usage=False))).generate_structured("s", "u", schema).title == "Titel"


def test_api_key_never_appears_in_logs_errors_or_repr(schema, caplog):
    azure = FakeAzure(completion("bad"), httpx2.Response(401, json={"error": {}}))
    llm = make(azure)
    with caplog.at_level(logging.DEBUG), pytest.raises(LLMError) as exc:
        llm.generate_structured("s", "u", schema)
    assert KEY not in caplog.text and KEY not in str(exc.value)
    assert KEY not in repr(llm) and KEY not in repr(llm.options)


# --- registration and configuration ------------------------------------------------------


def test_registered_and_built_from_secrets_via_the_registry():
    assert "azure_foundry" in list_providers()["llm"]
    llm = build_llm(AzureFoundryOptions(), secrets())
    assert isinstance(llm, AzureFoundryLLM) and isinstance(llm, LLMProvider)
    assert str(llm._client.base_url) == BASE + "/"


def test_config_selects_options_model_with_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("llm:\n  provider: azure_foundry\n", encoding="utf-8")
    cfg = load_config(path, env_file=tmp_path / ".env")
    assert isinstance(cfg.llm, AzureFoundryOptions)
    assert (cfg.llm.model, cfg.llm.max_retries, cfg.llm.reasoning_effort) == ("gpt-5.4", 2, None)


def test_config_rejects_unknown_option_and_bad_effort(tmp_path):
    from content2podcast.config import ConfigError

    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  provider: azure_foundry\n  temperature: 0.2\n  reasoning_effort: extreme\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path, env_file=tmp_path / ".env")
    assert "llm.temperature" in str(exc.value) and "llm.reasoning_effort" in str(exc.value)


def test_missing_secrets_give_a_clear_error():
    with pytest.raises(ProviderNotConfiguredError) as exc:
        build_llm(AzureFoundryOptions(), Secrets())
    assert "AZURE_FOUNDRY_BASE_URL and AZURE_FOUNDRY_API_KEY" in str(exc.value)
    with pytest.raises(ProviderNotConfiguredError, match="AZURE_FOUNDRY_API_KEY"):
        build_llm(AzureFoundryOptions(), Secrets(azure_foundry_base_url=BASE))
