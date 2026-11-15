"""Owner-facing guidance shown verbatim in the web app reads in plain words."""

import re

import pytest

from inky_bird_frame.journal import failure_guidance

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
