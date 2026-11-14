from __future__ import annotations

import inspect
import json
import os
import re
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame import birds
from inky_bird_frame.birdbuddy import BirdBuddySyncResult, BirdBuddySyncStats
from inky_bird_frame.birds import BirdBuddySpecies, BirdSpecies, ObservationWindow
from inky_bird_frame.catalog import CatalogEntry
from inky_bird_frame.config import AppConfig, DiscoveryProvider, load_config
from inky_bird_frame.controller import (
    PROVIDER_SOURCE_LABELS,
    discover_species,
    enqueue_seed_species,
    refresh_observations,
    run_refresh_cycle,
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

LOCATION = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
FIELD_BIRD = BirdSpecies(101, "Field Bird", "Avis campi", 3, "iNaturalist")
YARD_BIRD = BirdSpecies(202, "Yard Bird", "Avis horti", 2, "BirdNET-Go")
BIRDNET_GO_SOURCES = (
    '[discovery]\nsources = ["inaturalist", "birdnet-go"]\n'
    'birdnet_go_url = "http://birdnet-go.example.test:8080"\n'
)


def load(temporary: str, replacement: str | None = None, *, load_secrets: bool = True) -> AppConfig:
    path = Path(temporary) / "config.toml"
    path.write_text(CONFIG if replacement is None else CONFIG.replace("[discovery]\n", replacement))
    return load_config(path, load_secrets=load_secrets)


def approved(species: BirdSpecies) -> CatalogEntry:
    slug = species.common_name.casefold().replace(" ", "-")
    return CatalogEntry(
        species.taxon_id,
        species.common_name,
        species.scientific_name,
        slug,
        f"species/{species.taxon_id}/portrait.png",
        "a" * 64,
        f"species/{species.taxon_id}/display.png",
        "b" * 64,
        "2026-01-01T00:00:00+00:00",
    )


def birdbuddy_stats(*, complete: bool, skipped: int) -> BirdBuddySyncStats:
    return BirdBuddySyncStats(
        1,
        1,
        1,
        0,
        0,
        0,
        "2026-01-01T00:00:00+00:00",
        None,
        "2026-01-01T00:00:00+00:00",
        confirmed_records_skipped=skipped,
        confirmed_history_complete=complete,
    )


class ProviderIsolationTests(unittest.TestCase):
    def test_unexpected_provider_exception_is_isolated_with_a_safe_message(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, '[discovery]\nsources = ["inaturalist", "birdbuddy"]\n')
            with (
                patch(
                    "inky_bird_frame.controller.resolve_discovery_location",
                    return_value=LOCATION,
                ),
                patch(
                    "inky_bird_frame.controller.fetch_inaturalist_birds",
                    return_value=[FIELD_BIRD],
                ),
                patch(
                    "inky_bird_frame.controller.sync_birdbuddy_detections",
                    side_effect=KeyError("/private/state/birdbuddy.json"),
                ),
                self.assertLogs("inky_bird_frame.controller", level="WARNING") as logs,
            ):
                result = discover_species(config)

        # The service log keeps the traceback that the stored status leaves out.
        exc_info = logs.records[0].exc_info
        assert exc_info is not None
        self.assertIs(exc_info[0], KeyError)

        self.assertEqual(result.species, [FIELD_BIRD])
        self.assertEqual(
            [(provider.name, provider.status) for provider in result.providers],
            [("inaturalist", "ok"), ("birdbuddy", "error")],
        )
        error = result.providers[1].error
        assert error is not None
        self.assertIn("KeyError", error)
        self.assertNotIn("private", error)

    def test_unexpected_location_exception_marks_only_location_sources(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, BIRDNET_GO_SOURCES)
            with (
                patch(
                    "inky_bird_frame.controller.resolve_discovery_location",
                    side_effect=RuntimeError("1.0, 2.0"),
                ),
                patch(
                    "inky_bird_frame.controller.fetch_birdnet_go_species",
                    return_value=[object()],
                ),
                patch(
                    "inky_bird_frame.controller.resolve_birdnet_go_species",
                    return_value=([YARD_BIRD], []),
                ),
                self.assertLogs("inky_bird_frame.controller", level="WARNING") as logs,
            ):
                result = discover_species(config)

        # The service log keeps the traceback that the stored status leaves out.
        exc_info = logs.records[0].exc_info
        assert exc_info is not None
        self.assertIs(exc_info[0], RuntimeError)

        self.assertEqual(result.species, [YARD_BIRD])
        self.assertEqual(
            [(provider.name, provider.status) for provider in result.providers],
            [("inaturalist", "error"), ("birdnet-go", "ok")],
        )
        error = result.providers[0].error
        assert error is not None
        self.assertIn("Location lookup failed", error)
        self.assertIn("RuntimeError", error)
        self.assertNotIn("1.0", error)

    def test_each_provider_isolates_its_own_unexpected_exception(self) -> None:
        # Each provider's entry point, and the settings it needs beside BirdNET-Go
        # (or iNaturalist, when BirdNET-Go is the one failing).
        cases = {
            "inaturalist": ("fetch_inaturalist_birds", ""),
            "ebird": ("fetch_ebird_observations", 'ebird_api_key_env = "TEST_EBIRD_KEY"\n'),
            "ebird-archive": ("read_ebird_archive_history", ""),
            "birdweather": (
                "fetch_birdweather_species",
                'birdweather_token_env = "TEST_BIRDWEATHER_TOKEN"\n',
            ),
            "birdbuddy": ("sync_birdbuddy_detections", ""),
            "birdnet-go": ("fetch_birdnet_go_species", ""),
            "birdnet-analyzer": ("read_birdnet_analyzer_history", ""),
        }
        self.assertEqual(set(cases), {item.value for item in DiscoveryProvider})
        secrets = {"TEST_EBIRD_KEY": "example-key", "TEST_BIRDWEATHER_TOKEN": "example-token"}
        for failing, (entry_point, settings) in cases.items():
            survivor = "inaturalist" if failing == "birdnet-go" else "birdnet-go"
            replacement = (
                f'[discovery]\nsources = ["{failing}", "{survivor}"]\n'
                'birdnet_go_url = "http://birdnet-go.example.test:8080"\n' + settings
            )
            survivor_bird = FIELD_BIRD if survivor == "inaturalist" else YARD_BIRD
            with (
                self.subTest(provider=failing),
                TemporaryDirectory() as temporary,
                patch.dict(os.environ, secrets),
            ):
                config = load(temporary, replacement, load_secrets=False)
                with (
                    patch(
                        "inky_bird_frame.controller.resolve_discovery_location",
                        return_value=LOCATION,
                    ),
                    patch(
                        "inky_bird_frame.controller.fetch_inaturalist_birds",
                        return_value=[FIELD_BIRD],
                    ),
                    patch(
                        "inky_bird_frame.controller.fetch_birdnet_go_species",
                        return_value=[object()],
                    ),
                    patch(
                        "inky_bird_frame.controller.resolve_birdnet_go_species",
                        return_value=([YARD_BIRD], []),
                    ),
                    patch(
                        f"inky_bird_frame.controller.{entry_point}",
                        side_effect=KeyError("/private/state/source.json"),
                    ),
                    self.assertLogs("inky_bird_frame.controller", level="WARNING"),
                ):
                    result = discover_species(config)

                self.assertEqual(result.species, [survivor_bird])
                statuses = {provider.name: provider for provider in result.providers}
                self.assertEqual(statuses[survivor].status, "ok")
                self.assertEqual(statuses[failing].status, "error")
                self.assertEqual(
                    statuses[failing].error, "The source failed unexpectedly (KeyError)"
                )

    def test_project_error_from_a_provider_keeps_its_safe_message(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, BIRDNET_GO_SOURCES)
            with (
                patch(
                    "inky_bird_frame.controller.resolve_discovery_location",
                    return_value=LOCATION,
                ),
                patch(
                    "inky_bird_frame.controller.fetch_inaturalist_birds",
                    side_effect=CatalogError("Activity state is invalid"),
                ),
                patch(
                    "inky_bird_frame.controller.fetch_birdnet_go_species",
                    return_value=[object()],
                ),
                patch(
                    "inky_bird_frame.controller.resolve_birdnet_go_species",
                    return_value=([YARD_BIRD], []),
                ),
                self.assertLogs("inky_bird_frame.controller", level="WARNING"),
            ):
                result = discover_species(config)

        self.assertEqual(result.species, [YARD_BIRD])
        self.assertEqual(result.providers[0].error, "Activity state is invalid")

    def test_project_error_from_the_location_lookup_keeps_its_safe_message(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, BIRDNET_GO_SOURCES)
            with (
                patch(
                    "inky_bird_frame.controller.resolve_discovery_location",
                    side_effect=CatalogError("Activity state is invalid"),
                ),
                patch(
                    "inky_bird_frame.controller.fetch_birdnet_go_species",
                    return_value=[object()],
                ),
                patch(
                    "inky_bird_frame.controller.resolve_birdnet_go_species",
                    return_value=([YARD_BIRD], []),
                ),
                self.assertLogs("inky_bird_frame.controller", level="WARNING"),
            ):
                result = discover_species(config)

        self.assertEqual(
            result.providers[0].error, "Location lookup failed: Activity state is invalid"
        )

    def test_invalid_coordinate_override_is_still_a_usage_error(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with (
                patch("inky_bird_frame.controller.fetch_inaturalist_birds") as fetch,
                self.assertRaisesRegex(ValueError, "outside the valid range"),
            ):
                discover_species(config, latitude=200.0, longitude=0.0)

        fetch.assert_not_called()

    def test_invalid_coordinate_override_is_reported_beside_a_source_needing_no_location(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, BIRDNET_GO_SOURCES)
            with (
                patch(
                    "inky_bird_frame.controller.fetch_birdnet_go_species",
                    return_value=[object()],
                ),
                patch(
                    "inky_bird_frame.controller.resolve_birdnet_go_species",
                    return_value=([YARD_BIRD], []),
                ),
                self.assertRaisesRegex(ValueError, "outside the valid range"),
            ):
                enqueue_seed_species(
                    config,
                    window=ObservationWindow.LAST_WEEK,
                    latitude=200.0,
                    longitude=0.0,
                    dry_run=True,
                )

    def test_whitespace_only_environment_secrets_count_as_missing(self) -> None:
        replacement = (
            '[discovery]\nsources = ["ebird", "birdweather"]\n'
            'ebird_api_key_env = "TEST_BLANK_EBIRD"\n'
            'birdweather_token_env = "TEST_BLANK_BIRDWEATHER"\n'
        )
        blank = {"TEST_BLANK_EBIRD": "   ", "TEST_BLANK_BIRDWEATHER": "\t\n"}
        with TemporaryDirectory() as temporary, patch.dict(os.environ, blank):
            # Secrets are read at use time when configuration is loaded without them.
            config = load(temporary, replacement, load_secrets=False)
            with (
                patch(
                    "inky_bird_frame.controller.resolve_discovery_location",
                    return_value=LOCATION,
                ),
                patch("inky_bird_frame.controller.fetch_ebird_observations") as ebird,
                patch("inky_bird_frame.controller.fetch_birdweather_species") as birdweather,
                self.assertRaisesRegex(DataSourceError, "not configured") as raised,
            ):
                discover_species(config)

        ebird.assert_not_called()
        birdweather.assert_not_called()
        self.assertIn("eBird API key is not configured", str(raised.exception))
        self.assertIn("BirdWeather station token is not configured", str(raised.exception))

    def refresh_birdbuddy(
        self, config: AppConfig, stats: BirdBuddySyncStats
    ) -> tuple[dict[str, object], list[str], list[str], str | None]:
        resolved = BirdSpecies(303, "Feeder Bird", "Avis mensae", 1, "Bird Buddy")
        detection = BirdBuddySpecies(
            "species-feeder", "Feeder Bird", "Avis mensae", 1, "2026-01-01T00:00:00+00:00"
        )
        with (
            patch(
                "inky_bird_frame.controller.sync_birdbuddy_detections",
                return_value=BirdBuddySyncResult([detection], stats),
            ),
            patch(
                "inky_bird_frame.controller.resolve_birdbuddy_species",
                return_value=([resolved], []),
            ),
            patch("inky_bird_frame.controller.safe_record_degradation") as degraded,
            patch("inky_bird_frame.controller.safe_record_recovery") as recovered,
            patch("inky_bird_frame.controller.safe_notify"),
        ):
            result = refresh_observations(config)
        degraded_keys = [call.kwargs["key"] for call in degraded.call_args_list]
        recovered_keys = [call.kwargs["key"] for call in recovered.call_args_list]
        body = next(
            (
                call.kwargs["body"]
                for call in degraded.call_args_list
                if call.kwargs["key"] == "observation-provider-birdbuddy"
            ),
            None,
        )
        return result, degraded_keys, recovered_keys, body

    def test_skipped_birdbuddy_records_stay_ok_with_a_warning_and_notice(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, '[discovery]\nsources = ["birdbuddy"]\n')
            result, degraded_keys, recovered_keys, body = self.refresh_birdbuddy(
                config, birdbuddy_stats(complete=False, skipped=4)
            )

        providers = result["providers"]
        assert isinstance(providers, list)
        # Existing readers treat any status other than ok as a failed source.
        self.assertEqual(providers[0]["status"], "ok")
        self.assertEqual(providers[0]["species_count"], 1)
        self.assertEqual(providers[0]["details"]["confirmed_records_skipped"], 4)
        self.assertIn("4 records were skipped", providers[0]["warning"])
        self.assertEqual(result["species_count"], 1)
        self.assertIn("observation-provider-birdbuddy", degraded_keys)
        self.assertNotIn("observation-provider-birdbuddy", recovered_keys)
        assert body is not None
        self.assertIn("4 records were skipped", body)

    def test_birdbuddy_history_page_cap_is_not_reported_as_skipped_records(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, '[discovery]\nsources = ["birdbuddy"]\n')
            result, degraded_keys, recovered_keys, _ = self.refresh_birdbuddy(
                config, birdbuddy_stats(complete=False, skipped=0)
            )

        providers = result["providers"]
        assert isinstance(providers, list)
        self.assertEqual(providers[0]["status"], "ok")
        warning = providers[0]["warning"]
        self.assertNotIn("skipped", warning)
        self.assertIn("5000", warning)
        self.assertNotIn("observation-provider-birdbuddy", degraded_keys)
        self.assertIn("observation-provider-birdbuddy", recovered_keys)

    def test_complete_birdbuddy_history_stays_ok(self) -> None:
        resolved = BirdSpecies(303, "Feeder Bird", "Avis mensae", 1, "Bird Buddy")
        detection = BirdBuddySpecies(
            "species-feeder", "Feeder Bird", "Avis mensae", 1, "2026-01-01T00:00:00+00:00"
        )
        with TemporaryDirectory() as temporary:
            config = load(temporary, '[discovery]\nsources = ["birdbuddy"]\n')
            with (
                patch(
                    "inky_bird_frame.controller.sync_birdbuddy_detections",
                    return_value=BirdBuddySyncResult(
                        [detection], birdbuddy_stats(complete=True, skipped=0)
                    ),
                ),
                patch(
                    "inky_bird_frame.controller.resolve_birdbuddy_species",
                    return_value=([resolved], []),
                ),
            ):
                result = discover_species(config)

        self.assertEqual(result.providers[0].status, "ok")
        self.assertNotIn("warning", result.providers[0].as_dict())


class BirdBuddyHistoryBaselineTests(unittest.TestCase):
    def test_long_birdbuddy_history_still_lets_a_returning_species_be_announced(self) -> None:
        feeder = BirdSpecies(303, "Feeder Bird", "Avis mensae", 1, "Bird Buddy")
        migrant = BirdSpecies(404, "Migrant Bird", "Avis migrans", 1, "Bird Buddy")
        detection = BirdBuddySpecies(
            "species-feeder", "Feeder Bird", "Avis mensae", 1, "2026-01-01T00:00:00+00:00"
        )
        capped = BirdBuddySyncResult([detection], birdbuddy_stats(complete=False, skipped=0))
        with TemporaryDirectory() as temporary:
            config = load(
                temporary,
                "[notifications]\nenabled = true\n\n[[notifications.destinations]]\n"
                'name = "owner"\nurl = "json://notify.example.test/hook"\n'
                'events = ["discovery"]\n\n[discovery]\nsources = ["birdbuddy"]\n',
            )
            notices = config.controller.state_dir / "new-species-notices.json"
            with (
                patch(
                    "inky_bird_frame.controller.sync_birdbuddy_detections",
                    return_value=capped,
                ),
                patch(
                    "inky_bird_frame.controller.resolve_birdbuddy_species",
                    side_effect=[
                        ([feeder, migrant], []),
                        ([feeder], []),
                        ([feeder], []),
                        ([feeder, migrant], []),
                    ],
                ),
                patch("inky_bird_frame.controller.rebuild_catalog_index", return_value=[]),
                patch(
                    "inky_bird_frame.controller.safe_notify",
                    return_value={"queued": True, "attempted": 0, "delivered": 0, "failed": 0},
                ) as notify,
                patch("inky_bird_frame.controller.safe_record_degradation"),
                patch("inky_bird_frame.controller.safe_record_recovery"),
            ):
                refresh_observations(config)
                refresh_observations(config)
                refresh_observations(config)
                saved = json.loads(notices.read_text())
                saved["year"] = datetime.now(UTC).year - 1
                notices.write_text(json.dumps(saved))
                refresh_observations(config)

        self.assertEqual(notify.call_count, 2)
        self.assertEqual(notify.call_args.kwargs["body"], "Migrant Bird")


class CarryForwardTests(unittest.TestCase):
    def refresh(
        self,
        config: AppConfig,
        *,
        inaturalist: list[BirdSpecies] | Exception,
        birdnet_go: list[BirdSpecies],
    ) -> dict[str, object]:
        inaturalist_patch = (
            patch(
                "inky_bird_frame.controller.fetch_inaturalist_birds",
                side_effect=inaturalist,
            )
            if isinstance(inaturalist, Exception)
            else patch(
                "inky_bird_frame.controller.fetch_inaturalist_birds",
                return_value=inaturalist,
            )
        )
        with (
            patch(
                "inky_bird_frame.controller.resolve_discovery_location",
                return_value=LOCATION,
            ),
            inaturalist_patch,
            patch(
                "inky_bird_frame.controller.fetch_birdnet_go_species",
                return_value=[],
            ),
            patch(
                "inky_bird_frame.controller.resolve_birdnet_go_species",
                return_value=(birdnet_go, []),
            ),
            patch(
                "inky_bird_frame.controller.rebuild_catalog_index",
                return_value=[approved(FIELD_BIRD), approved(YARD_BIRD)],
            ),
        ):
            return run_refresh_cycle(config)

    @staticmethod
    def wall(config: AppConfig) -> list[int]:
        active = json.loads((config.controller.state_dir / "active-catalog.json").read_text())
        return [item["taxon_id"] for item in active["species"]]

    @staticmethod
    def rewrite_snapshot(config: AppConfig, change: dict[str, object]) -> None:
        path = config.controller.state_dir / "discovery.json"
        snapshot = json.loads(path.read_text())
        snapshot.update(change)
        path.write_text(json.dumps(snapshot))

    def test_timeout_plus_empty_survivor_keeps_the_failed_providers_birds(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, BIRDNET_GO_SOURCES)
            self.refresh(config, inaturalist=[FIELD_BIRD], birdnet_go=[YARD_BIRD])
            result = self.refresh(
                config,
                inaturalist=DataSourceError("iNaturalist request timed out"),
                birdnet_go=[],
            )
            wall = self.wall(config)
            snapshot = json.loads((config.controller.state_dir / "discovery.json").read_text())

        self.assertEqual(wall, [FIELD_BIRD.taxon_id])
        self.assertEqual(result["species_count"], 1)
        self.assertEqual(result["active_approved_count"], 1)
        providers = {row["name"]: row for row in snapshot["providers"]}
        self.assertEqual(providers["inaturalist"]["status"], "error")
        self.assertEqual(providers["inaturalist"]["carried_species_count"], 1)
        self.assertNotIn("carried_species_count", providers["birdnet-go"])
        self.assertEqual(
            [item["taxon_id"] for item in snapshot["species"]],
            [FIELD_BIRD.taxon_id],
        )
        self.assertEqual(snapshot["species"][0]["sources"], ["iNaturalist"])

    def test_carried_birds_drop_after_the_staleness_cap(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, BIRDNET_GO_SOURCES)
            self.refresh(config, inaturalist=[FIELD_BIRD], birdnet_go=[YARD_BIRD])
            self.refresh(config, inaturalist=DataSourceError("timed out"), birdnet_go=[YARD_BIRD])
            carried_wall = self.wall(config)
            path = config.controller.state_dir / "discovery.json"
            snapshot = json.loads(path.read_text())
            old = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0).isoformat()
            for row in snapshot["providers"]:
                if row["name"] == "inaturalist":
                    row["last_success_at"] = old
            path.write_text(json.dumps(snapshot))
            result = self.refresh(
                config, inaturalist=DataSourceError("timed out"), birdnet_go=[YARD_BIRD]
            )
            expired_wall = self.wall(config)
            final = json.loads(path.read_text())

        self.assertEqual(carried_wall, [FIELD_BIRD.taxon_id, YARD_BIRD.taxon_id])
        self.assertEqual(expired_wall, [YARD_BIRD.taxon_id])
        self.assertEqual(result["species_count"], 1)
        inaturalist = next(row for row in final["providers"] if row["name"] == "inaturalist")
        self.assertEqual(inaturalist["last_success_at"], old)
        self.assertEqual(inaturalist["carried_species_count"], 0)

    def test_future_dated_last_success_does_not_carry_birds(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, BIRDNET_GO_SOURCES)
            self.refresh(config, inaturalist=[FIELD_BIRD], birdnet_go=[YARD_BIRD])
            path = config.controller.state_dir / "discovery.json"
            snapshot = json.loads(path.read_text())
            future = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0).isoformat()
            for row in snapshot["providers"]:
                if row["name"] == "inaturalist":
                    row["last_success_at"] = future
            path.write_text(json.dumps(snapshot))
            self.refresh(config, inaturalist=DataSourceError("timed out"), birdnet_go=[YARD_BIRD])
            wall = self.wall(config)
            final = json.loads(path.read_text())

        self.assertEqual(wall, [YARD_BIRD.taxon_id])
        inaturalist = next(row for row in final["providers"] if row["name"] == "inaturalist")
        self.assertEqual(inaturalist["carried_species_count"], 0)

    def test_empty_success_never_carries_birds_forward(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, BIRDNET_GO_SOURCES)
            self.refresh(config, inaturalist=[FIELD_BIRD], birdnet_go=[YARD_BIRD])
            self.refresh(config, inaturalist=[FIELD_BIRD], birdnet_go=[])
            wall = self.wall(config)

        self.assertEqual(wall, [FIELD_BIRD.taxon_id])

    def test_snapshot_from_before_the_upgrade_still_carries_recent_birds(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, BIRDNET_GO_SOURCES)
            self.refresh(config, inaturalist=[FIELD_BIRD], birdnet_go=[YARD_BIRD])
            path = config.controller.state_dir / "discovery.json"
            snapshot = json.loads(path.read_text())
            for row in snapshot["providers"]:
                del row["last_success_at"]
            path.write_text(json.dumps(snapshot))
            self.refresh(config, inaturalist=DataSourceError("timed out"), birdnet_go=[])
            wall = self.wall(config)

        self.assertEqual(wall, [FIELD_BIRD.taxon_id])

    def test_carried_bird_keeps_only_the_failed_providers_attribution(self) -> None:
        both = BirdSpecies(
            FIELD_BIRD.taxon_id, FIELD_BIRD.common_name, FIELD_BIRD.scientific_name, 1, "BirdNET-Go"
        )
        with TemporaryDirectory() as temporary:
            config = load(temporary, BIRDNET_GO_SOURCES)
            self.refresh(config, inaturalist=[FIELD_BIRD], birdnet_go=[both])
            path = config.controller.state_dir / "discovery.json"
            before = json.loads(path.read_text())["species"]
            self.refresh(config, inaturalist=DataSourceError("timed out"), birdnet_go=[])
            after = json.loads(path.read_text())["species"]

        self.assertEqual(sorted(before[0]["sources"]), ["BirdNET-Go", "iNaturalist"])
        self.assertEqual([item["taxon_id"] for item in after], [FIELD_BIRD.taxon_id])
        self.assertEqual(after[0]["sources"], ["iNaturalist"])

    def test_changed_discovery_settings_do_not_carry_old_birds(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary, BIRDNET_GO_SOURCES)
            self.refresh(config, inaturalist=[FIELD_BIRD], birdnet_go=[YARD_BIRD])
            self.rewrite_snapshot(config, {"configuration_revision": "0" * 64})
            self.refresh(config, inaturalist=DataSourceError("timed out"), birdnet_go=[])
            wall = self.wall(config)

        self.assertEqual(wall, [])

    def test_recovered_provider_replaces_carried_birds(self) -> None:
        later = BirdSpecies(404, "Later Bird", "Avis serior", 1, "iNaturalist")
        with TemporaryDirectory() as temporary:
            config = load(temporary, BIRDNET_GO_SOURCES)
            self.refresh(config, inaturalist=[FIELD_BIRD], birdnet_go=[])
            self.refresh(config, inaturalist=DataSourceError("timed out"), birdnet_go=[])
            result = self.refresh(config, inaturalist=[later], birdnet_go=[])
            snapshot = json.loads((config.controller.state_dir / "discovery.json").read_text())

        self.assertEqual([item["taxon_id"] for item in snapshot["species"]], [later.taxon_id])
        self.assertEqual(result["species_count"], 1)
        inaturalist = next(row for row in snapshot["providers"] if row["name"] == "inaturalist")
        self.assertEqual(inaturalist["status"], "ok")
        self.assertNotIn("carried_species_count", inaturalist)


