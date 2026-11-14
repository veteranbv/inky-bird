from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import stat
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from ebird_fixtures import EXPORT_COLUMNS
from starlette.testclient import TestClient

from inky_bird_frame import birdbuddy, birdnet_analyzer, ebird_archive
from inky_bird_frame.access import AccessStore
from inky_bird_frame.birdbuddy import (
    BirdBuddyPostcard,
    ConfirmedSpeciesEvidence,
    PostcardSpecies,
    _update_history,
    read_birdbuddy_history_species,
    read_birdbuddy_history_summary,
)
from inky_bird_frame.birdnet_analyzer import (
    import_birdnet_analyzer_csv,
    read_birdnet_analyzer_history,
    read_birdnet_analyzer_summary,
)
from inky_bird_frame.birds import ObservationWindow
from inky_bird_frame.config import ControllerConfig
from inky_bird_frame.ebird_archive import (
    import_ebird_archive,
    read_ebird_archive_history,
    read_ebird_archive_summary,
)
from inky_bird_frame.history_summary import summary_path
from inky_bird_frame.http import write_json_atomic
from inky_bird_frame.journal import (
    ChecklistEvidence,
    CollectionKind,
    JournalCollection,
    JournalMember,
    JournalService,
    MemberOrigin,
    save_imported_collection,
)
from inky_bird_frame.presentation import PresentationAccess
from inky_bird_frame.web import create_app

ORIGIN = "https://inky.example.test"
STAMP = "2026-10-15T12:00:00+00:00"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-summary-image"
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
EBIRD = "ebird-archive-observations.json"
ANALYZER = "birdnet-analyzer-detections.json"
FEEDER = "birdbuddy-detections.json"
NEW_FIELDS = ("first_noted", "last_noted", "recent_species")


def controller(root: Path) -> ControllerConfig:
    return ControllerConfig(
        root / "work",
        root / "catalog",
        root / "state",
        Path("codex"),
        None,
        "127.0.0.1",
        8000,
        3,
        1,
        3,
        owner_allowed_origins=(ORIGIN,),
    )


def approved_catalog(config: ControllerConfig) -> None:
    species = config.catalog_dir / "species/1-robin"
    species.mkdir(parents=True)
    for kind in ("portrait", "display"):
        (species / f"{kind}.png").write_bytes(PNG)
    entry = {
        "taxon_id": 1,
        "common_name": "Robin",
        "scientific_name": "Turdus migratorius",
        "slug": "robin",
        "portrait_path": "species/1-robin/portrait.png",
        "display_path": "species/1-robin/display.png",
        "portrait_sha256": hashlib.sha256(PNG).hexdigest(),
        "display_sha256": hashlib.sha256(PNG).hexdigest(),
        "approved_at": STAMP,
    }
    write_json_atomic(
        config.catalog_dir / "index.json",
        {"schema_version": 1, "generated_at": STAMP, "species": [entry]},
    )


def ebird_import(state_dir: Path, root: Path, rows: list[tuple[str, str, str, str]]) -> None:
    path = root / "MyEBirdData.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS, restval="")
        writer.writeheader()
        for submission, common, scientific, observed in rows:
            writer.writerow(
                {
                    "Submission ID": submission,
                    "Common Name": common,
                    "Scientific Name": scientific,
                    "Date": observed,
                    "Location": "Exampleville fixture pond",
                }
            )
    import_ebird_archive(path, state_dir)


def analyzer_import(
    state_dir: Path, root: Path, rows: list[tuple[str, str, str]], observed_on: date | None
) -> None:
    path = root / "analyzer.csv"
    lines = ["Start (s),End (s),Scientific name,Common name,Confidence,File"]
    lines += [f"0,3,{scientific},{common},0.9,{file}" for scientific, common, file in rows]
    path.write_text("\n".join(lines) + "\n")
    import_birdnet_analyzer_csv(path, state_dir, observed_on=observed_on)


def postcard(identifier: str, observed: datetime, species: PostcardSpecies) -> BirdBuddyPostcard:
    return BirdBuddyPostcard(
        identifier, observed.isoformat(), (species,), media_ids=(f"media-{identifier}",)
    )


