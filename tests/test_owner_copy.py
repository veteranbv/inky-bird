"""Owner-facing guidance shown verbatim in the web app reads in plain words."""

import ast
import re
from pathlib import Path
from typing import cast

import pytest

from inky_bird_frame.catalog import rebuild_catalog_index
from inky_bird_frame.config import ControllerConfig
from inky_bird_frame.journal import JournalService, failure_guidance

MACHINERY = re.compile(
    r"backoff|operator|serialized|artifact|evidence|eligibility|provenance|candidate|retained",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "error_type",
    [
        "QualityReviewError",
        "InsufficientReferencesError",
        "ResearchLimitError",
        "DataSourceError",
        "GenerationError",
        "SpeciesStateError",
        "MissingDependencyError",
        "SomethingNewError",
        None,
    ],
)
def test_failure_guidance_is_plain(error_type: str | None) -> None:
    message = failure_guidance(error_type)["failure_message"]
    assert not MACHINERY.search(message), message
    assert message.endswith(".")


@pytest.mark.parametrize("stop_reason", ["retry_limit", "research_limit"])
def test_stopped_guidance_says_what_happened_once(stop_reason: str) -> None:
    # In the works follows this with how to try again, so it is not repeated here.
    guidance = failure_guidance("GenerationError", stop_reason)
    message = guidance["failure_message"]
    assert guidance["failure_kind"] == stop_reason
    assert not MACHINERY.search(message), message
    assert not re.search(r"\bfor retry\b|\bchoose\b", message, re.IGNORECASE), message
    assert message.endswith(".")


RETIRED = re.compile(
    r"Saved birds|Plate work|candidate|operator recovery|approved art|operator migration",
    re.IGNORECASE,
)
OWNER_ERRORS = {"JournalError", "JournalConflictError", "_error"}


def _owner_messages(module: str) -> list[str]:
    """String messages a module raises or returns to the owner pages, as written."""
    source = Path(__file__).parents[1] / "src/inky_bird_frame" / module
    messages = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", getattr(node.func, "attr", ""))
        if name in OWNER_ERRORS:
            messages += [
                argument.value
                for argument in node.args
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            ]
    return messages


@pytest.mark.parametrize("module", ["recovery.py", "web.py"])
def test_owner_errors_use_the_names_the_pages_show(module: str) -> None:
    messages = _owner_messages(module)
    assert messages
    retired = [message for message in messages if RETIRED.search(message)]
    assert not retired, retired


def test_health_reads_in_plain_words(tmp_path: Path) -> None:
    controller = ControllerConfig(
        tmp_path / "work",
        tmp_path / "catalog",
        tmp_path / "state",
        Path("codex"),
        None,
        "127.0.0.1",
        8000,
        3,
        1,
        3,
    )
    (tmp_path / "state").mkdir()
    rebuild_catalog_index(controller.catalog_dir)
    health = JournalService(controller).health()
    checks = cast(list[dict[str, str]], health["checks"])
    shown = [cast(str, health["notice"]), *(f"{c['name']} {c['detail']}" for c in checks)]
    for text in shown:
        assert not re.search(
            r"approved entries|catalog index|admission|privileged|raw logs", text
        ), text
    assert checks[0] == {
        "name": "Your plates",
        "status": "ok",
        "detail": "0 plates read. Each one shown is checked against its saved fingerprint.",
    }
