import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "docker-compose.example.yml"
DATA = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_services_and_volumes():
    assert set(DATA["services"]) == {"podcast", "caddy"}
    assert set(DATA["volumes"]) == {"podcast-data", "podcast-site"}


def test_podcast_service():
    svc = DATA["services"]["podcast"]
    assert svc["image"] == "${PODCAST_IMAGE:-content2podcast:local}"
    assert svc["restart"] == "unless-stopped"
    assert svc["stop_grace_period"] == "10m"
    assert "./config:/config:ro" in svc["volumes"]
    assert "podcast-data:/data" in svc["volumes"]
    assert "podcast-site:/srv/podcast" in svc["volumes"]


def test_caddy_serves_the_site_read_only():
    svc = DATA["services"]["caddy"]
    assert "podcast-site:/srv/podcast:ro" in svc["volumes"]
    assert "./Caddyfile.example:/etc/caddy/Caddyfile:ro" in svc["volumes"]
    assert svc["ports"] == ["${PODCAST_PORT:-8080}:8080"]


def test_smoke_script_and_ci_job():
    script = ROOT / "deploy" / "smoke-compose.sh"
    assert script.stat().st_mode & 0o111
    assert "smoke-compose.sh" in (ROOT / ".github" / "workflows" / "ci.yml").read_text()


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_compose_config_is_valid(tmp_path):
    (tmp_path / ".env").write_text("")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "--project-directory",
            str(tmp_path),
            "config",
            "--quiet",
        ],
        capture_output=True,
        text=True,
    )
    if "unknown shorthand flag" in result.stderr or "is not a docker command" in result.stderr:
        pytest.skip("docker compose plugin missing")
    assert result.returncode == 0, result.stderr
