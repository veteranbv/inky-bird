from __future__ import annotations

import csv
import json
import stat
import tracemalloc
import unittest
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ebird_fixtures import EXPORT_COLUMNS

from inky_bird_frame.birds import DateRange, EbirdArchiveSpecies, ObservationWindow
from inky_bird_frame.ebird_archive import (
    CSV_FILENAME,
    MAX_COLUMNS,
    MAX_LINE_CHARACTERS,
    MAX_NAME_LENGTH,
    MAX_ZIP_METADATA_BYTES,
    REQUIRED_COLUMNS,
    _parse_archive,
    import_ebird_archive,
    read_ebird_archive_history,
    read_ebird_checklists,
)
from inky_bird_frame.errors import DataSourceError


def _row(
    submission_id: str,
    common_name: str,
    scientific_name: str,
    observed_on: str,
    *,
    location: str = "Private Place",
) -> dict[str, str]:
    row = dict.fromkeys(EXPORT_COLUMNS, "")
    row.update(
        {
            "Submission ID": submission_id,
            "Common Name": common_name,
            "Scientific Name": scientific_name,
            "Taxonomic Order": "1",
            "Count": "1",
            "Location ID": "L-private",
            "Location": location,
            "Latitude": "1.25",
            "Longitude": "-2.5",
            "Date": observed_on,
            "Protocol": "Traveling",
            "All Obs Reported": "1",
        }
    )
    return row


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _write_zip(path: Path, rows: list[dict[str, str]]) -> None:
    csv_path = path.with_suffix(".csv")
    _write_csv(csv_path, rows)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(csv_path, CSV_FILENAME)


