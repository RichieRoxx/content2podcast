import re
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", ROOT / "docs" / "DEPLOYMENT.md", ROOT / "deploy" / "README.md"]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = re.sub(r"[`*_]", "", heading).strip().lower()
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text))


def anchors(path: Path) -> set[str]:
    in_code, found = False, set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and (m := re.match(r"#{1,6}\s+(.*)", line)):
            found.add(slug(m.group(1)))
    return found


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_and_anchors_resolve(doc):
    text = re.sub(r"```.*?```", "", doc.read_text(encoding="utf-8"), flags=re.S)
    for target in LINK.findall(text):
        if re.match(r"[a-z]+:", target):
            continue  # http(s), mailto
        path, _, anchor = unquote(target).partition("#")
        file = (doc.parent / path).resolve() if path else doc
        assert file.exists(), f"{doc.name}: {target} does not exist"
        if anchor and file.suffix == ".md":
            assert anchor in anchors(file), f"{doc.name}: no heading for #{anchor} in {file.name}"


def test_deployment_guide_is_linked_from_the_readme():
    assert "docs/DEPLOYMENT.md" in (ROOT / "README.md").read_text(encoding="utf-8")
