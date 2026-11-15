"""Every event in the research allowance rule (see ResearchBudget) and its effect."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from test_catalog_safety import approve, write_pending
from test_generation_budget import (
    CARDINAL,
    FIRST,
    PROFILE,
    TRIES_BEFORE_STOP,
    Killed,
    first_failure,
    generation,
    load,
    refresh,
    research_codex,
    seed_failures,
)

from inky_bird_frame.catalog import reject_candidate
from inky_bird_frame.config import AppConfig
from inky_bird_frame.controller import _research_budget, _research_profile, run_generation_cycle
from inky_bird_frame.errors import DataSourceError, GenerationError
from inky_bird_frame.models import SpeciesProfileData
from inky_bird_frame.recovery import retry_species

OTHER = 99
Counts = tuple[int, int]


def counts(config: AppConfig, today: datetime) -> Counts:
    """(this bird's per-species total, the shared total for the given day)."""
    raw = json.loads((config.controller.state_dir / "research-budget.json").read_text())
    daily = raw["total"] if raw["date"] == today.date().isoformat() else 0
    return int(raw["species_totals"].get(str(FIRST.taxon_id), 0)), int(daily)


def research(config: AppConfig, answer: bool, outcome: BaseException | None) -> Counts:
    output = config.controller.state_dir / "answer.json"
    seen: list[Counts] = []

    def call() -> SpeciesProfileData:
        seen.append(counts(config, datetime.now(UTC)))
        if answer:
            output.write_text(json.dumps(PROFILE))
        if outcome is not None:
            raise outcome
        return PROFILE

    # The failure itself is the event under test; only the counts matter here.
    with suppress(Exception, Killed):
        _research_profile(config, FIRST, output, call)
    return seen[0]


def during_call(config: AppConfig) -> Counts:
    return research(config, answer=True, outcome=None)


def completes(config: AppConfig) -> None:
    research(config, answer=True, outcome=None)


def completes_unusably(config: AppConfig) -> None:
    research(config, answer=True, outcome=GenerationError("profile cites one source"))


def ends_without_answer(config: AppConfig) -> None:
    research(config, answer=False, outcome=GenerationError("Codex timed out"))


def process_dies(config: AppConfig) -> None:
    research(config, answer=False, outcome=Killed())


def automatic_retry(config: AppConfig) -> None:
    seed_failures(config, FIRST.taxon_id, 1)
    with generation(DataSourceError("temporary")):
        run_generation_cycle(config)


def run_ends_with_plate(config: AppConfig) -> None:
    seed_failures(config, FIRST.taxon_id, 1)
    with (
        generation([config.controller.state_dir / "candidate"]),
        patch("inky_bird_frame.controller.approve_candidate", return_value=MagicMock()),
    ):
        run_generation_cycle(config)


def stop_for_owner(config: AppConfig) -> None:
    seed_failures(config, FIRST.taxon_id, TRIES_BEFORE_STOP - 1)
    with generation(DataSourceError("source down")):
        run_generation_cycle(config)


def owner_retry(config: AppConfig) -> None:
    seed_failures(config, FIRST.taxon_id, 1)
    retry_species(config, FIRST.taxon_id)


def owner_retry_with_fresh_research(config: AppConfig) -> None:
    seed_failures(config, FIRST.taxon_id, 1)
    retry_species(config, FIRST.taxon_id, refresh_research=True)


def owner_replacement(config: AppConfig) -> None:
    approve(config, FIRST)
    retry_species(config, FIRST.taxon_id, replace_approved=True, reason="Wrong bill shape.")


def owner_rejection(config: AppConfig) -> None:
    write_pending(config, FIRST)
    reject_candidate(config.controller.state_dir, FIRST.taxon_id, "Wrong bill shape.")


def search_next_day(config: AppConfig) -> None:
    _research_budget(config).consume(FIRST.taxon_id, datetime.now(UTC) + timedelta(days=1))


# Start: this bird has used one search; another bird one more, both today.
# (event, what it does, this bird's total after, today's shared total after,
#  which day the shared total is read for)
ROWS: list[tuple[str, Callable[[AppConfig], object], int, int, int]] = [
    ("research call starts", during_call, 2, 3, 0),
    ("research call completes", completes, 2, 3, 0),
    ("research call answers unusably", completes_unusably, 2, 3, 0),
    ("research call ends without an answer", ends_without_answer, 1, 2, 0),
    ("process dies mid-research", process_dies, 2, 3, 0),
    ("automatic retry after a failure", automatic_retry, 1, 2, 0),
    ("run ends with a plate", run_ends_with_plate, 0, 2, 0),
    ("stop for the owner", stop_for_owner, 0, 2, 0),
    ("owner retry", owner_retry, 0, 2, 0),
    ("owner retry with fresh research", owner_retry_with_fresh_research, 0, 2, 0),
    ("owner replacement request", owner_replacement, 0, 2, 0),
    ("owner rejection", owner_rejection, 1, 2, 0),
    ("search on the next day", search_next_day, 2, 1, 1),
]


@pytest.mark.parametrize(
    ("event", "act", "species", "daily", "day"), ROWS, ids=[row[0] for row in ROWS]
)
def test_research_allowance_event(
    tmp_path: Path,
    event: str,
    act: Callable[[AppConfig], object],
    species: int,
    daily: int,
    day: int,
) -> None:
    config = load(str(tmp_path))
    refresh(config, [FIRST])
    budget = _research_budget(config)
    budget.consume(FIRST.taxon_id)
    budget.consume(OTHER)

    observed = act(config)
    after = observed if isinstance(observed, tuple) else None
    result = after or counts(config, datetime.now(UTC) + timedelta(days=day))

    assert result == (species, daily), event


def test_owner_retry_with_fresh_research_researches_a_bird_that_used_its_allowance(
    tmp_path: Path,
) -> None:
    config = load(str(tmp_path))
    refresh(config, [CARDINAL])
    for _ in range(config.research.max_searches_per_species):
        _research_budget(config).consume(CARDINAL.taxon_id)
    seed_failures(config, CARDINAL.taxon_id, 1)

    retry_species(config, CARDINAL.taxon_id, refresh_research=True)
    with research_codex([None]) as runner:
        result = run_generation_cycle(config)

    assert runner.calls == 1
    assert first_failure(result)["terminal"] is False
