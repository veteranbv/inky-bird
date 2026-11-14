from __future__ import annotations

import json
import shutil
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from png_fixtures import write_plate_pair, write_png, write_truncated_png

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.catalog import (
    CandidateApprovalError,
    CollectionEntry,
    CollectionOrigin,
    add_collection_taxa,
    approve_candidate,
    archive_invalid_approved_candidate,
    candidate_directory,
    fsync_tree,
    has_passing_sourced_review,
    has_valid_approved_candidate,
    read_catalog_entries,
    read_collection,
    read_collection_state,
    read_json,
    rebuild_catalog_index,
    reject_candidate,
    remove_collection_taxa,
    withdraw_approved_candidate,
    write_candidate_manifest,
    write_collection,
)
from inky_bird_frame.errors import CatalogError
from inky_bird_frame.http import write_json_atomic
from inky_bird_frame.models import QualityReview, SpeciesProfileData

PROFILE = SpeciesProfileData(
    taxon_id=7513,
    common_name="Carolina Wren",
    scientific_name="Thryothorus ludovicianus",
    family="Troglodytidae",
    measurements={"length": "5.5 in", "wingspan": "11 in", "weight": "0.7 oz"},
    field_marks=["white eyebrow", "rufous back", "barred wings", "upright tail"],
    habitat="Brushy woodland",
    behavior="Forages low in cover",
    palette=["rufous", "cream", "umber"],
    sources=[
        {"title": "Source one", "url": "https://example.test/one"},
        {"title": "Source two", "url": "https://example.test/two"},
    ],
)


def make_candidate(state: Path, species: BirdSpecies, review: QualityReview) -> Path:
    candidate = candidate_directory(state, species)
    candidate.mkdir(parents=True)
    write_plate_pair(candidate)
    write_candidate_manifest(
        candidate,
        species,
        PROFILE,
        [],
        review,
        generator="test",
        prompt_version="test-v1",
    )
    return candidate


