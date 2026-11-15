from __future__ import annotations

import csv
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from ebird_fixtures import EXPORT_COLUMNS as EBIRD_COLUMNS
from starlette.testclient import TestClient

from inky_bird_frame.access import AccessStore
from inky_bird_frame.birds import BirdSpecies, ObservationWindow
from inky_bird_frame.catalog import (
    CollectionEntry,
    CollectionOrigin,
    rebuild_catalog_index,
    write_collection,
)
from inky_bird_frame.config import (
    AppConfig,
    ControllerConfig,
    DiscoveryConfig,
    DiscoveryProvider,
    DisplayNodeConfig,
)
from inky_bird_frame.controller import GenerationWork, TerminalQueueEntry
from inky_bird_frame.ebird_archive import import_ebird_archive
from inky_bird_frame.errors import CatalogError
from inky_bird_frame.http import write_json_atomic
from inky_bird_frame.journal import (
    MAX_JOURNAL_BYTES,
    ChecklistEvidence,
    CollectionKind,
    JournalCollection,
    JournalConflictError,
    JournalError,
    JournalMember,
    JournalService,
    JournalState,
    MemberOrigin,
    failure_guidance,
    read_journal,
    remove_collection,
    save_collection,
    save_imported_collection,
)
from inky_bird_frame.retry import RetryRecord
from inky_bird_frame.web import create_app

STAMP = "2026-10-15T12:00:00+00:00"
ORIGIN = "https://inky.example.test"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-test-image"


def config(root: Path) -> ControllerConfig:
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


def catalog(controller: ControllerConfig) -> None:
    species = controller.catalog_dir / "species/1-robin"
    species.mkdir(parents=True)
    (species / "portrait.png").write_bytes(PNG)
    entry = {
        "taxon_id": 1,
        "common_name": "Robin",
        "scientific_name": "Turdus migratorius",
        "slug": "robin",
        "portrait_path": "species/1-robin/portrait.png",
        "display_path": "species/1-robin/display.png",
        "portrait_sha256": hashlib.sha256(PNG).hexdigest(),
        "display_sha256": "a" * 64,
        "approved_at": STAMP,
        "private_account": "DO_NOT_PROJECT",
    }
    write_json_atomic(
        controller.catalog_dir / "index.json",
        {"schema_version": 1, "generated_at": STAMP, "species": [entry]},
    )
    write_json_atomic(
        species / "manifest.json",
        {"status": "approved", "profile": {}, "private_review": "DO_NOT_PROJECT"},
    )


