"""content2podcast: turn blog and news articles into podcast episodes."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("content2podcast")
except PackageNotFoundError:  # pragma: no cover - running from an uninstalled tree
    __version__ = "0.0.0"
