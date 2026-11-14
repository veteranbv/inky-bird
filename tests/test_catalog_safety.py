"""Catalog approval, removal and recovery must never leave the catalog unusable."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from png_fixtures import write_plate_pair, write_png, write_truncated_png
from test_controller import CONFIG, discovery_result

from inky_bird_frame import catalog as catalog_module
from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.catalog import (
    CandidateApprovalError,
    approve_candidate,
    candidate_directory,
    has_valid_approved_candidate,
    read_catalog_entries,
    reject_candidate,
    write_candidate_manifest,
)
from inky_bird_frame.config import AppConfig, load_config
from inky_bird_frame.controller import (
    _write_generation_queue,
    generate_candidate,
    read_generation_queue,
    retry_approved_candidate,
    run_controller_cycle,
)
from inky_bird_frame.errors import CatalogError
from inky_bird_frame.geo import DiscoveryLocation
from inky_bird_frame.models import QualityReview, SpeciesProfileData
from inky_bird_frame.prompts import PROMPT_VERSION
from inky_bird_frame.recovery import retry_species
from inky_bird_frame.retry import RetryStore

LOCATION = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
WREN = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 2, "test")
CARDINAL = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
SOURCED_REVIEW = QualityReview(
    True,
    5,
    5,
    5,
    5,
    True,
    (),
    (
        {"title": "Source one", "url": "https://example.test/one"},
        {"title": "Source two", "url": "https://example.test/two"},
    ),
)


def profile_for(species: BirdSpecies) -> SpeciesProfileData:
    return SpeciesProfileData(
        taxon_id=species.taxon_id,
        common_name=species.common_name,
        scientific_name=species.scientific_name,
        family="Example family",
        measurements={"length": "10 cm", "wingspan": "20 cm", "weight": "30 g"},
        field_marks=["mark one", "mark two", "mark three", "mark four"],
        habitat="Woodland",
        behavior="Forages low",
        palette=["brown", "cream", "black"],
        sources=[
            {"title": "Source one", "url": "https://example.test/one"},
            {"title": "Source two", "url": "https://example.test/two"},
        ],
    )


def load_test_config(root: Path) -> AppConfig:
    path = root / "config.toml"
    path.write_text(CONFIG)
    return load_config(path)


def write_pending(config: AppConfig, species: BirdSpecies, *, truncated: bool = False) -> Path:
    candidate = candidate_directory(config.controller.state_dir, species)
    if truncated:
        write_truncated_png(candidate / "portrait.png")
        write_png(candidate / "display.png")
    else:
        write_plate_pair(candidate)
    write_candidate_manifest(
        candidate,
        species,
        profile_for(species),
        [],
        SOURCED_REVIEW,
        generator="test",
        prompt_version=PROMPT_VERSION,
        attempt=1,
        max_attempts=3,
    )
    return candidate


def approve(config: AppConfig, species: BirdSpecies) -> Path:
    write_pending(config, species)
    approve_candidate(config.controller.state_dir, config.controller.catalog_dir, species.taxon_id)
    return (
        config.controller.catalog_dir
        / "species"
        / candidate_directory(config.controller.state_dir, species).name
    )


def corrupt(approved: Path) -> None:
    write_png(approved / "portrait.png", color=(1, 1, 1))


def tree(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_damaged_pending_plate_stays_pending_and_the_cycle_continues(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    damaged = write_pending(config, WREN, truncated=True)
    healthy = write_pending(config, CARDINAL)
    damaged_before = tree(damaged)

    with patch(
        "inky_bird_frame.controller.discover_species",
        return_value=discovery_result(LOCATION, [WREN, CARDINAL]),
    ):
        result = run_controller_cycle(config)

    approved = [entry.taxon_id for entry in read_catalog_entries(config.controller.catalog_dir)]
    assert approved == [CARDINAL.taxon_id]
    assert not healthy.exists()
    assert tree(damaged) == damaged_before
    failures = result["failures"]
    assert isinstance(failures, list)
    held = [item for item in failures if item["taxon_id"] == WREN.taxon_id]
    assert held == [
        {
            "taxon_id": WREN.taxon_id,
            "common_name": WREN.common_name,
            "error": held[0]["error"],
            "failure": f"pending/{damaged.name}",
            "terminal": True,
        }
    ]
    assert str(tmp_path) not in json.dumps(held)
    assert not (config.controller.catalog_dir / ".staging").exists()


def test_damaged_plate_from_this_cycle_is_held_without_aborting(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)

    def generate(*args: object, **_kwargs: object) -> Path:
        species = args[1]
        assert isinstance(species, BirdSpecies)
        return write_pending(config, species, truncated=True)

    with (
        patch(
            "inky_bird_frame.controller.discover_species",
            return_value=discovery_result(LOCATION, [WREN]),
        ),
        patch("inky_bird_frame.controller.generate_candidate", side_effect=generate),
    ):
        result = run_controller_cycle(config)

    assert read_catalog_entries(config.controller.catalog_dir) == []
    assert (candidate_directory(config.controller.state_dir, WREN) / "manifest.json").is_file()
    assert result["generated"] == []
    failures = result["failures"]
    assert isinstance(failures, list)
    assert [(item["taxon_id"], item["terminal"]) for item in failures] == [(WREN.taxon_id, True)]


def test_unexpected_failure_records_a_backoff_and_later_birds_still_run(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    attempted: list[int] = []

    def generate(*args: object, **_kwargs: object) -> Path:
        species = args[1]
        assert isinstance(species, BirdSpecies)
        attempted.append(species.taxon_id)
        if species.taxon_id == WREN.taxon_id:
            raise KeyError(str(tmp_path / "private"))
        return write_pending(config, species)

    with (
        patch(
            "inky_bird_frame.controller.discover_species",
            return_value=discovery_result(LOCATION, [WREN, CARDINAL]),
        ),
        patch("inky_bird_frame.controller.generate_candidate", side_effect=generate),
    ):
        result = run_controller_cycle(config)

    retry = RetryStore(config.controller.state_dir / "generation-retries.json").get(WREN.taxon_id)
    assert sorted(attempted) == [WREN.taxon_id, CARDINAL.taxon_id]
    assert retry is not None
    assert retry.error_type == "KeyError"
    assert str(tmp_path) not in retry.error
    assert retry.next_attempt_at > retry.last_failed_at
    failures = result["failures"]
    assert isinstance(failures, list)
    assert [(item["taxon_id"], item["terminal"]) for item in failures] == [(WREN.taxon_id, False)]
    generated = result["generated"]
    assert isinstance(generated, list)
    assert [item["taxon_id"] for item in generated] == [CARDINAL.taxon_id]


def test_second_rejection_of_one_bird_does_not_abort_generation(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    for _ in range(2):
        write_pending(config, WREN)
        reject_candidate(config.controller.state_dir, WREN.taxon_id, "Wrong bill")
    rejected = sorted(path.name for path in (config.controller.state_dir / "rejected").iterdir())
    assert rejected == ["7513-carolina-wren", "7513-carolina-wren-1"]

    with (
        patch(
            "inky_bird_frame.controller.discover_species",
            return_value=discovery_result(LOCATION, [WREN]),
        ),
        patch("inky_bird_frame.controller.generate_candidate") as generate,
    ):
        result = run_controller_cycle(config)

    # The rejected bird is held for an explicit retry, and the cycle completes.
    generate.assert_not_called()
    assert result["eligible_count"] == 0
    assert result["failures"] == []


def test_abandoned_generation_work_is_swept_at_cycle_start(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    leaked_work = config.controller.state_dir / "work/9083-abandoned"
    leaked_output = (
        config.controller.workspace_dir.resolve()
        / ".inky-bird-frame-generation/9083-attempt-01-abandoned"
    )
    unrelated = config.controller.state_dir / "work/publish-unrelated"
    for directory in (leaked_work, leaked_output, unrelated):
        directory.mkdir(parents=True)
        (directory / "partial.png").write_bytes(b"partial")

    with patch(
        "inky_bird_frame.controller.discover_species",
        return_value=discovery_result(LOCATION, []),
    ):
        run_controller_cycle(config)

    assert not leaked_work.exists()
    assert not leaked_output.exists()
    assert unrelated.is_dir()


class PassingRunner:
    def __init__(self, _executable: Path, _workspace: Path) -> None:
        pass

    def create_profile(self, *args: object, **_kwargs: object) -> SpeciesProfileData:
        output_path = args[-2]
        assert isinstance(output_path, Path)
        profile = profile_for(CARDINAL)
        output_path.write_text(json.dumps(profile))
        return profile

    def generate_plate(self, *args: object, **_kwargs: object) -> Path:
        output_path = args[-3]
        assert isinstance(output_path, Path)
        write_png(output_path)
        return output_path

    def review_plate(self, *_args: object, **_kwargs: object) -> QualityReview:
        return SOURCED_REVIEW


def prepare(_source: Path, portrait: Path, display: Path) -> None:
    write_png(portrait, color=(200, 40, 40))
    write_png(display, color=(40, 40, 200))


def test_interrupted_pending_creation_leaves_nothing_in_pending(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    original_copytree = shutil.copytree

    def interrupted_copy(source: Path, destination: Path) -> object:
        original_copytree(source, destination)
        (Path(destination) / "display.png").unlink()
        raise OSError("disk full")

    with (
        patch("inky_bird_frame.controller.load_or_fetch_references", return_value=[]),
        patch("inky_bird_frame.controller.fetch_taxon_context"),
        patch("inky_bird_frame.controller.CodexRunner", PassingRunner),
        patch("inky_bird_frame.controller.prepare_generated_plate", side_effect=prepare),
        patch("inky_bird_frame.controller.shutil.copytree", side_effect=interrupted_copy),
        pytest.raises(OSError, match="disk full"),
    ):
        generate_candidate(config, CARDINAL, config.controller.workspace_dir)

    pending_root = config.controller.state_dir / "pending"
    assert not pending_root.exists() or list(pending_root.iterdir()) == []


def test_completed_pending_creation_is_complete_and_approvable(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)

    with (
        patch("inky_bird_frame.controller.load_or_fetch_references", return_value=[]),
        patch("inky_bird_frame.controller.fetch_taxon_context"),
        patch("inky_bird_frame.controller.CodexRunner", PassingRunner),
        patch("inky_bird_frame.controller.prepare_generated_plate", side_effect=prepare),
    ):
        candidate = generate_candidate(config, CARDINAL, config.controller.workspace_dir)

    assert candidate.parent == config.controller.state_dir / "pending"
    assert sorted(path.name for path in candidate.iterdir()) == [
        "display.png",
        "manifest.json",
        "portrait.png",
        "profile.json",
        "quality-review.json",
    ]
    assert sorted(path.name for path in candidate.parent.iterdir()) == [candidate.name]
    approve_candidate(config.controller.state_dir, config.controller.catalog_dir, 9083)
    assert has_valid_approved_candidate(config.controller.catalog_dir, CARDINAL.taxon_id)


@pytest.mark.parametrize("refresh_research", [False, True])
def test_retry_recovers_an_invalid_approval_that_is_the_only_state(
    tmp_path: Path, refresh_research: bool
) -> None:
    config = load_test_config(tmp_path)
    approved = approve(config, WREN)
    corrupt(approved)

    result = retry_species(config, WREN.taxon_id, refresh_research=refresh_research)

    assert result["status"] == "eligible"
    assert not approved.exists()
    archived = result["archived"]
    assert isinstance(archived, list)
    assert [Path(str(path)).name for path in archived] == ["invalid-approved-7513-carolina-wren"]
    queued = read_generation_queue(config)
    assert [(item.taxon_id, item.common_name, item.scientific_name) for item in queued] == [
        (WREN.taxon_id, WREN.common_name, WREN.scientific_name)
    ]
    assert read_catalog_entries(config.controller.catalog_dir) == []


def test_retry_clears_each_of_two_invalid_approvals(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    for approved in [approve(config, species) for species in (WREN, CARDINAL)]:
        corrupt(approved)

    first = retry_species(config, WREN.taxon_id)
    second = retry_species(config, CARDINAL.taxon_id)

    assert first["status"] == second["status"] == "eligible"
    assert list((config.controller.catalog_dir / "species").iterdir()) == []
    assert read_catalog_entries(config.controller.catalog_dir) == []
    assert sorted(item.taxon_id for item in read_generation_queue(config)) == [
        WREN.taxon_id,
        CARDINAL.taxon_id,
    ]


def test_replace_approved_recovers_an_invalid_approval(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    approved = approve(config, WREN)
    other = approve(config, CARDINAL)
    corrupt(approved)
    corrupt(other)

    result = retry_species(
        config, WREN.taxon_id, replace_approved=True, reason="The plate files are damaged."
    )

    assert result["status"] == "eligible"
    assert not approved.exists()
    assert other.is_dir()
    archived = result["archived"]
    assert isinstance(archived, list)
    assert any(Path(str(path)).name == "invalid-approved-7513-carolina-wren" for path in archived)
    queued = read_generation_queue(config)
    assert [(item.taxon_id, item.common_name) for item in queued] == [
        (WREN.taxon_id, WREN.common_name)
    ]
    guidance = RetryStore(config.controller.state_dir / "generation-retries.json").quality_guidance(
        WREN.taxon_id
    )
    assert guidance is not None
    assert guidance.findings == ("The plate files are damaged.",)


def test_valid_replacement_does_not_depend_on_other_entries(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    approve(config, WREN)
    corrupt(approve(config, CARDINAL))

    result = retry_approved_candidate(config, WREN.taxon_id, "Wrong bill shape.")

    assert result["status"] == "eligible"
    assert not (config.controller.catalog_dir / "species/7513-carolina-wren").exists()
    index = json.loads((config.controller.catalog_dir / "index.json").read_text())
    assert WREN.taxon_id not in [item["taxon_id"] for item in index["species"]]


@pytest.mark.parametrize("extra", ["notes.txt", "nested"])
def test_pending_with_an_unexpected_entry_is_refused(tmp_path: Path, extra: str) -> None:
    config = load_test_config(tmp_path)
    pending = write_pending(config, WREN)
    if extra == "nested":
        (pending / extra).mkdir()
        (pending / extra / "file.txt").write_text("x")
    else:
        (pending / extra).write_text("x")
    before = tree(pending)

    with pytest.raises(CandidateApprovalError):
        approve_candidate(config.controller.state_dir, config.controller.catalog_dir, WREN.taxon_id)

    assert tree(pending) == before
    assert not (config.controller.catalog_dir / "species").exists() or not list(
        (config.controller.catalog_dir / "species").iterdir()
    )


def test_pending_with_a_symlinked_profile_is_refused(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    pending = write_pending(config, WREN)
    outside = tmp_path / "outside-profile.json"
    outside.write_text(json.dumps({"private": "value"}))
    (pending / "profile.json").unlink(missing_ok=True)
    (pending / "profile.json").symlink_to(outside)

    with pytest.raises(CandidateApprovalError):
        approve_candidate(config.controller.state_dir, config.controller.catalog_dir, WREN.taxon_id)

    assert (pending / "profile.json").is_symlink()
    species_root = config.controller.catalog_dir / "species"
    assert not species_root.exists() or not list(species_root.iterdir())


def test_symlinked_pending_directory_is_refused(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    pending = write_pending(config, WREN)
    real = tmp_path / "elsewhere"
    pending.rename(real)
    pending.symlink_to(real, target_is_directory=True)

    with pytest.raises(CandidateApprovalError):
        approve_candidate(config.controller.state_dir, config.controller.catalog_dir, WREN.taxon_id)

    assert pending.is_symlink()
    assert (real / "manifest.json").is_file()
    species_root = config.controller.catalog_dir / "species"
    assert not species_root.exists() or not list(species_root.iterdir())


def test_pending_is_kept_until_the_approved_entry_validates(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    pending = write_pending(config, WREN)
    before = tree(pending)
    destination = config.controller.catalog_dir / "species" / pending.name
    real_manifest_entry = catalog_module._manifest_entry

    def fail_for_destination(manifest_path: Path, catalog_dir: Path) -> object:
        if manifest_path == destination / "manifest.json":
            raise CatalogError("destination failed validation")
        return real_manifest_entry(manifest_path, catalog_dir)

    with (
        patch.object(catalog_module, "_manifest_entry", side_effect=fail_for_destination),
        pytest.raises(CatalogError, match="destination failed validation"),
    ):
        approve_candidate(config.controller.state_dir, config.controller.catalog_dir, WREN.taxon_id)

    assert tree(pending) == before
    assert not destination.exists()


def test_retry_archives_an_invalid_approval_whose_manifest_names_another_bird(
    tmp_path: Path,
) -> None:
    config = load_test_config(tmp_path)
    approved = approve(config, WREN)
    manifest_path = approved / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["taxon_id"] = CARDINAL.taxon_id
    manifest_path.write_text(json.dumps(manifest))
    _write_generation_queue(config, [WREN])

    result = retry_species(config, WREN.taxon_id)

    assert result["status"] == "eligible"
    assert not approved.exists()
    archived = result["archived"]
    assert isinstance(archived, list)
    assert [Path(str(path)).name for path in archived] == ["invalid-approved-7513-carolina-wren"]
    queued = read_generation_queue(config)
    assert [(item.taxon_id, item.common_name) for item in queued] == [
        (WREN.taxon_id, WREN.common_name)
    ]