ROBIN = PostcardSpecies("species-robin", "Robin", "Turdus migratorius")
WREN = PostcardSpecies("species-wren", "Carolina Wren", "Thryothorus ludovicianus")
FINCH = PostcardSpecies("species-finch", "House Finch", "Haemorhous mexicanus")


def feeder_sync(
    state_dir: Path,
    postcards: list[BirdBuddyPostcard],
    confirmed: tuple[ConfirmedSpeciesEvidence, ...] = (),
) -> None:
    _update_history(
        state_dir,
        "feeder-1",
        postcards,
        list(confirmed),
        NOW,
        persist=True,
        replace_manual_evidence=True,
        confirmed_species_by_media={},
    )


def all_histories(root: Path, state_dir: Path) -> None:
    ebird_import(
        state_dir,
        root,
        [
            ("S1", "Robin", "Turdus migratorius", "2019-03-12"),
            ("S2", "American Robin", "Turdus migratorius", "2024-05-01"),
            ("S2", "Unmatched", "Fixture unknown", "2024-05-01"),
        ],
    )
    analyzer_import(
        state_dir,
        root,
        [("Turdus migratorius", "Robin", "a.wav"), ("Fixture x", "X", "a.wav")],
        date(2026, 5, 1),
    )
    analyzer_import(state_dir, root, [("Turdus migratorius", "Robin", "b.wav")], None)
    feeder_sync(
        state_dir,
        [
            postcard("p1", NOW - timedelta(days=3), ROBIN),
            postcard("p2", NOW - timedelta(days=1), ROBIN),
            postcard("p3", NOW - timedelta(days=2), WREN),
        ],
        (ConfirmedSpeciesEvidence(FINCH, (NOW - timedelta(hours=2)).isoformat(), "manual"),),
    )


def full_rows(
    state_dir: Path, *, include_manual: bool = False
) -> dict[str, list[tuple[str, str, int]]]:
    archive = read_ebird_archive_history(state_dir, window=ObservationWindow.ALL_TIME, limit=None)
    analyzer = read_birdnet_analyzer_history(
        state_dir, window=ObservationWindow.ALL_TIME, limit=None
    )
    feeder, _ = read_birdbuddy_history_species(state_dir, include_manual_sightings=include_manual)
    return {
        "ebird-archive": [
            (item.common_name, item.scientific_name, item.observation_count)
            for item in archive.species
        ],
        "birdnet-analyzer": [
            (item.common_name, item.scientific_name, item.detection_count)
            for item in analyzer.species
        ],
        "birdbuddy": [
            (item.common_name, item.scientific_name, item.detection_count) for item in feeder
        ],
    }


def summary_rows(
    state_dir: Path, *, include_manual: bool = False
) -> dict[str, list[tuple[str, str, int]]]:
    feeder, _ = read_birdbuddy_history_summary(state_dir, include_manual_sightings=include_manual)
    return {
        "ebird-archive": [
            (row.common_name, row.scientific_name, row.count)
            for row in read_ebird_archive_summary(state_dir)
        ],
        "birdnet-analyzer": [
            (row.common_name, row.scientific_name, row.count)
            for row in read_birdnet_analyzer_summary(state_dir)[0]
        ],
        "birdbuddy": [(row.common_name, row.scientific_name, row.count) for row in feeder],
    }


@contextmanager
def refuse_full_reads() -> Iterator[None]:
    message = "a fresh summary must not parse complete history"
    patches = [
        patch.object(ebird_archive, "_read_state", side_effect=AssertionError(message)),
        patch.object(birdnet_analyzer, "_read_state", side_effect=AssertionError(message)),
        patch.object(birdbuddy, "_read_history", side_effect=AssertionError(message)),
        patch.object(
            ebird_archive, "read_ebird_archive_history", side_effect=AssertionError(message)
        ),
        patch.object(
            birdnet_analyzer,
            "read_birdnet_analyzer_history",
            side_effect=AssertionError(message),
        ),
        patch.object(
            birdbuddy, "read_birdbuddy_history_species", side_effect=AssertionError(message)
        ),
    ]
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        yield


def evidence(snapshot: dict[str, object]) -> list[dict[str, object]]:
    birds = cast(list[dict[str, object]], snapshot["birds"])
    robin = next(bird for bird in birds if bird["taxon_id"] == 1)
    return cast(list[dict[str, object]], robin["retained_evidence"])


