import io
import logging

import pytest

from content2podcast.logging_setup import kv, setup_logging


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = handlers, level
    for name in ("content2podcast", "httpx", "httpcore", "openai"):
        logging.getLogger(name).setLevel(logging.NOTSET)


def emit(env, **kwargs):
    stream = io.StringIO()
    setup_logging(env=env, stream=stream, **kwargs)
    logging.getLogger("content2podcast.test").info("hello world")
    logging.getLogger("content2podcast.test").error("boom")
    return stream.getvalue().splitlines()


def test_plain_format_without_journal_stream():
    info, error = emit({})
    assert not info.startswith("<")
    assert info.endswith("INFO content2podcast.test: hello world")
    assert error.endswith("ERROR content2podcast.test: boom")
    assert info[:4].isdigit()  # timestamp first


def test_journal_format_with_journal_stream():
    info, error = emit({"JOURNAL_STREAM": "8:12345"})
    assert info == "<6>content2podcast.test: hello world"
    assert error == "<3>content2podcast.test: boom"


def test_journal_prefixes_every_line_of_multiline_messages():
    stream = io.StringIO()
    setup_logging(env={"JOURNAL_STREAM": "1:1"}, stream=stream)
    logging.getLogger("content2podcast.x").warning("a\nb")
    assert stream.getvalue().splitlines() == ["<4>content2podcast.x: a", "<4>b"]


def test_quiet_hides_info():
    assert [line for line in emit({}, quiet=True) if "hello" in line] == []


def test_third_party_loggers_only_chatty_with_vv():
    setup_logging(verbosity=1, stream=io.StringIO(), env={})
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("content2podcast").getEffectiveLevel() == logging.DEBUG
    setup_logging(verbosity=2, stream=io.StringIO(), env={})
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.DEBUG


def test_setup_is_idempotent():
    root = logging.getLogger()
    setup_logging(stream=io.StringIO(), env={})
    count = len(root.handlers)
    setup_logging(stream=io.StringIO(), env={})
    assert len(root.handlers) == count


def test_kv():
    assert kv(a=1, b="x y", c="") == 'a=1 b="x y" c=""'