class CatalogTests(unittest.TestCase):
    def test_passing_review_gate_rejects_profile_conflicts(self) -> None:
        review = QualityReview(
            True,
            5,
            5,
            5,
            5,
            True,
            (),
            (
                {"title": "One", "url": "https://one.example/bird"},
                {"title": "Two", "url": "https://two.example/bird"},
            ),
        ).as_dict()
        review["profile_conflicts"] = [
            {
                "field": "measurements.length",
                "profile_value": "10 cm",
                "observed_value": "12 cm",
                "sources": [],
            }
        ]

        self.assertFalse(has_passing_sourced_review(review))
        review["profile_conflicts"] = "invalid"
        self.assertFalse(has_passing_sourced_review(review))

    def test_atomic_json_writes_support_concurrent_health_requests(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.json"
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(
                    executor.map(lambda value: write_json_atomic(path, {"value": value}), range(32))
                )
            payload = json.loads(path.read_text())

        self.assertIn(payload["value"], range(32))

    def test_collection_round_trip_contains_only_private_membership_metadata(self) -> None:
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            expected = [
                CollectionEntry(
                    12942,
                    "2026-08-02T12:00:00+00:00",
                    CollectionOrigin.CATALOG_IMPORT,
                )
            ]

            migrated_at = "2026-08-02T12:05:00+00:00"
            write_collection(
                state,
                expected,
                legacy_seed_queue_migrated_at=migrated_at,
            )

            payload = json.loads((state / "collection.json").read_text())
            actual = read_collection(state)
            collection_state = read_collection_state(state)

        self.assertEqual(actual, expected)
        self.assertEqual(collection_state.legacy_seed_queue_migrated_at, migrated_at)
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "updated_at",
                "legacy_seed_queue_migrated_at",
                "taxa",
            },
        )
        self.assertEqual(
            set(payload["taxa"][0]),
            {"taxon_id", "added_at", "origin"},
        )

    def test_collection_addition_is_idempotent_and_removal_is_explicit(self) -> None:
        entries, added = add_collection_taxa(
            [],
            {2, 1},
            CollectionOrigin.HISTORICAL_SEED,
        )

        repeated, repeated_added = add_collection_taxa(
            entries,
            {1},
            CollectionOrigin.MANUAL,
        )
        remaining, removed = remove_collection_taxa(repeated, {2})

        self.assertEqual([entry.taxon_id for entry in added], [1, 2])
        self.assertEqual(repeated, entries)
        self.assertEqual(repeated_added, [])
        self.assertEqual(
            [entry.origin for entry in repeated],
            [
                CollectionOrigin.HISTORICAL_SEED,
                CollectionOrigin.HISTORICAL_SEED,
            ],
        )
        self.assertEqual([entry.taxon_id for entry in remaining], [1])
        self.assertEqual([entry.taxon_id for entry in removed], [2])

    def test_collection_rejects_unrecognized_state_fields(self) -> None:
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "collection.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "updated_at": "2026-08-02T12:00:00+00:00",
                        "legacy_seed_queue_migrated_at": None,
                        "taxa": [
                            {
                                "taxon_id": 12942,
                                "added_at": "2026-08-02T12:00:00+00:00",
                                "origin": "manual",
                                "latitude": 35.0,
                            }
                        ],
                    }
                )
            )

            with self.assertRaisesRegex(CatalogError, "Invalid collection entry"):
                read_collection(state)

    @pytest.mark.repository_catalog
    def test_approved_seed_has_valid_checksums(self) -> None:
        catalog = Path(__file__).parents[1] / "catalog"

        entries = rebuild_catalog_index(catalog)

        taxon_ids = {entry.taxon_id for entry in entries}
        self.assertTrue({9083, 12942}.issubset(taxon_ids))
        first_index = (catalog / "index.json").read_bytes()

        rebuild_catalog_index(catalog)

        self.assertEqual((catalog / "index.json").read_bytes(), first_index)
        payload = json.loads(first_index)
        self.assertEqual(payload["generated_at"], max(entry.approved_at for entry in entries))

    def test_approval_is_explicit_and_cannot_overwrite(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = make_candidate(state, species, review)

            entry = approve_candidate(state, catalog, species.taxon_id)

            self.assertEqual(entry.taxon_id, species.taxon_id)
            self.assertFalse(candidate.exists())
            with self.assertRaises(CatalogError):
                approve_candidate(state, catalog, species.taxon_id)

    def test_valid_approved_candidate_requires_complete_checked_assets(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            make_candidate(state, species, review)
            approve_candidate(state, catalog, species.taxon_id)

            complete = has_valid_approved_candidate(catalog, species.taxon_id)
            (catalog / "species/7513-carolina-wren/display.png").unlink()
            incomplete = has_valid_approved_candidate(catalog, species.taxon_id)

        self.assertTrue(complete)
        self.assertFalse(incomplete)

    def test_invalid_approved_archival_rolls_back_when_removal_fails(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            source = catalog / "species/7513-carolina-wren"
            source.mkdir(parents=True)
            (source / "manifest.json").write_text(
                json.dumps({"taxon_id": 7513, "status": "approved"})
            )
            original_rmtree = shutil.rmtree

            failures = [OSError("removal failed")]

            def failing_rmtree(path: Path, *args: object, **kwargs: object) -> None:
                if Path(path) == source and failures:
                    raise failures.pop()
                original_rmtree(path)

            with (
                patch("inky_bird_frame.catalog.shutil.rmtree", side_effect=failing_rmtree),
                self.assertRaisesRegex(OSError, "removal failed"),
            ):
                archive_invalid_approved_candidate(state, catalog, 7513)

            source_exists = (source / "manifest.json").is_file()
            archive_exists = (state / "archive").exists()

        self.assertTrue(source_exists)
        self.assertFalse(archive_exists)

    def test_each_of_two_invalid_approved_entries_can_be_archived(self) -> None:
        first = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        second = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            for species in (first, second):
                make_candidate(state, species, review)
                approve_candidate(state, catalog, species.taxon_id)
            for name in ("7513-carolina-wren", "9083-northern-cardinal"):
                write_png(catalog / "species" / name / "portrait.png", color=(9, 9, 9))

            first_archive = archive_invalid_approved_candidate(state, catalog, first.taxon_id)
            index_after_first = read_json(catalog / "index.json")
            second_archive = archive_invalid_approved_candidate(state, catalog, second.taxon_id)
            index_after_second = read_json(catalog / "index.json")
            remaining = list((catalog / "species").iterdir())
            archived = sorted(path.name for path in (state / "archive").iterdir())

        self.assertIsNotNone(first_archive)
        self.assertIsNotNone(second_archive)
        self.assertEqual(remaining, [])
        self.assertEqual(
            archived,
            ["invalid-approved-7513-carolina-wren", "invalid-approved-9083-northern-cardinal"],
        )
        self.assertIsInstance(index_after_first, dict)
        if isinstance(index_after_first, dict):
            self.assertEqual(
                [item["taxon_id"] for item in index_after_first["species"]],
                [second.taxon_id],
            )
        self.assertIsInstance(index_after_second, dict)
        if isinstance(index_after_second, dict):
            self.assertEqual(index_after_second["species"], [])

    def test_withdrawal_does_not_depend_on_other_catalog_entries(self) -> None:
        kept = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        broken = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            for species in (kept, broken):
                make_candidate(state, species, review)
                approve_candidate(state, catalog, species.taxon_id)
            (catalog / "species/9083-northern-cardinal/display.png").unlink()

            destination = withdraw_approved_candidate(
                state, catalog, kept.taxon_id, "The bill shape is wrong."
            )

            source_exists = (catalog / "species/7513-carolina-wren").exists()
            rejected = read_json(destination / "manifest.json")
            index = read_json(catalog / "index.json")

        self.assertFalse(source_exists)
        self.assertIsInstance(rejected, dict)
        if isinstance(rejected, dict):
            self.assertEqual(rejected["status"], "rejected")
        self.assertIsInstance(index, dict)
        if isinstance(index, dict):
            self.assertNotIn(kept.taxon_id, [item["taxon_id"] for item in index["species"]])

    def test_truncated_pending_image_is_refused_before_anything_is_staged(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = candidate_directory(state, species)
            write_truncated_png(candidate / "portrait.png")
            write_png(candidate / "display.png")
            # The manifest checksum matches the truncated bytes, as it does when
            # a full disk cuts the plate short before the manifest is written.
            write_candidate_manifest(
                candidate, species, PROFILE, [], review, generator="test", prompt_version="v1"
            )
            before = {path.name: path.read_bytes() for path in candidate.iterdir()}

            with self.assertRaisesRegex(CandidateApprovalError, "unreadable portrait.png"):
                approve_candidate(state, catalog, species.taxon_id)

            after = {path.name: path.read_bytes() for path in candidate.iterdir()}
            catalog_exists = catalog.exists()

        self.assertEqual(after, before)
        self.assertFalse(catalog_exists)

    def test_pending_assets_must_match_their_manifest_checksums(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = make_candidate(state, species, review)
            write_png(candidate / "display.png", color=(0, 0, 0))

            with self.assertRaisesRegex(CandidateApprovalError, "do not match its manifest"):
                approve_candidate(state, catalog, species.taxon_id)

            pending_exists = (candidate / "manifest.json").is_file()
            catalog_exists = catalog.exists()

        self.assertTrue(pending_exists)
        self.assertFalse(catalog_exists)

    def test_pending_asset_filenames_must_be_canonical(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = make_candidate(state, species, review)
            shutil.copy(candidate / "portrait.png", root / "outside.png")
            manifest = json.loads((candidate / "manifest.json").read_text())
            manifest["assets"]["portrait"]["filename"] = "../../outside.png"
            (candidate / "manifest.json").write_text(json.dumps(manifest))

            with self.assertRaisesRegex(CandidateApprovalError, "do not match its manifest"):
                approve_candidate(state, catalog, species.taxon_id)

            catalog_exists = catalog.exists()

        self.assertFalse(catalog_exists)

    def test_failed_staging_copy_keeps_pending_and_leaves_no_catalog_entry(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = make_candidate(state, species, review)
            original_copytree = shutil.copytree

            def short_copy(source: Path, destination: Path) -> object:
                result = original_copytree(source, destination)
                with (Path(destination) / "portrait.png").open("r+b") as handle:
                    handle.truncate(10)
                return result

            with (
                patch("inky_bird_frame.catalog.shutil.copytree", side_effect=short_copy),
                self.assertRaisesRegex(CatalogError, "could not be copied"),
            ):
                approve_candidate(state, catalog, species.taxon_id)

            pending_exists = (candidate / "manifest.json").is_file()
            species_entries = (
                list((catalog / "species").iterdir()) if (catalog / "species").exists() else []
            )
            staging_exists = (catalog / ".staging").exists()

        self.assertTrue(pending_exists)
        self.assertEqual(species_entries, [])
        self.assertFalse(staging_exists)

    def test_staged_copy_is_flushed_before_it_is_renamed_into_the_catalog(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            make_candidate(state, species, review)
            destination = catalog / "species/7513-carolina-wren"
            flushed: list[tuple[str, bool]] = []
            original_fsync_tree = fsync_tree

            def record(path: Path) -> None:
                flushed.append((path.parent.name, destination.exists()))
                original_fsync_tree(path)

            with patch("inky_bird_frame.catalog.fsync_tree", side_effect=record):
                approve_candidate(state, catalog, species.taxon_id)

        self.assertEqual(flushed, [(".staging", False)])

    def test_existing_approval_is_found_by_taxon_after_a_rename(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        renamed = BirdSpecies(7513, "Southern Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            make_candidate(state, species, review)
            approve_candidate(state, catalog, species.taxon_id)
            candidate = make_candidate(state, renamed, review)

            with self.assertRaisesRegex(CandidateApprovalError, "already approved"):
                approve_candidate(state, catalog, species.taxon_id)

            directories = sorted(path.name for path in (catalog / "species").iterdir())
            pending_exists = candidate.is_dir()

        self.assertEqual(directories, ["7513-carolina-wren"])
        self.assertTrue(pending_exists)

    def test_catalog_reads_reject_noncanonical_and_missing_assets(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            make_candidate(state, species, review)
            approve_candidate(state, catalog, species.taxon_id)
            original = catalog / "species/7513-carolina-wren"
            manifest = json.loads((original / "manifest.json").read_text())
            manifest["assets"]["display"]["filename"] = "portrait.png"
            manifest["assets"]["display"]["sha256"] = manifest["assets"]["portrait"]["sha256"]
            (original / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(CatalogError, "invalid assets"):
                read_catalog_entries(catalog)

            manifest = json.loads((original / "manifest.json").read_text())
            manifest["assets"]["display"]["filename"] = "display.png"
            (original / "manifest.json").write_text(json.dumps(manifest))
            (original / "display.png").unlink()
            with self.assertRaisesRegex(CatalogError, "missing or unreadable"):
                read_catalog_entries(catalog)

    def test_rejection_moves_the_candidate_before_rewriting_its_manifest(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            candidate = make_candidate(state, species, review)

            with (
                patch(
                    "inky_bird_frame.catalog.write_json_atomic",
                    side_effect=OSError("stopped before the manifest rewrite"),
                ),
                self.assertRaisesRegex(OSError, "stopped before"),
            ):
                reject_candidate(state, species.taxon_id, "Wrong bill")

            pending_exists = candidate.exists()
            rejected = state / "rejected/7513-carolina-wren"
            rejected_files = sorted(path.name for path in rejected.iterdir())

        self.assertFalse(pending_exists)
        self.assertEqual(rejected_files, ["display.png", "manifest.json", "portrait.png"])

    def test_approved_candidate_withdrawal_preserves_human_rejection_audit(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            make_candidate(state, species, review)
            approved = approve_candidate(state, catalog, species.taxon_id)

            destination = withdraw_approved_candidate(
                state,
                catalog,
                species.taxon_id,
                "The eyes are not visually credible.",
            )

            manifest = read_json(destination / "manifest.json")
            index = read_json(catalog / "index.json")
            portrait_exists = (destination / "portrait.png").is_file()

        self.assertIsInstance(manifest, dict)
        if isinstance(manifest, dict):
            self.assertEqual(manifest["status"], "rejected")
            self.assertEqual(manifest["rejection_reason"], "The eyes are not visually credible.")
            self.assertEqual(manifest["approved_at"], approved.approved_at)
            self.assertIn("rejected_at", manifest)
        self.assertTrue(portrait_exists)
        self.assertIsInstance(index, dict)
        if isinstance(index, dict):
            self.assertEqual(index["species"], [])

    def test_approved_candidate_withdrawal_rolls_back_when_removal_fails(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            make_candidate(state, species, review)
            approve_candidate(state, catalog, species.taxon_id)
            source = catalog / "species/7513-carolina-wren"
            original_rmtree = shutil.rmtree

            failures = [OSError("removal failed")]

            def failing_rmtree(path: Path, *args: object, **kwargs: object) -> None:
                if Path(path) == source and failures:
                    raise failures.pop()
                original_rmtree(path)

            with (
                patch("inky_bird_frame.catalog.shutil.rmtree", side_effect=failing_rmtree),
                self.assertRaisesRegex(OSError, "removal failed"),
            ):
                withdraw_approved_candidate(
                    state,
                    catalog,
                    species.taxon_id,
                    "Human review failed.",
                )

            manifest = read_json(source / "manifest.json")
            source_exists = source.is_dir()
            rejected_exists = (state / "rejected").exists()

        self.assertTrue(source_exists)
        self.assertIsInstance(manifest, dict)
        if isinstance(manifest, dict):
            self.assertEqual(manifest["status"], "approved")
            self.assertNotIn("rejection_reason", manifest)
        self.assertFalse(rejected_exists)

    def test_interrupted_approval_with_leftover_pending_candidate_completes(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = make_candidate(state, species, review)
            backup = root / "backup"
            shutil.copytree(candidate, backup)
            approve_candidate(state, catalog, species.taxon_id)
            shutil.copytree(backup, candidate)

            entry = approve_candidate(state, catalog, species.taxon_id)

            self.assertEqual(entry.taxon_id, species.taxon_id)
            self.assertFalse(candidate.exists())
            self.assertEqual(
                [item.taxon_id for item in read_catalog_entries(catalog)],
                [species.taxon_id],
            )

    def test_stale_staging_is_cleared_and_invisible_to_catalog_reads(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            make_candidate(state, species, review)
            stale = catalog / ".staging" / "7513-carolina-wren"
            stale.mkdir(parents=True)
            (stale / "manifest.json").write_text("{not json")

            self.assertEqual(read_catalog_entries(catalog), [])

            entry = approve_candidate(state, catalog, species.taxon_id)

            self.assertFalse(stale.exists())
            self.assertEqual(entry.taxon_id, species.taxon_id)
            self.assertEqual(
                [item.taxon_id for item in read_catalog_entries(catalog)],
                [species.taxon_id],
            )

    def test_successful_approval_leaves_no_staging_directory(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            make_candidate(state, species, review)

            approve_candidate(state, catalog, species.taxon_id)

            self.assertFalse((catalog / ".staging").exists())
            self.assertEqual(
                sorted(path.name for path in catalog.iterdir()),
                ["index.json", "species"],
            )

    def test_partial_destination_is_discarded_and_reapproved_from_pending(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = make_candidate(state, species, review)
            manifest = json.loads((candidate / "manifest.json").read_text())
            manifest["status"] = "approved"
            manifest["approved_at"] = "2026-07-10T00:00:00+00:00"

            # An old-flow crash mid-copy: manifest landed, display.png did not.
            destination = catalog / "species" / "7513-carolina-wren"
            destination.mkdir(parents=True)
            (destination / "manifest.json").write_text(json.dumps(manifest))
            shutil.copy(candidate / "portrait.png", destination / "portrait.png")
            expected_display = (candidate / "display.png").read_bytes()

            entry = approve_candidate(state, catalog, species.taxon_id)

            self.assertEqual(entry.taxon_id, species.taxon_id)
            self.assertFalse(candidate.exists())
            self.assertEqual((destination / "display.png").read_bytes(), expected_display)
            self.assertEqual(
                [item.taxon_id for item in read_catalog_entries(catalog)],
                [species.taxon_id],
            )

    def test_destination_without_readable_manifest_is_discarded_and_reapproved(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = make_candidate(state, species, review)

            # An old-flow crash before the manifest landed in the destination.
            destination = catalog / "species" / "7513-carolina-wren"
            destination.mkdir(parents=True)
            (destination / "portrait.png").write_bytes(b"partial")
            expected_display = (candidate / "display.png").read_bytes()

            entry = approve_candidate(state, catalog, species.taxon_id)

            self.assertEqual(entry.taxon_id, species.taxon_id)
            self.assertFalse(candidate.exists())
            self.assertEqual((destination / "display.png").read_bytes(), expected_display)

    def test_read_json_reports_non_utf8_corruption_as_catalog_error(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "torn.json"
            path.write_bytes(b'{"schema_version": 1, "species": [\xff\xfe')
            with self.assertRaisesRegex(CatalogError, "Invalid JSON"):
                read_json(path)

    def test_destination_with_differing_manifest_preserves_pending_and_raises(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = make_candidate(state, species, review)
            manifest = json.loads((candidate / "manifest.json").read_text())
            manifest["status"] = "approved"
            manifest["approved_at"] = "2026-07-10T00:00:00+00:00"
            manifest["common_name"] = "Corrected Wren"

            destination = catalog / "species" / "7513-carolina-wren"
            destination.mkdir(parents=True)
            (destination / "manifest.json").write_text(json.dumps(manifest))
            shutil.copy(candidate / "portrait.png", destination / "portrait.png")
            shutil.copy(candidate / "display.png", destination / "display.png")

            with self.assertRaisesRegex(CatalogError, "explicit replacement workflow"):
                approve_candidate(state, catalog, species.taxon_id)

            self.assertTrue(candidate.exists())
            self.assertTrue((destination / "manifest.json").exists())

    def test_conflicting_destination_still_requires_explicit_replacement(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            make_candidate(state, species, review)
            approve_candidate(state, catalog, species.taxon_id)
            candidate = candidate_directory(state, species)
            candidate.mkdir(parents=True)
            write_png(candidate / "portrait.png", color=(1, 2, 3))
            write_png(candidate / "display.png", color=(3, 2, 1))
            write_candidate_manifest(
                candidate,
                species,
                PROFILE,
                [],
                review,
                generator="test",
                prompt_version="test-v1",
            )

            with self.assertRaisesRegex(CatalogError, "already approved"):
                approve_candidate(state, catalog, species.taxon_id)

            self.assertTrue(candidate.exists())


if __name__ == "__main__":
    unittest.main()
