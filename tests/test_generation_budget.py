"""Generation cycle time budget, stop handling, retry ceiling and kept attempts."""

from __future__ import annotations

import json
import signal
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from png_fixtures import write_png

from inky_bird_frame.birds import BirdSpecies, TaxonContext
from inky_bird_frame.config import AppConfig, load_config
from inky_bird_frame.controller import (
    MAX_AUTOMATIC_GENERATION_TRIES,
    PROFILE_REFRESH_CORRECTION,
    STOPPED_GENERATION_ERROR,
    DiscoveryResult,
    GenerationStopRequested,
    ProviderStatus,
    exclusive_cycle_lock,
    generate_candidate,
    generation_species_seconds,
    load_or_create_profile,
    read_generation_work,
    run_generation_cycle,
    run_refresh_cycle,
)
from inky_bird_frame.errors import (
    DataSourceError,
    GenerationError,
    InsufficientReferencesError,
    ResearchLimitError,
    SpeciesStateError,
)
from inky_bird_frame.installation import (
    GENERATION_UNIT_MARGIN_SECONDS,
    generation_cycle_timeout_seconds,
)
from inky_bird_frame.journal import terminal_failure
from inky_bird_frame.models import ProfileConflict, QualityReview, SpeciesProfileData
from inky_bird_frame.research import ResearchBudget
from inky_bird_frame.retry import RetryStore

CONFIG = """
[discovery]
zip_code = "12345"
radius_km = 8
species_limit = 12
window = "last-week"

[controller]
workspace_dir = "workspace"
catalog_dir = "catalog"
state_dir = "state"
codex_path = "/usr/bin/false"
bind_host = "127.0.0.1"
port = 8793
references_per_species = 4
generations_per_cycle = 2
max_generation_attempts = 3

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display"
"""

FIRST = BirdSpecies(1, "First Bird", "Avis prima", 3, "iNaturalist")
SECOND = BirdSpecies(2, "Second Bird", "Avis secunda", 2, "iNaturalist")
CARDINAL = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "iNaturalist")

PROFILE = SpeciesProfileData(
    taxon_id=9083,
    common_name="Northern Cardinal",
    scientific_name="Cardinalis cardinalis",
    family="Cardinalidae",
    measurements={"length": "8.3 in", "wingspan": "10 in", "weight": "1.5 oz"},
    field_marks=["crest", "red plumage", "black mask", "orange bill"],
    habitat="Woodland edges",
    behavior="Forages near cover",
    palette=["red", "black", "orange"],
    sources=[
        {"title": "Cornell", "url": "https://www.allaboutbirds.org/one"},
        {"title": "Audubon", "url": "https://www.audubon.org/two"},
    ],
)

CONFLICT: ProfileConflict = {
    "field": "habitat",
    "profile_value": "Woodland edges",
    "observed_value": "Desert",
    "sources": [
        {"title": "Cornell", "url": "https://www.allaboutbirds.org/x"},
        {"title": "Audubon", "url": "https://www.audubon.org/y"},
    ],
}

FAILED_REVIEW = QualityReview(
    False,
    3,
    4,
    5,
    5,
    True,
    ("Crest is too short",),
    correction_findings=("Crest is too short",),
)


def load(temporary: str) -> AppConfig:
    path = Path(temporary) / "config.toml"
    path.write_text(CONFIG)
    config = load_config(path)
    config.controller.workspace_dir.mkdir()
    return config


def refresh(config: AppConfig, species: list[BirdSpecies]) -> None:
    result = DiscoveryResult(
        location=None,
        species=species,
        providers=[ProviderStatus("inaturalist", "ok", len(species))],
        unresolved=[],
    )
    with (
        patch("inky_bird_frame.controller.discover_species", return_value=result),
        patch("inky_bird_frame.controller.rebuild_catalog_index", return_value=[]),
    ):
        run_refresh_cycle(config)


def retries(config: AppConfig) -> RetryStore:
    return RetryStore(config.controller.state_dir / "generation-retries.json")


def seed_failures(config: AppConfig, taxon_id: int, count: int) -> None:
    store = retries(config)
    past = datetime.now(UTC) - timedelta(days=60)
    for _ in range(count):
        store.record_failure(
            taxon_id, DataSourceError("earlier"), now=past, initial_minutes=1, maximum_minutes=1
        )


