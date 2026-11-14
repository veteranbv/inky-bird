"""Public entry documents must point at the canonical repository."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEGACY_REPOSITORY = "veteranbv/inky-bird-frame"
ENTRY_DOCUMENTS = sorted(
    [
        *ROOT.glob("*.md"),
        *(ROOT / ".github").glob("*.md"),
        *(ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml"),
    ]
)


def test_entry_documents_are_found() -> None:
    names = {path.relative_to(ROOT).as_posix() for path in ENTRY_DOCUMENTS}
    assert {"README.md", "SECURITY.md", "CONTRIBUTING.md"} <= names


@pytest.mark.parametrize(
    "path", ENTRY_DOCUMENTS, ids=lambda path: path.relative_to(ROOT).as_posix()
)
def test_entry_documents_use_the_canonical_repository_name(path: Path) -> None:
    assert LEGACY_REPOSITORY not in path.read_text(encoding="utf-8")
