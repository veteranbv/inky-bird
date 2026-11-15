"""Durable limits for subscription-backed web research."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from .errors import CatalogError, ResearchLimitError
from .http import write_json_atomic


class SpeciesResearchLimitError(ResearchLimitError):
    """Raised when one species has used its total research allowance."""


class ResearchBudget:
    """A daily search total plus a per-species total that does not reset by day.

    The per-species total is cleared only by reset_species, which the generation
    cycle calls when a bird starts fresh rather than as an automatic retry.
    """

    def __init__(self, path: Path, *, daily_limit: int, species_limit: int) -> None:
        self.path = path
        self.daily_limit = daily_limit
        self.species_limit = species_limit

    def consume(self, taxon_id: int, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        day, total, species, totals = self._read()
        if day != current.date():
            total = 0
            species = {}
        retry_at = datetime.combine(current.date() + timedelta(days=1), time.min, tzinfo=UTC)
        if total >= self.daily_limit:
            raise ResearchLimitError(
                f"Daily research limit of {self.daily_limit} searches has been reached",
                retry_at=retry_at,
            )
        if totals.get(taxon_id, 0) >= self.species_limit:
            raise SpeciesResearchLimitError(
                f"Taxon {taxon_id} reached its research limit of {self.species_limit} searches",
                retry_at=retry_at,
            )
        species[taxon_id] = species.get(taxon_id, 0) + 1
        totals[taxon_id] = totals.get(taxon_id, 0) + 1
        self._write(current.date(), total + 1, species, totals)

    def reset_species(self, taxon_id: int) -> None:
        if not self.path.exists():
            return
        day, total, species, totals = self._read()
        if totals.pop(taxon_id, None) is None:
            return
        self._write(day, total, species, totals)

    def _write(
        self, day: date, total: int, species: dict[int, int], totals: dict[int, int]
    ) -> None:
        # date, total and species keep their earlier meaning for older readers.
        write_json_atomic(
            self.path,
            {
                "schema_version": 1,
                "date": day.isoformat(),
                "total": total,
                "species": {str(key): value for key, value in sorted(species.items())},
                "species_totals": {str(key): value for key, value in sorted(totals.items())},
            },
        )

    def _read(self) -> tuple[date, int, dict[int, int], dict[int, int]]:
        if not self.path.exists():
            return date.min, 0, {}, {}
        try:
            raw = json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise CatalogError(f"Invalid research budget: {self.path}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise CatalogError(f"Unsupported research budget: {self.path}")
        day = raw.get("date")
        total = raw.get("total")
        species = raw.get("species")
        if (
            not isinstance(day, str)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or not isinstance(species, dict)
        ):
            raise CatalogError(f"Invalid research budget: {self.path}")
        try:
            parsed_day = date.fromisoformat(day)
        except ValueError as exc:
            raise CatalogError(f"Invalid research budget date: {self.path}") from exc
        parsed_species = self._counts(species)
        raw_totals = raw.get("species_totals")
        if raw_totals is None:
            # A file from an earlier release kept only one day's counts; they are
            # the only usage on record, so they start the totals.
            return parsed_day, total, parsed_species, dict(parsed_species)
        if not isinstance(raw_totals, dict):
            raise CatalogError(f"Invalid research budget: {self.path}")
        return parsed_day, total, parsed_species, self._counts(raw_totals)

    def _counts(self, raw: dict[object, object]) -> dict[int, int]:
        parsed: dict[int, int] = {}
        for key, value in raw.items():
            try:
                taxon_id = int(str(key))
            except ValueError as exc:
                raise CatalogError(f"Invalid research budget taxon: {self.path}") from exc
            if taxon_id <= 0 or not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CatalogError(f"Invalid research budget count: {self.path}")
            parsed[taxon_id] = value
        return parsed
