"""Build inputs that reach the published image stay pinned and updated."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_build_backend_is_pinned_to_one_version() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        requires = tomllib.load(handle)["build-system"]["requires"]
    assert requires
    assert all("==" in requirement for requirement in requires), requires


def test_dependabot_updates_container_base_images() -> None:
    config = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert '  - package-ecosystem: "docker"\n    directory: "/"\n' in config
