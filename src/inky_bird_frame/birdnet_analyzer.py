"""Private BirdNET Analyzer CSV detection history."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import math
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

from .birds import BirdNetAnalyzerSpecies, ObservationWindow, date_range_for_window
from .errors import DataSourceError
from .http import write_bytes_atomic, write_json_atomic

STATE_FILENAME = "birdnet-analyzer-detections.json"
REQUIRED_COLUMNS = (
    "Start (s)",
    "End (s)",
    "Scientific name",
    "Common name",
    "Confidence",
    "File",
)
# Bounds for one uploaded table. Analyzer names are short; the File column is
# a path. A combined table this large should be split into several imports.
MAX_CSV_ROWS = 250_000
MAX_NAME_LENGTH = 200
MAX_FILE_LENGTH = 4096


@dataclass(frozen=True)
class BirdNetAnalyzerImportStats:
    rows: int
    imported: int
    updated: int
    duplicates: int
    dated: int
    undated: int
    dry_run: bool
    removed: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows,
            "imported": self.imported,
            "updated": self.updated,
            "duplicates": self.duplicates,
            "removed": self.removed,
            "dated": self.dated,
            "undated": self.undated,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class BirdNetAnalyzerHistory:
    species: list[BirdNetAnalyzerSpecies]
    total_detections: int
    dated_detections: int
    undated_detections: int
    selected_detections: int
    excluded_undated: int
    history_started_at: str
    last_imported_at: str

    def details(self) -> dict[str, object]:
        return {
            "total_detections": self.total_detections,
            "dated_detections": self.dated_detections,
            "undated_detections": self.undated_detections,
            "selected_detections": self.selected_detections,
            "excluded_undated": self.excluded_undated,
            "history_started_at": self.history_started_at,
            "last_imported_at": self.last_imported_at,
        }


@dataclass(frozen=True)
class _StoredDetection:
    common_name: str
    scientific_name: str
    observed_on: date | None
    # One-way segment fingerprint. Rows written before per-species rows have
    # none: their own key is the segment fingerprint.
    segment: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "common_name": self.common_name,
            "scientific_name": self.scientific_name,
            "observed_on": self.observed_on.isoformat() if self.observed_on is not None else None,
            **({"segment": self.segment} if self.segment is not None else {}),
        }

    def same_record(self, other: _StoredDetection) -> bool:
        return (self.common_name, self.scientific_name, self.observed_on) == (
            other.common_name,
            other.scientific_name,
            other.observed_on,
        )


def _is_fingerprint(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@contextmanager
def _state_errors() -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        raise DataSourceError(
            "BirdNET Analyzer could not use its state folder. Check its permissions and free space."
        ) from exc


@contextmanager
def _history_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "birdnet-analyzer.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _state_path(state_dir: Path) -> Path:
    return state_dir / STATE_FILENAME


def _require_private_file(path: Path) -> None:
    if path.is_symlink():
        raise DataSourceError(f"Refusing symlinked BirdNET Analyzer state: {path.name}")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise DataSourceError(f"BirdNET Analyzer state must use mode 0600: {path.name}")


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DataSourceError(f"Invalid BirdNET Analyzer {label}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DataSourceError(f"Invalid BirdNET Analyzer {label}") from exc
    if parsed.tzinfo is None:
        raise DataSourceError(f"Invalid BirdNET Analyzer {label}")
    return parsed.isoformat()


def _parse_detection(value: object) -> _StoredDetection:
    if not isinstance(value, dict):
        raise DataSourceError("Invalid BirdNET Analyzer detection state")
    common_name = value.get("common_name")
    scientific_name = value.get("scientific_name")
    raw_observed_on = value.get("observed_on")
    segment = value.get("segment")
    if (
        not isinstance(common_name, str)
        or not common_name.strip()
        or not isinstance(scientific_name, str)
        or not scientific_name.strip()
        or (raw_observed_on is not None and not isinstance(raw_observed_on, str))
        or (segment is not None and not _is_fingerprint(segment))
    ):
        raise DataSourceError("Invalid BirdNET Analyzer detection state")
    try:
        observed_on = date.fromisoformat(raw_observed_on) if raw_observed_on is not None else None
    except ValueError as exc:
        raise DataSourceError("Invalid BirdNET Analyzer detection date") from exc
    if observed_on is not None and observed_on.isoformat() != raw_observed_on:
        raise DataSourceError("Invalid BirdNET Analyzer detection date")
    return _StoredDetection(
        common_name.strip(),
        scientific_name.strip(),
        observed_on,
        segment if isinstance(segment, str) else None,
    )


def _read_state(
    path: Path, *, required: bool
) -> tuple[dict[str, _StoredDetection], str | None, str | None]:
    try:
        path.stat()
    except FileNotFoundError:
        if required:
            raise DataSourceError(
                "BirdNET Analyzer history is missing; import an Analyzer CSV first"
            ) from None
        return {}, None, None
    _require_private_file(path)
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataSourceError(f"Invalid BirdNET Analyzer state: {path.name}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise DataSourceError(f"Unsupported BirdNET Analyzer state: {path.name}")
    raw_detections = payload.get("detections")
    if not isinstance(raw_detections, dict):
        raise DataSourceError(f"Invalid BirdNET Analyzer state: {path.name}")
    detections: dict[str, _StoredDetection] = {}
    for fingerprint, raw_detection in raw_detections.items():
        if not _is_fingerprint(fingerprint):
            raise DataSourceError("Invalid BirdNET Analyzer detection fingerprint")
        detections[fingerprint] = _parse_detection(raw_detection)
    history_started_at = _timestamp(payload.get("history_started_at"), "history start")
    last_imported_at = _timestamp(payload.get("last_imported_at"), "last import")
    return detections, history_started_at, last_imported_at


def _parse_number(row: dict[str, str | None], name: str, line: int) -> float:
    value = row.get(name)
    try:
        parsed = float(value) if value is not None else math.nan
    except ValueError as exc:
        raise DataSourceError(f"Invalid {name} on BirdNET Analyzer CSV row {line}") from exc
    if not math.isfinite(parsed):
        raise DataSourceError(f"Invalid {name} on BirdNET Analyzer CSV row {line}")
    return parsed


def _fingerprint(*parts: str) -> str:
    identity = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(identity).hexdigest()


def _parse_csv_row(
    row: dict[str, str | None], line: int, observed_on: date | None
) -> tuple[str, str, _StoredDetection]:
    start = _parse_number(row, "Start (s)", line)
    end = _parse_number(row, "End (s)", line)
    confidence = _parse_number(row, "Confidence", line)
    common_name = row.get("Common name")
    scientific_name = row.get("Scientific name")
    source_file = row.get("File")
    if (
        start < 0
        or end <= start
        or not 0 <= confidence <= 1
        or not isinstance(common_name, str)
        or not common_name.strip()
        or len(common_name.strip()) > MAX_NAME_LENGTH
        or not isinstance(scientific_name, str)
        or not scientific_name.strip()
        or len(scientific_name.strip()) > MAX_NAME_LENGTH
        or not isinstance(source_file, str)
        or not source_file.strip()
        or len(source_file.strip()) > MAX_FILE_LENGTH
    ):
        raise DataSourceError(f"Invalid BirdNET Analyzer CSV row {line}")
    # The segment fingerprint keeps the pre-1.0 formula so retained rows still
    # match their segment; each species in the segment is its own row.
    segment = _fingerprint(source_file.strip(), start.hex(), end.hex())
    fingerprint = _fingerprint(source_file.strip(), start.hex(), end.hex(), scientific_name.strip())
    return (
        segment,
        fingerprint,
        _StoredDetection(common_name.strip(), scientific_name.strip(), observed_on, segment),
    )


def _merge_segment(
    detections: dict[str, _StoredDetection],
    previous_keys: list[str],
    incoming: dict[str, _StoredDetection],
    observed_on: date | None,
) -> tuple[int, int, int, int]:
    """Replace one segment's species list; return imported, updated, duplicate, removed."""
    previous = {key: detections[key] for key in previous_keys}
    stored_dates = {item.observed_on for item in previous.values() if item.observed_on is not None}
    if len(stored_dates) > 1 or (
        observed_on is not None and stored_dates and stored_dates != {observed_on}
    ):
        raise DataSourceError("BirdNET Analyzer segment was already imported with a different date")
    segment_date = observed_on if observed_on is not None else next(iter(stored_dates), None)
    by_species = {item.scientific_name: key for key, item in previous.items()}
    unmatched = set(previous)
    added = updated = duplicates = 0
    for fingerprint, detection in incoming.items():
        detection = replace(detection, observed_on=segment_date)
        match = by_species.get(detection.scientific_name)
        if match is None:
            added += 1
        else:
            unmatched.discard(match)
            if previous[match].same_record(detection):
                duplicates += 1
            else:
                updated += 1
            del detections[match]
        detections[fingerprint] = detection
    for key in unmatched:
        del detections[key]
    # A changed species in a segment reads as one changed detection.
    reclassified = min(added, len(unmatched))
    return added - reclassified, updated + reclassified, duplicates, len(unmatched) - reclassified


