from __future__ import annotations

import hashlib
import json
import stat
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.birdnet_analyzer import (
    analyzer_history_revision,
    import_birdnet_analyzer_csv,
    read_birdnet_analyzer_history,
)
from inky_bird_frame.birds import BirdNetAnalyzerSpecies, ObservationWindow
from inky_bird_frame.errors import DataSourceError
from inky_bird_frame.imports import recover_import

HEADER = "Start (s),End (s),Scientific name,Common name,Confidence,File\n"


def legacy_fingerprint(source_file: str, start: float, end: float) -> str:
    """Segment-only fingerprint written by releases before per-species rows."""
    identity = json.dumps(
        [source_file, start.hex(), end.hex()], ensure_ascii=False, separators=(",", ":")
    ).encode()
    return hashlib.sha256(identity).hexdigest()


def write_legacy_state(state_dir: Path, detections: dict[str, dict[str, object]]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "birdnet-analyzer-detections.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "history_started_at": "2026-08-01T00:00:00+00:00",
                "last_imported_at": "2026-08-01T00:00:00+00:00",
                "detections": detections,
            }
        )
    )
    path.chmod(0o600)
    return path


def species_counts(state_dir: Path) -> dict[str, int]:
    history = read_birdnet_analyzer_history(
        state_dir, window=ObservationWindow.ALL_TIME, limit=None
    )
    return {item.scientific_name: item.detection_count for item in history.species}


