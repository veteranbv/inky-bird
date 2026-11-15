from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from plate_tuning.config import HarnessError, load_config
from plate_tuning.panel import (
    DEFAULT_PANEL,
    STRATA,
    freeze_references,
    frozen_references,
    load_panel,
    select,
)
from plate_tuning.rundir import Run
from plate_tuning_fixtures import FakeDownloader, write_catalog

from inky_bird_frame.models import ReferencePhoto
from inky_bird_frame.references import ReferenceCandidate

REPOSITORY_CATALOG = Path(__file__).resolve().parents[1] / "catalog"


def test_checked_in_panel_matches_the_committed_catalog() -> None:
    panel = load_panel(REPOSITORY_CATALOG, DEFAULT_PANEL)

    assert len(panel) == 20
    splits = Counter(entry.split for entry in panel)
    assert splits == {"dev": 12, "holdout": 8}
    assert {entry.stratum for entry in panel} == set(STRATA)
    assert len({entry.taxon_id for entry in panel}) == 20
    # The skimmer exercises the prompt's species-gated bill wording.
    assert any(entry.scientific_name.startswith("Rynchops ") for entry in panel)
    for entry in panel:
        assert entry.profile["taxon_id"] == entry.taxon_id
        assert len(entry.catalog_references) >= 2


def test_panel_entries_are_checked_against_the_catalog(tmp_path: Path) -> None:
    catalog, panel_path = write_catalog(tmp_path)
    entries = json.loads(panel_path.read_text())["species"]
    cases = {
        "unknown taxon": {**entries[0], "taxon_id": 999},
        "name mismatch": {**entries[0], "common_name": "Other Bird"},
        "unknown stratum": {**entries[0], "stratum": "submarine"},
        "bad split": {**entries[0], "split": "train"},
    }
    for broken in cases.values():
        panel_path.write_text(json.dumps({"species": [broken, *entries[1:]]}))
        with pytest.raises(HarnessError):
            load_panel(catalog, panel_path)
    panel_path.write_text(json.dumps({"species": [entries[0], entries[0]]}))
    with pytest.raises(HarnessError, match="unique"):
        load_panel(catalog, panel_path)


def test_unapproved_catalog_profile_is_refused(tmp_path: Path) -> None:
    catalog, panel_path = write_catalog(tmp_path)
    manifest_path = next(catalog.glob("species/101-*/manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = "rejected"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(HarnessError, match="not approved"):
        load_panel(catalog, panel_path)


def test_select_by_split_and_taxon(tmp_path: Path) -> None:
    panel = load_panel(*write_catalog(tmp_path))

    assert [entry.taxon_id for entry in select(panel, split="holdout")] == [103, 104]
    assert [entry.taxon_id for entry in select(panel, taxa=[102])] == [102]
    with pytest.raises(HarnessError):
        select(panel, taxa=[555])


def test_references_are_downloaded_once_and_frozen(tmp_path: Path) -> None:
    panel = load_panel(*write_catalog(tmp_path))
    run = Run.open(tmp_path / "run", seed=7)
    downloader = FakeDownloader()

    with pytest.raises(HarnessError, match="not cached"):
        frozen_references(run, panel[0])
    first = freeze_references(run, panel[0], downloader=downloader)
    again = freeze_references(run, panel[0], downloader=downloader)

    assert downloader.calls == 1
    assert again == first
    assert [path.is_file() for path in first.paths] == [True, True]
    manifest = json.loads((first.directory / "manifest.json").read_text())
    # The catalog's recorded checksums are placeholders here, so they cannot match.
    assert manifest["matches_catalog"] is False
    assert frozen_references(run, panel[0]) == first


def test_a_changed_frozen_reference_is_never_silently_refetched(tmp_path: Path) -> None:
    panel = load_panel(*write_catalog(tmp_path))
    run = Run.open(tmp_path / "run", seed=7)
    downloader = FakeDownloader()
    frozen = freeze_references(run, panel[0], downloader=downloader)
    frozen.paths[0].write_bytes(b"different")

    with pytest.raises(HarnessError, match="changed"):
        freeze_references(run, panel[0], downloader=downloader)
    assert downloader.calls == 1


def test_a_failed_download_leaves_nothing_frozen(tmp_path: Path) -> None:
    panel = load_panel(*write_catalog(tmp_path))
    run = Run.open(tmp_path / "run", seed=7)

    def broken(candidates: list[ReferenceCandidate], destination: Path) -> list[ReferencePhoto]:
        (destination / "01-partial.jpg").write_bytes(b"half")
        raise HarnessError("network down")

    with pytest.raises(HarnessError, match="network down"):
        freeze_references(run, panel[0], downloader=broken)
    assert not run.path("references", "101").exists()
    assert not any(run.path("references").iterdir())


def test_run_seed_is_kept_and_checked(tmp_path: Path) -> None:
    run = Run.open(tmp_path / "run")
    assert Run.open(tmp_path / "run").seed == run.seed
    with pytest.raises(HarnessError, match="different seed"):
        Run.open(tmp_path / "run", seed=run.seed + 1)
    assert run.derived("x") == Run.open(tmp_path / "run").derived("x")
    assert run.derived("x") != run.derived("y")


def test_config_file_overrides_defaults_and_rejects_unknown_keys(tmp_path: Path) -> None:
    default = load_config(None)
    assert [judge.kind for judge in default.judges] == ["claude", "claude", "codex"]
    assert default.judges[0].model == "claude-fable-5-2"
    assert default.judges[2].model == "gpt-6-astra"

    path = tmp_path / "eval.toml"
    path.write_text(
        'samples_per_species = 2\nconfidence = 0.9\n[[judges]]\nname = "solo"\nkind = "codex"\n'
    )
    loaded = load_config(path)
    assert loaded.samples_per_species == 2
    assert loaded.confidence == 0.9
    assert [judge.name for judge in loaded.judges] == ["solo"]
    assert loaded.judges[0].model == "gpt-6-astra"
    path.write_text('[[judges]]\nname = "c"\nkind = "codex"\nmodel = "other-model"\n')
    assert load_config(path).judges[0].model == "other-model"

    for text in ("surprise = 1\n", 'samples_per_species = "3"\n', "anatomy_weight = 0.9\n"):
        path.write_text(text)
        with pytest.raises(HarnessError):
            load_config(path)
