import logging
from datetime import UTC, datetime

import httpx
import pytest
import respx

from content2podcast import __version__
from content2podcast.config import HttpConfig
from content2podcast.http import (
    HttpError,
    make_client,
    parse_retry_after,
    request_with_retry,
)

URL = "https://example.com/feed"


@pytest.fixture
def client():
    with make_client() as c:
        yield c


@pytest.fixture
def sleeps():
    return []


def call(client, sleeps, **kwargs):
    kwargs.setdefault("rand", lambda: 1.0)  # no jitter reduction: delay == nominal backoff
    return request_with_retry(client, "GET", URL, sleep=sleeps.append, **kwargs)


@respx.mock
def test_user_agent_on_every_request(client, sleeps):
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200)])
    call(client, sleeps)
    agents = [r.request.headers["user-agent"] for r in route.calls]
    assert len(agents) == 2
    assert all(
        a == f"content2podcast/{__version__} (+https://github.com/RichieRoxx/content2podcast)"
        for a in agents
    )


@respx.mock
def test_custom_user_agent_and_timeouts():
    cfg = HttpConfig(user_agent="custom/1", connect_timeout=3, read_timeout=7)
    with make_client(cfg) as c:
        route = respx.get(URL).mock(return_value=httpx.Response(200))
        c.get(URL)
        assert route.calls[0].request.headers["user-agent"] == "custom/1"
        assert (c.timeout.connect, c.timeout.read) == (3, 7)


@respx.mock
def test_redirects_followed(client):
    respx.get(URL).mock(return_value=httpx.Response(301, headers={"Location": URL + "2"}))
    respx.get(URL + "2").mock(return_value=httpx.Response(200, text="ok"))
    assert client.get(URL).text == "ok"


@respx.mock
def test_429_with_retry_after_then_success(client, sleeps, caplog):
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    with caplog.at_level(logging.WARNING):
        assert call(client, sleeps).status_code == 200
    assert route.call_count == 2
    assert sleeps == [7.0]
    assert any("attempt=1" in r.message and "HTTP 429" in r.message for r in caplog.records)


@respx.mock
def test_retry_after_is_capped(client, sleeps):
    respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(200)]
    )
    call(client, sleeps, max_retry_after=45)
    assert sleeps == [45]


@respx.mock
def test_retry_after_http_date(client, sleeps):
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "Thu, 01 Jan 2026 12:00:10 GMT"}),
            httpx.Response(200),
        ]
    )
    call(client, sleeps, now=lambda: now)
    assert sleeps == [10.0]


@respx.mock
def test_503_several_times_then_success_with_exponential_backoff(client, sleeps):
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(503)] * 3 + [httpx.Response(200, text="finally")]
    )
    assert call(client, sleeps, max_attempts=4).text == "finally"
    assert route.call_count == 4
    assert sleeps == [1.0, 2.0, 4.0]


@respx.mock
def test_jitter_scales_delay(client, sleeps):
    respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200)])
    call(client, sleeps, rand=lambda: 0.0)
    assert sleeps == [0.5]


@respx.mock
def test_exhausted_retries_raise(client, sleeps):
    route = respx.get(URL).mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(HttpError) as exc:
        call(client, sleeps, max_attempts=3)
    assert route.call_count == 3
    assert len(sleeps) == 2
    assert exc.value.status == 502 and exc.value.url == URL
    assert "bad gateway" in exc.value.body


@respx.mock
def test_404_raises_immediately(client, sleeps):
    route = respx.get(URL).mock(return_value=httpx.Response(404, text="nope" * 500))
    with pytest.raises(HttpError) as exc:
        call(client, sleeps)
    assert route.call_count == 1 and sleeps == []
    assert exc.value.status == 404
    assert len(exc.value.body) <= 200


@respx.mock
def test_timeout_is_retried(client, sleeps):
    route = respx.get(URL).mock(side_effect=[httpx.ConnectTimeout("slow"), httpx.Response(200)])
    assert call(client, sleeps).status_code == 200
    assert route.call_count == 2


@respx.mock
def test_transport_error_exhausted_raises_http_error_without_status(client, sleeps):
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(HttpError) as exc:
        call(client, sleeps, max_attempts=2)
    assert exc.value.status is None
    assert isinstance(exc.value.__cause__, httpx.ReadTimeout)


@respx.mock
def test_query_string_not_logged(client, sleeps, caplog):
    respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200)])
    with caplog.at_level(logging.WARNING):
        request_with_retry(client, "GET", URL + "?key=SECRET", sleep=sleeps.append)
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5", 5.0), ("0", 0.0), ("-3", 0.0), (None, None), ("", None), ("garbage", None)],
)
def test_parse_retry_after(value, expected):
    assert parse_retry_after(value) == expected