class BirdNetAnalyzerImportTests(unittest.TestCase):
    def test_import_is_private_idempotent_and_does_not_retain_file_metadata(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(
                HEADER
                + "0.0,3.0,Sialia sialis,Eastern Bluebird,0.91,/private/audio/one.wav\n"
                + "3.0,6.0,Cardinalis cardinalis,Northern Cardinal,0.88,second.wav\n"
            )
            first = import_birdnet_analyzer_csv(
                csv_path,
                root / "state",
                now=datetime(2026, 8, 9, 12, tzinfo=UTC),
            )
            second = import_birdnet_analyzer_csv(
                csv_path,
                root / "state",
                now=datetime(2026, 8, 9, 13, tzinfo=UTC),
            )
            state_path = root / "state/birdnet-analyzer-detections.json"
            serialized = state_path.read_text()
            mode = stat.S_IMODE(state_path.stat().st_mode)

        self.assertEqual(first.imported, 2)
        self.assertEqual(first.undated, 2)
        self.assertEqual(second.imported, 0)
        self.assertEqual(second.duplicates, 2)
        self.assertEqual(mode, 0o600)
        self.assertNotIn("/private/audio", serialized)
        self.assertNotIn("second.wav", serialized)
        self.assertNotIn("0.91", serialized)

    def test_reimport_reclassifies_a_segment_and_adds_an_explicit_date(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.91,one.wav\n")
            import_birdnet_analyzer_csv(csv_path, root / "state")
            csv_path.write_text(
                HEADER + "0,3,Cardinalis cardinalis,Northern Cardinal,0.95,one.wav\n"
            )

            result = import_birdnet_analyzer_csv(
                csv_path,
                root / "state",
                observed_on=date(2026, 8, 8),
            )
            history = read_birdnet_analyzer_history(
                root / "state",
                window=ObservationWindow.ALL_TIME,
                limit=50,
            )

        self.assertEqual(result.updated, 1)
        self.assertEqual(result.dated, 1)
        self.assertEqual(
            history.species,
            [BirdNetAnalyzerSpecies("Northern Cardinal", "Cardinalis cardinalis", 1)],
        )

    def test_conflicting_explicit_date_fails_without_replacing_state(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.91,one.wav\n")
            import_birdnet_analyzer_csv(
                csv_path,
                root / "state",
                observed_on=date(2026, 8, 8),
            )
            before = (root / "state/birdnet-analyzer-detections.json").read_bytes()

            with self.assertRaisesRegex(DataSourceError, "different date"):
                import_birdnet_analyzer_csv(
                    csv_path,
                    root / "state",
                    observed_on=date(2026, 8, 9),
                )
            after = (root / "state/birdnet-analyzer-detections.json").read_bytes()

        self.assertEqual(after, before)

    def test_missing_columns_and_invalid_rows_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text("Scientific name,Common name\nSialia sialis,Eastern Bluebird\n")
            with self.assertRaisesRegex(DataSourceError, "missing required columns"):
                import_birdnet_analyzer_csv(csv_path, root / "state")

            csv_path.write_text(HEADER + "3,0,Sialia sialis,Eastern Bluebird,1.5,one.wav\n")
            with self.assertRaisesRegex(DataSourceError, "CSV row 2"):
                import_birdnet_analyzer_csv(csv_path, root / "state")

        self.assertFalse((root / "state/birdnet-analyzer-detections.json").exists())

    def test_read_failure_does_not_expose_csv_path(self) -> None:
        private_path = Path("/private/recordings/results.csv")
        with TemporaryDirectory() as temporary, self.assertRaises(DataSourceError) as raised:
            import_birdnet_analyzer_csv(private_path, Path(temporary) / "state")

        self.assertNotIn(str(private_path), str(raised.exception))

    def test_dry_run_does_not_write_state(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.91,one.wav\n")
            result = import_birdnet_analyzer_csv(csv_path, root / "state", dry_run=True)

        self.assertTrue(result.dry_run)
        self.assertFalse((root / "state/birdnet-analyzer-detections.json").exists())


class BirdNetAnalyzerMultiSpeciesTests(unittest.TestCase):
    TWO_SPECIES = (
        HEADER
        + "0,3,Sialia sialis,Eastern Bluebird,0.91,one.wav\n"
        + "0,3,Cardinalis cardinalis,Northern Cardinal,0.62,one.wav\n"
    )

    def test_every_species_in_one_segment_is_kept(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(self.TWO_SPECIES)
            first = import_birdnet_analyzer_csv(csv_path, root / "state")
            second = import_birdnet_analyzer_csv(csv_path, root / "state")
            counts = species_counts(root / "state")

        self.assertEqual((first.imported, first.updated, first.duplicates), (2, 0, 0))
        self.assertEqual((second.imported, second.updated, second.duplicates), (0, 0, 2))
        self.assertEqual(second.removed, 0)
        self.assertEqual(counts, {"Sialia sialis": 1, "Cardinalis cardinalis": 1})

    def test_reimport_replaces_the_species_list_of_each_segment_present(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(
                self.TWO_SPECIES + "3,6,Sialia sialis,Eastern Bluebird,0.9,one.wav\n"
            )
            import_birdnet_analyzer_csv(csv_path, root / "state")
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.95,one.wav\n")
            preview = import_birdnet_analyzer_csv(csv_path, root / "state", dry_run=True)
            result = import_birdnet_analyzer_csv(csv_path, root / "state")
            counts = species_counts(root / "state")

        self.assertEqual(preview.as_dict() | {"dry_run": False}, result.as_dict())
        self.assertEqual((result.duplicates, result.removed), (1, 1))
        # The untouched 3-6 s segment keeps its row.
        self.assertEqual(counts, {"Sialia sialis": 2})

    def test_committed_import_with_removals_can_be_recovered_by_operation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            csv_path = root / "results.csv"
            csv_path.write_text(self.TWO_SPECIES)
            import_birdnet_analyzer_csv(csv_path, state_dir)
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.95,one.wav\n")
            operation_id = "b" * 64
            result = import_birdnet_analyzer_csv(
                csv_path,
                state_dir,
                expected_revision=analyzer_history_revision(state_dir),
                operation_id=operation_id,
            )
            recovered = recover_import(state_dir, operation_id)

        self.assertEqual(result.removed, 1)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(
            {key: value for key, value in recovered.items() if key != "message"},
            {key: value for key, value in result.as_dict().items() if key != "removed"},
        )

    def test_legacy_state_loads_and_reimport_replaces_rather_than_duplicates(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            write_legacy_state(
                state_dir,
                {
                    legacy_fingerprint("one.wav", 0.0, 3.0): {
                        "common_name": "Eastern Bluebird",
                        "scientific_name": "Sialia sialis",
                        "observed_on": "2026-08-08",
                    },
                    legacy_fingerprint("one.wav", 3.0, 6.0): {
                        "common_name": "Northern Cardinal",
                        "scientific_name": "Cardinalis cardinalis",
                        "observed_on": None,
                    },
                },
            )
            legacy_counts = species_counts(state_dir)
            csv_path = root / "results.csv"
            csv_path.write_text(self.TWO_SPECIES)
            result = import_birdnet_analyzer_csv(csv_path, state_dir)
            counts = species_counts(state_dir)
            dated = read_birdnet_analyzer_history(
                state_dir,
                window=ObservationWindow.LAST_WEEK,
                limit=None,
                today=date(2026, 8, 9),
            )
            again = import_birdnet_analyzer_csv(csv_path, state_dir)

        self.assertEqual(legacy_counts, {"Sialia sialis": 1, "Cardinalis cardinalis": 1})
        self.assertEqual((result.imported, result.duplicates, result.removed), (1, 1, 0))
        self.assertEqual(counts, {"Sialia sialis": 1, "Cardinalis cardinalis": 2})
        # The segment's stored date carries over to the newly kept species.
        self.assertEqual(
            {item.scientific_name: item.detection_count for item in dated.species},
            {"Sialia sialis": 1, "Cardinalis cardinalis": 1},
        )
        self.assertEqual((again.imported, again.duplicates), (0, 2))

    def test_legacy_reclassification_counts_as_an_update(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            write_legacy_state(
                state_dir,
                {
                    legacy_fingerprint("one.wav", 0.0, 3.0): {
                        "common_name": "Eastern Bluebird",
                        "scientific_name": "Sialia sialis",
                        "observed_on": None,
                    }
                },
            )
            csv_path = root / "results.csv"
            csv_path.write_text(
                HEADER + "0,3,Cardinalis cardinalis,Northern Cardinal,0.95,one.wav\n"
            )
            result = import_birdnet_analyzer_csv(csv_path, state_dir)
            counts = species_counts(state_dir)

        self.assertEqual((result.imported, result.updated, result.removed), (0, 1, 0))
        self.assertEqual(counts, {"Cardinalis cardinalis": 1})

    def test_repeated_species_row_in_one_segment_counts_once(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(
                HEADER
                + "0,3,Sialia sialis,Eastern Bluebird,0.91,one.wav\n"
                + "0,3,Sialia sialis,Eastern Bluebird,0.55,one.wav\n"
            )
            result = import_birdnet_analyzer_csv(csv_path, root / "state")
            counts = species_counts(root / "state")

        self.assertEqual((result.rows, result.imported, result.duplicates), (2, 1, 1))
        self.assertEqual(counts, {"Sialia sialis": 1})


class BirdNetAnalyzerLocalFailureTests(unittest.TestCase):
    def test_non_utf8_csv_is_a_data_source_error(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_bytes(
                HEADER.encode() + b"0,3,Sialia sialis,Eastern Bluebird \xff,0.9,one.wav\n"
            )
            with self.assertRaisesRegex(DataSourceError, "not UTF-8"):
                import_birdnet_analyzer_csv(csv_path, root / "state")

        self.assertFalse((root / "state/birdnet-analyzer-detections.json").exists())

    def test_non_utf8_state_is_a_data_source_error_without_its_path(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            path = write_legacy_state(state_dir, {})
            path.write_bytes(b'{"schema_version": 1, "detections": {"\xff": 1}}')
            for read in (
                lambda: read_birdnet_analyzer_history(
                    state_dir, window=ObservationWindow.ALL_TIME, limit=None
                ),
                lambda: analyzer_history_revision(state_dir),
            ):
                with self.assertRaisesRegex(
                    DataSourceError, "Invalid BirdNET Analyzer state"
                ) as raised:
                    read()
                self.assertNotIn(temporary, str(raised.exception))

    def test_state_errors_do_not_contain_absolute_paths(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            path = write_legacy_state(state_dir, {})
            messages = []
            path.chmod(0o644)
            with self.assertRaises(DataSourceError) as insecure:
                analyzer_history_revision(state_dir)
            messages.append(str(insecure.exception))
            path.chmod(0o600)
            path.write_text("{}")
            with self.assertRaises(DataSourceError) as unsupported:
                analyzer_history_revision(state_dir)
            messages.append(str(unsupported.exception))
            path.write_text("[")
            with self.assertRaises(DataSourceError) as invalid:
                analyzer_history_revision(state_dir)
            messages.append(str(invalid.exception))

        for message in messages:
            self.assertNotIn(temporary, message)

    def test_unusable_state_folder_is_a_data_source_error(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.91,one.wav\n")
            (root / "state").write_text("not a folder")
            for action in (
                lambda: import_birdnet_analyzer_csv(csv_path, root / "state"),
                lambda: analyzer_history_revision(root / "state"),
                lambda: read_birdnet_analyzer_history(
                    root / "state", window=ObservationWindow.ALL_TIME, limit=None
                ),
            ):
                with self.assertRaisesRegex(DataSourceError, "could not use its state") as raised:
                    action()
                self.assertNotIn(temporary, str(raised.exception))

    def test_failed_write_is_a_data_source_error_and_keeps_history(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.91,one.wav\n")
            import_birdnet_analyzer_csv(csv_path, root / "state")
            before = (root / "state/birdnet-analyzer-detections.json").read_bytes()
            csv_path.write_text(HEADER + "3,6,Sialia sialis,Eastern Bluebird,0.91,one.wav\n")
            with (
                patch(
                    "inky_bird_frame.birdnet_analyzer.write_json_atomic",
                    side_effect=OSError(28, "No space left on device"),
                ),
                self.assertRaisesRegex(DataSourceError, "could not use its state"),
            ):
                import_birdnet_analyzer_csv(csv_path, root / "state")
            after = (root / "state/birdnet-analyzer-detections.json").read_bytes()

        self.assertEqual(after, before)


class BirdNetAnalyzerBoundsTests(unittest.TestCase):
    def test_overlong_names_and_file_values_are_rejected(self) -> None:
        for row in (
            f"0,3,Sialia sialis,{'B' * 201},0.91,one.wav\n",
            f"0,3,{'S' * 201},Eastern Bluebird,0.91,one.wav\n",
            f"0,3,Sialia sialis,Eastern Bluebird,0.91,{'f' * 4097}.wav\n",
        ):
            with TemporaryDirectory() as temporary, self.subTest(length=len(row)):
                root = Path(temporary)
                csv_path = root / "results.csv"
                csv_path.write_text(HEADER + row)
                with self.assertRaisesRegex(DataSourceError, "CSV row 2"):
                    import_birdnet_analyzer_csv(csv_path, root / "state")

    def test_row_count_is_bounded(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(
                HEADER
                + "".join(
                    f"{start},{start + 3},Sialia sialis,Eastern Bluebird,0.9,one.wav\n"
                    for start in range(0, 12, 3)
                )
            )
            with (
                patch("inky_bird_frame.birdnet_analyzer.MAX_CSV_ROWS", 3),
                self.assertRaisesRegex(DataSourceError, "more than 3 rows"),
            ):
                import_birdnet_analyzer_csv(csv_path, root / "state")
            with patch("inky_bird_frame.birdnet_analyzer.MAX_CSV_ROWS", 4):
                accepted = import_birdnet_analyzer_csv(csv_path, root / "state")

        self.assertEqual(accepted.imported, 4)


class BirdNetAnalyzerHistoryTests(unittest.TestCase):
    def test_windows_exclude_undated_and_out_of_range_detections(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.91,undated.wav\n")
            import_birdnet_analyzer_csv(csv_path, root / "state")
            csv_path.write_text(
                HEADER
                + "0,3,Sialia sialis,Eastern Bluebird,0.92,recent.wav\n"
                + "3,6,Sialia sialis,Eastern Bluebird,0.93,recent.wav\n"
            )
            import_birdnet_analyzer_csv(
                csv_path,
                root / "state",
                observed_on=date(2026, 8, 8),
            )
            all_time = read_birdnet_analyzer_history(
                root / "state",
                window=ObservationWindow.ALL_TIME,
                limit=50,
                today=date(2026, 8, 9),
            )
            last_day = read_birdnet_analyzer_history(
                root / "state",
                window=ObservationWindow.LAST_DAY,
                limit=50,
                today=date(2026, 8, 9),
            )

        self.assertEqual(all_time.species[0].detection_count, 3)
        self.assertEqual(last_day.species[0].detection_count, 2)
        self.assertEqual(last_day.excluded_undated, 1)

    def test_rejects_insecure_permissions(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.91,one.wav\n")
            import_birdnet_analyzer_csv(csv_path, root / "state")
            state_path = root / "state/birdnet-analyzer-detections.json"
            state_path.chmod(0o644)

            with self.assertRaisesRegex(DataSourceError, "mode 0600"):
                read_birdnet_analyzer_history(
                    root / "state",
                    window=ObservationWindow.ALL_TIME,
                    limit=50,
                )

    def test_rejects_tampered_state_without_leaking_detection_data(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            state_dir.mkdir()
            state_path = state_dir / "birdnet-analyzer-detections.json"
            state_path.write_text(json.dumps({"schema_version": 99}))
            state_path.chmod(0o600)

            with self.assertRaisesRegex(DataSourceError, "Unsupported BirdNET Analyzer state"):
                read_birdnet_analyzer_history(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=50,
                )


if __name__ == "__main__":
    unittest.main()
