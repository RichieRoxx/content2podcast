import shutil

import pytest


def pytest_collection_modifyitems(config, items):
    if shutil.which("ffmpeg"):
        return
    skip = pytest.mark.skip(reason="ffmpeg not found on PATH")
    for item in items:
        if "ffmpeg" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def env(tmp_path):
    """Config, database and HTTP client for pipeline tests."""
    from content2podcast.config import AppConfig
    from content2podcast.db import connect
    from content2podcast.http import make_client

    config = AppConfig()
    config.paths.data_dir = tmp_path / "data"
    config.paths.output_dir = tmp_path / "public"
    config.podcast.title = "Mein Podcast"
    config.feed.base_url = "https://nas.example.ts.net/"
    conn = connect(tmp_path / "data" / "db.sqlite3")
    with make_client() as http:
        yield {"config": config, "conn": conn, "http": http, "tmp": tmp_path}
    conn.close()
