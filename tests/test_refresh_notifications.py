from __future__ import annotations

import json
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from inky_bird_frame.birds import BirdSpecies, EbirdSpecies
from inky_bird_frame.config import AppConfig, load_config
from inky_bird_frame.controller import DiscoveryResult, ProviderStatus, refresh_observations
from inky_bird_frame.errors import DataSourceError

CONFIG = """
[discovery]
sources = ["inaturalist", "ebird"]
zip_code = "12345"
ebird_api_key = "example-key"
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

ROUTED = """
[notifications]
enabled = true

[[notifications.destinations]]
name = "owner"
url = "json://notify.example.test/hook"
events = ["discovery"]
"""

FIRST = BirdSpecies(1, "First Bird", "Avis prima", 1, "iNaturalist")
SECOND = BirdSpecies(2, "Second Bird", "Avis secunda", 1, "eBird")


def discovery(
    species: list[BirdSpecies],
    *,
    ebird: str = "ok",
    unresolved: list[EbirdSpecies] | None = None,
) -> DiscoveryResult:
    return DiscoveryResult(
        location=None,
        species=species,
        providers=[
            ProviderStatus("inaturalist", "ok", len(species)),
            ProviderStatus("ebird", ebird, 0, error="timed out" if ebird == "error" else None),
        ],
        unresolved=list(unresolved or []),
    )


class Calls:
    def __init__(self, notify: MagicMock, degraded: MagicMock, recovered: MagicMock) -> None:
        self.notify = notify
        self.degraded = degraded
        self.recovered = recovered

    def keys(self, mock: MagicMock) -> list[str]:
        return [call.kwargs["key"] for call in mock.call_args_list]

    def reset(self) -> None:
        for mock in (self.notify, self.degraded, self.recovered):
            mock.reset_mock()


@contextmanager
def recorded(results: list[DiscoveryResult]) -> Iterator[Calls]:
    with (
        patch("inky_bird_frame.controller.discover_species", side_effect=results),
        patch("inky_bird_frame.controller.rebuild_catalog_index", return_value=[]),
        patch(
            "inky_bird_frame.controller.safe_notify",
            return_value={"queued": True, "attempted": 0, "delivered": 0, "failed": 0},
        ) as notify,
        patch("inky_bird_frame.controller.safe_record_degradation") as degraded,
        patch("inky_bird_frame.controller.safe_record_recovery") as recovered,
    ):
        yield Calls(notify, degraded, recovered)


def load(temporary: str, notifications: str = ROUTED) -> AppConfig:
    path = Path(temporary) / "config.toml"
    path.write_text(CONFIG + notifications)
    return load_config(path)


class RefreshNotificationTests(unittest.TestCase):
    def test_provider_degradation_and_recovery_follow_real_refresh_results(self) -> None:
        unmatched = EbirdSpecies("unmatched", "Unmatched Bird", "Avis ignota", None)
        results = [
            discovery([FIRST, SECOND]),
            discovery([FIRST], ebird="error"),
            discovery([FIRST, SECOND], unresolved=[unmatched]),
        ]
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with recorded(results) as calls:
                refresh_observations(config)
                self.assertIn("observation-refresh", calls.keys(calls.recovered))
                self.assertIn("observation-provider-ebird", calls.keys(calls.recovered))
                self.assertIn("ebird-taxonomy", calls.keys(calls.recovered))
                calls.reset()

                refresh_observations(config)
                self.assertEqual(calls.keys(calls.degraded), ["observation-provider-ebird"])
                self.assertNotIn("ebird-taxonomy", calls.keys(calls.recovered))
                self.assertIn("observation-provider-inaturalist", calls.keys(calls.recovered))
                calls.reset()

                refresh_observations(config)
                self.assertIn("observation-provider-ebird", calls.keys(calls.recovered))
                self.assertEqual(calls.keys(calls.degraded), ["ebird-taxonomy"])
                self.assertIn("1 species", calls.degraded.call_args.kwargs["body"])

    def test_failed_refresh_records_a_degradation_and_raises(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            failure = DataSourceError("All configured observation providers failed")
            with recorded([]) as calls:
                calls_discover = patch(
                    "inky_bird_frame.controller.discover_species", side_effect=failure
                )
                with calls_discover, self.assertRaises(DataSourceError):
                    refresh_observations(config)

        self.assertEqual(calls.keys(calls.degraded), ["observation-refresh"])
        body = calls.degraded.call_args.kwargs["body"]
        self.assertIn("data source error", body)
        self.assertNotIn("DataSourceError", body)
        calls.recovered.assert_not_called()

    def test_first_refresh_announces_discovered_species_in_this_year(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with recorded([discovery([FIRST, SECOND])]) as calls:
                refresh_observations(config)

        calls.notify.assert_called_once()
        kwargs = calls.notify.call_args.kwargs
        self.assertEqual(kwargs["title"], "2 new bird species discovered")
        self.assertEqual(kwargs["body"], "First Bird, Second Bird")
        self.assertEqual(kwargs["dedupe_key"], f"{datetime.now(UTC).year}:1:2")

    def test_returning_species_is_announced_once_per_calendar_year(self) -> None:
        results = [
            discovery([FIRST, SECOND]),
            discovery([FIRST]),
            discovery([FIRST, SECOND]),
            discovery([FIRST]),
            discovery([FIRST, SECOND]),
        ]
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            notices = config.controller.state_dir / "new-species-notices.json"
            with recorded(results) as calls:
                refresh_observations(config)
                self.assertEqual(calls.notify.call_count, 1)
                refresh_observations(config)
                refresh_observations(config)
                self.assertEqual(calls.notify.call_count, 1)

                saved = json.loads(notices.read_text())
                saved["year"] = datetime.now(UTC).year - 1
                notices.write_text(json.dumps(saved))
                refresh_observations(config)
                refresh_observations(config)
            mode = notices.stat().st_mode & 0o777

        self.assertEqual(calls.notify.call_count, 2)
        kwargs = calls.notify.call_args.kwargs
        self.assertEqual(kwargs["title"], "1 new bird species discovered")
        self.assertEqual(kwargs["body"], "Second Bird")
        self.assertEqual(kwargs["dedupe_key"], f"{datetime.now(UTC).year}:2")
        self.assertEqual(mode, 0o600)

    def test_failed_notice_is_not_recorded_as_announced(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            notices = config.controller.state_dir / "new-species-notices.json"
            with recorded([discovery([FIRST]), discovery([]), discovery([FIRST])]) as calls:
                calls.notify.return_value = {
                    "queued": False,
                    "attempted": 1,
                    "delivered": 0,
                    "failed": 1,
                }
                refresh_observations(config)
                recorded_after_failure = notices.exists()
                refresh_observations(config)
                refresh_observations(config)

        self.assertFalse(recorded_after_failure)
        self.assertEqual(calls.notify.call_count, 2)
        self.assertEqual(calls.notify.call_args.kwargs["body"], "First Bird")

    def test_species_are_not_recorded_as_announced_while_nothing_is_sent(self) -> None:
        unrouted = ROUTED.replace('events = ["discovery"]', 'events = ["display_stale"]')
        with TemporaryDirectory() as temporary:
            notices = Path(temporary) / "state" / "new-species-notices.json"
            for notifications in ("", unrouted):
                with (
                    patch(
                        "inky_bird_frame.controller.discover_species",
                        side_effect=[discovery([FIRST]), discovery([])],
                    ),
                    patch("inky_bird_frame.controller.rebuild_catalog_index", return_value=[]),
                    patch("inky_bird_frame.controller.safe_record_degradation"),
                    patch("inky_bird_frame.controller.safe_record_recovery"),
                ):
                    config = load(temporary, notifications)
                    refresh_observations(config)
                    self.assertFalse(notices.exists())
                    refresh_observations(config)

            # The owner turns discovery notices on later in the year and the bird returns.
            with recorded([discovery([FIRST])]) as calls:
                refresh_observations(load(temporary))

        calls.notify.assert_called_once()
        self.assertEqual(calls.notify.call_args.kwargs["body"], "First Bird")

    def test_symlinked_notice_record_is_not_trusted(self) -> None:
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            config.controller.state_dir.mkdir(parents=True, exist_ok=True)
            elsewhere = Path(temporary) / "elsewhere.json"
            elsewhere.write_text(
                json.dumps({"schema_version": 1, "year": datetime.now(UTC).year, "taxon_ids": [1]})
            )
            elsewhere.chmod(0o600)
            (config.controller.state_dir / "new-species-notices.json").symlink_to(elsewhere)
            with recorded([discovery([FIRST])]) as calls:
                refresh_observations(config)

        calls.notify.assert_called_once()
        self.assertEqual(calls.notify.call_args.kwargs["body"], "First Bird")

    def test_long_new_species_list_names_the_first_ten(self) -> None:
        many = [
            BirdSpecies(index, f"Bird {index:02d}", f"Avis numerus{index}", 1, "iNaturalist")
            for index in range(1, 51)
        ]
        with TemporaryDirectory() as temporary:
            config = load(temporary)
            with recorded([discovery(many)]) as calls:
                result = refresh_observations(config)

        kwargs = calls.notify.call_args.kwargs
        self.assertEqual(kwargs["title"], "50 new bird species discovered")
        self.assertEqual(
            kwargs["body"],
            ", ".join(f"Bird {index:02d}" for index in range(1, 11)) + ", and 40 more",
        )
        new_species = result["new_species"]
        assert isinstance(new_species, list)
        self.assertEqual(len(new_species), 50)


if __name__ == "__main__":
    unittest.main()
