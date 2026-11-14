"""Discovery location lookup helpers."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from .config import DiscoveryConfig
from .errors import DataSourceError
from .http import get_json, read_private_json, write_json_atomic
from .timeutil import parse_utc_timestamp

GEOAPIFY_POSTCODE_URL = "https://api.geoapify.com/v1/postcode/search"
GEOAPIFY_ATTRIBUTION = "Powered by Geoapify"
GEOCODE_CACHE_TTL = timedelta(days=30)
GEOCODE_CACHE_MAX_BYTES = 64 * 1024
# Separate records of one postal code can differ by rounding; about 1 km
# is still one place for discovery radii measured in kilometres.
COORDINATE_TOLERANCE_DEGREES = 0.01


@dataclass(frozen=True)
class DiscoveryLocation:
    postal_code: str | None
    place_name: str
    state: str
    latitude: float
    longitude: float
    country_code: str | None = None
    geocoder: str = "configured"
    geocoder_attribution: str | None = None

    @property
    def label(self) -> str:
        place = ", ".join(part for part in (self.place_name, self.state) if part)
        if self.postal_code is not None:
            postal = f"postal code {self.postal_code}"
            return f"{place} | {postal}" if place else postal
        return place or "configured coordinates"


def _valid_coordinate(value: object, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and minimum <= parsed <= maximum else None


def _normalized_postal_code(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def parse_zippopotam_response(zip_code: str, payload: object) -> DiscoveryLocation:
    if not isinstance(payload, dict):
        raise DataSourceError("US ZIP lookup response was not an object")

    places = payload.get("places")
    if not isinstance(places, list) or not places:
        raise DataSourceError("US ZIP lookup returned no places")

    first = places[0]
    if not isinstance(first, dict):
        raise DataSourceError("US ZIP lookup returned a malformed place")

    try:
        latitude = float(first["latitude"])
        longitude = float(first["longitude"])
        place_name = str(first["place name"])
        state = str(first["state abbreviation"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DataSourceError("US ZIP lookup returned malformed data") from exc
    if (
        not math.isfinite(latitude)
        or not -90 <= latitude <= 90
        or not math.isfinite(longitude)
        or not -180 <= longitude <= 180
    ):
        raise DataSourceError("US ZIP lookup returned invalid coordinates")
    return DiscoveryLocation(
        postal_code=zip_code,
        place_name=place_name,
        state=state,
        latitude=latitude,
        longitude=longitude,
        country_code="us",
        geocoder="zippopotam",
    )


def lookup_us_zip(zip_code: str, timeout_seconds: float = 10.0) -> DiscoveryLocation:
    if not zip_code.isdigit() or len(zip_code) != 5:
        raise ValueError("zip_code must be a five digit US ZIP code")

    payload = get_json(
        f"https://api.zippopotam.us/us/{zip_code}",
        timeout_seconds,
        error_label="Zippopotam US ZIP API",
    )
    return parse_zippopotam_response(zip_code, payload)


def parse_geoapify_postcode_response(
    postal_code: str,
    country_code: str,
    payload: object,
) -> DiscoveryLocation:
    if not isinstance(payload, dict):
        raise DataSourceError("Geoapify Postcode API response was not an object")
    features = payload.get("features")
    if not isinstance(features, list):
        raise DataSourceError("Geoapify Postcode API response had no feature list")

    expected_postal = _normalized_postal_code(postal_code)
    candidates: list[DiscoveryLocation] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            continue
        result_postal = properties.get("postcode")
        result_country = properties.get("country_code")
        if (
            not isinstance(result_postal, str)
            or _normalized_postal_code(result_postal) != expected_postal
            or not isinstance(result_country, str)
            or result_country.casefold() != country_code.casefold()
        ):
            continue
        latitude = _valid_coordinate(properties.get("lat"), minimum=-90, maximum=90)
        longitude = _valid_coordinate(properties.get("lon"), minimum=-180, maximum=180)
        if latitude is None or longitude is None:
            continue
        place_name = next(
            (
                value
                for name in ("city", "municipality", "county", "country")
                if isinstance((value := properties.get(name)), str) and value.strip()
            ),
            "",
        )
        state = next(
            (
                value
                for name in ("state_code", "state")
                if isinstance((value := properties.get(name)), str) and value.strip()
            ),
            "",
        )
        datasource = properties.get("datasource")
        source_attribution = datasource.get("attribution") if isinstance(datasource, dict) else None
        source = (
            source_attribution.strip()
            if isinstance(source_attribution, str) and source_attribution.strip()
            else None
        )
        attribution = f"{GEOAPIFY_ATTRIBUTION}; {source}" if source else GEOAPIFY_ATTRIBUTION
        candidates.append(
            DiscoveryLocation(
                postal_code=postal_code,
                place_name=place_name,
                state=state,
                latitude=latitude,
                longitude=longitude,
                country_code=country_code.casefold(),
                geocoder="geoapify",
                geocoder_attribution=attribution,
            )
        )

    if not candidates:
        raise DataSourceError("Geoapify found no exact match for the configured postal code")
    latitudes = [item.latitude for item in candidates]
    longitudes = [item.longitude for item in candidates]
    if (
        max(latitudes) - min(latitudes) > COORDINATE_TOLERANCE_DEGREES
        or max(longitudes) - min(longitudes) > COORDINATE_TOLERANCE_DEGREES
    ):
        raise DataSourceError(
            "Geoapify returned multiple locations for the configured postal code; "
            "configure latitude and longitude instead"
        )
    return candidates[0]


def lookup_geoapify_postcode(
    postal_code: str,
    country_code: str,
    api_key: str,
    timeout_seconds: float = 10.0,
) -> DiscoveryLocation:
    query = urlencode(
        {
            "postcode": postal_code,
            "countrycode": country_code,
            "geometry": "point",
            "apiKey": api_key,
        }
    )
    payload = get_json(
        f"{GEOAPIFY_POSTCODE_URL}?{query}",
        timeout_seconds,
        error_label="Geoapify Postcode API",
    )
    return parse_geoapify_postcode_response(postal_code, country_code, payload)


def _cache_key(geocoder: str, query: str) -> str:
    return hashlib.sha256(f"{geocoder}\0{query}".encode()).hexdigest()


def _read_cached_location(path: Path, key: str) -> tuple[DiscoveryLocation, datetime] | None:
    if not path.exists():
        return None
    try:
        raw = read_private_json(path, maximum=GEOCODE_CACHE_MAX_BYTES)
    except DataSourceError:
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != 1 or raw.get("key") != key:
        return None
    resolved_at = parse_utc_timestamp(raw.get("resolved_at"))
    value = raw.get("location")
    if resolved_at is None or not isinstance(value, dict):
        return None
    latitude = _valid_coordinate(value.get("latitude"), minimum=-90, maximum=90)
    longitude = _valid_coordinate(value.get("longitude"), minimum=-180, maximum=180)
    postal_code = value.get("postal_code")
    place_name = value.get("place_name")
    state = value.get("state")
    geocoder = value.get("geocoder")
    country_code = value.get("country_code")
    attribution = value.get("geocoder_attribution")
    if (
        latitude is None
        or longitude is None
        or not isinstance(postal_code, str)
        or not isinstance(place_name, str)
        or not isinstance(state, str)
        or not isinstance(geocoder, str)
        or (country_code is not None and not isinstance(country_code, str))
        or (attribution is not None and not isinstance(attribution, str))
    ):
        return None
    location = DiscoveryLocation(
        postal_code=postal_code,
        place_name=place_name,
        state=state,
        latitude=latitude,
        longitude=longitude,
        country_code=country_code,
        geocoder=geocoder,
        geocoder_attribution=attribution,
    )
    return location, resolved_at


def _cached_lookup(
    cache_path: Path | None,
    *,
    persist_cache: bool,
    now: datetime | None,
    geocoder: str,
    query: str,
    lookup: Callable[[], DiscoveryLocation],
) -> DiscoveryLocation:
    if cache_path is None:
        return lookup()
    current = (now or datetime.now(UTC)).astimezone(UTC)
    key = _cache_key(geocoder, query)
    cached = _read_cached_location(cache_path, key)
    # An entry dated in the future (clock skew, a restored backup) is not fresh.
    if cached is not None and timedelta(0) <= current - cached[1] <= GEOCODE_CACHE_TTL:
        return cached[0]
    try:
        location = lookup()
    except DataSourceError:
        if cached is None:
            raise
        return cached[0]
    if persist_cache:
        # The lookup itself succeeded; a cache that cannot be saved only
        # means the next refresh asks the geocoder again.
        with suppress(OSError):
            write_json_atomic(
                cache_path,
                {
                    "schema_version": 1,
                    "key": key,
                    "resolved_at": current.replace(microsecond=0).isoformat(),
                    "location": asdict(location),
                },
                mode=0o600,
            )
    return location


def resolve_discovery_location(
    config: DiscoveryConfig,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    cache_path: Path | None = None,
    persist_cache: bool = True,
    now: datetime | None = None,
) -> DiscoveryLocation:
    if (latitude is None) != (longitude is None):
        raise ValueError("latitude and longitude must be provided together")
    if latitude is not None and longitude is not None:
        valid_latitude = _valid_coordinate(latitude, minimum=-90, maximum=90)
        valid_longitude = _valid_coordinate(longitude, minimum=-180, maximum=180)
        if valid_latitude is None or valid_longitude is None:
            raise ValueError("latitude or longitude is outside the valid range")
        return DiscoveryLocation(
            postal_code=None,
            place_name="",
            state="",
            latitude=valid_latitude,
            longitude=valid_longitude,
        )
    if config.latitude is not None and config.longitude is not None:
        return DiscoveryLocation(
            postal_code=None,
            place_name="",
            state="",
            latitude=config.latitude,
            longitude=config.longitude,
        )
    if config.postal_code is not None and config.country_code is not None:
        api_key = config.geoapify_api_key
        if api_key is None:
            raise DataSourceError("Geoapify API key is not configured")
        postal_code = config.postal_code
        country_code = config.country_code
        return _cached_lookup(
            cache_path,
            persist_cache=persist_cache,
            now=now,
            geocoder="geoapify",
            query=f"{country_code.casefold()}\0{_normalized_postal_code(postal_code)}",
            lookup=lambda: lookup_geoapify_postcode(postal_code, country_code, api_key),
        )
    if config.zip_code is not None:
        zip_code = config.zip_code
        return _cached_lookup(
            cache_path,
            persist_cache=persist_cache,
            now=now,
            geocoder="zippopotam",
            query=zip_code,
            lookup=lambda: lookup_us_zip(zip_code),
        )
    raise DataSourceError("Discovery location is not configured")
