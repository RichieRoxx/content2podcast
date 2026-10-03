import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SCRIPT = DEPLOY / "verify-units.sh"


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # keep the case of the keys
    parser.read(DEPLOY / name, encoding="utf-8")
    return parser


# --- content of the units (no systemd needed) --------------------------------------------


def test_service_runs_the_pipeline_as_an_unprivileged_one_shot():
    service = unit("podcast.service")["Service"]
    assert service["Type"] == "oneshot"
    assert service["User"] == "podcast"
    assert service["EnvironmentFile"] == "/etc/content2podcast/.env"
    assert service["ExecStart"] == (
        "/opt/content2podcast/.venv/bin/podcast run --config /etc/content2podcast/config.yaml"
    )
    assert service["TimeoutStartSec"] == "4h"
    assert service["SuccessExitStatus"] == "3"  # the run lock is not a failure


def test_service_is_hardened_and_may_only_write_to_its_directories():
    service = unit("podcast.service")["Service"]
    for key in ("ProtectHome", "PrivateTmp", "NoNewPrivileges"):
        assert service[key] == "true"
    assert service["ProtectSystem"] == "strict"
    assert set(service["ReadWritePaths"].split()) == {"/var/lib/content2podcast", "/srv/podcast"}


def test_service_waits_for_the_network():
    unit_section = unit("podcast.service")["Unit"]
    assert "network-online.target" in unit_section["After"]
    assert "network-online.target" in unit_section["Wants"]


def test_timer_runs_daily_at_half_past_five_and_catches_up():
    parser = unit("podcast.timer")
    timer = parser["Timer"]
    assert timer["OnCalendar"] == "*-*-* 05:30:00"
    assert timer["Persistent"] == "true"
    assert timer["RandomizedDelaySec"] == "5min"
    assert parser["Install"]["WantedBy"] == "timers.target"


def test_the_paths_in_the_units_match_the_install_documentation():
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    for path in (
        "/opt/content2podcast",
        "/etc/content2podcast",
        "/var/lib/content2podcast",
        "/srv/podcast",
    ):
        assert path in readme
    assert "systemctl enable --now podcast.timer" in readme
    assert "chmod 640 /etc/content2podcast/.env" in readme


# --- the verification script -------------------------------------------------------------

needs_systemd = pytest.mark.skipif(
    shutil.which("systemd-analyze") is None or shutil.which("bash") is None,
    reason="systemd-analyze not available",
)


def verify(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), str(directory)], capture_output=True, text=True, check=False
    )


@pytest.fixture
def units(tmp_path):
    directory = tmp_path / "units"
    directory.mkdir()
    for name in ("podcast.service", "podcast.timer"):
        shutil.copy(DEPLOY / name, directory / name)
    return directory


def edit(directory: Path, name: str, old: str, new: str) -> None:
    path = directory / name
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


@needs_systemd
def test_the_shipped_units_pass_verification():
    result = verify(DEPLOY)
    assert result.returncode == 0, result.stderr
    assert "systemd units OK" in result.stdout


@needs_systemd
@pytest.mark.parametrize(
    ("name", "old", "new", "message"),
    [
        ("podcast.service", "ProtectSystem=strict", "ProtectSystem=strikt", "strikt"),
        ("podcast.service", "Type=oneshot", "Typ=oneshot", "Unknown key name 'Typ'"),
        ("podcast.timer", "Persistent=true", "Persistant=true", "Persistant"),
        ("podcast.timer", "*-*-* 05:30:00", "never-ever", "calendar"),
    ],
    ids=["bad-value", "unknown-service-key", "unknown-timer-key", "bad-calendar"],
)
def test_verification_catches_mistakes(units, name, old, new, message):
    edit(units, name, old, new)
    result = verify(units)
    assert result.returncode == 1
    assert message in result.stderr and "verification FAILED" in result.stderr


@needs_systemd
def test_verification_notices_a_missing_exec_start(units):
    edit(
        units,
        "podcast.service",
        "ExecStart=/opt/content2podcast/.venv/bin/podcast run",
        "#ExecStart=/x",
    )
    result = verify(units)
    assert result.returncode == 1 and "no ExecStart=" in result.stderr


@needs_systemd
def test_the_script_does_not_depend_on_an_installed_binary(units):
    # a stub for the binary is created inside a temporary root, so nothing has to be installed
    assert verify(units).returncode == 0
