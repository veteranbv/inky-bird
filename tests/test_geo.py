from __future__ import annotations

import json
import stat
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.birds import ObservationWindow
from inky_bird_frame.config import DiscoveryConfig
from inky_bird_frame.errors import DataSourceError
from inky_bird_frame.geo import (
    GEOCODE_CACHE_TTL,
    DiscoveryLocation,
    lookup_geoapify_postcode,
    lookup_us_zip,
    parse_geoapify_postcode_response,
    parse_zippopotam_response,
    resolve_discovery_location,
)


def discovery_config(
    *,
    zip_code: str | None = None,
    postal_code: str | None = None,
    country_code: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    geoapify_api_key: str | None = None,
) -> DiscoveryConfig:
    return DiscoveryConfig(
        zip_code=zip_code,
        postal_code=postal_code,
        country_code=country_code,
        latitude=latitude,
        longitude=longitude,
        radius_km=8,
        species_limit=50,
        observation_window=ObservationWindow.LAST_30_DAYS,
        geoapify_api_key=geoapify_api_key,
    )


class ZipParsingTests(unittest.TestCase):
    def test_parse_zip_response(self) -> None:
        location = parse_zippopotam_response(
            "12345",
            {
                "places": [
                    {
                        "place name": "Exampleville",
                        "state abbreviation": "XY",
                        "latitude": "38.25",
                        "longitude": "-77.50",
                    }
                ]
            },
        )

        self.assertEqual(location.postal_code, "12345")
        self.assertEqual(location.place_name, "Exampleville")
        self.assertEqual(location.state, "XY")
        self.assertEqual(location.latitude, 38.25)
        self.assertEqual(location.longitude, -77.50)
        self.assertEqual(location.country_code, "us")
        self.assertEqual(location.geocoder, "zippopotam")
        self.assertEqual(location.label, "Exampleville, XY | postal code 12345")

    def test_parse_zip_response_rejects_missing_places(self) -> None:
        with self.assertRaises(DataSourceError):
            parse_zippopotam_response("12345", {"places": []})

    def test_zip_lookup_redacts_postal_code_from_transport_errors(self) -> None:
        with (
            patch("inky_bird_frame.geo.get_json", side_effect=DataSourceError("failure")) as get,
            self.assertRaises(DataSourceError),
        ):
            lookup_us_zip("12345")

        self.assertEqual(get.call_args.kwargs["error_label"], "Zippopotam US ZIP API")


class GeoapifyParsingTests(unittest.TestCase):
    def test_parses_exact_international_postcode(self) -> None:
        location = parse_geoapify_postcode_response(
            "ZZ1 1ZZ",
            "gb",
            {
                "features": [
                    {
                        "properties": {
                            "postcode": "ZZ1 1ZZ",
                            "country_code": "gb",
                            "city": "Exampleville",
                            "state": "Exampleshire",
                            "lat": 1.25,
                            "lon": 2.5,
                            "datasource": {"attribution": "© OpenStreetMap contributors"},
                        }
                    }
                ]
            },
        )

        self.assertEqual(location.postal_code, "ZZ1 1ZZ")
        self.assertEqual(location.country_code, "gb")
        self.assertEqual(location.place_name, "Exampleville")
        self.assertEqual(location.state, "Exampleshire")
        self.assertEqual(location.latitude, 1.25)
        self.assertEqual(location.longitude, 2.5)
        self.assertEqual(location.geocoder, "geoapify")
        self.assertEqual(
            location.geocoder_attribution,
            "Powered by Geoapify; © OpenStreetMap contributors",
        )

    def test_rejects_non_exact_postcode_result(self) -> None:
        payload = {
            "features": [
                {
                    "properties": {
                        "postcode": "SW1A 2AA",
                        "country_code": "gb",
                        "lat": 51.5,
                        "lon": -0.1,
                    }
                }
            ]
        }

        with self.assertRaisesRegex(DataSourceError, "no exact match"):
            parse_geoapify_postcode_response("SW1A 1AA", "gb", payload)

    def test_rejects_ambiguous_exact_postcode_results(self) -> None:
        payload = {
            "features": [
                {
                    "properties": {
                        "postcode": "100 00",
                        "country_code": "cz",
                        "lat": 50.1,
                        "lon": 14.4,
                    }
                },
                {
                    "properties": {
                        "postcode": "10000",
                        "country_code": "cz",
                        "lat": 50.2,
                        "lon": 14.5,
                    }
                },
            ]
        }

        with self.assertRaisesRegex(DataSourceError, "multiple locations"):
            parse_geoapify_postcode_response("100 00", "cz", payload)

    def test_accepts_duplicate_results_that_differ_only_by_rounding(self) -> None:
        payload = {
            "features": [
                {
                    "properties": {
                        "postcode": "100 00",
                        "country_code": "cz",
                        "city": "Exampleville",
                        "lat": 1.25,
                        "lon": 2.5,
                    }
                },
                {
                    "properties": {
                        "postcode": "10000",
                        "country_code": "cz",
                        "city": "Exampleville",
                        "lat": 1.2500004,
                        "lon": 2.4999996,
                    }
                },
            ]
        }

        location = parse_geoapify_postcode_response("100 00", "cz", payload)

        self.assertEqual((location.latitude, location.longitude), (1.25, 2.5))

    def test_lookup_encodes_query_and_redacts_key_from_errors(self) -> None:
        with (
            patch("inky_bird_frame.geo.get_json", return_value={"features": []}) as get,
            self.assertRaises(DataSourceError),
        ):
            lookup_geoapify_postcode("SW1A 1AA", "gb", "private-key")

        url = get.call_args.args[0]
        self.assertIn("postcode=SW1A+1AA", url)
        self.assertIn("countrycode=gb", url)
        self.assertIn("apiKey=private-key", url)
        self.assertEqual(get.call_args.kwargs["error_label"], "Geoapify Postcode API")