class GeocodeCacheRefreshTests(unittest.TestCase):
    def refresh(self, config: AppConfig) -> dict[str, object]:
        with patch(
            "inky_bird_frame.controller.fetch_inaturalist_birds",
            return_value=[FIELD_BIRD],
        ):
            return run_refresh_cycle(config)

    def test_second_refresh_reuses_the_saved_geocode(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with patch("inky_bird_frame.geo.lookup_us_zip", return_value=LOCATION) as lookup:
                self.refresh(config)
                result = self.refresh(config)
            cache_mode = (config.controller.state_dir / "geocode-cache.json").stat().st_mode

        self.assertEqual(lookup.call_count, 1)
        providers = result["providers"]
        assert isinstance(providers, list)
        self.assertEqual(providers[0]["status"], "ok")
        self.assertEqual(cache_mode & 0o777, 0o600)

    def test_discovery_preview_does_not_save_the_geocode(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with (
                patch("inky_bird_frame.geo.lookup_us_zip", return_value=LOCATION) as lookup,
                patch(
                    "inky_bird_frame.controller.fetch_inaturalist_birds",
                    return_value=[FIELD_BIRD],
                ),
            ):
                result = discover_species(config, persist_taxonomy_cache=False)
            cached = (config.controller.state_dir / "geocode-cache.json").exists()

        lookup.assert_called_once()
        self.assertEqual(result.species, [FIELD_BIRD])
        self.assertFalse(cached)

    def test_geocoder_outage_with_a_saved_location_keeps_the_source_healthy(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with patch("inky_bird_frame.geo.lookup_us_zip", return_value=LOCATION):
                self.refresh(config)
            cache = config.controller.state_dir / "geocode-cache.json"
            saved = json.loads(cache.read_text())
            saved["resolved_at"] = "2000-01-01T00:00:00+00:00"
            cache.write_text(json.dumps(saved))
            with patch(
                "inky_bird_frame.geo.lookup_us_zip",
                side_effect=DataSourceError("Zippopotam US ZIP API is unavailable"),
            ) as lookup:
                result = self.refresh(config)

        lookup.assert_called_once()
        providers = result["providers"]
        assert isinstance(providers, list)
        self.assertEqual(providers[0]["status"], "ok")
        self.assertEqual(result["species_count"], 1)


class ProviderSourceLabelTests(unittest.TestCase):
    def test_every_provider_maps_to_the_source_label_its_species_carry(self) -> None:
        birds_source = Path(inspect.getfile(birds)).read_text()
        labels = set(re.findall(r'\bsource(?:: str)? ?= ?"([^"]+)"', birds_source))

        self.assertEqual(set(PROVIDER_SOURCE_LABELS), {item.value for item in DiscoveryProvider})
        self.assertEqual(set(PROVIDER_SOURCE_LABELS.values()), labels)


if __name__ == "__main__":
    unittest.main()