def test_named_selections_leave_legacy_membership_and_generation_untouched(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    write_collection(
        controller.state_dir,
        [CollectionEntry(1, STAMP, CollectionOrigin.MANUAL)],
        legacy_seed_queue_migrated_at=None,
    )
    before = (controller.state_dir / "collection.json").read_bytes()
    first = save_collection(
        controller.state_dir, revision=0, label="  Birds to learn  ", taxon_ids=[2, 1]
    )
    assert first.collections[0].taxon_ids == (1, 2)
    assert first.collections[0].label == "Birds to learn"
    assert (controller.state_dir / "journal.json").stat().st_mode & 0o777 == 0o600
    snapshot = JournalService(controller).snapshot()
    birds = snapshot["birds"]
    assert isinstance(birds, list)
    assert [bird["taxon_id"] for bird in birds] == [1, 2]
    assert birds[1]["plate_ready"] is False
    assert snapshot["active_taxon_ids"] == []
    assert (controller.state_dir / "collection.json").read_bytes() == before
    assert not (controller.state_dir / "generation-queue.json").exists()
    assert not (controller.state_dir / "display-last-success.json").exists()
    assert "DO_NOT_PROJECT" not in json.dumps(snapshot)
    removed = remove_collection(
        controller.state_dir, revision=1, collection_id=first.collections[0].collection_id
    )
    assert removed.collections == ()
    assert (controller.state_dir / "collection.json").read_bytes() == before


def test_manual_selection_retains_names_and_rejects_unknown_ids(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    service = JournalService(controller)
    state = service.save_selection(revision=0, label="Learning birds", taxon_ids=[1])
    assert state.collections[0].members == (JournalMember(1, "Robin", "Turdus migratorius"),)
    (controller.catalog_dir / "index.json").unlink()
    birds = cast(list[dict[str, object]], service.snapshot()["birds"])
    assert birds[0]["scientific_name"] == "Turdus migratorius"
    with pytest.raises(JournalError, match="Choose birds"):
        service.save_selection(revision=1, label="Unknown", taxon_ids=[999])
    assert read_journal(controller.state_dir) == state


def _import_archive(state_dir: Path, root: Path, rows: list[tuple[str, str, str, str]]) -> None:
    path = root / "MyEBirdData.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EBIRD_COLUMNS, restval="")
        writer.writeheader()
        for submission, common, scientific, observed in rows:
            writer.writerow(
                {
                    "Submission ID": submission,
                    "Common Name": common,
                    "Scientific Name": scientific,
                    "Date": observed,
                    "Location": "Fixture place",
                }
            )
    import_ebird_archive(path, state_dir)


def test_retained_library_does_not_truncate_or_publish_partial_source_coverage(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    catalog(controller)
    _import_archive(
        controller.state_dir,
        tmp_path,
        [
            *[(f"S{day}", "Robin", "Turdus migratorius", f"2020-01-0{day}") for day in range(1, 5)],
            ("S5", "Unmatched", "Fixture unknown", "2020-01-05"),
            ("S6", "Unmatched", "Fixture unknown", "2020-01-06"),
            ("S7", "Another", "Fixture another", "2020-01-07"),
        ],
    )
    service = JournalService(controller)
    snapshot = service.snapshot()
    birds = cast(list[dict[str, object]], snapshot["birds"])
    assert birds[0]["known"] and birds[0]["retained_evidence"] == [
        {
            "source": "ebird-archive",
            "count": 4,
            "unit": "checklists",
            "first_noted": "2020-01-01",
            "last_noted": "2020-01-04",
        }
    ]
    assert len(cast(list[object], snapshot["unresolved_history"])) == 2
    assert not (controller.state_dir / "generation-queue.json").exists()
    summary = controller.state_dir / "ebird-archive-observations-summary.json"
    payload = json.loads(summary.read_text())
    payload["species"][1]["common_name"] = "Bad\nname"
    write_json_atomic(summary, payload, mode=0o600)
    # The history itself is sound, so an unshowable summary is rebuilt from it.
    assert service.snapshot() == snapshot
    assert "Bad" not in summary.read_text()


def test_browsing_art_never_claims_private_evidence_or_writes_state(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    snapshot = JournalService(controller).snapshot()
    assert snapshot["collections"] == []
    assert snapshot["saved_taxon_ids"] == snapshot["active_taxon_ids"] == []
    assert isinstance(snapshot["birds"], list)
    assert snapshot["birds"][0]["known"] is False
    assert snapshot["birds"][0]["sources"] == []
    assert not (controller.state_dir / "journal.json").exists()
    assert not (controller.state_dir / "collection.json").exists()


def test_first_run_with_canonical_empty_approved_index_is_a_valid_empty_journal(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    rebuild_catalog_index(controller.catalog_dir)
    before = (controller.catalog_dir / "index.json").read_bytes()
    snapshot = JournalService(controller).snapshot()
    assert snapshot["birds"] == []
    assert snapshot["featured_taxon_id"] is None
    assert (controller.catalog_dir / "index.json").read_bytes() == before
    write_json_atomic(
        controller.catalog_dir / "index.json",
        {
            "schema_version": True,
            "generated_at": None,
            "species": [],
        },
    )
    with pytest.raises(CatalogError):
        JournalService(controller).snapshot()


def test_stale_edits_and_concurrent_creates_do_not_overwrite(tmp_path: Path) -> None:
    def create(label: str) -> object:
        try:
            return save_collection(tmp_path, revision=0, label=label, taxon_ids=[])
        except JournalConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, ["First", "Second"]))
    assert sum(isinstance(value, JournalConflictError) for value in outcomes) == 1
    assert len(read_journal(tmp_path).collections) == 1
    with pytest.raises(JournalConflictError):
        remove_collection(tmp_path, revision=0, collection_id="a" * 32)
    with pytest.raises(JournalConflictError):
        save_collection(tmp_path, revision=1, label="Missing", taxon_ids=[], collection_id="a" * 32)


def test_storage_limit_checks_exact_persisted_bytes_and_preserves_previous_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = save_collection(tmp_path, revision=0, label="First", taxon_ids=list(range(1, 30)))
    before = (tmp_path / "journal.json").read_bytes()
    next_group = type(first.collections[0])(
        "a" * 32, "Second", tuple(JournalMember(id) for id in range(1, 30))
    )
    proposed = JournalState(2, (*first.collections, next_group))
    compact_bytes = len(json.dumps(proposed.as_dict()).encode())
    persisted_bytes = len(
        (json.dumps(proposed.as_dict(), indent=2, sort_keys=True) + "\n").encode()
    )
    limit = max(compact_bytes, len(before)) + 1
    assert compact_bytes < limit < persisted_bytes
    monkeypatch.setattr("inky_bird_frame.journal.MAX_JOURNAL_BYTES", limit)
    with pytest.raises(JournalError, match="storage is full"):
        save_collection(tmp_path, revision=1, label="Second", taxon_ids=list(range(1, 30)))
    assert (tmp_path / "journal.json").read_bytes() == before
    assert read_journal(tmp_path) == first


@pytest.mark.parametrize("taxa", [[True], [0], [-1], [1, 1], ["1"], "1"])
def test_invalid_taxa_fail_before_state_write(tmp_path: Path, taxa: object) -> None:
    with pytest.raises(JournalError):
        save_collection(tmp_path, revision=0, label="Birds", taxon_ids=taxa)
    assert not (tmp_path / "journal.json").exists()


@pytest.mark.parametrize("label", ["", "   ", "a" * 129, "Birds\nsecret", "Birds\u202e"])
def test_invalid_labels_fail_closed(tmp_path: Path, label: str) -> None:
    with pytest.raises(JournalError):
        save_collection(tmp_path, revision=0, label=label, taxon_ids=[])


def test_private_state_permissions_symlink_corruption_and_bound(tmp_path: Path) -> None:
    save_collection(tmp_path, revision=0, label="Birds", taxon_ids=[])
    path = tmp_path / "journal.json"
    path.chmod(0o644)
    with pytest.raises(JournalError, match="0600"):
        read_journal(tmp_path)
    path.chmod(0o600)
    path.write_text("x" * (MAX_JOURNAL_BYTES + 1))
    with pytest.raises(JournalError, match="size"):
        read_journal(tmp_path)
    path.write_text("not json")
    with pytest.raises(JournalError, match="cannot be read"):
        read_journal(tmp_path)
    target = tmp_path / "outside.json"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(JournalError, match="regular file"):
        read_journal(tmp_path)


def test_profile_and_portrait_allowlists_integrity_and_symlink_boundary(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    service = JournalService(controller)
    assert service.portrait(1) == PNG
    assert service.profile(1)["profile"] is None
    assert "DO_NOT_PROJECT" not in json.dumps(service.profile(1))
    with pytest.raises(JournalError):
        service.portrait(99)
    path = controller.catalog_dir / "species/1-robin/portrait.png"
    path.write_bytes(b"corrupt")
    with pytest.raises(JournalError, match="integrity"):
        service.portrait(1)
    outside = tmp_path / "secret"
    outside.write_bytes(PNG)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(JournalError, match="unavailable"):
        service.portrait(1)


def test_every_journal_route_uses_owner_and_csrf_boundary(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    AccessStore(controller.state_dir).setup_owner("qa-owner", "a synthetic long passphrase")
    with TestClient(create_app(controller), base_url=ORIGIN) as client:
        for route in [
            "/v1/owner/journal",
            "/v1/owner/journal/birds/1/portrait",
            "/v1/owner/journal/birds/1/profile",
        ]:
            response = client.get(route)
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        login = client.post(
            "/v1/owner/login",
            headers={"Origin": ORIGIN},
            json={"username": "qa-owner", "passphrase": "a synthetic long passphrase"},
        )
        csrf = login.json()["data"]["csrf"]
        body = {"revision": 0, "label": "<script>not executable</script>", "taxon_ids": [1]}
        assert (
            client.post(
                "/v1/owner/journal/collections/save", json=body, headers={"Origin": ORIGIN}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/v1/owner/journal/collections/save",
                json=body,
                headers={"Origin": "https://wrong.example", "X-Inky-CSRF": csrf},
            ).status_code
            == 403
        )
        saved = client.post(
            "/v1/owner/journal/collections/save",
            json=body,
            headers={"Origin": ORIGIN, "X-Inky-CSRF": csrf},
        )
        assert saved.status_code == 200
        assert (
            client.post(
                "/v1/owner/journal/collections/save",
                json=body,
                headers={"Origin": ORIGIN, "X-Inky-CSRF": csrf},
            ).status_code
            == 409
        )
        assert client.get("/v1/owner/journal/birds/1/portrait").content == PNG
        assert client.get("/v1/owner/journal").json()["data"]["revision"] == 1
        assert client.get("/app/journal.js").status_code == 200
        assert client.get("/app/../journal.json").status_code == 404
        # Corruption errors never echo a private filename or a stored payload.
        (controller.state_dir / "journal.json").write_text("PRIVATE_PAYLOAD")
        failed = client.get("/v1/owner/journal")
        assert failed.status_code == 400
        assert "PRIVATE_PAYLOAD" not in failed.text and str(tmp_path) not in failed.text


def test_journal_keeps_missing_birds_and_projects_all_source_types_without_raw_errors(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    catalog(controller)
    app = AppConfig(
        DiscoveryConfig(
            "12345",
            None,
            None,
            None,
            None,
            8,
            20,
            ObservationWindow.LAST_30_DAYS,
            tuple(DiscoveryProvider),
        ),
        controller,
        DisplayNodeConfig("http://localhost:8000", tmp_path / "display"),
    )
    write_json_atomic(
        controller.state_dir / "discovery.json",
        {
            "schema_version": 2,
            "refreshed_at": STAMP,
            "place_name": "PRIVATE_PLACE",
            "state": "",
            "species": [
                {
                    "taxon_id": 2,
                    "common_name": "Missing bird",
                    "scientific_name": "Example bird",
                    "observation_count": 1,
                    "source": "ebird-archive",
                    "sources": ["ebird-archive"],
                }
            ],
            "notification_baseline_taxon_ids": [2],
            "providers": [
                {
                    "name": "ebird-archive",
                    "status": "ok",
                    "species_count": 1,
                    "details": {"raw_checklist_id": "PRIVATE_ID"},
                },
                {
                    "name": "birdbuddy",
                    "status": "error",
                    "species_count": 0,
                    "error": "PRIVATE_EMAIL_AND_TOKEN",
                    "details": {"feeder_id": "PRIVATE_ID"},
                },
            ],
        },
    )
    payload = JournalService(controller, app_config=app).snapshot()
    assert isinstance(payload["birds"], list)
    missing = next(bird for bird in payload["birds"] if bird["taxon_id"] == 2)
    assert missing["known"] and not missing["plate_ready"]
    assert missing["sources"] == ["ebird-archive"]
    management = payload["management"]
    assert isinstance(management, dict)
    assert len(management["sources"]) == 7
    assert management["work"]["can_run"] == []  # Stale discovery must not invent eligible work.
    assert "PRIVATE_" not in json.dumps(payload)
    assert management["last_confirmed_panel_update"] is None


def test_management_names_stopped_work_without_exposing_private_failure_details(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    stamp = datetime.fromisoformat(STAMP)
    manual = RetryRecord(
        2,
        3,
        "PRIVATE_ERROR_TYPE",
        "PRIVATE_FAILURE_BODY",
        stamp,
        stamp,
        stamp,
        "American Robin",
        "Turdus migratorius",
    )
    unnamed = RetryRecord(3, 1, "PRIVATE_ERROR_TYPE", "PRIVATE_FAILURE_BODY", stamp, stamp, stamp)
    terminal = TerminalQueueEntry(
        BirdSpecies(4, "Northern Cardinal", "Cardinalis cardinalis", 1, "fixture"),
        "quality-filtered",
        (tmp_path / "PRIVATE_RUN_PATH",),
    )
    work = GenerationWork(True, "fresh", stamp, [], [], [unnamed], [manual], [terminal])
    app = AppConfig(
        DiscoveryConfig("12345", None, None, None, None, 8, 20, ObservationWindow.LAST_30_DAYS),
        controller,
        DisplayNodeConfig("http://localhost:8000", tmp_path / "display"),
    )
    with patch("inky_bird_frame.journal.read_generation_work", return_value=work):
        management = JournalService(controller, app_config=app).management(approved=set())
    projected = cast(dict[str, object], management["work"])
    assert projected["waiting"] == [3]
    assert projected["needs_attention"] == [2, 4]
    assert projected["retry_details"] == [
        {
            "taxon_id": 3,
            "common_name": None,
            "scientific_name": None,
            "attempts": 1,
            "next_attempt_at": STAMP,
            "last_failed_at": STAMP,
            **failure_guidance(None),
        },
        {
            "taxon_id": 2,
            "common_name": "American Robin",
            "scientific_name": "Turdus migratorius",
            "attempts": 3,
            "next_attempt_at": STAMP,
            "last_failed_at": STAMP,
            **failure_guidance(None),
        },
    ]
    assert projected["terminal_details"] == [
        {
            "taxon_id": 4,
            "common_name": "Northern Cardinal",
            "scientific_name": "Cardinalis cardinalis",
            "state": "quality-filtered",
            "last_failed_at": None,
            **failure_guidance(None),
        }
    ]
    assert "PRIVATE_" not in json.dumps(management)


def test_quality_hold_shows_numeric_evidence_without_raw_model_content(tmp_path: Path) -> None:
    from inky_bird_frame.journal import terminal_failure

    failed = tmp_path / "42-synthetic-failure"
    write_json_atomic(
        failed / "attempt-history.json",
        {
            "schema_version": 1,
            "attempts": [
                {
                    "started_at": STAMP,
                    "outcome": "correction_required",
                    "quality_review": {
                        "anatomy_accuracy": 2,
                        "species_accuracy": 5,
                        "text_accuracy": True,
                        "composition_quality": 99,
                        "correction_findings": ["PRIVATE_MODEL_CONTENT"],
                        "sources": ["PRIVATE_SOURCE"],
                    },
                }
            ],
        },
    )
    summary = terminal_failure((failed,), 42)
    assert summary["failure_kind"] == "quality_review"
    assert summary["recorded_attempts"] == 1
    assert summary["review_scores"] == {"anatomy_accuracy": 2, "species_accuracy": 5}
    assert summary["last_attempt_at"] == STAMP
    assert "PRIVATE_" not in json.dumps(summary)


def test_management_poll_does_not_join_source_history(tmp_path: Path) -> None:
    from inky_bird_frame.config import load_config
    from inky_bird_frame.settings import initialize_config

    path = tmp_path / "config.toml"
    initialize_config(path)
    app = load_config(path, load_secrets=False)
    with patch.object(
        JournalService, "_retained_history", side_effect=AssertionError("must not scan history")
    ):
        result = JournalService(app.controller, app_config=app).management_snapshot()
    assert result["available"] is True


def test_merged_latest_timestamp_is_not_attributed_to_each_provider(tmp_path: Path) -> None:
    controller = config(tmp_path)
    app = AppConfig(
        DiscoveryConfig(
            "12345",
            None,
            None,
            None,
            None,
            8,
            20,
            ObservationWindow.LAST_30_DAYS,
            sources=(
                DiscoveryProvider.BIRDWEATHER,
                DiscoveryProvider.BIRDBUDDY,
                DiscoveryProvider.EBIRD_ARCHIVE,
            ),
        ),
        controller,
        DisplayNodeConfig("http://localhost:8000", tmp_path / "display"),
    )
    earlier = "2026-10-15T11:00:00+00:00"
    write_json_atomic(
        controller.state_dir / "discovery.json",
        {
            "schema_version": 1,
            "refreshed_at": STAMP,
            "place_name": "",
            "state": "",
            "species": [
                {
                    "taxon_id": 1,
                    "common_name": "Combined",
                    "scientific_name": "Avis combinata",
                    "observation_count": 1,
                    "source": "birdweather+birdbuddy+ebird-archive",
                    "sources": ["birdweather", "birdbuddy", "ebird-archive"],
                    "latest_detection_at": STAMP,
                },
                {
                    "taxon_id": 2,
                    "common_name": "Single",
                    "scientific_name": "Avis sola",
                    "observation_count": 1,
                    "source": "birdweather",
                    "sources": ["birdweather"],
                    "latest_detection_at": earlier,
                },
            ],
        },
    )
    management = JournalService(controller, app_config=app).management(approved=set())
    sources = {row["name"]: row for row in cast(list[dict[str, object]], management["sources"])}
    assert sources["birdweather"]["latest_detection_at"] == earlier
    assert sources["birdbuddy"]["latest_detection_at"] is None
    assert sources["ebird-archive"]["latest_detection_at"] is None
    records = cast(list[dict[str, object]], management["recent_detections"])
    assert records[0]["taxon_id"] == 1 and records[0]["timestamp_source"] is None
    assert records[1]["timestamp_source"] == "birdweather"


def test_v1_migration_happens_only_on_edit_and_keeps_exact_private_backup(tmp_path: Path) -> None:
    original = {
        "schema_version": 1,
        "revision": 4,
        "collections": [{"collection_id": "a" * 32, "label": "Old collection", "taxon_ids": [1]}],
    }
    write_json_atomic(tmp_path / "journal.json", original, mode=0o600)
    before = (tmp_path / "journal.json").read_bytes()
    state = read_journal(tmp_path)
    assert state.collections[0].kind == CollectionKind.SELECTION
    assert state.collections[0].members[0].origin == MemberOrigin.SAVED
    assert not (tmp_path / "journal-v1-backup.json").exists()
    assert (tmp_path / "journal.json").read_bytes() == before
    save_collection(tmp_path, revision=4, label="Renamed", taxon_ids=[1], collection_id="a" * 32)
    backup = tmp_path / "journal-v1-backup.json"
    assert backup.read_bytes() == before
    assert backup.stat().st_mode & 0o777 == 0o600
    assert read_journal(tmp_path).revision == 5
    save_collection(tmp_path, revision=5, label="Another", taxon_ids=[])
    assert backup.read_bytes() == before


def test_favorites_are_separate_idempotent_bookmarks_with_stale_edit_protection(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    catalog(controller)
    service = JournalService(controller)
    first = service.favorite(revision=0, taxon_id=1, favorite=True)
    assert first.collections[0].kind == CollectionKind.FAVORITES
    assert first.collections[0].taxon_ids == (1,)
    assert service.favorite(revision=1, taxon_id=1, favorite=True) == first
    with pytest.raises(JournalConflictError):
        service.favorite(revision=0, taxon_id=1, favorite=False)
    with pytest.raises(JournalError):
        remove_collection(
            controller.state_dir, revision=1, collection_id=first.collections[0].collection_id
        )
    with pytest.raises(JournalError):
        service.favorite(revision=1, taxon_id=1, favorite="true")
    second = service.favorite(revision=1, taxon_id=1, favorite=False)
    assert second.collections[0].taxon_ids == ()
    assert not (controller.state_dir / "collection.json").exists()
    assert not (controller.state_dir / "generation-queue.json").exists()


@pytest.mark.parametrize("name", ["x" * 257, "Bird\nName"])
def test_favorite_rejects_unstorable_source_names_before_writing(tmp_path: Path, name: str) -> None:
    controller = config(tmp_path)
    catalog(controller)
    first = save_collection(controller.state_dir, revision=0, label="Keep me", taxon_ids=[1])
    before = (controller.state_dir / "journal.json").read_bytes()
    path = controller.catalog_dir / "index.json"
    raw = json.loads(path.read_text())
    raw["species"][0]["common_name"] = name
    write_json_atomic(path, raw)
    with pytest.raises(JournalError):
        JournalService(controller).favorite(revision=1, taxon_id=1, favorite=True)
    assert (controller.state_dir / "journal.json").read_bytes() == before
    assert read_journal(controller.state_dir) == first


def portable_document() -> dict[str, object]:
    return {
        "format": "inky-collection",
        "schema_version": 1,
        "label": "A trip",
        "start_date": "2001-03-01",
        "end_date": "2001-03-02",
        "members": [
            {
                "taxon_id": 1,
                "common_name": "Robin",
                "scientific_name": "Turdus migratorius",
                "origin": "ebird-checklist",
            },
            {
                "taxon_id": 900,
                "common_name": "Unknown",
                "scientific_name": "Unmatched species",
                "origin": "saved-selection",
            },
        ],
    }


def test_portable_import_preview_exact_identity_unresolved_and_reconnect(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    service = JournalService(controller)
    document = portable_document()
    preview = service.collection_import_preview(document)
    assert preview["resolved"] == preview["ready"] == 1
    assert len(cast(list[object], preview["unresolved"])) == 1
    state = service.import_collection(
        document=document,
        revision=0,
        preview_hash=str(preview["preview_hash"]),
        collection_id="a" * 32,
    )
    group = state.collections[0]
    assert group.kind == CollectionKind.IMPORTED
    assert group.members[0].evidence == ()
    assert group.members[1].taxon_id is None
    assert (
        service.import_collection(
            document=document,
            revision=0,
            preview_hash=str(preview["preview_hash"]),
            collection_id="a" * 32,
        )
        == state
    )
    exported = service.export_collection("a" * 32)
    assert set(exported) == {
        "format",
        "schema_version",
        "label",
        "start_date",
        "end_date",
        "members",
    }
    assert "checklist_hash" not in json.dumps(exported)
    assert "ebird-checklist" in json.dumps(exported)  # Origin label, not linked proof.
    save_collection(
        controller.state_dir, revision=1, label="Changed", taxon_ids=[1], collection_id="a" * 32
    )
    with pytest.raises(JournalConflictError):
        service.import_collection(
            document=document,
            revision=0,
            preview_hash=str(preview["preview_hash"]),
            collection_id="a" * 32,
        )
    assert not (controller.state_dir / "ebird-archive.json").exists()
    assert not (controller.state_dir / "generation-queue.json").exists()


@pytest.mark.parametrize("mutation", ["secret", "link", "duplicate", "date", "boolean"])
def test_portable_documents_fail_closed(tmp_path: Path, mutation: str) -> None:
    controller = config(tmp_path)
    catalog(controller)
    document = portable_document()
    if mutation == "secret":
        document["password"] = "not accepted"
    elif mutation == "link":
        cast(list[dict[str, object]], document["members"])[0]["evidence"] = []
    elif mutation == "duplicate":
        members = cast(list[dict[str, object]], document["members"])
        members.append(members[0])
    elif mutation == "date":
        document["end_date"] = "2001-02-28"
    else:
        document["schema_version"] = True
    with pytest.raises(JournalError):
        JournalService(controller).collection_import_preview(document)
    assert not (controller.state_dir / "journal.json").exists()


def test_portable_taxonomy_conflict_does_not_relabel_or_enqueue(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    service = JournalService(controller)
    document = portable_document()
    cast(list[dict[str, object]], document["members"])[0]["scientific_name"] = "Different species"
    preview = service.collection_import_preview(document)
    assert preview["resolved"] == 0
    document["label"] = "Changed after preview"
    with pytest.raises(JournalConflictError):
        service.import_collection(
            document=document,
            revision=0,
            preview_hash=str(preview["preview_hash"]),
            collection_id="a" * 32,
        )


def test_collection_portability_routes_have_owner_and_csrf_guards(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    AccessStore(controller.state_dir).setup_owner("qa-owner", "a synthetic long passphrase")
    with TestClient(create_app(controller), base_url=ORIGIN) as client:
        assert (
            client.get("/v1/owner/journal/collections/export?collection=" + "a" * 32).status_code
            == 401
        )
        login = client.post(
            "/v1/owner/login",
            headers={"Origin": ORIGIN},
            json={"username": "qa-owner", "passphrase": "a synthetic long passphrase"},
        )
        headers = {"Origin": ORIGIN, "X-Inky-CSRF": login.json()["data"]["csrf"]}
        assert (
            client.post(
                "/v1/owner/journal/collections/favorite",
                json={"revision": 0, "taxon_id": 1, "favorite": True},
                headers={"Origin": ORIGIN},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/v1/owner/journal/collections/favorite",
                json={"revision": 0, "taxon_id": 1, "favorite": True},
                headers=headers,
            ).status_code
            == 200
        )
        preview = client.post(
            "/v1/owner/journal/collections/import-preview",
            json={"document": portable_document()},
            headers=headers,
        )
        assert preview.status_code == 200
        body = {
            "document": portable_document(),
            "revision": 1,
            "preview_hash": preview.json()["data"]["preview_hash"],
            "collection_id": "a" * 32,
        }
        assert (
            client.post(
                "/v1/owner/journal/collections/import", json=body, headers=headers
            ).status_code
            == 200
        )
        exported = client.get("/v1/owner/journal/collections/export?collection=" + "a" * 32)
        assert exported.status_code == 200
        assert exported.headers["cache-control"] == "no-store"
        assert "checklist_hash" not in exported.text


def test_newer_journal_asks_for_a_newer_version(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    newer = {
        "schema_version": 3,
        "revision": 4,
        "collections": [],
        "notes": {"1": "A field a later release may add"},
    }
    write_json_atomic(controller.state_dir / "journal.json", newer, mode=0o600)
    before = (controller.state_dir / "journal.json").read_bytes()
    with pytest.raises(JournalError) as raised:
        read_journal(controller.state_dir)
    assert str(raised.value) == "This journal needs a newer Inky version"
    assert "restore" not in str(raised.value)
    assert (controller.state_dir / "journal.json").read_bytes() == before


def test_newer_collection_export_asks_for_a_newer_version(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    document = {**portable_document(), "schema_version": 2, "note": "A later field"}
    with pytest.raises(JournalError, match="^This collection needs a newer Inky version$"):
        JournalService(controller).prepare_collection_import(document)


def _app_config(controller: ControllerConfig, tmp_path: Path) -> AppConfig:
    return AppConfig(
        DiscoveryConfig(
            "12345",
            None,
            None,
            None,
            None,
            8,
            20,
            ObservationWindow.LAST_30_DAYS,
            (),
        ),
        controller,
        DisplayNodeConfig("http://localhost:8000", tmp_path / "display"),
    )


@pytest.mark.parametrize("part", ["current-birds", "plate-queue", "plates-in-review"])
def test_one_unreadable_operator_file_degrades_only_its_part(tmp_path: Path, part: str) -> None:
    controller = config(tmp_path)
    catalog(controller)
    app = _app_config(controller, tmp_path)
    save_collection(controller.state_dir, revision=0, label="Kept", taxon_ids=[1])
    controller.state_dir.mkdir(parents=True, exist_ok=True)
    if part == "current-birds":
        (controller.state_dir / "active-catalog.json").write_bytes(b'{"species": "\xff"}')
    elif part == "plate-queue":
        (controller.state_dir / "generation-queue.json").write_bytes(b"\xff\xfe")
    service = JournalService(controller, app_config=app)
    with patch("inky_bird_frame.journal.current_config", side_effect=lambda value: value):
        if part == "plates-in-review":
            with patch(
                "inky_bird_frame.recovery.pending_inventory",
                side_effect=PermissionError("private path"),
            ):
                snapshot = service.snapshot()
        else:
            snapshot = service.snapshot()
    parts = [notice["part"] for notice in cast(list[dict[str, str]], snapshot["notices"])]
    # Plate work reads the same queue, so it is withheld with it.
    assert parts == ([part, "plate-work"] if part == "plate-queue" else [part])
    assert "private path" not in json.dumps(snapshot)
    assert cast(list[dict[str, object]], snapshot["collections"])[0]["label"] == "Kept"
    assert [bird["taxon_id"] for bird in cast(list[dict[str, object]], snapshot["birds"])] == [1]


def test_unreadable_queue_refuses_changes_that_need_every_known_bird(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    app = _app_config(controller, tmp_path)
    save_collection(controller.state_dir, revision=0, label="Kept", taxon_ids=[1])
    (controller.state_dir / "generation-queue.json").write_bytes(b"\xff\xfe")
    before = (controller.state_dir / "journal.json").read_bytes()
    service = JournalService(controller, app_config=app)
    unreadable = "^The list of birds waiting for plates could not be read.$"
    with patch("inky_bird_frame.journal.current_config", side_effect=lambda value: value):
        with pytest.raises(JournalError, match=unreadable):
            service.collection_import_preview(portable_document())
        with pytest.raises(JournalError, match=unreadable):
            service.import_collection(
                document=portable_document(),
                revision=1,
                preview_hash="0" * 64,
                collection_id="a" * 32,
            )
        with pytest.raises(JournalError, match=unreadable):
            service.favorite(revision=1, taxon_id=900, favorite=True)
        with pytest.raises(JournalError, match=unreadable):
            service.save_selection(revision=1, label="Later", taxon_ids=[900])
    assert (controller.state_dir / "journal.json").read_bytes() == before


def test_unreadable_crosswalk_refuses_changes_that_need_every_known_bird(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    catalog(controller)
    _import_archive(
        controller.state_dir, tmp_path, [("S1", "Robin", "Turdus migratorius", "2020-01-01")]
    )
    (controller.state_dir / "ebird-archive-taxonomy-crosswalk.json").write_bytes(b"\xff")
    with pytest.raises(JournalError, match="^Saved species matches for this history"):
        JournalService(controller).collection_import_preview(portable_document())


def test_unrelated_unavailable_history_still_allows_changes_for_listed_birds(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    catalog(controller)
    controller.state_dir.mkdir(parents=True, exist_ok=True)
    damaged = controller.state_dir / "birdnet-analyzer-detections.json"
    damaged.write_text("not json")
    damaged.chmod(0o600)
    service = JournalService(controller)
    assert service.favorite(revision=0, taxon_id=1, favorite=True).revision == 1
    assert service.save_selection(revision=1, label="Later", taxon_ids=[1]).revision == 2
    # A bird missing from the list may be in the unreadable history: say so.
    with pytest.raises(JournalError, match="^Retained history needs attention"):
        service.favorite(revision=2, taxon_id=900, favorite=True)
    with pytest.raises(JournalError, match="^Retained history needs attention"):
        service.save_selection(revision=2, label="Later", taxon_ids=[1, 900])


def test_unreadable_plate_work_never_refuses_bird_changes(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    app = _app_config(controller, tmp_path)
    service = JournalService(controller, app_config=app)
    with (
        patch("inky_bird_frame.journal.current_config", side_effect=lambda value: value),
        patch.object(JournalService, "management", side_effect=PermissionError("private path")),
    ):
        snapshot = service.snapshot()
        assert snapshot["management"] == {"available": False, "reason": "unreadable"}
        assert [n["part"] for n in cast(list[dict[str, str]], snapshot["notices"])] == [
            "plate-work"
        ]
        assert service.favorite(revision=0, taxon_id=1, favorite=True).revision == 1
        assert service.save_selection(revision=1, label="Later", taxon_ids=[1]).revision == 2
        service.collection_import_preview(portable_document())
        with pytest.raises(JournalError, match="^Choose a bird from your journal"):
            service.favorite(revision=2, taxon_id=900, favorite=True)


def test_unshowable_name_in_history_marks_the_source_unavailable_without_partial_rows(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    catalog(controller)
    _import_archive(
        controller.state_dir,
        tmp_path,
        [
            ("S1", "Robin", "Turdus migratorius", "2020-01-01"),
            ("S2", "Unmatched", "Fixture unknown", "2020-01-02"),
        ],
    )
    history = controller.state_dir / "ebird-archive-observations.json"
    payload = json.loads(history.read_text())
    for checklist in payload["checklists"].values():
        for species in checklist["species"]:
            if species["scientific_name"] == "Fixture unknown":
                species["common_name"] = "Bad\u200bname"
    write_json_atomic(history, payload, mode=0o600)
    snapshot = JournalService(controller).snapshot()
    coverage = cast(list[dict[str, object]], snapshot["history_coverage"])
    assert [row["status"] for row in coverage] == ["unavailable"]
    assert cast(list[dict[str, object]], snapshot["birds"])[0]["retained_evidence"] == []
    assert snapshot["unresolved_history"] == []


def test_unusual_name_on_a_matched_history_row_still_shows_like_before(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    _import_archive(
        controller.state_dir, tmp_path, [("S1", "Robin", "Turdus migratorius", "2020-01-01")]
    )
    history = controller.state_dir / "ebird-archive-observations.json"
    payload = json.loads(history.read_text())
    for checklist in payload["checklists"].values():
        checklist["species"][0]["common_name"] = "Rob\u200din" + "x" * 300
    write_json_atomic(history, payload, mode=0o600)
    service = JournalService(controller)
    snapshot = service.snapshot()
    coverage = cast(list[dict[str, object]], snapshot["history_coverage"])
    assert [row["status"] for row in coverage] == ["available"]
    bird = cast(list[dict[str, object]], snapshot["birds"])[0]
    assert bird["common_name"] == "Robin"
    assert [row["count"] for row in cast(list[dict[str, object]], bird["retained_evidence"])] == [1]
    # Such a summary is rebuilt on each read, the full parse every read did before.
    assert service.snapshot() == snapshot


def test_unreadable_crosswalk_marks_only_its_source_unavailable(tmp_path: Path) -> None:
    from inky_bird_frame.birdnet_analyzer import import_birdnet_analyzer_csv

    controller = config(tmp_path)
    catalog(controller)
    _import_archive(
        controller.state_dir, tmp_path, [("S1", "Robin", "Turdus migratorius", "2020-01-01")]
    )
    analyzer = tmp_path / "analyzer.csv"
    analyzer.write_text(
        "Start (s),End (s),Scientific name,Common name,Confidence,File\n"
        "0,3,Turdus migratorius,Robin,0.9,fixture.wav\n"
    )
    import_birdnet_analyzer_csv(analyzer, controller.state_dir)
    (controller.state_dir / "ebird-archive-taxonomy-crosswalk.json").write_bytes(b"\xff")
    snapshot = JournalService(controller).snapshot()
    coverage = {
        row["source"]: row for row in cast(list[dict[str, object]], snapshot["history_coverage"])
    }
    assert coverage["ebird-archive"]["status"] == "unavailable"
    assert "species matches" in cast(str, coverage["ebird-archive"]["coverage"])
    assert coverage["birdnet-analyzer"]["status"] == "available"
    bird = cast(list[dict[str, object]], snapshot["birds"])[0]
    assert [row["source"] for row in cast(list[dict[str, object]], bird["retained_evidence"])] == [
        "birdnet-analyzer"
    ]


def test_non_utf8_analyzer_history_marks_only_that_source_unavailable(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    _import_archive(
        controller.state_dir, tmp_path, [("S1", "Robin", "Turdus migratorius", "2020-01-01")]
    )
    history = controller.state_dir / "birdnet-analyzer-detections.json"
    history.write_bytes(b'{"schema_version": 1, "detections": {"\xff": 1}}')
    history.chmod(0o600)
    snapshot = JournalService(controller).snapshot()
    coverage = {
        row["source"]: row["status"]
        for row in cast(list[dict[str, object]], snapshot["history_coverage"])
    }
    assert coverage == {"ebird-archive": "available", "birdnet-analyzer": "unavailable"}


def _saved_failure(tmp_path: Path, **fields: object) -> Path:
    failed = tmp_path / "42-synthetic-failure"
    write_json_atomic(
        failed / "failure.json",
        {
            "schema_version": 1,
            "status": "failed",
            "failed_at": STAMP,
            "taxon_id": 42,
            "common_name": "Example Bird",
            "scientific_name": "Avis exempli",
            "error": "synthetic",
            **fields,
        },
    )
    return failed


def test_stopped_bird_tells_the_owner_automatic_retries_stopped(tmp_path: Path) -> None:
    from inky_bird_frame.journal import terminal_failure

    for reason, kind in (("retry_limit", "retry_limit"), ("research_limit", "research_limit")):
        failed = _saved_failure(tmp_path, error_type="DataSourceError", stop_reason=reason)
        summary = terminal_failure((failed,), 42)
        assert summary["failure_kind"] == kind
        assert "Automatic retries have stopped" in str(summary["failure_message"])
        assert "backoff" not in str(summary["failure_message"])


def test_failure_without_a_stop_reason_reads_as_before(tmp_path: Path) -> None:
    from inky_bird_frame.journal import terminal_failure

    for fields in ({}, {"stop_reason": "unknown-reason"}, {"stop_reason": 7}):
        failed = _saved_failure(tmp_path, error_type="DataSourceError", **fields)
        assert terminal_failure((failed,), 42) == {
            **failure_guidance("DataSourceError"),
            "recorded_attempts": 0,
            "last_attempt_at": None,
            "review_scores": {},
            "last_failed_at": STAMP,
        }


def trip_with_unresolved(controller: ControllerConfig) -> JournalCollection:
    link = ChecklistEvidence("c" * 64, "Turdus migratorius")
    group = JournalCollection(
        "b" * 32,
        "A trip",
        (
            JournalMember(None, "Robin", "Turdus migratorius", MemberOrigin.EBIRD, (link,)),
            JournalMember(None, "", "Unmatched species", MemberOrigin.EBIRD, ()),
        ),
        CollectionKind.TRIP,
        "2001-03-01",
        "2001-03-02",
        1,
        1,
    )
    save_imported_collection(controller.state_dir, revision=0, collection=group)
    return group


def test_saving_without_an_unresolved_choice_keeps_every_unresolved_member(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    catalog(controller)
    group = trip_with_unresolved(controller)
    state = JournalService(controller).save_selection(
        revision=1, label="Renamed", taxon_ids=[], collection_id=group.collection_id
    )
    assert state.collections[0].members == group.members


def test_owner_can_remove_and_resolve_unresolved_collection_members(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    group = trip_with_unresolved(controller)
    unresolved = cast(list[object], group.owner_dict()["unresolved"])
    assert unresolved == [
        {"common_name": "Robin", "scientific_name": "Turdus migratorius"},
        {"common_name": "", "scientific_name": "Unmatched species"},
    ]
    service = JournalService(controller)
    # Remove the second: nothing else changes.
    kept = service.save_selection(
        revision=1,
        label="A trip",
        taxon_ids=[],
        collection_id=group.collection_id,
        remove_unresolved=[unresolved[1]],
    )
    assert kept.collections[0].members == group.members[:1]
    assert kept.collections[0].kind == CollectionKind.TRIP
    # Choosing the matching bird in place of its unresolved name resolves it
    # and keeps its checklist link.
    resolved = service.save_selection(
        revision=2,
        label="A trip",
        taxon_ids=[1],
        collection_id=group.collection_id,
        remove_unresolved=[unresolved[0]],
    )
    member = resolved.collections[0].members
    assert member == (
        JournalMember(
            1, "Robin", "Turdus migratorius", MemberOrigin.EBIRD, group.members[0].evidence
        ),
    )
    assert read_journal(controller.state_dir) == resolved
    owner = resolved.collections[0].owner_dict()
    assert owner["unresolved"] == [] and owner["taxon_ids"] == [1]


def test_resolving_does_not_replace_a_member_already_chosen(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    group = trip_with_unresolved(controller)
    service = JournalService(controller)
    first = service.save_selection(
        revision=1, label="A trip", taxon_ids=[1], collection_id=group.collection_id
    )
    manual = next(item for item in first.collections[0].members if item.taxon_id == 1)
    assert manual.origin == MemberOrigin.SAVED
    second = service.save_selection(
        revision=2,
        label="A trip",
        taxon_ids=[1],
        collection_id=group.collection_id,
        remove_unresolved=group.owner_dict()["unresolved"],
    )
    assert second.collections[0].members == (manual,)


@pytest.mark.parametrize(
    "choice",
    [
        "all",
        [{"common_name": "", "scientific_name": "Not in this collection"}],
        [{"scientific_name": "Unmatched species"}],
        [
            {"common_name": "", "scientific_name": "Unmatched species"},
            {"common_name": "", "scientific_name": "Unmatched species"},
        ],
    ],
)
def test_unresolved_choice_must_name_this_collections_members(
    tmp_path: Path, choice: object
) -> None:
    controller = config(tmp_path)
    catalog(controller)
    group = trip_with_unresolved(controller)
    before = (controller.state_dir / "journal.json").read_bytes()
    with pytest.raises(JournalError):
        JournalService(controller).save_selection(
            revision=1,
            label="A trip",
            taxon_ids=[],
            collection_id=group.collection_id,
            remove_unresolved=choice,
        )
    assert (controller.state_dir / "journal.json").read_bytes() == before


def test_new_collection_has_no_unresolved_members_to_keep(tmp_path: Path) -> None:
    with pytest.raises(JournalError):
        save_collection(
            tmp_path,
            revision=0,
            label="New",
            taxon_ids=[],
            remove_unresolved=[{"common_name": "", "scientific_name": "Unmatched species"}],
        )
    assert save_collection(tmp_path, revision=0, label="New", taxon_ids=[], remove_unresolved=[])