def first_failure(result: dict[str, object]) -> dict[str, object]:
    failures = result["failures"]
    assert isinstance(failures, list) and failures and isinstance(failures[0], dict)
    return failures[0]


class Clock:
    def __init__(self, tick: float = 0.0) -> None:
        self.now = 1000.0
        self.tick = tick

    def __call__(self) -> float:
        self.now += self.tick
        return self.now


@contextmanager
def generation(side_effect: object) -> Iterator[MagicMock]:
    with (
        patch("inky_bird_frame.controller.generate_candidate", side_effect=side_effect) as call,
        patch("inky_bird_frame.controller.rebuild_catalog_index", return_value=[]),
    ):
        yield call


class GenerationBudgetTests(unittest.TestCase):
    def run_two_birds(self, first_bird_seconds: float) -> tuple[list[int], dict[str, object]]:
        attempted: list[int] = []
        clock = Clock()

        def slow_failure(
            _config: object, species: BirdSpecies, *_args: object, **_kw: object
        ) -> Path:
            attempted.append(species.taxon_id)
            clock.now += first_bird_seconds
            raise DataSourceError("temporary")

        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [FIRST, SECOND])
            with (
                patch("inky_bird_frame.controller.monotonic", clock),
                generation(slow_failure),
            ):
                result = run_generation_cycle(config)
            self.second_retry = retries(config).get(SECOND.taxon_id)
        return attempted, result

    def test_next_bird_is_not_started_when_it_could_overrun_the_cycle(self) -> None:
        with TemporaryDirectory() as temporary:
            species_seconds = generation_species_seconds(load(temporary))
        cycle_seconds = species_seconds * 5
        attempted, result = self.run_two_birds(cycle_seconds - species_seconds + 1)

        self.assertEqual(attempted, [FIRST.taxon_id])
        self.assertIs(result["stopped_for_time"], True)
        self.assertEqual(result["attempted_count"], 1)
        # The bird left for later keeps no failure record.
        self.assertIsNone(self.second_retry)

    def test_next_bird_starts_while_its_longest_run_still_fits(self) -> None:
        with TemporaryDirectory() as temporary:
            species_seconds = generation_species_seconds(load(temporary))
        attempted, result = self.run_two_birds(3 * species_seconds)

        self.assertEqual(attempted, [FIRST.taxon_id, SECOND.taxon_id])
        self.assertIs(result["stopped_for_time"], False)

    def test_first_bird_starts_after_slow_preparation_with_one_bird_per_cycle(self) -> None:
        clock = Clock(tick=1.0)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                CONFIG.replace(
                    "max_generation_attempts = 3",
                    "max_generation_attempts = 3\nmax_species_attempts_per_cycle = 1",
                )
            )
            config = load_config(path)
            config.controller.workspace_dir.mkdir()
            refresh(config, [FIRST])
            species_seconds = generation_species_seconds(config)

            def slow_sweep(_config: object) -> None:
                clock.now += species_seconds

            with (
                patch("inky_bird_frame.controller.monotonic", clock),
                patch(
                    "inky_bird_frame.controller._sweep_abandoned_generation_work",
                    side_effect=slow_sweep,
                ),
                generation(DataSourceError("temporary")) as call,
            ):
                result = run_generation_cycle(config)

        self.assertEqual(call.call_count, 1)
        self.assertIs(result["stopped_for_time"], False)

    def test_time_before_the_first_bird_counts_toward_the_cycle(self) -> None:
        clock = Clock()
        attempted: list[int] = []
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [FIRST, SECOND])
            species_seconds = generation_species_seconds(config)

            def slow_sweep(_config: object) -> None:
                clock.now += 4.5 * species_seconds

            def quick_failure(
                _config: object, species: BirdSpecies, *_args: object, **_kw: object
            ) -> Path:
                attempted.append(species.taxon_id)
                clock.now += 1
                raise DataSourceError("temporary")

            with (
                patch("inky_bird_frame.controller.monotonic", clock),
                patch(
                    "inky_bird_frame.controller._sweep_abandoned_generation_work",
                    side_effect=slow_sweep,
                ),
                generation(quick_failure),
            ):
                result = run_generation_cycle(config)

        # The first bird always starts; a later one must fit what is left after preparation.
        self.assertEqual(attempted, [FIRST.taxon_id])
        self.assertIs(result["stopped_for_time"], True)

    def test_species_budget_counts_profile_refresh_and_two_calls_per_attempt(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
        # Three attempts: profile, one refresh, and a generation plus review each.
        self.assertEqual(generation_species_seconds(config), 8 * 1200)

    def test_service_timeout_is_the_cycle_budget_plus_its_margin(self) -> None:
        for attempts, birds in ((1, 1), (3, 5), (4, 2)):
            with self.subTest(attempts=attempts, birds=birds), TemporaryDirectory() as temporary:
                path = Path(temporary) / "config.toml"
                path.write_text(
                    CONFIG.replace(
                        "max_generation_attempts = 3",
                        f"max_generation_attempts = {attempts}\n"
                        f"max_species_attempts_per_cycle = {birds}",
                    )
                )
                config = load_config(path)
                self.assertEqual(
                    generation_cycle_timeout_seconds(config),
                    birds * generation_species_seconds(config) + GENERATION_UNIT_MARGIN_SECONDS,
                )


class GenerationStopTests(unittest.TestCase):
    def test_stop_request_records_the_running_bird_and_ends_the_cycle(self) -> None:
        attempted: list[int] = []

        def stopped(_config: object, species: BirdSpecies, *_args: object, **_kw: object) -> Path:
            attempted.append(species.taxon_id)
            raise GenerationStopRequested(signal.SIGTERM)

        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [FIRST, SECOND])
            with generation(stopped), self.assertRaises(GenerationStopRequested):
                run_generation_cycle(config)
            record = retries(config).get(FIRST.taxon_id)
            second = retries(config).get(SECOND.taxon_id)
            # The cycle lock is released for the next run.
            with exclusive_cycle_lock(config.controller.state_dir):
                pass

        self.assertEqual(attempted, [FIRST.taxon_id])
        self.assertIsNotNone(record)
        if record is not None:
            self.assertEqual(record.error, STOPPED_GENERATION_ERROR)
            self.assertEqual(record.attempts, 1)
            self.assertGreater(record.next_attempt_at, datetime.now(UTC))
        self.assertIsNone(second)

    def test_stop_after_the_bird_was_approved_records_no_failure(self) -> None:
        approved: set[int] = set()

        def approve(_state: Path, _catalog: Path, taxon_id: int) -> object:
            approved.add(taxon_id)
            raise GenerationStopRequested(signal.SIGTERM)

        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [FIRST])
            seed_failures(config, FIRST.taxon_id, 1)
            with (
                generation([Path(temporary) / "candidate"]),
                patch("inky_bird_frame.controller.approve_candidate", side_effect=approve),
                patch(
                    "inky_bird_frame.controller.approved_taxon_ids",
                    side_effect=lambda _catalog: set(approved),
                ),
                self.assertRaises(GenerationStopRequested),
            ):
                run_generation_cycle(config)
            record = retries(config).get(FIRST.taxon_id)

        self.assertEqual(approved, {FIRST.taxon_id})
        self.assertIsNone(record)

    def test_a_stop_never_makes_the_try_that_stops_the_bird(self) -> None:
        stop = GenerationStopRequested(signal.SIGTERM)
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [FIRST])
            seed_failures(config, FIRST.taxon_id, MAX_AUTOMATIC_GENERATION_TRIES - 1)
            with generation(stop), self.assertRaises(GenerationStopRequested):
                run_generation_cycle(config)
            stopped = retries(config).get(FIRST.taxon_id)
            saved_after_stop = list((config.controller.state_dir / "failed").glob("1-*"))
            path = retries(config).path
            raw = json.loads(path.read_text())
            raw["records"][0]["next_attempt_at"] = "2000-01-01T00:00:00+00:00"
            path.write_text(json.dumps(raw))
            with generation(DataSourceError("source down")):
                result = run_generation_cycle(config)

        self.assertIsNotNone(stopped)
        if stopped is not None:
            self.assertEqual(stopped.error, STOPPED_GENERATION_ERROR)
        self.assertEqual(saved_after_stop, [])
        # The next real failure decides.
        self.assertIs(first_failure(result)["terminal"], True)


