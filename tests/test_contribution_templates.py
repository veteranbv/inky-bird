"""Contributor-facing templates must match the public contribution contract."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"


def _first_block_after(text: str, marker: str) -> list[str]:
    after = text.split(marker, 1)[1]
    block = re.search(r"```[a-z]*\n(.*?)\n```", after, re.DOTALL)
    assert block is not None
    return block.group(1).splitlines()


def test_pull_request_checks_match_the_contributing_guide() -> None:
    template = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    guide = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert _first_block_after(template, "## Validation") == _first_block_after(
        guide, "Required checks:"
    )


def test_pull_request_template_uses_the_canonical_command() -> None:
    template = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    assert re.search(r"\binky-bird-frame\b", template) is None
    assert "`inky-bird catalog prepare`" in template


@pytest.mark.parametrize("form", ["bug_report.yml", "feature_request.yml"])
def test_issue_forms_offer_every_owner_facing_area(form: str) -> None:
    text = (TEMPLATES / form).read_text(encoding="utf-8")
    for area in ("Web journal", "Owner access and sign-in", "History imports", "Home Assistant"):
        assert f"        - {area}\n" in text


def test_issue_forms_use_the_current_product_name() -> None:
    for form in TEMPLATES.glob("*.yml"):
        assert "Inky Bird Frame" not in form.read_text(encoding="utf-8"), form.name