class DiscoveryLocationTests(unittest.TestCase):
    def test_resolves_configured_coordinates_without_network(self) -> None:
        config = discovery_config(
            latitude=51.5,
            longitude=-0.1,
        )
        with patch("inky_bird_frame.geo.get_json") as get:
            location = resolve_discovery_location(config)

        self.assertEqual(location.latitude, 51.5)
        self.assertEqual(location.longitude, -0.1)
        self.assertEqual(location.geocoder, "configured")
        get.assert_not_called()

    def test_resolves_geoapify_postcode_configuration(self) -> None:
        config = discovery_config(
            postal_code="SW1A 1AA",
            country_code="gb",
            geoapify_api_key="secret",
        )
        expected = object()
        with patch("inky_bird_frame.geo.lookup_geoapify_postcode", return_value=expected) as lookup:
            location = resolve_discovery_location(config)

        self.assertIs(location, expected)
        lookup.assert_called_once_with("SW1A 1AA", "gb", "secret")

    def test_resolves_legacy_us_zip_configuration(self) -> None:
        config = discovery_config(zip_code="12345")
        expected = object()
        with patch("inky_bird_frame.geo.lookup_us_zip", return_value=expected) as lookup:
            location = resolve_discovery_location(config)

        self.assertIs(location, expected)
        lookup.assert_called_once_with("12345")


