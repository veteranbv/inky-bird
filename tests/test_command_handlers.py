"""Run each command handler through the real CLI entry point, parser to JSON envelope.

Only work that would leave this machine (discovery providers, the display
node's controller fetch, the e-paper panel, service installation) is replaced,
at the command module's boundary; everything else is the real code path.
"""

from __future__ import annotations

import json
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from PIL import Image

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.catalog import write_candidate_manifest
from inky_bird_frame.cli import main
from inky_bird_frame.config import AppConfig, NotificationEvent, load_config
from inky_bird_frame.controller import DiscoveryResult
from inky_bird_frame.geo import DiscoveryLocation
from inky_bird_frame.models import QualityReview, SourceLink, SpeciesProfileData
from inky_bird_frame.notifications import NotificationItem, notification_state_path

Envelope = dict[str, Any]


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Envelope]:
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def installation(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> tuple[Path, AppConfig]:
    path = tmp_path / "config.toml"
    code, envelope = run(capsys, "config", "init", "--destination", str(path))
    assert (code, envelope["ok"]) == (0, True)
    return path, load_config(path)


def approve_fixture(
    capsys: pytest.CaptureFixture[str], path: Path, config: AppConfig, taxon_id: int = 10
) -> None:
    name = f"Fixture candidate {taxon_id}"
    bird = BirdSpecies(taxon_id, name, name, 1, "fixture")
    root = config.controller.state_dir / f"pending/{taxon_id}-fixture-candidate-{taxon_id}"
    root.mkdir(parents=True)
    for filename, size in (("portrait.png", (1200, 1600)), ("display.png", (1600, 1200))):
        Image.new("RGB", size, "white").save(root / filename)
    links = [
        SourceLink(title="Fixture source one", url="https://first.example/bird"),
        SourceLink(title="Fixture source two", url="https://second.example/bird"),
    ]
    profile = SpeciesProfileData(
        taxon_id=taxon_id,
        common_name=bird.common_name,
        scientific_name=bird.scientific_name,
        family="Fixture family",
        measurements={"length": "10 cm", "wingspan": "20 cm", "weight": "30 g"},
        field_marks=["First mark", "Second mark", "Third mark", "Fourth mark"],
        habitat="Fixture habitat",
        behavior="Fixture behavior",
        palette=["white", "black", "gray"],
        sources=links,
    )
    review = QualityReview(True, 5, 5, 5, 5, True, (), tuple(links))
    write_candidate_manifest(
        root, bird, profile, [], review, generator="fixture", prompt_version="fixture"
    )
    code, envelope = run(capsys, "approve", "--config", str(path), str(taxon_id))
    assert (code, envelope["data"]["taxon_id"]) == (0, taxon_id)


def test_config_init_creates_a_private_loadable_config_and_never_replaces_one(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "config.toml"
    code, envelope = run(capsys, "config", "init", "--destination", str(path))
    assert code == 0
    assert envelope["data"] == {
        "created": True,
        "browser_registration_prepared": True,
        "ownership": "app",
        "generation_enabled": False,
        "sources": [],
        "config": str(path),
    }
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    config = load_config(path)
    assert config.controller.generation_enabled is False
    assert config.discovery.sources == ()

    original = path.read_bytes()
    code, envelope = run(capsys, "config", "init", "--destination", str(path))
    assert code == 1
    assert envelope["ok"] is False
    assert envelope["error"]["type"] == "configuration"
    assert path.read_bytes() == original


def test_collection_commands_preview_add_list_remove_and_import(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path, config = installation(capsys, tmp_path)
    approve_fixture(capsys, path, config)
    collection = config.controller.state_dir / "collection.json"
    arguments = ("--config", str(path))

    code, envelope = run(capsys, "collection", "list", *arguments)
    assert (code, envelope["data"]["collection_count"], envelope["data"]["members"]) == (0, 0, [])

    code, envelope = run(capsys, "collection", "add", *arguments, "10", "--dry-run")
    assert (code, envelope["data"]["dry_run"], envelope["data"]["added_taxon_ids"]) == (
        0,
        True,
        [10],
    )
    assert not collection.exists()

    code, envelope = run(capsys, "collection", "add", *arguments, "10")
    assert (code, envelope["data"]["dry_run"], envelope["data"]["added_taxon_ids"]) == (
        0,
        False,
        [10],
    )
    code, envelope = run(capsys, "collection", "list", *arguments)
    members = envelope["data"]["members"]
    assert [(m["taxon_id"], m["origin"], m["active"]) for m in members] == [(10, "manual", True)]

    code, envelope = run(capsys, "collection", "remove", *arguments, "10")
    assert (code, envelope["data"]["removed_taxon_ids"]) == (0, [10])
    code, envelope = run(capsys, "collection", "list", *arguments)
    assert envelope["data"]["members"] == []

    code, envelope = run(capsys, "collection", "import-approved", *arguments, "--dry-run")
    assert (code, envelope["data"]["added_taxon_ids"]) == (0, [10])
    code, envelope = run(capsys, "collection", "list", *arguments)
    assert envelope["data"]["members"] == []
    code, envelope = run(capsys, "collection", "import-approved", *arguments)
    assert (code, envelope["data"]["added_taxon_ids"]) == (0, [10])
    code, envelope = run(capsys, "collection", "list", *arguments)
    assert [m["taxon_id"] for m in envelope["data"]["members"]] == [10]


def test_catalog_validate_reports_species_additions_and_rejects_damage(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path, config = installation(capsys, tmp_path)
    approve_fixture(capsys, path, config)
    catalog = config.controller.catalog_dir

    code, envelope = run(capsys, "catalog", "validate", "--catalog", str(catalog))
    assert code == 0
    assert envelope["data"] == {"catalog": str(catalog), "valid": True, "species_count": 1}

    base = tmp_path / "base-catalog"
    shutil.copytree(catalog, base)
    approve_fixture(capsys, path, config, taxon_id=11)
    code, envelope = run(
        capsys, "catalog", "validate", "--catalog", str(catalog), "--base-catalog", str(base)
    )
    assert code == 0
    assert envelope["data"]["base_catalog"] == str(base)
    assert [entry["taxon_id"] for entry in envelope["data"]["additions"]] == [11]

    portrait = catalog / "species/10-fixture-candidate-10/portrait.png"
    Image.new("RGB", (1200, 1600), "black").save(portrait)
    code, envelope = run(capsys, "catalog", "validate", "--catalog", str(catalog))
    assert code == 1
    assert (envelope["ok"], envelope["error"]["type"]) == (False, "publication")


def test_discover_reports_the_area_without_coordinates_and_keeps_cache_read_only(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path, _ = installation(capsys, tmp_path)
    location = DiscoveryLocation(
        "00000", "Exampleville", "EX", 1.0, 2.0, "US", "zippopotam", "Example geocoder"
    )
    species = BirdSpecies(10, "Fixture bird", "Avis fixture", 3, "inaturalist")
    calls: list[dict[str, object]] = []

    def discovered(config: AppConfig, **options: object) -> DiscoveryResult:
        calls.append(options)
        return DiscoveryResult(location=location, species=[species], providers=[], unresolved=[])

    with patch("inky_bird_frame.cli.discover_species", discovered):
        code, envelope = run(capsys, "discover", "--config", str(path))
    assert code == 0
    assert calls == [{"persist_taxonomy_cache": False}]
    data = envelope["data"]
    assert data["location"] == {
        "zip_code": "00000",
        "postal_code": "00000",
        "country_code": "US",
        "place_name": "Exampleville",
        "state": "EX",
        "geocoder": "zippopotam",
        "geocoder_attribution": "Example geocoder",
    }
    assert [item["taxon_id"] for item in data["species"]] == [10]
    assert (data["unresolved_count"], data["sources"], data["radius_km"]) == (0, [], 8)

    other = DiscoveryLocation("00000", "Exampleville", "EX", 1.0, 2.0, "US", "geoapify")
    with patch(
        "inky_bird_frame.cli.discover_species",
        lambda config, **_: DiscoveryResult(other, [], [], []),
    ):
        code, envelope = run(capsys, "discover", "--config", str(path))
    assert (code, envelope["data"]["location"]["zip_code"]) == (0, None)


def test_controller_cycle_runs_with_no_sources_and_generation_off(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path, _ = installation(capsys, tmp_path)
    code, envelope = run(capsys, "controller-cycle", "--config", str(path))
    assert code == 0
    data = envelope["data"]
    assert (data["generation_enabled"], data["generated"], data["published"]) == (False, [], [])
    assert (data["refresh"]["species_count"], data["refresh"]["new_species"]) == (0, [])


def test_display_cycle_passes_the_display_settings_and_force(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path, config = installation(capsys, tmp_path)
    calls: list[tuple[object, bool]] = []

    def cycle(display_node: object, *, force: bool) -> dict[str, object]:
        calls.append((display_node, force))
        return {"displayed": force}

    with patch("inky_bird_frame.cli.run_display_cycle", cycle):
        assert run(capsys, "display-cycle", "--config", str(path)) == (
            0,
            {"ok": True, "data": {"displayed": False}, "schema_version": 1},
        )
        code, envelope = run(capsys, "display-cycle", "--config", str(path), "--force")
    assert (code, envelope["data"]) == (0, {"displayed": True})
    assert calls == [(config.display_node, False), (config.display_node, True)]


def test_notification_status_and_retry_requeue_dead_letters(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path, config = installation(capsys, tmp_path)
    arguments = ("--config", str(path))
    code, envelope = run(capsys, "notifications", "status", *arguments)
    assert code == 0
    assert (envelope["data"]["pending"], envelope["data"]["dead_letters"]) == (0, 0)

    created = datetime(2001, 1, 1, tzinfo=UTC)
    dead = NotificationItem(
        item_id="f" * 64,
        event=NotificationEvent.DEGRADED,
        title="Fixture title",
        body="Fixture body",
        created_at=created,
        attempts=20,
        next_attempt_at=created,
        delivered_to=(),
        last_error="fixture failure",
    )
    state = notification_state_path(config)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pending": [],
                "dead_letters": [dead.as_dict()],
                "delivered_ids": [],
            }
        )
    )
    code, envelope = run(capsys, "notifications", "status", *arguments)
    assert (envelope["data"]["pending"], envelope["data"]["dead_letters"]) == (0, 1)

    code, envelope = run(capsys, "notifications", "retry", *arguments)
    assert code == 0
    assert envelope["data"]["requeued"] == 1
    assert envelope["data"]["delivery"]["dead_letters"] == 0
    code, envelope = run(capsys, "notifications", "status", *arguments)
    assert (envelope["data"]["pending"], envelope["data"]["dead_letters"]) == (1, 0)
    retried = json.loads(state.read_text())["pending"]
    assert [(item["id"], item["attempts"]) for item in retried] == [("f" * 64, 0)]


def test_birdbuddy_status_reads_a_signed_out_installation(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path, _ = installation(capsys, tmp_path)
    code, envelope = run(capsys, "birdbuddy", "status", "--config", str(path))
    assert code == 0
    assert envelope["data"] == {
        "authenticated": False,
        "authorization_confirmed": False,
        "history_present": False,
    }


def test_prepare_and_display_image_send_only_when_asked(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    source = tmp_path / "upload.png"
    Image.new("RGB", (900, 1200), "white").save(source)
    output = tmp_path / "prepared"
    shown: list[Path] = []

    def panel(image: Path) -> tuple[int, int]:
        shown.append(image)
        return (1600, 1200)

    with patch("inky_bird_frame.cli.show_on_inky", panel):
        code, envelope = run(capsys, "prepare-image", str(source), "--output-dir", str(output))
        assert code == 0
        data = envelope["data"]
        assert data["display_update"] == "not-requested"
        assert Image.open(data["portrait"]).size == (1200, 1600)
        assert Image.open(data["display"]).size == (1600, 1200)
        assert shown == []

        code, envelope = run(
            capsys, "prepare-image", str(source), "--output-dir", str(output), "--display"
        )
        assert (code, envelope["data"]["display_update"]) == (0, "sent")
        assert shown == [Path(envelope["data"]["display"])]

        code, envelope = run(capsys, "display-image", str(shown[0]))
    assert code == 0
    assert envelope["data"] == {
        "display_update": "sent",
        "image": str(shown[0]),
        "display_size": [1600, 1200],
    }
    assert shown == [shown[0], shown[0]]


@pytest.mark.parametrize(
    ("role", "extra", "expected"),
    [
        (
            "controller",
            ["--uv-bin", "/opt/example/uv", "--python-version", "3.12"],
            {"uv_bin": Path("/opt/example/uv"), "python_version": "3.12", "venv": None},
        ),
        (
            "display",
            ["--venv", "/opt/example/venv"],
            {"uv_bin": None, "python_version": None, "venv": Path("/opt/example/venv")},
        ),
    ],
)
def test_setup_passes_preview_or_apply_and_role_options(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
    extra: list[str],
    expected: dict[str, object],
) -> None:
    path, _ = installation(capsys, tmp_path)
    calls: list[tuple[object, Path, dict[str, object]]] = []

    def setup(role_value: object, config: Path, **options: object) -> dict[str, object]:
        calls.append((role_value, config, options))
        return {"applied": options["apply"]}

    with patch("inky_bird_frame.cli.setup", setup):
        code, envelope = run(capsys, "setup", role, "--config", str(path), *extra)
        assert (code, envelope["data"]) == (0, {"applied": False})
        code, envelope = run(capsys, "setup", role, "--config", str(path), "--yes", *extra)
        assert (code, envelope["data"]) == (0, {"applied": True})
    assert [str(getattr(call[0], "value", call[0])) for call in calls] == [role, role]
    assert [call[1] for call in calls] == [path, path]
    for call, apply in zip(calls, (False, True), strict=True):
        options = call[2]
        assert options["apply"] is apply
        assert {key: options[key] for key in expected} == expected
        assert options["source_dir"] is None and options["app_dir"] is None