def import_birdnet_analyzer_csv(
    csv_path: Path,
    state_dir: Path,
    *,
    observed_on: date | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
    operation_id: str | None = None,
) -> BirdNetAnalyzerImportStats:
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat()
    with _state_errors(), _history_lock(state_dir):
        detections, history_started_at, _ = _read_state(_state_path(state_dir), required=False)
        if expected_revision is not None and _revision(state_dir) != expected_revision:
            raise DataSourceError("Analyzer history changed. Preview the CSV again.")
        if operation_id is not None and not re.fullmatch(r"[0-9a-f]{64}", operation_id):
            raise ValueError("Invalid import operation identity")
        rows = imported = updated = duplicates = removed = 0
        upload: dict[str, dict[str, _StoredDetection]] = {}
        try:
            handle = csv_path.open("r", encoding="utf-8-sig", newline="")
        except OSError as exc:
            raise DataSourceError("Could not read BirdNET Analyzer CSV") from exc
        with handle:
            try:
                reader = csv.DictReader(handle, strict=True)
                missing = [
                    name for name in REQUIRED_COLUMNS if name not in (reader.fieldnames or ())
                ]
                if missing:
                    raise DataSourceError(
                        "BirdNET Analyzer CSV is missing required columns: " + ", ".join(missing)
                    )
                for line, row in enumerate(reader, start=2):
                    rows += 1
                    if rows > MAX_CSV_ROWS:
                        raise DataSourceError(
                            f"BirdNET Analyzer CSV has more than {MAX_CSV_ROWS:,} rows. "
                            "Split it into smaller files."
                        )
                    segment, fingerprint, detection = _parse_csv_row(row, line, observed_on)
                    segment_rows = upload.setdefault(segment, {})
                    if fingerprint in segment_rows:
                        duplicates += 1
                        continue
                    segment_rows[fingerprint] = detection
            except csv.Error as exc:
                raise DataSourceError("Invalid BirdNET Analyzer CSV") from exc
            except UnicodeDecodeError as exc:
                raise DataSourceError("BirdNET Analyzer CSV is not UTF-8 text") from exc
            except OSError as exc:
                raise DataSourceError("Could not read BirdNET Analyzer CSV") from exc
        if rows == 0:
            raise DataSourceError("BirdNET Analyzer CSV contains no detection rows")
        stored_by_segment: dict[str, list[str]] = {}
        for key, stored in detections.items():
            stored_by_segment.setdefault(stored.segment or key, []).append(key)
        for segment, incoming in upload.items():
            counts = _merge_segment(
                detections, stored_by_segment.get(segment, []), incoming, observed_on
            )
            imported += counts[0]
            updated += counts[1]
            duplicates += counts[2]
            removed += counts[3]
        dated = sum(item.observed_on is not None for item in detections.values())
        undated = len(detections) - dated
        stats = BirdNetAnalyzerImportStats(
            rows, imported, updated, duplicates, dated, undated, dry_run, removed
        )
        if not dry_run:
            path = _state_path(state_dir)
            previous = path.read_bytes() if path.exists() else None
            # Commit recovery accepts only the original result fields, so the
            # optional removal count stays out of the persisted marker.
            marker = (
                {
                    "operation_id": operation_id,
                    "result": {
                        key: value for key, value in stats.as_dict().items() if key != "removed"
                    },
                }
                if operation_id
                else json.loads(previous or b"{}").get("owner_operation")
            )
            if expected_revision is not None and previous is not None:
                write_bytes_atomic(path.with_suffix(".json.previous"), previous, mode=0o600)
            write_json_atomic(
                _state_path(state_dir),
                {
                    "schema_version": 1,
                    "history_started_at": history_started_at or current,
                    "last_imported_at": current,
                    "detections": {
                        fingerprint: detection.as_dict()
                        for fingerprint, detection in sorted(detections.items())
                    },
                    **({"owner_operation": marker} if marker is not None else {}),
                },
                mode=0o600,
            )
        return stats