def test_fresh_summaries_answer_the_journal_without_reading_full_histories(
    tmp_path: Path,
) -> None:
    config = controller(tmp_path)
    approved_catalog(config)
    all_histories(tmp_path, config.state_dir)
    # Straight after the writers: their summaries answer without a full parse.
    with refuse_full_reads():
        snapshot = JournalService(config).snapshot()
    assert snapshot == JournalService(config).snapshot()
    assert evidence(snapshot) == [
        {
            "source": "ebird-archive",
            "count": 2,
            "unit": "checklists",
            "first_noted": "2019-03-12",
            "last_noted": "2024-05-01",
        },
        {
            "source": "birdnet-analyzer",
            "count": 2,
            "unit": "recording segments",
            "first_noted": "2026-05-01",
            "last_noted": "2026-05-01",
        },
        {
            "source": "birdbuddy",
            "count": 2,
            "unit": "postcards or confirmed presence",
            "first_noted": (NOW - timedelta(days=3)).astimezone().date().isoformat(),
            "last_noted": (NOW - timedelta(days=1)).astimezone().date().isoformat(),
        },
    ]
    coverage = cast(list[dict[str, object]], snapshot["history_coverage"])
    assert [row["status"] for row in coverage] == ["available"] * 3
    assert coverage[1]["coverage"] == "Retained imports; 1 undated segments"
    assert snapshot["recent_species"] == [
        {
            "taxon_id": 1,
            "last_noted": (NOW - timedelta(days=1)).astimezone().date().isoformat(),
            "source": "birdbuddy",
        }
    ]


def test_writer_summaries_match_a_rebuild_and_old_installs_keep_their_counts(
    tmp_path: Path,
) -> None:
    config = controller(tmp_path)
    approved_catalog(config)
    state = config.state_dir
    all_histories(tmp_path, state)
    written = {
        name: json.loads(summary_path(state / name).read_text())
        for name in (EBIRD, ANALYZER, FEEDER)
    }
    with_summaries = JournalService(config).snapshot()
    for name in (EBIRD, ANALYZER, FEEDER):
        summary_path(state / name).unlink()
    # An install from before summaries existed: the first read builds them.
    rebuilt = JournalService(config).snapshot()
    assert rebuilt == with_summaries
    for name, payload in written.items():
        assert json.loads(summary_path(state / name).read_text()) == payload
    for include_manual in (False, True):
        assert summary_rows(state, include_manual=include_manual) == full_rows(
            state, include_manual=include_manual
        )
    unresolved = cast(list[dict[str, object]], rebuilt["unresolved_history"])
    assert sorted((row["source"], row["scientific_name"], row["count"]) for row in unresolved) == [
        ("birdbuddy", "Thryothorus ludovicianus", 1),
        ("birdnet-analyzer", "Fixture x", 1),
        ("ebird-archive", "Fixture unknown", 1),
    ]


@pytest.mark.parametrize("change", ["rewrite", "restore-previous", "crash-before-summary"])
def test_stale_identity_rebuilds_exactly_once(tmp_path: Path, change: str) -> None:
    config = controller(tmp_path)
    approved_catalog(config)
    state = config.state_dir
    ebird_import(state, tmp_path, [("S1", "Robin", "Turdus migratorius", "2019-03-12")])
    history = state / EBIRD
    older = tmp_path / "older.json"
    shutil.copy2(history, older)
    if change == "crash-before-summary":
        with patch.object(ebird_archive, "write_summary"):
            ebird_import(
                state,
                tmp_path,
                [
                    ("S1", "Robin", "Turdus migratorius", "2019-03-12"),
                    ("S2", "Robin", "Turdus migratorius", "2026-11-02"),
                ],
            )
        expected = (2, "2026-11-02")
    elif change == "restore-previous":
        ebird_import(
            state,
            tmp_path,
            [
                ("S1", "Robin", "Turdus migratorius", "2019-03-12"),
                ("S2", "Robin", "Turdus migratorius", "2026-11-02"),
            ],
        )
        assert evidence(JournalService(config).snapshot())[0]["count"] == 2
        # An operator restores an older copy, keeping its original times.
        shutil.copy2(older, history)
        os.chmod(history, 0o600)
        expected = (1, "2019-03-12")
    else:
        os.utime(history, ns=(history.stat().st_atime_ns, history.stat().st_mtime_ns + 1000))
        expected = (1, "2019-03-12")
    with patch.object(ebird_archive, "_read_state", wraps=ebird_archive._read_state) as reader:
        first = evidence(JournalService(config).snapshot())
        second = evidence(JournalService(config).snapshot())
    assert reader.call_count == 1
    assert first == second
    assert (first[0]["count"], first[0]["last_noted"]) == expected


