import pytest

from content2podcast.sources.normalize import normalize_url

RULES = [
    # scheme -> https
    ("http://example.com/a", "https://example.com/a"),
    ("HTTPS://example.com/a", "https://example.com/a"),
    # lowercase host, strip www.
    ("https://Example.COM/a", "https://example.com/a"),
    ("https://www.example.com/a", "https://example.com/a"),
    ("https://WWW.Example.com/A", "https://example.com/A"),  # path stays case-sensitive
    ("https://example.com./a", "https://example.com/a"),
    # default ports dropped, others kept
    ("https://example.com:443/a", "https://example.com/a"),
    ("http://example.com:80/a", "https://example.com/a"),
    ("http://example.com:443/a", "https://example.com/a"),  # default of either scheme
    ("https://example.com:80/a", "https://example.com:80/a"),
    ("https://example.com:8443/a", "https://example.com:8443/a"),
    ("http://example.com:8080/a", "https://example.com:8080/a"),
    # fragment dropped
    ("https://example.com/a#section", "https://example.com/a"),
    ("https://example.com/a?x=1#section", "https://example.com/a?x=1"),
    # tracking parameters removed
    ("https://example.com/a?utm_source=x&utm_medium=y", "https://example.com/a"),
    ("https://example.com/a?UTM_Campaign=x&id=5", "https://example.com/a?id=5"),
    ("https://example.com/a?fbclid=1", "https://example.com/a"),
    ("https://example.com/a?gclid=1&mc_cid=2&mc_eid=3", "https://example.com/a"),
    ("https://example.com/a?ref=home&ref_src=tw&igshid=9", "https://example.com/a"),
    ("https://example.com/a?id=5&utm_source=x", "https://example.com/a?id=5"),
    # remaining params sorted, blank values kept
    ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
    ("https://example.com/a?b=&a", "https://example.com/a?a=&b="),
    ("https://example.com/a?x=2&x=1", "https://example.com/a?x=1&x=2"),
    # percent-encoding normalized
    ("https://example.com/%7Euser", "https://example.com/~user"),
    ("https://example.com/a%2fb", "https://example.com/a%2Fb"),
    ("https://example.com/%41bc", "https://example.com/Abc"),
    ("https://example.com/café", "https://example.com/caf%C3%A9"),
    ("https://example.com/caf%c3%a9", "https://example.com/caf%C3%A9"),
    ("https://example.com/a?q=café", "https://example.com/a?q=caf%C3%A9"),
    ("https://example.com/a?q=a%20b", "https://example.com/a?q=a+b"),
    ("https://example.com/a?q=a+b", "https://example.com/a?q=a+b"),
    # trailing slash removed except root
    ("https://example.com/a/", "https://example.com/a"),
    ("https://example.com/a/b//", "https://example.com/a/b"),
    ("https://example.com/", "https://example.com/"),
    ("https://example.com", "https://example.com/"),
    # whitespace around the URL
    ("  https://example.com/a  ", "https://example.com/a"),
    # same article from different forms collapses to one key
    ("http://www.example.com/post/?utm_source=rss#c", "https://example.com/post"),
]


@pytest.mark.parametrize(("raw", "expected"), RULES, ids=[r[0] for r in RULES])
def test_rules(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", [r[0] for r in RULES])
def test_idempotent(raw):
    once = normalize_url(raw)
    assert normalize_url(once) == once


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/a?b=%zz&a=1",
        "https://[2001:db8::1]:8443/a/",
        "https://bücher.example/a",
        "ftp://example.com/file/",
        "not a url",
        "",
    ],
)
def test_odd_input_is_stable_and_does_not_raise(raw):
    once = normalize_url(raw)
    assert normalize_url(once) == once


def test_ipv6_host_keeps_brackets_and_port():
    assert normalize_url("http://[2001:DB8::1]:8443/a/") == "https://[2001:db8::1]:8443/a"


def test_idn_host_is_punycoded():
    assert normalize_url("https://Bücher.example/a") == "https://xn--bcher-kva.example/a"


def test_only_http_is_upgraded():
    assert normalize_url("ftp://example.com/x/") == "ftp://example.com/x"


def test_repository_keys_on_normalized_url(tmp_path):
    from content2podcast import repository as repo
    from content2podcast.db import connect

    conn = connect(tmp_path / "db.sqlite3")
    source = repo.upsert_source(conn, "S", "https://example.com/feed", "rss")
    first = repo.insert_article_if_new(conn, source, "http://www.example.com/post/?utm_source=x")
    again = repo.insert_article_if_new(conn, source, "https://example.com/post#top")
    assert first is not None and again is None
    row = repo.get_article(conn, first)
    assert row["url"] == "http://www.example.com/post/?utm_source=x"  # original kept
    assert row["url_norm"] == "https://example.com/post"