class RetryCeilingTests(unittest.TestCase):
    def cycle_with(
        self, error: Exception, earlier_failures: int
    ) -> tuple[AppConfig, dict[str, object]]:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = load(temporary.name)
        refresh(config, [FIRST])
        seed_failures(config, FIRST.taxon_id, earlier_failures)
        with generation(error):
            result = run_generation_cycle(config)
        return config, result

    def test_last_automatic_try_stops_the_bird_for_the_owner(self) -> None:
        config, result = self.cycle_with(
            DataSourceError("source down"), MAX_AUTOMATIC_GENERATION_TRIES - 1
        )
        failure = first_failure(result)
        work = read_generation_work(config)

        self.assertIs(failure["terminal"], True)
        self.assertIn("retry this bird", str(failure["error"]))
        self.assertIn("source down", str(failure["error"]))
        self.assertIsNone(retries(config).get(FIRST.taxon_id))
        saved = list((config.controller.state_dir / "failed").glob("1-*/failure.json"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0].read_text())["taxon_id"], FIRST.taxon_id)
        self.assertEqual(
            [entry.species.taxon_id for entry in work.terminal_blocked], [FIRST.taxon_id]
        )
        self.assertEqual(work.actionable, [])

    def test_earlier_tries_keep_retrying_with_backoff(self) -> None:
        config, result = self.cycle_with(
            DataSourceError("source down"), MAX_AUTOMATIC_GENERATION_TRIES - 2
        )
        failure = first_failure(result)
        record = retries(config).get(FIRST.taxon_id)

        self.assertIs(failure["terminal"], False)
        self.assertIsNotNone(record)
        self.assertEqual(list((config.controller.state_dir / "failed").glob("*")), [])

    def test_unexpected_errors_count_toward_the_ceiling(self) -> None:
        config, result = self.cycle_with(
            RuntimeError("/private/path"), MAX_AUTOMATIC_GENERATION_TRIES - 1
        )
        failure = first_failure(result)

        self.assertIs(failure["terminal"], True)
        self.assertNotIn("/private/path", str(failure["error"]))

    def test_shared_daily_research_limit_never_stops_a_bird(self) -> None:
        retry_at = datetime.now(UTC) + timedelta(hours=3)
        config, result = self.cycle_with(
            ResearchLimitError("Daily research limit", retry_at=retry_at),
            MAX_AUTOMATIC_GENERATION_TRIES + 3,
        )

        self.assertIs(first_failure(result)["terminal"], False)
        self.assertIsNotNone(retries(config).get(FIRST.taxon_id))

    def test_daily_research_waits_leave_every_counted_try(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [FIRST])
            already_due = datetime.now(UTC) - timedelta(minutes=1)
            for _ in range(MAX_AUTOMATIC_GENERATION_TRIES - 1):
                with generation(ResearchLimitError("Daily research limit", retry_at=already_due)):
                    waited = run_generation_cycle(config)
                self.assertIs(first_failure(waited)["terminal"], False)
            with generation(DataSourceError("one short outage")):
                result = run_generation_cycle(config)
            record = retries(config).get(FIRST.taxon_id)

        self.assertIs(first_failure(result)["terminal"], False)
        self.assertIsNotNone(record)
        if record is not None:
            self.assertEqual(record.attempts, MAX_AUTOMATIC_GENERATION_TRIES)
            self.assertEqual(record.counted_failures, 1)

    def test_photo_waits_do_not_count_toward_the_ceiling(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [FIRST])
            seed_failures(config, FIRST.taxon_id, MAX_AUTOMATIC_GENERATION_TRIES - 1)
            path = retries(config).path
            raw = json.loads(path.read_text())
            raw["records"][0]["next_attempt_at"] = "2000-01-01T00:00:00+00:00"
            path.write_text(json.dumps(raw))
            with generation(InsufficientReferencesError("not enough photos")):
                result = run_generation_cycle(config)
            record = retries(config).get(FIRST.taxon_id)

        self.assertIs(first_failure(result)["terminal"], False)
        self.assertIsNotNone(record)
        if record is not None:
            self.assertEqual(record.counted_failures, MAX_AUTOMATIC_GENERATION_TRIES - 1)

    def test_long_photo_wait_from_an_earlier_release_starts_the_count_fresh(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [FIRST])
            retries(config).path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "records": [
                            {
                                "taxon_id": FIRST.taxon_id,
                                "attempts": 20,
                                "error_type": "InsufficientReferencesError",
                                "error": "not enough photos",
                                "first_failed_at": "2026-01-01T00:00:00+00:00",
                                "last_failed_at": "2026-05-01T00:00:00+00:00",
                                "next_attempt_at": "2026-05-08T00:00:00+00:00",
                            }
                        ],
                    }
                )
            )
            with generation(DataSourceError("one short outage")):
                result = run_generation_cycle(config)

        self.assertIs(first_failure(result)["terminal"], False)

    def test_a_bird_out_of_research_stops_for_the_owner_at_once(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [FIRST])
            seed_failures(config, FIRST.taxon_id, 1)
            budget = ResearchBudget(
                config.controller.state_dir / "research-budget.json",
                daily_limit=config.research.max_searches_per_day,
                species_limit=config.research.max_searches_per_species,
            )
            for _ in range(config.research.max_searches_per_species):
                budget.consume(FIRST.taxon_id)

            def research(_config: object, species: BirdSpecies, *_a: object, **_k: object) -> Path:
                budget.consume(species.taxon_id)
                raise AssertionError("the research limit was not applied")

            with generation(research):
                result = run_generation_cycle(config)
            failure = first_failure(result)
            saved = list((config.controller.state_dir / "failed").glob("1-*"))
            guidance = terminal_failure(tuple(saved), FIRST.taxon_id)
            record = retries(config).get(FIRST.taxon_id)

        self.assertIs(failure["terminal"], True)
        self.assertIn("retry this bird", str(failure["error"]))
        self.assertIsNone(record)
        self.assertEqual(guidance["failure_kind"], "research_limit")

    def test_a_bird_out_of_research_is_told_so_when_the_daily_limit_was_hit_too(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [FIRST])
            seed_failures(config, FIRST.taxon_id, 1)
            budget = ResearchBudget(
                config.controller.state_dir / "research-budget.json",
                daily_limit=config.research.max_searches_per_day,
                species_limit=config.research.max_searches_per_species,
            )
            for _ in range(config.research.max_searches_per_species):
                budget.consume(FIRST.taxon_id)
            daily = ResearchLimitError(
                "Daily research limit of 9 searches has been reached",
                retry_at=datetime.now(UTC) + timedelta(hours=3),
            )
            with generation(daily):
                result = run_generation_cycle(config)
            failure = first_failure(result)

        self.assertIs(failure["terminal"], True)
        self.assertNotIn("Daily", str(failure["error"]))
        self.assertIn(
            f"research limit of {config.research.max_searches_per_species}", str(failure["error"])
        )

    def test_ceiling_after_source_outages_keeps_the_source_guidance(self) -> None:
        config, result = self.cycle_with(
            DataSourceError("source down"), MAX_AUTOMATIC_GENERATION_TRIES - 1
        )
        saved = list((config.controller.state_dir / "failed").glob("1-*"))
        guidance = terminal_failure(tuple(saved), FIRST.taxon_id)

        self.assertIs(first_failure(result)["terminal"], True)
        self.assertEqual(guidance["failure_kind"], "source")


