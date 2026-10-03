import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
FILES = ["Caddyfile.example", "Caddyfile.tailscale.example"]


def text(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def directives(name: str) -> set[str]:
    """Directive lines without comments and indentation."""
    lines = (line.strip() for line in text(name).splitlines())
    return {line for line in lines if line and not line.startswith("#")}


@pytest.mark.parametrize("name", FILES)
def test_content_types_caching_and_hardening(name):
    found = directives(name)
    assert 'header @feed Content-Type "application/rss+xml; charset=utf-8"' in found
    assert 'header @audio Content-Type "audio/mpeg"' in found
    assert "@audio path *.mp3" in found
    assert 'header @audio Accept-Ranges "bytes"' in found
    assert 'header @feed Cache-Control "no-cache"' in found
    assert 'header @audio Cache-Control "public, max-age=31536000, immutable"' in found
    assert "respond @hidden 404" in found
    assert "file_server" in found
    assert "admin off" in found
    assert not any(line.startswith("browse") for line in found)  # no directory listings


def test_the_two_variants_serve_the_files_identically():
    def site_body(name: str) -> list[str]:
        return [
            line
            for line in sorted(directives(name))
            if line.startswith(("header", "@", "encode", "respond", "root", "file_server"))
        ]

    assert site_body("Caddyfile.example") == site_body("Caddyfile.tailscale.example")


def test_lan_variant_is_plain_http_on_a_configurable_port():
    found = directives("Caddyfile.example")
    assert "auto_https off" in found
    assert ":{$PODCAST_PORT:8080} {" in found
    assert "root * {$PODCAST_ROOT:/srv/podcast}" in found


def test_tailscale_variant_uses_the_tailscale_certificate():
    found = directives("Caddyfile.tailscale.example")
    assert "get_certificate tailscale" in found
    assert "{$PODCAST_HOST:podcast.example.ts.net} {" in found
    assert not any(line.startswith("auto_https off") for line in found)


def test_both_ways_to_get_https_are_documented():
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    assert "tailscale serve --bg --https=443 http://127.0.0.1:8080" in readme
    assert "tailscale set --operator=caddy" in readme
    assert "Caddyfile.tailscale.example" in readme and "feed.base_url" in readme
    assert re.search(r"curl -I http://localhost:8080/feed\.xml", readme)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_the_verification_script_is_valid_shell_and_executable():
    script = DEPLOY / "verify-caddy.sh"
    assert script.stat().st_mode & 0o111
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0
