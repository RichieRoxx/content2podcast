import shutil

import pytest


def pytest_collection_modifyitems(config, items):
    if shutil.which("ffmpeg"):
        return
    skip = pytest.mark.skip(reason="ffmpeg not found on PATH")
    for item in items:
        if "ffmpeg" in item.keywords:
            item.add_marker(skip)
