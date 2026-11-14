from __future__ import annotations

import hashlib
import json
import stat
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.birdnet_analyzer import (
    import_birdnet_analyzer_csv,
    read_birdnet_analyzer_history,
)
from inky_bird_frame.birds import BirdNetAnalyzerSpecies, ObservationWindow
from inky_bird_frame.errors import DataSourceError

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
