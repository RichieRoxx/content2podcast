from typer.testing import CliRunner

from content2podcast import __version__
from content2podcast.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
