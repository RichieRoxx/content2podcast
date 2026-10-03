import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
RUNTIME = DOCKERFILE.split("AS runtime", 1)[1]


def instructions(name: str) -> list[str]:
    """Arguments of every ``name`` instruction in the runtime stage (continuations joined)."""
    joined = RUNTIME.replace("\\\n", " ")
    return [
        line.split(None, 1)[1].strip()
        for line in joined.splitlines()
        if line.startswith(f"{name} ") or line == name
    ]


def test_runs_as_a_non_root_user_with_configurable_ids():
    assert instructions("USER") == ["podcast"]
    args = instructions("ARG")
    assert "UID=1000" in args and "GID=1000" in args
    run = " ".join(instructions("RUN"))
    assert 'groupadd --gid "${GID}" podcast' in run
    assert 'useradd --uid "${UID}" --gid "${GID}"' in run
    assert "/usr/sbin/nologin" in run  # no login shell


def test_volumes_and_their_ownership():
    assert instructions("VOLUME") == ['["/config", "/data", "/srv/podcast"]']
    run = " ".join(instructions("RUN"))
    assert "install -d -o podcast -g podcast /config /data /srv/podcast" in run
    assert instructions("WORKDIR") == ["/data"]


def test_environment_defaults():
    env = " ".join(instructions("ENV"))
    for expected in (
        "C2P_CONFIG=/config/config.yaml",
        "C2P_PATHS__DATA_DIR=/data",
        "C2P_PATHS__OUTPUT_DIR=/srv/podcast",
        "TZ=UTC",
    ):
        assert expected in env


def test_daemon_by_default_and_a_healthcheck():
    assert instructions("ENTRYPOINT") == ['["podcast"]']
    assert instructions("CMD") == ['["daemon"]']
    [health] = instructions("HEALTHCHECK")
    assert 'CMD ["podcast", "health"]' in health
    assert "--start-period=" in health and "--interval=" in health


def test_ffmpeg_and_tzdata_are_installed_without_recommends():
    run = " ".join(instructions("RUN"))
    assert "apt-get install -y --no-install-recommends ffmpeg tzdata" in run
    assert "rm -rf /var/lib/apt/lists/*" in run


def test_oci_labels():
    labels = " ".join(instructions("LABEL"))
    for key in ("title", "description", "source", "licenses", "version", "revision", "created"):
        assert f"org.opencontainers.image.{key}=" in labels
    assert 'org.opencontainers.image.licenses="AGPL-3.0"' in labels
    assert 'org.opencontainers.image.version="${VERSION}"' in labels
    assert {"VERSION=dev", "REVISION=unknown", "CREATED=unknown"} <= set(instructions("ARG"))


def test_the_runtime_image_contains_neither_uv_nor_the_source_tree():
    assert "COPY --from=builder /app/.venv /app/.venv" in RUNTIME
    assert "COPY src" not in RUNTIME and "uv sync" not in RUNTIME


def test_the_config_dir_is_not_baked_into_the_image():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored and "data" in ignored and "public" in ignored


def test_ci_builds_and_smoke_tests_the_image():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "deploy/smoke-image.sh" in workflow and "push: false" in workflow


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_the_smoke_script_is_valid_shell_and_executable():
    script = ROOT / "deploy" / "smoke-image.sh"
    assert script.stat().st_mode & 0o111
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0


def test_docs_describe_the_volumes_and_the_build_arguments():
    readme = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    for needle in ("/config", "/data", "/srv/podcast", "--build-arg UID=$(id -u)", "docker stop"):
        assert needle in readme
    assert re.search(r"TZ=Europe/Berlin", readme)


def test_builder_uses_the_pinned_official_uv_binary_and_the_runtime_python():
    builder = DOCKERFILE.split("AS builder", 1)[1].split("AS runtime", 1)[0]
    copies = [line for line in builder.splitlines() if line.startswith("COPY --from=")]
    assert len(copies) == 1
    assert re.match(r"COPY --from=ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+ /uv /uvx /bin/", copies[0])
    assert "pip install" not in builder
    # same Python base in both stages: the virtualenv is built where it will run
    bases = re.findall(r"^FROM (\S+)", DOCKERFILE, flags=re.M)
    assert len(bases) == 2 and bases[0] == bases[1]