ZIP_LOCATION = DiscoveryLocation(
    postal_code="12345",
    place_name="Exampleville",
    state="XY",
    latitude=1.25,
    longitude=2.5,
    country_code="us",
    geocoder="zippopotam",
)
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class GeocodeCacheTests(unittest.TestCase):
    def test_second_lookup_uses_private_cache_without_network(self) -> None:
        config = discovery_config(zip_code="12345")
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "state" / "geocode-cache.json"
            with patch("inky_bird_frame.geo.lookup_us_zip", return_value=ZIP_LOCATION) as lookup:
                first = resolve_discovery_location(config, cache_path=cache, now=NOW)
                second = resolve_discovery_location(
                    config, cache_path=cache, now=NOW + timedelta(days=1)
                )
            mode = stat.S_IMODE(cache.stat().st_mode)

        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(first, ZIP_LOCATION)
        self.assertEqual(second, ZIP_LOCATION)
        self.assertEqual(mode, 0o600)

    def test_outage_uses_expired_cache_entry(self) -> None:
        config = discovery_config(zip_code="12345")
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "geocode-cache.json"
            with patch("inky_bird_frame.geo.lookup_us_zip", return_value=ZIP_LOCATION):
                resolve_discovery_location(config, cache_path=cache, now=NOW)
            with patch(
                "inky_bird_frame.geo.lookup_us_zip",
                side_effect=DataSourceError("Zippopotam US ZIP API is unavailable"),
            ) as lookup:
                location = resolve_discovery_location(
                    config,
                    cache_path=cache,
                    now=NOW + GEOCODE_CACHE_TTL + timedelta(days=1),
                )

        lookup.assert_called_once_with("12345")
        self.assertEqual(location, ZIP_LOCATION)

    def test_expired_entry_is_refreshed_when_lookup_succeeds(self) -> None:
        config = discovery_config(zip_code="12345")
        moved = DiscoveryLocation("12345", "Newville", "XY", 3.0, 4.0, "us", "zippopotam")
        later = NOW + GEOCODE_CACHE_TTL + timedelta(seconds=1)
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "geocode-cache.json"
            with patch("inky_bird_frame.geo.lookup_us_zip", return_value=ZIP_LOCATION):
                resolve_discovery_location(config, cache_path=cache, now=NOW)
            with patch("inky_bird_frame.geo.lookup_us_zip", return_value=moved):
                refreshed = resolve_discovery_location(config, cache_path=cache, now=later)
            with patch("inky_bird_frame.geo.lookup_us_zip") as lookup:
                cached = resolve_discovery_location(config, cache_path=cache, now=later)

        self.assertEqual(refreshed, moved)
        self.assertEqual(cached, moved)
        lookup.assert_not_called()

    def test_entry_dated_in_the_future_is_looked_up_again(self) -> None:
        config = discovery_config(zip_code="12345")
        moved = DiscoveryLocation("12345", "Newville", "XY", 3.0, 4.0, "us", "zippopotam")
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "geocode-cache.json"
            # Written under a clock that ran ahead, or restored from elsewhere.
            with patch("inky_bird_frame.geo.lookup_us_zip", return_value=ZIP_LOCATION):
                resolve_discovery_location(config, cache_path=cache, now=NOW + GEOCODE_CACHE_TTL)
            with patch("inky_bird_frame.geo.lookup_us_zip", return_value=moved) as lookup:
                location = resolve_discovery_location(config, cache_path=cache, now=NOW)

        lookup.assert_called_once_with("12345")
        self.assertEqual(location, moved)

    def test_changed_query_does_not_reuse_another_entry(self) -> None:
        other = DiscoveryLocation("54321", "Otherville", "XY", 3.0, 4.0, "us", "zippopotam")
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "geocode-cache.json"
            with patch("inky_bird_frame.geo.lookup_us_zip", return_value=ZIP_LOCATION):
                resolve_discovery_location(
                    discovery_config(zip_code="12345"), cache_path=cache, now=NOW
                )
            with patch("inky_bird_frame.geo.lookup_us_zip", return_value=other) as lookup:
                location = resolve_discovery_location(
                    discovery_config(zip_code="54321"), cache_path=cache, now=NOW
                )
            with (
                patch(
                    "inky_bird_frame.geo.lookup_us_zip",
                    side_effect=DataSourceError("Zippopotam US ZIP API is unavailable"),
                ),
                self.assertRaises(DataSourceError),
            ):
                resolve_discovery_location(
                    discovery_config(zip_code="12345"),
                    cache_path=cache,
                    now=NOW + GEOCODE_CACHE_TTL + timedelta(days=1),
                )

        lookup.assert_called_once_with("54321")
        self.assertEqual(location, other)

    def test_outage_without_cache_still_fails(self) -> None:
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "geocode-cache.json"
            with (
                patch(
                    "inky_bird_frame.geo.lookup_us_zip",
                    side_effect=DataSourceError("Zippopotam US ZIP API is unavailable"),
                ),
                self.assertRaises(DataSourceError),
            ):
                resolve_discovery_location(
                    discovery_config(zip_code="12345"), cache_path=cache, now=NOW
                )
            exists = cache.exists()

        self.assertFalse(exists)

    def test_preview_reads_but_never_writes_the_cache(self) -> None:
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "state" / "geocode-cache.json"
            with patch("inky_bird_frame.geo.lookup_us_zip", return_value=ZIP_LOCATION):
                resolve_discovery_location(
                    discovery_config(zip_code="12345"),
                    cache_path=cache,
                    persist_cache=False,
                    now=NOW,
                )
            exists = cache.parent.exists()

        self.assertFalse(exists)

    def test_unreadable_cache_falls_back_to_lookup_and_is_replaced(self) -> None:
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "geocode-cache.json"
            cache.write_text("not json")
            cache.chmod(0o600)
            with patch("inky_bird_frame.geo.lookup_us_zip", return_value=ZIP_LOCATION) as lookup:
                location = resolve_discovery_location(
                    discovery_config(zip_code="12345"), cache_path=cache, now=NOW
                )
            stored = json.loads(cache.read_text())

        lookup.assert_called_once_with("12345")
        self.assertEqual(location, ZIP_LOCATION)
        self.assertEqual(stored["schema_version"], 1)

    def test_geoapify_entry_omits_the_api_key_and_plain_query_key(self) -> None:
        config = discovery_config(
            postal_code="ZZ1 1ZZ", country_code="gb", geoapify_api_key="private-key"
        )
        expected = DiscoveryLocation(
            "ZZ1 1ZZ", "Exampleville", "", 1.25, 2.5, "gb", "geoapify", "Powered by Geoapify"
        )
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "geocode-cache.json"
            with patch(
                "inky_bird_frame.geo.lookup_geoapify_postcode", return_value=expected
            ) as lookup:
                resolve_discovery_location(config, cache_path=cache, now=NOW)
                cached = resolve_discovery_location(config, cache_path=cache, now=NOW)
            text = cache.read_text()

        lookup.assert_called_once()
        self.assertEqual(cached, expected)
        self.assertNotIn("private-key", text)


if __name__ == "__main__":
    unittest.main()
