from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.errors import CatalogError, ResearchLimitError
from inky_bird_frame.research import ResearchBudget, SpeciesResearchLimitError


class ResearchBudgetTests(unittest.TestCase):
    def test_enforces_daily_and_per_species_limits(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            budget = ResearchBudget(Path(temporary) / "budget.json", daily_limit=3, species_limit=2)
            budget.consume(1, now)
            budget.consume(1, now)
            with self.assertRaisesRegex(ResearchLimitError, "research limit") as species_limit:
                budget.consume(1, now)
            self.assertIsInstance(species_limit.exception, SpeciesResearchLimitError)
            self.assertEqual(species_limit.exception.retry_at, now + timedelta(days=1))
            budget.consume(2, now)
            with self.assertRaisesRegex(ResearchLimitError, "Daily research limit") as daily_limit:
                budget.consume(3, now)
            self.assertNotIsInstance(daily_limit.exception, SpeciesResearchLimitError)
            self.assertEqual(daily_limit.exception.retry_at, now + timedelta(days=1))

    def test_new_utc_day_resets_the_daily_total(self) -> None:
        now = datetime(2026, 7, 10, 23, 59, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            budget = ResearchBudget(Path(temporary) / "budget.json", daily_limit=1, species_limit=9)
            budget.consume(1, now)
            budget.consume(2, now + timedelta(minutes=2))

    def test_species_limit_is_a_total_across_days(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            budget = ResearchBudget(Path(temporary) / "budget.json", daily_limit=9, species_limit=2)
            budget.consume(1, now)
            budget.consume(1, now + timedelta(days=1))
            with self.assertRaises(SpeciesResearchLimitError):
                budget.consume(1, now + timedelta(days=2))
            with self.assertRaises(SpeciesResearchLimitError):
                budget.consume(1, now + timedelta(days=30))
            budget.consume(2, now + timedelta(days=2))

    def test_reset_species_allows_new_research(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "budget.json"
            budget = ResearchBudget(path, daily_limit=9, species_limit=1)
            budget.consume(1, now)
            budget.consume(2, now)
            budget.reset_species(1)
            budget.consume(1, now + timedelta(days=1))
            with self.assertRaises(SpeciesResearchLimitError):
                budget.consume(2, now + timedelta(days=1))

    def test_reset_without_a_record_writes_nothing(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "budget.json"
            ResearchBudget(path, daily_limit=9, species_limit=1).reset_species(1)
            self.assertFalse(path.exists())

    def test_earlier_budget_file_counts_its_species_toward_the_total(self) -> None:
        now = datetime(2026, 7, 11, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "budget.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "date": "2026-07-10",
                        "total": 1,
                        "species": {"1": 1},
                    }
                )
            )
            budget = ResearchBudget(path, daily_limit=9, species_limit=2)
            budget.consume(1, now)
            with self.assertRaises(SpeciesResearchLimitError):
                budget.consume(1, now + timedelta(days=1))

    def test_file_keeps_the_daily_fields_earlier_releases_read(self) -> None:
        now = datetime(2026, 7, 11, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "budget.json"
            budget = ResearchBudget(path, daily_limit=9, species_limit=5)
            budget.consume(1, now - timedelta(days=1))
            budget.consume(1, now)
            raw = json.loads(path.read_text())
        self.assertEqual(raw["schema_version"], 1)
        self.assertEqual(raw["date"], "2026-07-11")
        self.assertEqual(raw["total"], 1)
        self.assertEqual(raw["species"], {"1": 1})
        self.assertEqual(raw["species_totals"], {"1": 2})

    def test_invalid_species_totals_are_refused(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "budget.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "date": "2026-07-10",
                        "total": 0,
                        "species": {},
                        "species_totals": {"1": -1},
                    }
                )
            )
            budget = ResearchBudget(path, daily_limit=9, species_limit=2)
            with self.assertRaisesRegex(CatalogError, "research budget"):
                budget.consume(1, datetime(2026, 7, 10, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
