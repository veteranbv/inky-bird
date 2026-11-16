"""Small derived per-species summaries kept beside retained source history.

A summary holds names, counts and calendar dates only; never places. It is
rebuilt from its history file whenever it is missing, invalid or does not
match that file's identity, so it is always safe to delete.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TypeVar

from .errors import DataSourceError
from .http import write_json_atomic

SCHEMA_VERSION = 1
_ENVELOPE = frozenset({"schema_version", "source", "history", "body_sha256"})
# Far above the largest supported history: one short row per species.
MAX_SUMMARY_BYTES = 32 * 1024 * 1024
T = TypeVar("T")


@dataclass(frozen=True)
class SpeciesSummary:
    common_name: str
    scientific_name: str
    count: int
    first_noted: str | None
    last_noted: str | None
    undated: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "common_name": self.common_name,
            "scientific_name": self.scientific_name,
            "count": self.count,
            "first_noted": self.first_noted,
            "last_noted": self.last_noted,
            "undated": self.undated,
        }


class _Range:
    """Accumulates one species' count and dated range in source order."""

    def __init__(self) -> None:
        self.common_name = ""
        self.count = 0
        self.undated = 0
        self.first: date | None = None
        self.last: date | None = None

    def add(self, common_name: str, observed_on: date | None, *, name_from_latest: bool) -> None:
        if (
            not name_from_latest
            or self.last is None
            or (observed_on is not None and observed_on >= self.last)
        ):
            self.common_name = common_name
        self.count += 1
        if observed_on is None:
            self.undated += 1
            return
        self.first = observed_on if self.first is None else min(self.first, observed_on)
        self.last = observed_on if self.last is None else max(self.last, observed_on)


def summarize(
    records: Sequence[tuple[str, str, date | None]], *, name_from_latest: bool
) -> list[SpeciesSummary]:
    """Rows in the full-history readers' order: count, then common name.

    Records arrive in stored order. The shown common name is the last one seen,
    or with ``name_from_latest`` the one on the newest dated record.
    """
    ranges: dict[str, _Range] = {}
    for common, scientific, observed_on in records:
        ranges.setdefault(scientific, _Range()).add(
            common, observed_on, name_from_latest=name_from_latest
        )
    rows = [
        SpeciesSummary(
            item.common_name,
            scientific,
            item.count,
            item.first.isoformat() if item.first is not None else None,
            item.last.isoformat() if item.last is not None else None,
            item.undated,
        )
        for scientific, item in ranges.items()
    ]
    rows.sort(key=lambda item: (-item.count, item.common_name.casefold()))
    return rows


def summary_path(history_path: Path) -> Path:
    return history_path.with_name(f"{history_path.stem}-summary.json")


def _identity(info: os.stat_result) -> dict[str, int]:
    # Every history write replaces the file, so a new write or a restored copy
    # always changes at least one of these.
    return {"st_ino": info.st_ino, "st_size": info.st_size, "st_mtime_ns": info.st_mtime_ns}


def _body_sha256(payload: dict[str, object]) -> str:
    body = {key: value for key, value in payload.items() if key not in _ENVELOPE}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def write_summary(history_path: Path, source: str, body: dict[str, object]) -> None:
    """Caller holds the source's history lock and has just written its history.

    Best effort: the history is already saved, and a summary that could not be
    written is rebuilt by the next read.
    """
    with suppress(OSError):
        write_json_atomic(
            summary_path(history_path),
            {
                **body,
                "body_sha256": _body_sha256(body),
                "schema_version": SCHEMA_VERSION,
                "source": source,
                "history": _identity(history_path.lstat()),
            },
            mode=0o600,
        )


def _fresh_payload(history_path: Path, source: str) -> dict[str, object] | None:
    path = summary_path(history_path)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            return None
        with path.open("rb") as handle:
            encoded = handle.read(MAX_SUMMARY_BYTES + 1)
        if len(encoded) > MAX_SUMMARY_BYTES:
            return None
        payload: object = json.loads(encoded)
        history = history_path.lstat()
    except (OSError, ValueError, RecursionError):
        return None
    # The complete readers refuse a shared or linked history file; so must this.
    if not stat.S_ISREG(history.st_mode) or stat.S_IMODE(history.st_mode) & 0o077:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("source") != source
        or payload.get("history") != _identity(history)
    ):
        return None
    return payload


def load_summary(
    history_path: Path,
    source: str,
    parse: Callable[[dict[str, object]], T],
    rebuild: Callable[[], dict[str, object]],
) -> T:
    """Caller holds the source's history lock, so one reader rebuilds at a time.

    ``rebuild`` reads the complete history without taking that lock again.
    """
    payload = _fresh_payload(history_path, source)
    if payload is not None and (
        _names_showable(payload) or payload.get("body_sha256") == _body_sha256(payload)
    ):
        try:
            return parse(payload)
        except DataSourceError:
            pass
    body = rebuild()
    write_summary(history_path, source, body)
    return parse(body)


def _is_iso_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _count(value: object, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise DataSourceError("Invalid history summary")
    return value


def _showable(value: str) -> bool:
    text = unicodedata.normalize("NFC", value.strip())
    return len(text) <= 256 and not any(unicodedata.category(char).startswith("C") for char in text)


def _names_showable(payload: dict[str, object]) -> bool:
    """False for a summary holding a name the journal would refuse.

    Such a summary is trusted only when its body matches the checksum written
    with it, so its names came from the history; otherwise it is rebuilt.
    Names from the history itself are kept as they are: the journal shows a
    matched bird under its own name and refuses only an unmatched odd name.
    """
    pending: list[object] = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in {"common_name", "scientific_name"}:
                    if isinstance(item, str) and not _showable(item):
                        return False
                else:
                    pending.append(item)
    return True


def parse_species_rows(value: object) -> list[SpeciesSummary]:
    if not isinstance(value, list):
        raise DataSourceError("Invalid history summary")
    rows: list[SpeciesSummary] = []
    fields = {"common_name", "scientific_name", "count", "first_noted", "last_noted", "undated"}
    for item in value:
        if not isinstance(item, dict) or set(item) != fields:
            raise DataSourceError("Invalid history summary")
        common, scientific = item["common_name"], item["scientific_name"]
        first, last = item["first_noted"], item["last_noted"]
        count = _count(item["count"], minimum=1)
        undated = _count(item["undated"], minimum=0)
        if (
            not isinstance(common, str)
            or not isinstance(scientific, str)
            or not scientific
            or undated > count
            or first is not None
            and (not _is_iso_date(first) or last is None)
            or last is not None
            and not _is_iso_date(last)
            or isinstance(first, str)
            and isinstance(last, str)
            and first > last
        ):
            raise DataSourceError("Invalid history summary")
        rows.append(SpeciesSummary(common, scientific, count, first, last, undated))
    return rows


def count_field(payload: dict[str, object], name: str) -> int:
    return _count(payload.get(name), minimum=0)