class ResearchAllowanceTests(unittest.TestCase):
    def budget(self, config: AppConfig) -> ResearchBudget:
        return ResearchBudget(
            config.controller.state_dir / "research-budget.json", daily_limit=9, species_limit=1
        )

    def test_fresh_bird_gets_its_research_allowance_back(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [FIRST])
            self.budget(config).consume(FIRST.taxon_id)
            with generation(DataSourceError("temporary")):
                run_generation_cycle(config)
            # The allowance is back, so one more search fits the limit of one.
            self.budget(config).consume(FIRST.taxon_id)

    def test_automatic_retry_keeps_counting_research(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [FIRST])
            self.budget(config).consume(FIRST.taxon_id)
            seed_failures(config, FIRST.taxon_id, 1)
            with generation(DataSourceError("temporary")):
                run_generation_cycle(config)
            with self.assertRaises(ResearchLimitError):
                self.budget(config).consume(FIRST.taxon_id)


class FakeRunner:
    generation_failures: list[BaseException | None] = []
    reviews: list[QualityReview] = []
    corrections: list[tuple[str, ...]] = []

    def __init__(self, _executable: Path, workspace: Path, **_kwargs: object) -> None:
        self.workspace = workspace

    def create_profile(self, *args: object, **_kwargs: object) -> SpeciesProfileData:
        output_path = args[-2]
        assert isinstance(output_path, Path)
        output_path.write_text(json.dumps(PROFILE))
        return PROFILE

    def generate_plate(self, *args: object, **_kwargs: object) -> Path:
        output_path = args[-3]
        correction = args[-1]
        assert isinstance(output_path, Path) and isinstance(correction, tuple)
        self.corrections.append(correction)
        failure = self.generation_failures.pop(0)
        if failure is not None:
            raise failure
        output_path.write_bytes(b"generated")
        return output_path

    def review_plate(self, *_args: object, **_kwargs: object) -> QualityReview:
        return self.reviews.pop(0)


