from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "image.yml").read_text())


def test_triggers_and_permissions():
    on = WORKFLOW[True] if True in WORKFLOW else WORKFLOW["on"]  # YAML parses `on` as True
    assert on["push"]["branches"] == ["main"]
    assert on["push"]["tags"] == ["v*"]
    assert "pull_request" not in on
    assert WORKFLOW["permissions"]["packages"] == "write"


def test_builds_and_pushes_both_architectures():
    steps = WORKFLOW["jobs"]["publish"]["steps"]
    build = next(s for s in steps if s.get("uses", "").startswith("docker/build-push-action"))
    assert build["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert build["with"]["push"] is True
    assert "REVISION=${{ github.sha }}" in build["with"]["build-args"]
    assert any(s.get("uses", "").startswith("docker/setup-qemu-action") for s in steps)


def test_tags_edge_semver_and_sha():
    steps = WORKFLOW["jobs"]["publish"]["steps"]
    meta = next(s for s in steps if s.get("uses", "").startswith("docker/metadata-action"))
    tags = meta["with"]["tags"]
    assert meta["with"]["images"] == "${{ env.IMAGE }}"
    assert WORKFLOW["env"]["IMAGE"] == "ghcr.io/richieroxx/content2podcast"
    for expected in ("type=edge,branch=main", "type=semver,pattern={{version}}", "type=sha"):
        assert expected in tags
