"""Copy rules for text people read: README, docs, package metadata, and the
docstrings, comments and messages in the source. No em-dashes anywhere."""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EM_DASH = chr(0x2014)
FILES = sorted(
    [ROOT / "README.md", ROOT / "pyproject.toml"]
    + list((ROOT / "docs").glob("*.md"))
    + list((ROOT / "src").rglob("*.py"))
    + list((ROOT / "tests").glob("*.py"))
    + list((ROOT / "scripts").glob("*.py"))
)


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_em_dashes(path):
    hits = [
        f"{path.relative_to(ROOT)}:{n}: {line.strip()}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "—" in line
    ]
    assert not hits, "\n".join(hits)
