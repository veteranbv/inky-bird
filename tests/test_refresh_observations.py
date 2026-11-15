"""refresh_observations failure alerts and what scheduled output may show."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.config import AppConfig, load_config
from inky_bird_frame.controller import (
    DiscoveryResult,
    ProviderStatus,
    exclusive_refresh_lock,
    refresh_observations,
    run_generation_cycle,
)
from inky_bird_frame.errors import CatalogError, DataSourceError
from inky_bird_frame.geo import DiscoveryLocation

CONFIG = """
[discovery]
zip_code = "12345"
radius_km = 8
species_limit = 12
window = "last-week"

[controller]
workspace_dir = "."
catalog_dir = "catalog"
state_dir = "state"
codex_path = "/usr/bin/false"
bind_host = "127.0.0.1"
port = 8793
references_per_species = 4
generations_per_cycle = 1
max_generation_attempts = 3

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display"
"""

PLACE = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
BIRD = BirdSpecies(1, "First Bird", "Avis prima", 1, "iNaturalist")


def load(temporary: str) -> AppConfig:
    path = Path(temporary) / "config.toml"
    path.write_text(CONFIG)
    return load_config(path)


def found(species: list[BirdSpecies]) -> DiscoveryResult:
    return DiscoveryResult(
        location=PLACE,
        species=species,
        providers=[ProviderStatus("inaturalist", "ok", len(species))],
        unresolved=[],
    )


class RefreshFailureTests(unittest.TestCase):
    def test_every_failure_that_is_not_a_busy_refresh_is_a_degradation(self) -> None:
        failures: list[tuple[Exception, str]] = [
            (DataSourceError("providers failed"), "data source error"),
            (CatalogError("state unreadable"), "catalog error"),
            (OSError("disk full"), "storage error"),
            (ValueError("unexpected value"), "internal error"),
            (KeyError("missing"), "internal error"),
        ]
        for failure, label in failures:
            with self.subTest(type(failure).__name__), TemporaryDirectory() as temporary:
                config = load(temporary)
                with (
                    patch("inky_bird_frame.controller.discover_species", side_effect=failure),
                    patch("inky_bird_frame.controller.safe_record_degradation") as degraded,
                    self.assertRaises(type(failure)),
                ):
                    refresh_observations(config)
                self.assertEqual(
                    [call.kwargs["key"] for call in degraded.call_args_list],
                    ["observation-refresh"],
                )
                body = degraded.call_args.kwargs["body"]
                self.assertIn(f"({label})", body)
                self.assertNotIn(type(failure).__name__, body)

    def test_refresh_already_running_is_not_a_degradation(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with (
                exclusive_refresh_lock(config.controller.state_dir),
                patch("inky_bird_frame.controller.safe_record_degradation") as degraded,
                patch("inky_bird_frame.controller.safe_record_recovery") as recovered,
                self.assertRaisesRegex(DataSourceError, "already running"),
            ):
                refresh_observations(config)

        degraded.assert_not_called()
        recovered.assert_not_called()


class ScheduledOutputPrivacyTests(unittest.TestCase):
    def test_refresh_and_generation_output_leave_the_place_in_private_state(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with (
                patch("inky_bird_frame.controller.discover_species", return_value=found([BIRD])),
                patch("inky_bird_frame.controller.rebuild_catalog_index", return_value=[]),
            ):
                refreshed = refresh_observations(config)
            with (
                patch("inky_bird_frame.controller.generate_candidate") as generate,
                patch("inky_bird_frame.controller.rebuild_catalog_index", return_value=[]),
            ):
                generate.side_effect = DataSourceError("temporary")
                generated = run_generation_cycle(config)
            snapshot = json.loads((config.controller.state_dir / "discovery.json").read_text())

        printed = json.dumps([refreshed, generated])
        self.assertNotIn("Exampleville", printed)
        self.assertNotIn('"XY"', printed)
        # The keys stay so 1.x readers of the output keep working.
        self.assertEqual((refreshed["place_name"], refreshed["state"]), ("", ""))
        discovery = generated["discovery"]
        assert isinstance(discovery, dict)
        self.assertEqual((discovery["place_name"], discovery["state"]), ("", ""))
        # The private snapshot still holds the place for publication privacy checks.
        self.assertEqual((snapshot["place_name"], snapshot["state"]), ("Exampleville", "XY"))


if __name__ == "__main__":
    unittest.main()