class EbirdArchiveImportTests(unittest.TestCase):
    def test_zip_metadata_is_bounded_before_member_objects_are_allocated(self) -> None:
        for zip64 in [False, True]:
            with self.subTest(zip64=zip64), TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / "many-members.zip"
                with (
                    patch("zipfile.ZIP64_LIMIT", 0 if zip64 else zipfile.ZIP64_LIMIT),
                    zipfile.ZipFile(path, "w") as archive,
                ):
                    for index in range(5000):
                        archive.writestr(str(index), b"")
                with (
                    patch("inky_bird_frame.ebird_archive.zipfile.ZipInfo") as member,
                    self.assertRaisesRegex(DataSourceError, "ZIP metadata"),
                ):
                    import_ebird_archive(path, root / "state")
                member.assert_not_called()
                self.assertFalse((root / "state/ebird-archive-observations.json").exists())

    def test_forged_member_count_cannot_bypass_metadata_byte_limit(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "many-members.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for index in range(5000):
                    archive.writestr(str(index), b"")
            contents = bytearray(path.read_bytes())
            # EOCD's two 16-bit member counts start eight bytes into its header.
            contents[-14:-10] = b"\x01\x00\x01\x00"
            path.write_bytes(contents)
            with (
                patch("inky_bird_frame.ebird_archive.zipfile.ZipInfo") as member,
                self.assertRaisesRegex(DataSourceError, "ZIP metadata"),
            ):
                import_ebird_archive(path, root / "state")
            member.assert_not_called()
            self.assertFalse((root / "state/ebird-archive-observations.json").exists())

    def test_single_file_zip64_archive_is_accepted(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "zip64.zip"
            with patch("zipfile.ZIP64_LIMIT", 0):
                _write_zip(
                    path, [_row("S1", "Northern Cardinal", "Cardinalis cardinalis", "2026-08-08")]
                )
            self.assertIn(b"PK\x06\x06", path.read_bytes())
            self.assertEqual(import_ebird_archive(path, root / "state").checklists, 1)

    def test_metadata_read_limit_does_not_limit_streamed_csv_content(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "history.zip"
            rows = [
                _row(f"S{index}", "Northern Cardinal", "Cardinalis cardinalis", "2026-08-08")
                for index in range(3000)
            ]
            _write_zip(path, rows)
            with zipfile.ZipFile(path) as archive:
                self.assertGreater(archive.getinfo(CSV_FILENAME).file_size, MAX_ZIP_METADATA_BYTES)
            self.assertEqual(import_ebird_archive(path, root / "state").checklists, 3000)

    def test_zip_import_is_private_atomic_and_idempotent(self) -> None:
        rows = [
            _row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08"),
            _row("S1", "Northern Cardinal", "Cardinalis cardinalis", "2026-08-08"),
            _row("S2", "Eastern Bluebird", "Sialia sialis", "2026-08-09"),
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "download.zip"
            _write_zip(archive_path, rows)
            first = import_ebird_archive(
                archive_path,
                root / "state",
                now=datetime(2026, 8, 9, 12, tzinfo=UTC),
            )
            second = import_ebird_archive(
                archive_path,
                root / "state",
                now=datetime(2026, 8, 9, 13, tzinfo=UTC),
            )
            state_path = root / "state/ebird-archive-observations.json"
            serialized = state_path.read_text()
            mode = stat.S_IMODE(state_path.stat().st_mode)

        self.assertEqual(first.rows, 3)
        self.assertEqual(first.checklists, 2)
        self.assertEqual(first.species, 2)
        self.assertEqual(first.added_checklists, 2)
        self.assertEqual(second.unchanged_checklists, 2)
        self.assertEqual(mode, 0o600)
        self.assertNotIn("S1", serialized)
        self.assertNotIn("Private Place", serialized)
        self.assertNotIn("L-private", serialized)
        self.assertNotIn("1.25", serialized)

    def test_direct_csv_supports_all_time_and_finite_windows(self) -> None:
        rows = [
            _row("S1", "Eastern Bluebird", "Sialia sialis", "2025-01-01"),
            _row("S2", "Eastern Bluebird", "Sialia sialis", "2026-08-08"),
            _row("S2", "Northern Cardinal", "Cardinalis cardinalis", "2026-08-08"),
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "renamed-export.csv"
            _write_csv(csv_path, rows)
            import_ebird_archive(csv_path, root / "state")
            all_time = read_ebird_archive_history(
                root / "state",
                window=ObservationWindow.ALL_TIME,
                limit=50,
                today=date(2026, 8, 9),
            )
            last_day = read_ebird_archive_history(
                root / "state",
                window=ObservationWindow.LAST_DAY,
                limit=50,
                today=date(2026, 8, 9),
            )
            exact_range = read_ebird_archive_history(
                root / "state",
                window=ObservationWindow.ALL_TIME,
                date_range=DateRange(date(2025, 1, 1), date(2025, 1, 1)),
                limit=50,
            )

        self.assertEqual(
            all_time.species,
            [
                EbirdArchiveSpecies("Eastern Bluebird", "Sialia sialis", 2),
                EbirdArchiveSpecies("Northern Cardinal", "Cardinalis cardinalis", 1),
            ],
        )
        self.assertEqual(last_day.selected_observations, 2)
        self.assertEqual(last_day.excluded_observations, 1)
        self.assertEqual(last_day.earliest_observed_on, "2025-01-01")
        self.assertEqual(
            exact_range.species, [EbirdArchiveSpecies("Eastern Bluebird", "Sialia sialis", 1)]
        )

    def test_complete_snapshot_updates_checklists_and_protects_against_reduction(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(
                csv_path,
                [
                    _row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08"),
                    _row("S2", "Northern Cardinal", "Cardinalis cardinalis", "2026-08-09"),
                ],
            )
            import_ebird_archive(csv_path, root / "state")
            before = (root / "state/ebird-archive-observations.json").read_bytes()
            _write_csv(
                csv_path,
                [_row("S1", "Bluebird", "Sialia sialis", "2026-08-08")],
            )
            with self.assertRaisesRegex(DataSourceError, "omits previously imported checklists"):
                import_ebird_archive(csv_path, root / "state")
            self.assertEqual((root / "state/ebird-archive-observations.json").read_bytes(), before)
            result = import_ebird_archive(
                csv_path,
                root / "state",
                allow_history_reduction=True,
            )
            history = read_ebird_archive_history(
                root / "state",
                window=ObservationWindow.ALL_TIME,
                limit=50,
            )

        self.assertEqual(result.removed_checklists, 1)
        self.assertEqual(result.updated_checklists, 1)
        self.assertEqual(history.species, [EbirdArchiveSpecies("Bluebird", "Sialia sialis", 1)])

    def test_duplicate_rows_are_counted_once(self) -> None:
        duplicated = _row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(csv_path, [duplicated, duplicated])
            result = import_ebird_archive(csv_path, root / "state")
            history = read_ebird_archive_history(
                root / "state",
                window=ObservationWindow.ALL_TIME,
                limit=50,
            )

        self.assertEqual(result.rows, 2)
        self.assertEqual(result.duplicate_rows, 1)
        self.assertEqual(history.total_observations, 1)

    def test_dry_run_does_not_write_history(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(
                csv_path,
                [_row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08")],
            )
            result = import_ebird_archive(csv_path, root / "state", dry_run=True)

        self.assertTrue(result.dry_run)
        self.assertFalse((root / "state/ebird-archive-observations.json").exists())

    def test_malformed_and_unsafe_archives_fail_closed_without_leaking_path(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "private-download.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../MyEBirdData.csv", "not safe")
            with self.assertRaises(DataSourceError) as raised:
                import_ebird_archive(archive_path, root / "state")
            self.assertNotIn(str(archive_path), str(raised.exception))

            csv_path = root / "bad.csv"
            csv_path.write_text("Submission ID,Common Name\nS1,Bluebird\n")
            with self.assertRaisesRegex(DataSourceError, "missing required columns"):
                import_ebird_archive(csv_path, root / "state")

            _write_csv(
                csv_path,
                [_row("S1", "Eastern Bluebird", "Sialia sialis", "08/08/2026")],
            )
            with self.assertRaisesRegex(DataSourceError, "date on row 2"):
                import_ebird_archive(csv_path, root / "state")

        self.assertFalse((root / "state/ebird-archive-observations.json").exists())

    def test_rejects_insecure_or_oversized_state_and_input(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(
                csv_path,
                [_row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08")],
            )
            with (
                patch("inky_bird_frame.ebird_archive.MAX_UNCOMPRESSED_BYTES", 1),
                self.assertRaisesRegex(DataSourceError, "size limit"),
            ):
                import_ebird_archive(csv_path, root / "state")
            import_ebird_archive(csv_path, root / "state")
            state_path = root / "state/ebird-archive-observations.json"
            state_path.chmod(0o644)
            with self.assertRaisesRegex(DataSourceError, "mode 0600"):
                read_ebird_archive_history(
                    root / "state",
                    window=ObservationWindow.ALL_TIME,
                    limit=50,
                )

    def test_rejects_tampered_state(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            state_dir.mkdir()
            state_path = state_dir / "ebird-archive-observations.json"
            state_path.write_text(json.dumps({"schema_version": 99}))
            state_path.chmod(0o600)
            with self.assertRaisesRegex(DataSourceError, "Unsupported eBird archive state"):
                read_ebird_archive_history(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=50,
                )

    def test_rejects_inconsistent_state_metadata_and_species(self) -> None:
        rows = [_row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08")]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(csv_path, rows)
            import_ebird_archive(csv_path, root / "state")
            state_path = root / "state/ebird-archive-observations.json"
            payload = json.loads(state_path.read_text())
            payload["earliest_observed_on"] = "2026-08-07"
            state_path.write_text(json.dumps(payload))
            state_path.chmod(0o600)
            with self.assertRaisesRegex(DataSourceError, "date range"):
                read_ebird_archive_history(
                    root / "state",
                    window=ObservationWindow.ALL_TIME,
                    limit=50,
                )

            payload["earliest_observed_on"] = "2026-08-08"
            checklist = next(iter(payload["checklists"].values()))
            checklist["species"].append(
                {
                    "common_name": "Bluebird",
                    "scientific_name": "Sialia sialis",
                }
            )
            state_path.write_text(json.dumps(payload))
            state_path.chmod(0o600)
            with self.assertRaisesRegex(DataSourceError, "Duplicate species"):
                read_ebird_archive_history(
                    root / "state",
                    window=ObservationWindow.ALL_TIME,
                    limit=50,
                )

    def test_rejects_csv_rows_with_extra_columns(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(
                csv_path,
                [_row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08")],
            )
            lines = csv_path.read_text().splitlines()
            csv_path.write_text("\n".join((lines[0], f"{lines[1]},unexpected")))
            with self.assertRaisesRegex(DataSourceError, "Malformed.*row 2"):
                import_ebird_archive(csv_path, root / "state")

    def test_damaged_or_non_utf8_exports_are_reported_without_cell_content(self) -> None:
        rows = [
            _row(f"S{index}", "Eastern Bluebird", "Sialia sialis", "2026-08-08")
            for index in range(2000)
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            truncated = root / "truncated.zip"
            _write_zip(truncated, rows)
            truncated.write_bytes(truncated.read_bytes()[: truncated.stat().st_size // 2])
            corrupted = root / "corrupted.zip"
            _write_zip(corrupted, rows)
            contents = bytearray(corrupted.read_bytes())
            with zipfile.ZipFile(corrupted) as archive:
                info = archive.getinfo(CSV_FILENAME)
            start = info.header_offset + 30 + len(info.filename) + len(info.extra)
            # A first deflate block of type 3 does not exist: zlib rejects the stream.
            contents[start : start + info.compress_size] = b"\xff" * info.compress_size
            corrupted.write_bytes(contents)
            checksum = root / "checksum.zip"
            _write_zip(checksum, rows)
            contents = bytearray(checksum.read_bytes())
            directory = contents.index(b"PK\x01\x02") + 16
            contents[directory : directory + 4] = bytes(4)
            checksum.write_bytes(contents)
            short = root / "short.csv"
            _write_csv(short, rows[:1])
            short.write_bytes(short.read_bytes().replace(b"Bluebird", b"Blue\xffbird"))
            late = root / "late.csv"
            _write_csv(late, rows)
            body = late.read_bytes()
            cut = body.rindex(b"Bluebird")
            late.write_bytes(body[:cut] + b"\xff" + body[cut:])
            cases = {
                truncated: "^This eBird export is damaged; download it again$",
                corrupted: "^This eBird export is damaged; download it again$",
                checksum: "damaged near row [0-9]+; download it again",
                short: "not an eBird export: it is not UTF-8 text",
                late: "not UTF-8 text near row [0-9]+",
            }
            for path, message in cases.items():
                with self.subTest(path=path.name):
                    with self.assertRaisesRegex(DataSourceError, message) as raised:
                        import_ebird_archive(path, state)
                    for private in ("Bluebird", "Sialia", "S1", str(root)):
                        self.assertNotIn(private, str(raised.exception))
            self.assertFalse((state / "ebird-archive-observations.json").exists())

    def test_unused_export_columns_may_be_renamed_or_removed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            columns = [
                "Renamed Count" if name == "Count" else name
                for name in EXPORT_COLUMNS
                if name not in {"Latitude", "Longitude", "ML Catalog Numbers"}
            ]
            row = _row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08")
            row["Renamed Count"] = row.pop("Count")
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                writer.writerow(row)
            self.assertEqual(import_ebird_archive(csv_path, root / "state").species, 1)
            minimal = root / "minimal.csv"
            with minimal.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                writer.writerow(row)
            self.assertEqual(import_ebird_archive(minimal, root / "state").checklists, 1)
            for missing in REQUIRED_COLUMNS:
                with self.subTest(missing=missing):
                    with minimal.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.DictWriter(
                            handle,
                            fieldnames=[name for name in REQUIRED_COLUMNS if name != missing],
                            extrasaction="ignore",
                        )
                        writer.writeheader()
                        writer.writerow(row)
                    with self.assertRaisesRegex(
                        DataSourceError, f"missing required columns: {missing}$"
                    ):
                        import_ebird_archive(minimal, root / "state")

    def test_species_names_are_bounded_visible_text(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            for column, value in (
                ("Common Name", "B" * (MAX_NAME_LENGTH + 1)),
                ("Scientific Name", "Sialia\u0007sialis"),
                ("Common Name", "Eastern\u200bBluebird"),
            ):
                with self.subTest(column=column, value=value[:12]):
                    row = _row("S2", "Eastern Bluebird", "Sialia sialis", "2026-08-08")
                    row[column] = value
                    _write_csv(
                        csv_path,
                        [
                            _row("S1", "Northern Cardinal", "Cardinalis cardinalis", "2026-08-08"),
                            row,
                        ],
                    )
                    with self.assertRaisesRegex(
                        DataSourceError, f"^Invalid {column} on eBird archive row 3$"
                    ):
                        import_ebird_archive(csv_path, root / "state")
            self.assertFalse((root / "state/ebird-archive-observations.json").exists())
            row = _row("S1", "B" * MAX_NAME_LENGTH, "Sialia sialis", "2026-08-08")
            _write_csv(csv_path, [row])
            self.assertEqual(import_ebird_archive(csv_path, root / "state").species, 1)


class EbirdArchiveBoundsTests(unittest.TestCase):
    """A hostile export within the byte limit must not exhaust a small controller."""

    def _rows(self, count: int, *, checklists: int, species: int) -> list[dict[str, str]]:
        return [
            _row(
                f"S{index % checklists}",
                f"Fixture bird {index % species}",
                f"Fixturus avis{index % species}",
                "2026-08-08",
            )
            for index in range(count)
        ]

    def test_rows_checklists_and_species_are_capped(self) -> None:
        cases = (
            ("MAX_ROWS", self._rows(5, checklists=5, species=5), "more than 4 rows"),
            ("MAX_CHECKLISTS", self._rows(5, checklists=5, species=1), "more than 4 checklists"),
            ("MAX_SPECIES", self._rows(5, checklists=1, species=5), "more than 4 species"),
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            for limit, rows, message in cases:
                with self.subTest(limit=limit):
                    _write_csv(csv_path, rows)
                    with (
                        patch(f"inky_bird_frame.ebird_archive.{limit}", 4),
                        self.assertRaisesRegex(DataSourceError, message),
                    ):
                        import_ebird_archive(csv_path, root / "state", dry_run=True)
                    with patch(f"inky_bird_frame.ebird_archive.{limit}", 5):
                        import_ebird_archive(csv_path, root / "state", dry_run=True)
            self.assertFalse((root / "state/ebird-archive-observations.json").exists())

    def _peak_parse_bytes(self, path: Path) -> int:
        tracemalloc.start()
        try:
            _parse_archive(path)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_names_repeated_across_checklists_are_held_once(self) -> None:
        # Each stored observation may cost a dictionary slot, but not its own
        # copy of a name that other checklists already hold.
        checklists = 3000
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            single, repeated = root / "single.csv", root / "repeated.csv"
            _write_csv(single, self._rows(checklists, checklists=checklists, species=50))
            _write_csv(
                repeated,
                [
                    _row(
                        f"S{index // 6}",
                        f"Fixture bird {index % 50}",
                        f"Fixturus avis{index % 50}",
                        "2026-08-08",
                    )
                    for index in range(checklists * 6)
                ],
            )
            extra_pairs = checklists * 5
            growth = self._peak_parse_bytes(repeated) - self._peak_parse_bytes(single)
            self.assertLess(growth / extra_pairs, 50)

    def test_stored_history_shares_species_between_checklists(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(
                csv_path,
                [
                    _row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08"),
                    _row("S2", "Eastern Bluebird", "Sialia sialis", "2026-08-09"),
                ],
            )
            import_ebird_archive(csv_path, root / "state")
            _, (first, second) = read_ebird_checklists(root / "state")
            self.assertEqual(first.species, second.species)
            self.assertIs(first.species[0], second.species[0])
            self.assertIs(first.species[0].scientific_name, second.species[0].scientific_name)

    def test_header_column_count_is_capped(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            row = _row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08")
            extra = [f"Extra {index}" for index in range(MAX_COLUMNS - len(EXPORT_COLUMNS) + 1)]
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[*EXPORT_COLUMNS, *extra], restval="")
                writer.writeheader()
                writer.writerow(row)
            with self.assertRaisesRegex(DataSourceError, f"more than {MAX_COLUMNS} columns"):
                import_ebird_archive(csv_path, root / "state")
            _write_csv(csv_path, [row])
            self.assertEqual(import_ebird_archive(csv_path, root / "state").rows, 1)

    def test_a_line_longer_than_the_limit_is_refused_even_when_each_field_fits(self) -> None:
        # Each field stays under the CSV module's own per-field limit.
        field = "x" * (MAX_LINE_CHARACTERS // 3 + 1)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            first = _row("S1", "Northern Cardinal", "Cardinalis cardinalis", "2026-08-08")
            long = _row("S2", "Eastern Bluebird", "Sialia sialis", "2026-08-08")
            long.update({"Observation Details": field, "Checklist Comments": field, "Time": field})
            for path, writer in (
                (csv_path, _write_csv),
                (root / "MyEBirdData.zip", _write_zip),
            ):
                with self.subTest(path=path.suffix):
                    writer(path, [first, long])
                    with self.assertRaisesRegex(
                        DataSourceError, "^eBird archive line 3 is too long$"
                    ) as raised:
                        import_ebird_archive(path, root / "state")
                    self.assertNotIn("xxxx", str(raised.exception))
            # A long quoted value split over several lines is still one row.
            first["Checklist Comments"] = "\n".join(["y" * 1000] * 50)
            _write_csv(csv_path, [first])
            self.assertEqual(import_ebird_archive(csv_path, root / "state").rows, 1)


if __name__ == "__main__":
    unittest.main()