def test_concurrent_readers_rebuild_a_missing_summary_once(tmp_path: Path) -> None:
    state = tmp_path / "state"
    ebird_import(state, tmp_path, [("S1", "Robin", "Turdus migratorius", "2019-03-12")])
    summary_path(state / EBIRD).unlink()
    calls = []
    lock = threading.Lock()
    original = ebird_archive._read_state

    def slow(path: Path, *, required: bool) -> object:
        with lock:
            calls.append(1)
        time.sleep(0.2)
        return original(path, required=required)

    with (
        patch.object(ebird_archive, "_read_state", side_effect=slow),
        ThreadPoolExecutor(4) as pool,
    ):
        results = list(pool.map(lambda _: read_ebird_archive_summary(state), range(4)))
    assert len(calls) == 1
    assert all(result == results[0] for result in results)


def test_first_and_last_dates_follow_two_imports(tmp_path: Path) -> None:
    state = tmp_path / "state"
    ebird_import(
        state,
        tmp_path,
        [
            ("S2", "Robin", "Turdus migratorius", "2020-01-01"),
            ("S1", "Robin", "Turdus migratorius", "2019-03-12"),
        ],
    )
    ebird_import(
        state,
        tmp_path,
        [
            ("S1", "Robin", "Turdus migratorius", "2019-03-12"),
            ("S2", "Robin", "Turdus migratorius", "2020-01-01"),
            ("S3", "American Robin", "Turdus migratorius", "2026-11-02"),
            ("S3", "Carolina Wren", "Thryothorus ludovicianus", "2026-11-02"),
        ],
    )
    rows = {row.scientific_name: row for row in read_ebird_archive_summary(state)}
    robin = rows["Turdus migratorius"]
    assert (robin.count, robin.first_noted, robin.last_noted) == (3, "2019-03-12", "2026-11-02")
    assert robin.common_name == "American Robin"
    assert (
        rows["Thryothorus ludovicianus"].first_noted,
        rows["Thryothorus ludovicianus"].count,
    ) == (
        "2026-11-02",
        1,
    )
    analyzer_import(state, tmp_path, [("Turdus migratorius", "Robin", "a.wav")], date(2026, 5, 1))
    analyzer_import(state, tmp_path, [("Turdus migratorius", "Robin", "b.wav")], date(2026, 6, 9))
    analyzer_import(state, tmp_path, [("Turdus migratorius", "Robin", "c.wav")], None)
    detections, undated = read_birdnet_analyzer_summary(state)
    assert undated == 1
    assert [(row.count, row.undated, row.first_noted, row.last_noted) for row in detections] == [
        (3, 1, "2026-05-01", "2026-06-09")
    ]
    analyzer_import(state, tmp_path, [("Fixture x", "X", "d.wav")], None)
    detections, undated = read_birdnet_analyzer_summary(state)
    undated_only = next(row for row in detections if row.scientific_name == "Fixture x")
    assert (undated, undated_only.first_noted, undated_only.last_noted) == (2, None, None)