def _revision(state_dir: Path) -> str:
    path = _state_path(state_dir)
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def analyzer_history_revision(state_dir: Path) -> str:
    with _state_errors(), _history_lock(state_dir):
        _read_state(_state_path(state_dir), required=False)
        return _revision(state_dir)


def read_birdnet_analyzer_history(
    state_dir: Path,
    *,
    window: ObservationWindow,
    limit: int | None,
    today: date | None = None,
) -> BirdNetAnalyzerHistory:
    if limit is not None and limit <= 0:
        raise ValueError("BirdNET Analyzer species_limit must be greater than zero")
    with _state_errors(), _history_lock(state_dir):
        detections, history_started_at, last_imported_at = _read_state(
            _state_path(state_dir), required=True
        )
    selected_range = date_range_for_window(window, today)
    counts: dict[str, int] = {}
    common_names: dict[str, str] = {}
    excluded_undated = 0
    for detection in detections.values():
        if selected_range.start is not None:
            if detection.observed_on is None:
                excluded_undated += 1
                continue
            if selected_range.end is None or not (
                selected_range.start <= detection.observed_on <= selected_range.end
            ):
                continue
        counts[detection.scientific_name] = counts.get(detection.scientific_name, 0) + 1
        common_names[detection.scientific_name] = detection.common_name
    species = [
        BirdNetAnalyzerSpecies(common_names[name], name, count) for name, count in counts.items()
    ]
    species.sort(key=lambda item: (-item.detection_count, item.common_name.casefold()))
    dated = sum(item.observed_on is not None for item in detections.values())
    return BirdNetAnalyzerHistory(
        species=species[:limit],
        total_detections=len(detections),
        dated_detections=dated,
        undated_detections=len(detections) - dated,
        selected_detections=sum(counts.values()),
        excluded_undated=excluded_undated,
        history_started_at=history_started_at or "",
        last_imported_at=last_imported_at or "",
    )
