from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from inky_bird_frame import birds
from inky_bird_frame.birds import (
    BirdSpecies,
    EbirdArchiveSpecies,
    EbirdSpecies,
    ObservationWindow,
    fetch_ebird_observations,
    fetch_taxon_context,
    parse_inaturalist_taxon,
    parse_inaturalist_taxon_match,
    resolve_ebird_archive_names,
    resolve_ebird_species,
)
from inky_bird_frame.config import load_config
from inky_bird_frame.controller import load_or_create_profile
from inky_bird_frame.errors import CatalogError, DataSourceError, TaxonomyMatchError
from inky_bird_frame.settings import initialize_config

NOW = datetime(2026, 11, 14, tzinfo=UTC)


def _observations(count: int) -> list[EbirdSpecies]:
    return [
        EbirdSpecies(f"code{index}", f"Bird {index}", f"Avis number{index}", "2026-11-14")
        for index in range(count)
    ]


def _species(name: str, taxon_id: int) -> BirdSpecies:
    return BirdSpecies(taxon_id, f"Common {taxon_id}", name, 1, "eBird")


def _entries(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    return dict(json.loads(path.read_text())["entries"])


def _taxon(**changes: object) -> dict[str, object]:
    return {
        "id": 1,
        "preferred_common_name": "Example Jay",
        "name": "Aphelocoma exampla",
        "rank": "species",
        "is_active": True,
        "iconic_taxon_name": "Aves",
    } | changes


class _CountingLock:
    def __init__(self) -> None:
        self.acquired = 0
        self.original = birds._taxonomy_crosswalk_lock

    @contextmanager
    def __call__(self, cache_path: Path) -> Iterator[None]:
        with self.original(cache_path):
            self.acquired += 1
            yield


def test_interrupted_resolution_keeps_completed_lookups_and_checkpoints(tmp_path: Path) -> None:
    cache = tmp_path / "crosswalk.json"
    observations = _observations(30)
    on_disk_before_lookup: list[int] = []
    lock = _CountingLock()

    def lookup(name: str, timeout_seconds: float) -> BirdSpecies:
        on_disk_before_lookup.append(len(_entries(cache)))
        index = len(on_disk_before_lookup)
        if index == 28:
            raise RuntimeError("process interrupted")
        return _species(name, index)

    with (
        patch("inky_bird_frame.birds.fetch_inaturalist_taxon_match", side_effect=lookup),
        patch("inky_bird_frame.birds._taxonomy_crosswalk_lock", lock),
        pytest.raises(RuntimeError, match="interrupted"),
    ):
        resolve_ebird_species(observations, cache, now=NOW)

    # The first 25 were saved before the 26th request, and the 27 completed
    # lookups survive the interruption.
    assert on_disk_before_lookup[25] == 25
    assert len(_entries(cache)) == 27
    assert lock.acquired == 2

    with patch("inky_bird_frame.birds.fetch_inaturalist_taxon_match") as fetch:
        fetch.side_effect = lambda name, timeout_seconds: _species(name, 100)
        result = resolve_ebird_species(observations, cache, now=NOW)
    assert fetch.call_count == 3
    assert len(result.species) == 30
    assert [item.common_name for item in result.species][:2] == ["Common 1", "Common 2"]


def test_resolution_releases_the_lock_after_its_hold_time(tmp_path: Path) -> None:
    cache = tmp_path / "crosswalk.json"
    clock = [0.0]
    on_disk_before_lookup: list[int] = []
    lock = _CountingLock()

    def lookup(name: str, timeout_seconds: float) -> BirdSpecies:
        on_disk_before_lookup.append(len(_entries(cache)))
        clock[0] += 20.0
        return _species(name, len(on_disk_before_lookup))

    with (
        patch("inky_bird_frame.birds.fetch_inaturalist_taxon_match", side_effect=lookup),
        patch("inky_bird_frame.birds.monotonic", side_effect=lambda: clock[0]),
        patch("inky_bird_frame.birds._taxonomy_crosswalk_lock", lock),
    ):
        result = resolve_ebird_species(_observations(3), cache, now=NOW)

    assert len(result.species) == 3
    assert on_disk_before_lookup == [0, 0, 2]
    assert lock.acquired == 2


def test_archive_names_resolve_in_one_bounded_call(tmp_path: Path) -> None:
    cache = tmp_path / "crosswalk.json"
    names = [EbirdArchiveSpecies(f"Bird {index}", f"Avis number{index}", 1) for index in range(5)]
    calls: list[str] = []

    def lookup(name: str, timeout_seconds: float) -> BirdSpecies:
        calls.append(name)
        if name == "Avis number1":
            raise TaxonomyMatchError("no exact match")
        return _species(name, len(calls))

    with patch("inky_bird_frame.birds.fetch_inaturalist_taxon_match", side_effect=lookup):
        first = resolve_ebird_archive_names(names, cache, max_lookups=3, now=NOW)
        second = resolve_ebird_archive_names(names, cache, max_lookups=3, now=NOW)

    assert calls == [f"Avis number{index}" for index in range(5)]
    assert sorted(first.resolved) == ["Avis number0", "Avis number2"]
    assert first.unresolved == [names[1]]
    assert first.deferred == names[3:]
    assert sorted(second.resolved) == [
        "Avis number0",
        "Avis number2",
        "Avis number3",
        "Avis number4",
    ]
    assert second.unresolved == [names[1]]
    assert second.deferred == []


def test_archive_name_deadline_defers_the_rest(tmp_path: Path) -> None:
    cache = tmp_path / "crosswalk.json"
    names = [EbirdArchiveSpecies(f"Bird {index}", f"Avis number{index}", 1) for index in range(4)]
    clock = [100.0]

    def lookup(name: str, timeout_seconds: float) -> BirdSpecies:
        clock[0] += 25.0
        return _species(name, 7)

    with (
        patch("inky_bird_frame.birds.fetch_inaturalist_taxon_match", side_effect=lookup) as fetch,
        patch("inky_bird_frame.birds.monotonic", side_effect=lambda: clock[0]),
    ):
        result = resolve_ebird_archive_names(names, cache, deadline_seconds=40.0, now=NOW)

    assert fetch.call_count == 2
    assert sorted(result.resolved) == ["Avis number0", "Avis number1"]
    assert result.deferred == names[2:]
    assert len(_entries(cache)) == 2


def test_exact_scientific_name_beats_a_synonym_after_a_split() -> None:
    payload = {
        "results": [
            _taxon(id=2, name="Aphelocoma woodhousei", matched_term="Aphelocoma exampla"),
            _taxon(),
        ]
    }

    species = parse_inaturalist_taxon_match(payload, "Aphelocoma exampla")

    assert species.taxon_id == 1
    assert species.scientific_name == "Aphelocoma exampla"


def test_two_synonym_only_matches_remain_ambiguous() -> None:
    payload = {
        "results": [
            _taxon(id=2, name="Aphelocoma woodhousei", matched_term="Aphelocoma exampla"),
            _taxon(id=3, name="Aphelocoma californica", matched_term="Aphelocoma exampla"),
        ]
    }

    with pytest.raises(TaxonomyMatchError, match="found 2"):
        parse_inaturalist_taxon_match(payload, "Aphelocoma exampla")


@patch("inky_bird_frame.birds.sleep")
@patch("inky_bird_frame.birds.get_json")
def test_split_shape_resolves_instead_of_caching_a_miss(
    get_json: MagicMock, sleep: MagicMock, tmp_path: Path
) -> None:
    get_json.return_value = {
        "results": [
            _taxon(id=2, name="Aphelocoma woodhousei", matched_term="Aphelocoma exampla"),
            _taxon(),
        ]
    }
    cache = tmp_path / "crosswalk.json"
    observation = EbirdSpecies("exajay", "Example Jay", "Aphelocoma exampla", "2026-11-14")

    result = resolve_ebird_species([observation], cache, now=NOW)

    assert result.unresolved == []
    assert result.species[0].taxon_id == 1
    assert "retry_at" not in cast_entry(_entries(cache)["exajay"])


def cast_entry(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _context_payload(**changes: object) -> dict[str, object]:
    return {
        "results": [
            {
                "id": 41,
                "preferred_common_name": "Example Jay",
                "name": "Aphelocoma exampla",
                "wikipedia_summary": "",
                "ancestors": [{"rank": "family", "name": "Corvidae"}],
                "is_active": True,
            }
            | changes
        ]
    }


def test_inactive_taxon_names_its_successor() -> None:
    with pytest.raises(DataSourceError, match="replaced by taxon 42") as raised:
        parse_inaturalist_taxon(
            _context_payload(is_active=False, current_synonymous_taxon_ids=[42])
        )
    assert not isinstance(raised.value, CatalogError)


def test_inactive_taxon_without_a_successor_is_reported() -> None:
    with pytest.raises(DataSourceError, match="no current replacement"):
        parse_inaturalist_taxon(_context_payload(is_active=False))


def test_missing_active_flag_is_not_treated_as_inactive() -> None:
    payload = _context_payload(wikipedia_summary="A jay.")
    del cast_entry(cast_list(payload["results"])[0])["is_active"]
    assert parse_inaturalist_taxon(payload).taxon_id == 41


def cast_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def test_inactive_taxon_stops_before_fallback_context() -> None:
    with (
        patch(
            "inky_bird_frame.birds.get_json",
            return_value=_context_payload(is_active=False, current_synonymous_taxon_ids=[42]),
        ) as get_json,
        pytest.raises(DataSourceError, match="replaced by taxon 42"),
    ):
        fetch_taxon_context(41)
    get_json.assert_called_once()


def test_inactive_taxon_stops_profile_creation_before_research(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    initialize_config(path)
    config = load_config(path, load_secrets=False)
    runner = MagicMock()
    species = BirdSpecies(41, "Example Jay", "Aphelocoma exampla", 1, "eBird")
    with (
        patch(
            "inky_bird_frame.birds.get_json",
            return_value=_context_payload(is_active=False, current_synonymous_taxon_ids=[42]),
        ),
        pytest.raises(DataSourceError, match="replaced by taxon 42"),
    ):
        load_or_create_profile(
            config, species, [], [], runner, tmp_path / "profile.json", tmp_path / "log"
        )
    runner.create_profile.assert_not_called()
    assert not (config.controller.state_dir / "research-budget.json").exists()


def test_failed_ebird_request_does_not_reveal_coordinates() -> None:
    opener = MagicMock()
    opener.return_value.open.side_effect = URLError("unreachable")
    with (
        patch("inky_bird_frame.http.build_opener", opener),
        pytest.raises(DataSourceError) as raised,
    ):
        fetch_ebird_observations(
            latitude=1.23456,
            longitude=2.34567,
            radius_km=5,
            limit=10,
            window=ObservationWindow.LAST_WEEK,
            api_key="synthetic-key",
        )
    message = str(raised.value)
    assert "eBird API" in message
    for fragment in ("1.234560", "2.345670", "lat=", "lng=", "synthetic-key"):
        assert fragment not in message


@patch("inky_bird_frame.birds.sleep")
@patch("inky_bird_frame.birds.get_json")
def test_miss_cached_under_the_previous_rule_is_rechecked(
    get_json: MagicMock, sleep: MagicMock, tmp_path: Path
) -> None:
    get_json.return_value = {
        "results": [
            _taxon(id=2, name="Aphelocoma woodhousei", matched_term="Aphelocoma exampla"),
            _taxon(),
        ]
    }
    cache = tmp_path / "crosswalk.json"
    old_miss = {
        "match_strategy": "scientific-name-or-exact-synonym-v1",
        "scientific_name": "Aphelocoma exampla",
        "retry_at": "2026-11-20T00:00:00+00:00",
    }
    cache.write_text(json.dumps({"schema_version": 1, "entries": {"exajay": old_miss}}))
    observation = EbirdSpecies("exajay", "Example Jay", "Aphelocoma exampla", "2026-11-14")

    result = resolve_ebird_species([observation], cache, now=NOW)

    get_json.assert_called_once()
    assert [item.taxon_id for item in result.species] == [1]
