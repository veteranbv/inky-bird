from __future__ import annotations

import json
from pathlib import Path

from inky_bird_frame.activity import activity_run, progress, read_activity


def _entries(state_dir: Path) -> dict[str, dict[str, object]]:
    raw = json.loads((state_dir / "activity.json").read_text())
    entries: dict[str, dict[str, object]] = raw["entries"]
    return entries


def test_over_long_value_is_bounded_when_written(tmp_path: Path) -> None:
    with activity_run(tmp_path, "generation"):
        progress("researching", taxon_id=1, common_name="Synthetic " + "x" * 400)
        assert _entries(tmp_path)["generation"]["common_name"] == ("Synthetic " + "x" * 400)[:200]
    view = read_activity(tmp_path)
    assert view["available"] is True
    entries = view["entries"]
    assert isinstance(entries, dict)
    generation = entries["generation"]
    assert generation["status"] == "succeeded"
    assert len(generation["common_name"]) == 200


def test_invalid_values_are_not_written(tmp_path: Path) -> None:
    with activity_run(tmp_path, "refresh"):
        progress("loading", species_count=-1, attempt="two", unknown_field="x")
    row = _entries(tmp_path)["refresh"]
    assert "species_count" not in row and "attempt" not in row and "unknown_field" not in row
    assert row["status"] == "succeeded"


def test_one_bad_entry_or_newer_field_does_not_silence_activity(tmp_path: Path) -> None:
    with activity_run(tmp_path, "refresh"):
        pass
    path = tmp_path / "activity.json"
    raw = json.loads(path.read_text())
    raw["entries"]["generation"] = {"status": "running", "common_name": "x" * 500}
    raw["entries"]["source-future-provider"] = {"status": "succeeded"}
    raw["entries"]["refresh"]["added_in_a_later_release"] = "synthetic"
    path.write_text(json.dumps(raw))
    path.chmod(0o600)

    view = read_activity(tmp_path)
    assert view["available"] is True
    entries = view["entries"]
    assert isinstance(entries, dict)
    assert set(entries) == {"refresh"}
    assert entries["refresh"]["status"] == "succeeded"
    assert "added_in_a_later_release" not in entries["refresh"]

    # Later runs still record their activity in the same file.
    with activity_run(tmp_path, "generation"):
        pass
    assert _entries(tmp_path)["generation"]["status"] == "succeeded"