def test_feeder_manual_counts_apply_at_read_time_and_archived_first_is_unknown(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    old = postcard("p-old", NOW - timedelta(days=400), WREN)
    feeder_sync(
        state,
        [old, postcard("p-new", NOW - timedelta(days=5), WREN)],
        (ConfirmedSpeciesEvidence(FINCH, (NOW - timedelta(hours=2)).isoformat(), "manual"),),
    )
    automatic, _ = read_birdbuddy_history_summary(state)
    manual, description = read_birdbuddy_history_summary(state, include_manual_sightings=True)
    assert [row.scientific_name for row in automatic] == ["Thryothorus ludovicianus"]
    assert [(row.scientific_name, row.count) for row in manual] == [
        ("Thryothorus ludovicianus", 2),
        ("Haemorhous mexicanus", 1),
    ]
    wren = manual[0]
    assert wren.first_noted is None  # The oldest postcard was archived with its count only.
    assert wren.last_noted == (NOW - timedelta(days=5)).astimezone().date().isoformat()
    assert description.startswith("Since integration began")


def test_summaries_are_private_and_hold_no_places_or_identifiers(tmp_path: Path) -> None:
    state = tmp_path / "state"
    all_histories(tmp_path, state)
    for name in (EBIRD, ANALYZER, FEEDER):
        path = summary_path(state / name)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        body = path.read_text()
        for private in ("Exampleville", "S1", "a.wav", "media-p1", "p1"):
            assert private not in body
    loose = summary_path(state / EBIRD)
    loose.chmod(0o644)
    with patch.object(ebird_archive, "_read_state", wraps=ebird_archive._read_state) as reader:
        read_ebird_archive_summary(state)
    assert reader.call_count == 1
    assert stat.S_IMODE(loose.stat().st_mode) == 0o600


def test_recent_species_are_newest_first_at_most_ten_and_owner_only(tmp_path: Path) -> None:
    config = controller(tmp_path)
    approved_catalog(config)
    state = config.state_dir
    rows = [
        (f"S{index}", f"Fixture {index}", f"Fixture species{index}", f"2026-01-{index:02d}")
        for index in range(1, 13)
    ]
    ebird_import(state, tmp_path, [*rows, ("S20", "Robin", "Turdus migratorius", "2025-12-31")])
    write_json_atomic(
        state / "ebird-archive-taxonomy-crosswalk.json",
        {
            "schema_version": 1,
            "entries": {
                f"species{index}": {
                    "source_scientific_name": f"Fixture species{index}",
                    "taxon_id": 100 + index,
                    "common_name": f"Fixture {index}",
                    "scientific_name": f"Fixture species{index}",
                }
                for index in range(1, 13)
            },
        },
    )
    snapshot = JournalService(config).snapshot()
    recent = cast(list[dict[str, object]], snapshot["recent_species"])
    assert [row["taxon_id"] for row in recent] == list(range(112, 102, -1))
    assert recent[0] == {"taxon_id": 112, "last_noted": "2026-01-12", "source": "ebird-archive"}
    write_json_atomic(
        state / "active-catalog.json",
        {"schema_version": 1, "generated_at": STAMP, "species": []},
    )
    access = PresentationAccess(state, JournalService(config))
    with (
        TestClient(create_app(config), base_url=ORIGIN) as owner,
        TestClient(create_app(config), base_url=ORIGIN) as screen,
        TestClient(create_app(config), base_url=ORIGIN) as guest,
    ):
        AccessStore(state).setup_owner("fixture-owner", "long synthetic password")
        login = owner.post(
            "/v1/owner/login",
            json={"username": "fixture-owner", "passphrase": "long synthetic password"},
            headers={"Origin": ORIGIN},
        )
        headers = {"Origin": ORIGIN, "X-Inky-CSRF": login.json()["data"]["csrf"]}
        owned = owner.get("/v1/owner/journal")
        assert owned.status_code == 200 and "recent_species" in owned.text
        assert (
            screen.post("/v1/screen/pair", json={}, headers={"Origin": ORIGIN}).status_code == 200
        )
        status = owner.get("/v1/owner/presentation").json()["data"]
        paired = owner.post(
            "/v1/owner/presentation",
            json={
                "revision": status["revision"],
                "action": "pair",
                "id": status["grants"][0]["id"],
                "name": "Fixture screen",
                "view": "all",
                "confirm": True,
            },
            headers=headers,
        )
        assert paired.status_code == 200
        access.update(revision=access.status()["revision"], action="wall", view="all", confirm=True)
        responses = {
            "screen": screen.get("/v1/screen/view"),
            "wall": guest.get("/wall/v1/catalog"),
            "guest-journal": guest.get("/v1/owner/journal"),
            "guest-access": guest.get("/v1/access"),
        }
    assert responses["screen"].status_code == 200
    assert responses["wall"].status_code == 200
    assert responses["guest-journal"].status_code == 401
    for name, response in responses.items():
        for field in (*NEW_FIELDS, "2026-01-12", "2025-12-31"):
            assert field not in response.text, (name, field)


def test_trip_evidence_status_is_parsed_once_per_history_and_journal_change(
    tmp_path: Path,
) -> None:
    config = controller(tmp_path)
    approved_catalog(config)
    state = config.state_dir
    ebird_import(state, tmp_path, [("S1", "Robin", "Turdus migratorius", "2019-03-12")])
    trip = JournalCollection(
        "b" * 32,
        "Fixture trip",
        (
            JournalMember(
                1,
                "Robin",
                "Turdus migratorius",
                MemberOrigin.EBIRD,
                (ChecklistEvidence(hashlib.sha256(b"S1").hexdigest(), "Turdus migratorius"),),
            ),
        ),
        CollectionKind.TRIP,
        "2019-03-01",
        "2019-03-31",
        1,
        1,
    )
    save_imported_collection(state, revision=0, collection=trip)
    service = JournalService(config)

    def status() -> object:
        collections = cast(list[dict[str, object]], service.snapshot()["collections"])
        return cast(list[dict[str, object]], collections[0]["evidence"])[0]["status"]

    with patch.object(ebird_archive, "_read_state", wraps=ebird_archive._read_state) as reader:
        assert status() == "linked"
        assert status() == "linked"
        assert reader.call_count == 1
        # A complete re-export without that checklist changes its status at once.
        ebird_import_reduced = [("S2", "Robin", "Turdus migratorius", "2019-03-13")]
        path = tmp_path / "MyEBirdData.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS, restval="")
            writer.writeheader()
            for submission, common, scientific, observed in ebird_import_reduced:
                writer.writerow(
                    {
                        "Submission ID": submission,
                        "Common Name": common,
                        "Scientific Name": scientific,
                        "Date": observed,
                        "Location": "Exampleville fixture pond",
                    }
                )
        import_ebird_archive(path, state, allow_history_reduction=True)
        calls = reader.call_count
        assert status() == "source-changed"
        assert reader.call_count == calls + 1


def test_deeply_nested_summary_is_rebuilt(tmp_path: Path) -> None:
    state = tmp_path / "state"
    ebird_import(state, tmp_path, [("S1", "Robin", "Turdus migratorius", "2019-03-12")])
    expected = read_ebird_archive_summary(state)
    path = summary_path(state / EBIRD)
    path.write_text("[" * 200_000)
    path.chmod(0o600)
    assert read_ebird_archive_summary(state) == expected


def test_analyzer_name_is_the_last_one_stored_even_from_an_older_dated_batch(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    analyzer_import(
        state, tmp_path, [("Turdus migratorius", "Newer name", "a.wav")], date(2026, 5, 2)
    )
    analyzer_import(
        state, tmp_path, [("Turdus migratorius", "Older name", "b.wav")], date(2026, 5, 1)
    )
    full = read_birdnet_analyzer_history(state, window=ObservationWindow.ALL_TIME, limit=None)
    assert [item.common_name for item in full.species] == ["Older name"]
    assert [row.common_name for row in read_birdnet_analyzer_summary(state)[0]] == ["Older name"]
    summary_path(state / ANALYZER).unlink()
    assert [row.common_name for row in read_birdnet_analyzer_summary(state)[0]] == ["Older name"]


def test_a_failed_trip_evidence_read_is_retried_on_the_next_snapshot(tmp_path: Path) -> None:
    from inky_bird_frame.errors import DataSourceError

    config = controller(tmp_path)
    approved_catalog(config)
    state = config.state_dir
    ebird_import(state, tmp_path, [("S1", "Robin", "Turdus migratorius", "2019-03-12")])
    trip = JournalCollection(
        "b" * 32,
        "Fixture trip",
        (
            JournalMember(
                1,
                "Robin",
                "Turdus migratorius",
                MemberOrigin.EBIRD,
                (ChecklistEvidence(hashlib.sha256(b"S1").hexdigest(), "Turdus migratorius"),),
            ),
        ),
        CollectionKind.TRIP,
        "2019-03-01",
        "2019-03-31",
        1,
        1,
    )
    save_imported_collection(state, revision=0, collection=trip)
    service = JournalService(config)

    def status() -> object:
        collections = cast(list[dict[str, object]], service.snapshot()["collections"])
        return cast(list[dict[str, object]], collections[0]["evidence"])[0]["status"]

    with patch.object(ebird_archive, "read_ebird_checklists", side_effect=DataSourceError("busy")):
        assert status() == "unavailable"
    assert status() == "linked"