def prepare(_source: Path, portrait: Path, display: Path) -> None:
    write_png(portrait, color=(200, 40, 40))
    write_png(display, color=(40, 40, 200))


@contextmanager
def fake_codex(
    failures: list[BaseException | None], reviews: list[QualityReview]
) -> Iterator[None]:
    FakeRunner.generation_failures = list(failures)
    FakeRunner.reviews = list(reviews)
    FakeRunner.corrections = []
    with (
        patch("inky_bird_frame.controller.load_or_fetch_references", return_value=[]),
        patch("inky_bird_frame.controller.fetch_taxon_context"),
        patch("inky_bird_frame.controller.CodexRunner", FakeRunner),
        patch("inky_bird_frame.controller.prepare_generated_plate", side_effect=prepare),
        patch("inky_bird_frame.controller.rebuild_catalog_index", return_value=[]),
    ):
        yield


class KeptAttemptTests(unittest.TestCase):
    def test_late_failure_keeps_the_reviewed_attempt_for_the_next_try(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [CARDINAL])
            with fake_codex([None, GenerationError("image tool failed")], [FAILED_REVIEW]):
                result = run_generation_cycle(config)
            store = retries(config)
            guidance = store.quality_guidance(CARDINAL.taxon_id)
            record = store.get(CARDINAL.taxon_id)
            kept = sorted((config.controller.state_dir / "archive").glob("9083-*-interrupted"))
            source = (
                config.controller.state_dir / guidance.source_plate
                if guidance is not None and guidance.source_plate is not None
                else None
            )
            source_exists = source is not None and source.is_file()

            # The next try starts from the kept attempt's corrections.
            store.clear(CARDINAL.taxon_id)
            with generation(DataSourceError("later")) as call:
                run_generation_cycle(config)

        self.assertIs(first_failure(result)["terminal"], False)
        self.assertIsNotNone(record)
        self.assertEqual(len(kept), 1)
        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, ("Crest is too short",))
            self.assertEqual(
                guidance.source_plate, f"archive/{kept[0].name}/attempt-01/portrait.png"
            )
        self.assertTrue(source_exists)
        self.assertEqual(
            call.call_args.kwargs["initial_correction_findings"], ("Crest is too short",)
        )
        self.assertEqual(call.call_args.kwargs["initial_correction_source"], source)

    def conflict_review(self, finding: str) -> QualityReview:
        return QualityReview(
            False,
            3,
            4,
            5,
            5,
            True,
            (finding,),
            correction_findings=(finding,),
            profile_conflicts=(CONFLICT,),
        )

    def test_profile_refresh_outage_after_a_conflict_keeps_the_reviewed_attempt(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            refresh(config, [CARDINAL])
            with (
                fake_codex([None], [self.conflict_review("Habitat is wrong")]),
                patch(
                    "inky_bird_frame.controller._refresh_profile_after_conflict",
                    side_effect=DataSourceError("iNaturalist timed out"),
                ),
            ):
                result = run_generation_cycle(config)
            guidance = retries(config).quality_guidance(CARDINAL.taxon_id)

        self.assertIs(first_failure(result)["terminal"], False)
        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, ("Habitat is wrong", PROFILE_REFRESH_CORRECTION))
            self.assertEqual(guidance.profile_conflicts, (CONFLICT,))
            self.assertIsNotNone(guidance.source_plate)
            self.assertTrue(str(guidance.source_plate).endswith("/attempt-01/portrait.png"))

    def test_profile_refresh_outage_on_a_later_attempt_keeps_that_attempt(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with (
                fake_codex([None, None], [FAILED_REVIEW, self.conflict_review("Bill is pale")]),
                patch(
                    "inky_bird_frame.controller._refresh_profile_after_conflict",
                    side_effect=ResearchLimitError(
                        "Daily research limit", retry_at=datetime.now(UTC)
                    ),
                ),
                self.assertRaises(ResearchLimitError),
            ):
                generate_candidate(config, CARDINAL, config.controller.workspace_dir)
            guidance = retries(config).quality_guidance(CARDINAL.taxon_id)

        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, ("Bill is pale", PROFILE_REFRESH_CORRECTION))
            self.assertEqual(guidance.profile_conflicts, (CONFLICT,))
            self.assertTrue(str(guidance.source_plate).endswith("/attempt-02/portrait.png"))

    def test_kept_run_holds_only_the_attempt_it_resumes_from(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with (
                fake_codex(
                    [None, None, GenerationError("image tool failed")],
                    [FAILED_REVIEW, FAILED_REVIEW],
                ),
                self.assertRaises(GenerationError),
            ):
                generate_candidate(config, CARDINAL, config.controller.workspace_dir)
            kept = sorted((config.controller.state_dir / "archive").glob("9083-*-interrupted"))
            contents = sorted(path.name for path in kept[0].iterdir()) if kept else []

        self.assertEqual(len(kept), 1)
        self.assertEqual(contents, ["attempt-02", "attempt-history.json", "profile.json"])

    def test_a_stop_while_keeping_the_attempt_leaves_no_partial_run(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with (
                fake_codex([None, GenerationError("image tool failed")], [FAILED_REVIEW]),
                patch(
                    "inky_bird_frame.controller.fsync_tree",
                    side_effect=GenerationStopRequested(signal.SIGTERM),
                ),
                self.assertRaises(GenerationStopRequested),
            ):
                generate_candidate(config, CARDINAL, config.controller.workspace_dir)
            archive = config.controller.state_dir / "archive"
            kept = list(archive.glob("9083-*")) if archive.exists() else []
            guidance = retries(config).quality_guidance(CARDINAL.taxon_id)

        self.assertEqual(kept, [])
        self.assertIsNone(guidance)

    def test_stop_during_a_later_attempt_keeps_the_reviewed_attempt(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            stop = GenerationStopRequested(signal.SIGTERM)
            with (
                fake_codex([None, stop], [FAILED_REVIEW]),
                self.assertRaises(GenerationStopRequested),
            ):
                generate_candidate(config, CARDINAL, config.controller.workspace_dir)
            guidance = retries(config).quality_guidance(CARDINAL.taxon_id)

        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, ("Crest is too short",))

    def test_failure_before_any_review_keeps_nothing(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with (
                fake_codex([GenerationError("image tool failed")], []),
                self.assertRaises(GenerationError),
            ):
                generate_candidate(config, CARDINAL, config.controller.workspace_dir)
            guidance = retries(config).quality_guidance(CARDINAL.taxon_id)
            archive = config.controller.state_dir / "archive"
            kept = list(archive.glob("*")) if archive.exists() else []

        self.assertIsNone(guidance)
        self.assertEqual(kept, [])

    def test_existing_owner_guidance_stays_when_nothing_new_was_reviewed(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            retries(config).set_quality_guidance(
                CARDINAL.taxon_id, ("Owner correction",), invariant_findings=("Owner correction",)
            )
            with (
                fake_codex([GenerationError("image tool failed")], []),
                self.assertRaises(GenerationError),
            ):
                generate_candidate(
                    config,
                    CARDINAL,
                    config.controller.workspace_dir,
                    invariant_correction_findings=("Owner correction",),
                )
            guidance = retries(config).quality_guidance(CARDINAL.taxon_id)

        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, ("Owner correction",))
            self.assertIsNone(guidance.source_plate)


def live_names(common_name: str) -> TaxonContext:
    return TaxonContext(
        9083,
        common_name,
        "Cardinalis cardinalis",
        "Cardinalidae",
        "Summary",
        "https://example.test",
    )


class RenamedProfileTests(unittest.TestCase):
    def write_cached(self, config: AppConfig, common_name: str) -> Path:
        path = config.controller.state_dir / "profiles" / "9083" / "profile.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({**PROFILE, "common_name": common_name}))
        return path

    def test_renamed_bird_without_a_retry_researches_the_new_name(self) -> None:
        runner = MagicMock()
        runner.create_profile.return_value = PROFILE
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            cached = self.write_cached(config, "Old Cardinal Name")
            with patch(
                "inky_bird_frame.controller.fetch_taxon_context",
                return_value=live_names("Northern Cardinal"),
            ) as fetch:
                profile, _ = load_or_create_profile(
                    config,
                    CARDINAL,
                    [],
                    [],
                    runner,
                    Path(temporary) / "profile.json",
                    Path(temporary) / "profile.log",
                )
            archived = list((config.controller.state_dir / "archive").glob("9083*/profile.json"))
            archived_name = json.loads(archived[0].read_text())["common_name"] if archived else ""
            refreshed = json.loads(cached.read_text())

        runner.create_profile.assert_called_once()
        fetch.assert_called_once()
        self.assertEqual(profile["common_name"], "Northern Cardinal")
        self.assertEqual(archived_name, "Old Cardinal Name")
        self.assertEqual(refreshed["common_name"], "Northern Cardinal")

    def test_a_stale_discovery_name_keeps_the_researched_profile(self) -> None:
        runner = MagicMock()
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            cached = self.write_cached(config, "Old Cardinal Name")
            with (
                patch(
                    "inky_bird_frame.controller.fetch_taxon_context",
                    return_value=live_names("Old Cardinal Name"),
                ),
                self.assertRaises(SpeciesStateError),
            ):
                load_or_create_profile(
                    config,
                    CARDINAL,
                    [],
                    [],
                    runner,
                    Path(temporary) / "profile.json",
                    Path(temporary) / "profile.log",
                )
            kept = json.loads(cached.read_text())["common_name"]
            archive = config.controller.state_dir / "archive"
            archived = list(archive.glob("*")) if archive.exists() else []

        runner.create_profile.assert_not_called()
        self.assertEqual(kept, "Old Cardinal Name")
        self.assertEqual(archived, [])

    def test_renamed_bird_without_research_still_stops(self) -> None:
        runner = MagicMock()
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(CONFIG + "\n[research]\nenabled = false\n")
            config = load_config(path)
            cached = self.write_cached(config, "Old Cardinal Name")
            with self.assertRaises(SpeciesStateError):
                load_or_create_profile(
                    config,
                    CARDINAL,
                    [],
                    [],
                    runner,
                    Path(temporary) / "profile.json",
                    Path(temporary) / "profile.log",
                )
            kept = cached.is_file()

        runner.create_profile.assert_not_called()
        self.assertTrue(kept)


class StopSignalCommandTests(unittest.TestCase):
    def test_stop_signal_becomes_a_stop_request_while_generation_runs(self) -> None:
        from inky_bird_frame.cli import _stop_signal_raises

        before = signal.getsignal(signal.SIGTERM)
        with _stop_signal_raises():
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            with self.assertRaises(GenerationStopRequested) as raised:
                handler(signal.SIGTERM, None)
            # A second stop while the try is saved is ignored.
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
        self.assertEqual(raised.exception.signum, signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_generate_ends_by_the_stop_signal_after_saving_the_try(self) -> None:
        from argparse import Namespace

        from inky_bird_frame.cli import generate_command

        config = MagicMock()
        config.controller.generation_enabled = True
        with (
            patch("inky_bird_frame.cli._config", return_value=config),
            patch(
                "inky_bird_frame.cli.run_generation_cycle",
                side_effect=GenerationStopRequested(signal.SIGTERM),
            ),
            patch("inky_bird_frame.cli.safe_record_degradation") as degradation,
            patch("inky_bird_frame.cli.signal.signal") as set_handler,
            patch("inky_bird_frame.cli.os.kill") as kill,
        ):
            code = generate_command(Namespace())

        set_handler.assert_any_call(signal.SIGTERM, signal.SIG_DFL)
        kill.assert_called_once()
        self.assertEqual(kill.call_args.args[1], signal.SIGTERM)
        self.assertEqual(code, 128 + signal.SIGTERM)
        degradation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
