"""Validate approved plates and publish them through owner-bypassed catalog PRs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Literal, cast

from PIL import Image

from .catalog import (
    CatalogEntry,
    catalog_index_data,
    catalog_state_lock,
    clear_catalog_staging,
    has_passing_sourced_review,
    is_bounded_generation,
    read_catalog_entries,
    read_json,
    rebuild_catalog_index,
    sha256_file,
)
from .codex_runner import parse_species_profile
from .config import AppConfig, PublicCatalogConfig
from .errors import CatalogError, CatalogPublishError, GenerationError
from .http import write_json_atomic
from .timeutil import parse_utc_timestamp

_ALLOWED_SPECIES_FILES = frozenset(
    {
        "display.png",
        "manifest.json",
        "portrait.png",
        "profile.json",
        "quality-review.json",
    }
)
_PRIVATE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization_confirmed_at",
        "birdnet_analyzer",
        "birdnet_go_url",
        "birdbuddy",
        "catalog_dir",
        "checkout_dir",
        "controller_url",
        "coordinates",
        "country_code",
        "ebird_archive",
        "geocoder",
        "geocoder_attribution",
        "history_started_at",
        "geoapify_api_key",
        "geoapify_api_key_env",
        "latitude",
        "longitude",
        "last_imported_at",
        "email",
        "observation_count",
        "observed_on",
        "password",
        "submission_id",
        "place_name",
        "postal_code",
        "postcard_id",
        "radius_km",
        "refresh_token",
        "state_dir",
        "feeder_id",
        "workspace_dir",
        "zip_code",
    }
)
_PRIVATE_KEY_IDENTIFIERS = frozenset(key.replace("_", "") for key in _PRIVATE_KEYS)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_UNC_PATH = re.compile(r"^(?:\\\\|//)[^\\/]")
_CREDENTIALS_IN_URL = re.compile(r"(https?://)[^/@\s]+@")
_GIT_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<repository>[^/\s]+/[^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_IMAGE_DIMENSIONS = {"portrait.png": (1200, 1600), "display.png": (1600, 1200)}
_COMMAND_TIMEOUT_SECONDS = 180
# A first publication of a large backlog moves hundreds of megabytes over a home uplink.
_TRANSFER_TIMEOUT_SECONDS = 3600
_CHECKOUT_TIMEOUT_SECONDS = 900
_PUBLICATION_BRANCH = re.compile(r"catalog/publish-[0-9a-f]{8}-[0-9a-f]{8}")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IMAGE_CHUNKS = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND"})
_FINDER_METADATA = ".DS_Store"
_MINIMUM_PRIVATE_TERM_LENGTH = 3
_SHA256 = re.compile(r"[0-9a-f]{64}")


@contextmanager
def exclusive_publish_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "catalog-publish.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CatalogPublishError("Another catalog publication is running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _abandoned_publish_work(work_parent: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for candidate in sorted(work_parent.iterdir())
        if candidate.name.startswith("publish-")
        and not candidate.is_symlink()
        and candidate.is_dir()
    )


def _cleanup_abandoned_publish_work_locked(work_parent: Path) -> int:
    """Remove scratch directories left by publishers that no longer hold the lock."""
    removed = 0
    for candidate in _abandoned_publish_work(work_parent):
        try:
            shutil.rmtree(candidate)
        except OSError as exc:
            raise CatalogPublishError(
                f"Could not remove abandoned catalog publication work: {candidate.name}"
            ) from exc
        removed += 1
    return removed


def _word_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(term.casefold())}(?!\w)")


def _coordinate_terms(value: float) -> set[str]:
    magnitude = abs(value)
    exact = abs(Decimal(str(value)))
    terms: set[str] = set()
    # Two decimals would also match common DOI prefixes such as "10.12" in source links.
    for digits in range(3, 7):
        terms.add(f"{magnitude:.{digits}f}")
        terms.add(f"{exact.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_DOWN)}")
    return terms


def _discovered_place(state_dir: Path) -> tuple[str, str] | None:
    try:
        discovery = json.loads((state_dir / "discovery.json").read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(discovery, dict):
        return None
    place_name = discovery.get("place_name")
    state = discovery.get("state")
    if not isinstance(place_name, str) or not isinstance(state, str):
        return None
    place_name, state = place_name.strip(), state.strip()
    return (place_name, state) if place_name and state else None


def publication_privacy_patterns(config: AppConfig) -> tuple[re.Pattern[str], ...]:
    """Build matchers for this controller's location and paths, rejected anywhere in JSON text."""
    discovery = config.discovery
    words: set[str] = set()
    for code in (discovery.zip_code, discovery.postal_code):
        if code is not None:
            words.add(code.strip())
            words.add(re.sub(r"[\s-]", "", code))
    patterns = [
        _word_pattern(word) for word in sorted(words) if len(word) >= _MINIMUM_PRIVATE_TERM_LENGTH
    ]
    place = _discovered_place(config.controller.state_dir)
    if place is not None:
        # A bare place name also names birds such as "Baltimore Oriole" or "Lincoln's Sparrow",
        # so only the place together with its state counts as the controller's location.
        place_name, state = (re.escape(part.casefold()) for part in place)
        patterns.append(re.compile(rf"(?<!\w){place_name}[\s,]+{state}(?!\w)"))
    coordinates: set[str] = set()
    for coordinate in (discovery.latitude, discovery.longitude):
        if coordinate is not None:
            coordinates |= _coordinate_terms(coordinate)
    patterns.extend(re.compile(rf"(?<![0-9]){re.escape(term)}") for term in sorted(coordinates))

    paths: list[Path] = [
        config.controller.workspace_dir,
        config.controller.state_dir,
        config.controller.catalog_dir,
    ]
    if config.public_catalog.checkout_dir is not None:
        paths.append(config.public_catalog.checkout_dir)
    with suppress(RuntimeError, KeyError):
        paths.append(Path.home())
    path_terms: set[str] = set()
    for path in paths:
        for candidate in (path, path.resolve()):
            # A root-level path such as "/" or "/root" would match ordinary URLs.
            if candidate.is_absolute() and len(candidate.parts) >= 3:
                path_terms.add(str(candidate).casefold())
    patterns.extend(re.compile(rf"{re.escape(term)}(?![\w-])") for term in sorted(path_terms))
    return tuple(patterns)


def _check_json_privacy(
    value: object, source: Path, private_patterns: tuple[re.Pattern[str], ...] = ()
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CatalogPublishError(f"JSON object has a non-string key: {source}")
            identifier = re.sub(r"[^a-z0-9]", "", key.casefold())
            if identifier in _PRIVATE_KEY_IDENTIFIERS:
                raise CatalogPublishError(f"Private field {key!r} found in {source}")
            _check_json_privacy(child, source, private_patterns)
        return
    if isinstance(value, list):
        for child in value:
            _check_json_privacy(child, source, private_patterns)
        return
    if isinstance(value, str) and private_patterns:
        folded = value.casefold()
        if any(pattern.search(folded) is not None for pattern in private_patterns):
            raise CatalogPublishError(f"Private location or path text found in {source}")
    if isinstance(value, str) and (
        value.startswith("file://")
        or Path(value).is_absolute()
        or _WINDOWS_ABSOLUTE_PATH.match(value) is not None
        or _WINDOWS_UNC_PATH.match(value) is not None
    ):
        raise CatalogPublishError(f"Local path found in catalog JSON: {source}")


def _png_has_only_image_chunks(path: Path) -> bool:
    data = path.read_bytes()
    if not data.startswith(_PNG_SIGNATURE):
        return False
    offset = len(_PNG_SIGNATURE)
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        offset += 12 + length
        if chunk_type not in _PNG_IMAGE_CHUNKS or offset > len(data):
            return False
        if chunk_type == b"IEND":
            return offset == len(data)
    return False


def _validate_image(path: Path, expected_size: tuple[int, int]) -> None:
    try:
        with Image.open(path) as image:
            if image.format != "PNG" or image.size != expected_size:
                raise CatalogPublishError(
                    f"{path} must be a {expected_size[0]}x{expected_size[1]} PNG"
                )
            image.verify()
        with Image.open(path) as image:
            if image.info or image.getexif():
                raise CatalogPublishError(f"Image metadata is not allowed in {path}")
        # Pillow reads only the chunks before the image data and ignores unknown ones.
        if not _png_has_only_image_chunks(path):
            raise CatalogPublishError(f"Image metadata is not allowed in {path}")
    except CatalogPublishError:
        raise
    except (OSError, RuntimeError, SyntaxError) as exc:
        raise CatalogPublishError(f"Invalid catalog image: {path}") from exc


def _legacy_seed_review_passed(review: object, generation: object) -> bool:
    if not isinstance(review, dict) or not isinstance(generation, dict):
        return False
    score_fields = (
        "species_accuracy",
        "anatomy_accuracy",
        "text_accuracy",
        "composition_quality",
    )
    return (
        generation.get("generator") == "User-approved seed image"
        and review.get("passed") is True
        and review.get("location_free") is True
        and all(
            isinstance(review.get(field), int)
            and not isinstance(review.get(field), bool)
            and cast(int, review[field]) >= 4
            for field in score_fields
        )
    )


def _legacy_sourced_review_passed(review: object, generation: object) -> bool:
    if not isinstance(generation, dict):
        return False
    return generation.get("prompt_version") == "field-journal-v1" and has_passing_sourced_review(
        review
    )


def _validate_catalog_root(catalog_dir: Path, *, allow_create: bool) -> None:
    if catalog_dir.exists():
        if catalog_dir.is_symlink() or not catalog_dir.is_dir():
            raise CatalogPublishError(f"Catalog root must be a directory: {catalog_dir}")
    elif allow_create:
        parent = catalog_dir.parent
        if parent.is_symlink() or not parent.is_dir():
            raise CatalogPublishError(f"Catalog parent must be a directory: {parent}")
        catalog_dir.mkdir()
    else:
        raise CatalogPublishError(f"Catalog root does not exist: {catalog_dir}")

    species_root = catalog_dir / "species"
    if species_root.exists():
        if species_root.is_symlink() or not species_root.is_dir():
            raise CatalogPublishError(f"Species root must be a directory: {species_root}")
    elif allow_create:
        species_root.mkdir()
    else:
        raise CatalogPublishError(f"Species root does not exist: {species_root}")


def _validate_species_directory(
    directory: Path,
    *,
    expected_status: Literal["approved", "pending"] = "approved",
    private_patterns: tuple[re.Pattern[str], ...] = (),
) -> tuple[int, str]:
    if directory.is_symlink() or not directory.is_dir():
        raise CatalogPublishError(f"Species path must be a directory, not a symlink: {directory}")
    files = {path.name for path in directory.iterdir()}
    unexpected = files - _ALLOWED_SPECIES_FILES
    missing = {"manifest.json", "portrait.png", "display.png"} - files
    if unexpected:
        raise CatalogPublishError(
            f"Unexpected catalog files in {directory}: {', '.join(sorted(unexpected))}"
        )
    if missing:
        raise CatalogPublishError(
            f"Required catalog files missing from {directory}: {', '.join(sorted(missing))}"
        )
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise CatalogPublishError(f"Catalog entries must be regular files: {path}")

    manifest = read_json(directory / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise CatalogPublishError(f"Unsupported catalog manifest: {directory / 'manifest.json'}")
    taxon_id = manifest.get("taxon_id")
    slug = manifest.get("slug")
    if (
        manifest.get("status") != expected_status
        or not isinstance(taxon_id, int)
        or isinstance(taxon_id, bool)
        or not isinstance(slug, str)
        or directory.name != f"{taxon_id}-{slug}"
    ):
        raise CatalogPublishError(f"Manifest identity does not match {directory}")
    _catalog_migration_records(manifest, directory / "manifest.json")

    review = manifest.get("quality_review")
    generation = manifest.get("generation")
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        raise CatalogPublishError(f"Manifest has no asset map: {directory}")
    for asset_name, expected_filename in (
        ("portrait", "portrait.png"),
        ("display", "display.png"),
    ):
        asset = assets.get(asset_name)
        if not isinstance(asset, dict) or asset.get("filename") != expected_filename:
            raise CatalogPublishError(
                f"Manifest {asset_name} asset must use {expected_filename}: {directory}"
            )
        checksum = asset.get("sha256")
        if (
            not isinstance(checksum, str)
            or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
            or sha256_file(directory / expected_filename) != checksum
        ):
            raise CatalogPublishError(
                f"Manifest {asset_name} checksum does not match {expected_filename}: {directory}"
            )
    automated = has_passing_sourced_review(review) and is_bounded_generation(generation)
    legacy = _legacy_seed_review_passed(review, generation) or _legacy_sourced_review_passed(
        review, generation
    )
    if not automated and not (expected_status == "approved" and legacy):
        raise CatalogPublishError(f"Manifest lacks a publishable quality review: {directory}")

    for path in directory.glob("*.json"):
        payload = read_json(path)
        _check_json_privacy(payload, path, private_patterns)
        if path.name == "profile.json" and payload != manifest.get("profile"):
            raise CatalogPublishError(f"Profile does not match the manifest: {directory}")
        if path.name == "quality-review.json" and payload != review:
            raise CatalogPublishError(f"Quality review does not match the manifest: {directory}")
    for filename, dimensions in _IMAGE_DIMENSIONS.items():
        _validate_image(directory / filename, dimensions)
    return taxon_id, slug


def validate_pending_candidate(directory: Path) -> None:
    """Use the publication gates before ordinary owner approval, without legacy bypasses."""
    _validate_species_directory(directory, expected_status="pending")
    manifest = read_json(directory / "manifest.json")
    if not isinstance(manifest, dict):
        raise CatalogPublishError("Pending profile is invalid")
    try:
        profile = parse_species_profile(manifest.get("profile"))
    except GenerationError as exc:
        raise CatalogPublishError("Pending profile is incomplete") from exc
    if any(
        profile[key] != manifest.get(key) for key in ("taxon_id", "common_name", "scientific_name")
    ):
        raise CatalogPublishError("Pending profile identity does not match the candidate")


def validate_public_catalog(catalog_dir: Path) -> list[CatalogEntry]:
    _validate_catalog_root(catalog_dir, allow_create=False)
    root_entries = {path.name for path in catalog_dir.iterdir()}
    unexpected_root_entries = root_entries - {"index.json", "species"}
    if unexpected_root_entries:
        unexpected = ", ".join(sorted(unexpected_root_entries))
        raise CatalogPublishError(f"Unexpected catalog root entries in {catalog_dir}: {unexpected}")
    index_path = catalog_dir / "index.json"
    if index_path.is_symlink() or not index_path.is_file():
        raise CatalogPublishError(f"Catalog index must be a regular file: {index_path}")
    species_root = catalog_dir / "species"
    directories = _species_directories(species_root)
    seen_taxa: set[int] = set()
    for directory in directories:
        taxon_id, _ = _validate_species_directory(directory)
        if taxon_id in seen_taxa:
            raise CatalogPublishError(f"Catalog contains duplicate taxon ID {taxon_id}")
        seen_taxa.add(taxon_id)
    entries = read_catalog_entries(catalog_dir)
    if len(entries) != len(directories):
        raise CatalogPublishError(f"Every species directory needs one manifest: {species_root}")
    index = read_json(index_path)
    _check_json_privacy(index, index_path)
    if index != catalog_index_data(entries):
        raise CatalogPublishError(f"Catalog index does not match species manifests: {catalog_dir}")
    return entries


def _trees_match(left: Path, right: Path) -> bool:
    left_files = sorted(path.relative_to(left) for path in left.rglob("*") if path.is_file())
    right_files = sorted(path.relative_to(right) for path in right.rglob("*") if path.is_file())
    return left_files == right_files and all(
        (left / relative).read_bytes() == (right / relative).read_bytes() for relative in left_files
    )


def _parse_catalog_migration_record(
    raw: object,
    source: Path,
) -> tuple[str, str, str, str]:
    if not isinstance(raw, dict) or set(raw) != {"reason", "replaces"}:
        raise CatalogPublishError(f"Invalid catalog migration record: {source}")
    reason = raw.get("reason")
    replaces = raw.get("replaces")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or reason != reason.strip()
        or not isinstance(replaces, dict)
        or set(replaces) != {"approved_at", "display_sha256", "portrait_sha256"}
    ):
        raise CatalogPublishError(f"Invalid catalog migration record: {source}")
    approved_at = replaces.get("approved_at")
    display_sha256 = replaces.get("display_sha256")
    portrait_sha256 = replaces.get("portrait_sha256")
    if (
        not isinstance(approved_at, str)
        or parse_utc_timestamp(approved_at) is None
        or not isinstance(display_sha256, str)
        or _SHA256.fullmatch(display_sha256) is None
        or not isinstance(portrait_sha256, str)
        or _SHA256.fullmatch(portrait_sha256) is None
    ):
        raise CatalogPublishError(f"Invalid catalog migration record: {source}")
    return reason, approved_at, display_sha256, portrait_sha256


def _catalog_migration_records(
    manifest: dict[str, object],
    source: Path,
) -> list[tuple[str, str, str, str]]:
    raw = manifest.get("catalog_migration")
    if raw is None:
        return []
    if not isinstance(raw, dict) or set(raw) not in (
        {"reason", "replaces"},
        {"history", "reason", "replaces"},
    ):
        raise CatalogPublishError(f"Invalid catalog migration record: {source}")
    history = raw.get("history", [])
    if not isinstance(history, list) or ("history" in raw and not history):
        raise CatalogPublishError(f"Invalid catalog migration record: {source}")
    current = {key: value for key, value in raw.items() if key != "history"}
    records = [
        *(_parse_catalog_migration_record(item, source) for item in history),
        _parse_catalog_migration_record(current, source),
    ]
    approved_times = [parse_utc_timestamp(record[1]) for record in records]
    candidate_approved_at = parse_utc_timestamp(manifest.get("approved_at"))
    if (
        any(value is None for value in approved_times)
        or candidate_approved_at is None
        or any(
            previous >= current_time
            for previous, current_time in zip(approved_times, approved_times[1:], strict=False)
            if previous is not None and current_time is not None
        )
        or approved_times[-1] is None
        or approved_times[-1] >= candidate_approved_at
        or len({record[1:] for record in records}) != len(records)
    ):
        raise CatalogPublishError(f"Invalid catalog migration record: {source}")
    return records


def _catalog_migration_payload(record: tuple[str, str, str, str]) -> dict[str, object]:
    reason, approved_at, display_sha256, portrait_sha256 = record
    return {
        "reason": reason,
        "replaces": {
            "approved_at": approved_at,
            "display_sha256": display_sha256,
            "portrait_sha256": portrait_sha256,
        },
    }


def _manifest_asset_sha256(manifest: dict[str, object], asset_name: str) -> str:
    assets = manifest.get("assets")
    asset = assets.get(asset_name) if isinstance(assets, dict) else None
    checksum = asset.get("sha256") if isinstance(asset, dict) else None
    if not isinstance(checksum, str) or _SHA256.fullmatch(checksum) is None:
        raise CatalogPublishError(f"Manifest has invalid {asset_name} checksum")
    return checksum


def _validate_catalog_replacement(
    base_directory: Path,
    candidate_directory: Path,
    *,
    allow_ancestor: bool = False,
) -> None:
    base = read_json(base_directory / "manifest.json")
    candidate = read_json(candidate_directory / "manifest.json")
    if not isinstance(base, dict) or not isinstance(candidate, dict):
        raise CatalogPublishError("Catalog replacement manifests must be JSON objects")
    records = _catalog_migration_records(candidate, candidate_directory / "manifest.json")
    if not records:
        raise CatalogPublishError(
            f"Catalog contribution changed immutable taxon {base.get('taxon_id')}"
        )
    identity_fields = ("taxon_id", "common_name", "scientific_name", "slug")
    if any(base.get(field) != candidate.get(field) for field in identity_fields):
        raise CatalogPublishError("Catalog replacement changed the approved taxon identity")
    base_records = _catalog_migration_records(base, base_directory / "manifest.json")
    if records[: len(base_records)] != base_records or (
        not allow_ancestor and records[:-1] != base_records
    ):
        raise CatalogPublishError("Catalog replacement did not preserve migration history")
    base_approval = (
        base.get("approved_at"),
        _manifest_asset_sha256(base, "display"),
        _manifest_asset_sha256(base, "portrait"),
    )
    matching_index = next(
        (index for index, record in enumerate(records) if record[1:] == base_approval),
        None,
    )
    if matching_index != len(base_records):
        raise CatalogPublishError("Catalog migration does not match the approved base artifacts")
    candidate_approved_at = parse_utc_timestamp(candidate.get("approved_at"))
    replaced_approved_at = parse_utc_timestamp(records[matching_index][1])
    if (
        candidate_approved_at is None
        or replaced_approved_at is None
        or candidate_approved_at <= replaced_approved_at
    ):
        raise CatalogPublishError("Catalog replacement must have a newer approval timestamp")
    if (
        not allow_ancestor
        and _manifest_asset_sha256(candidate, "display") == records[-1][2]
        and _manifest_asset_sha256(candidate, "portrait") == records[-1][3]
    ):
        raise CatalogPublishError("Catalog replacement does not change an approved image")


def _trees_match_without_catalog_migration(left: Path, right: Path) -> bool:
    left_files = sorted(path.relative_to(left) for path in left.rglob("*") if path.is_file())
    right_files = sorted(path.relative_to(right) for path in right.rglob("*") if path.is_file())
    if left_files != right_files:
        return False
    for relative in left_files:
        if relative != Path("manifest.json"):
            if (left / relative).read_bytes() != (right / relative).read_bytes():
                return False
            continue
        left_manifest = read_json(left / relative)
        right_manifest = read_json(right / relative)
        if not isinstance(left_manifest, dict) or not isinstance(right_manifest, dict):
            return False
        left_manifest.pop("catalog_migration", None)
        right_manifest.pop("catalog_migration", None)
        if left_manifest != right_manifest:
            return False
    return True


def _validate_catalog_migration_convergence(
    destination_directory: Path,
    candidate_directory: Path,
) -> None:
    destination = read_json(destination_directory / "manifest.json")
    candidate = read_json(candidate_directory / "manifest.json")
    if not isinstance(destination, dict) or not isinstance(candidate, dict):
        raise CatalogPublishError("Catalog replacement manifests must be JSON objects")
    destination_records = _catalog_migration_records(
        destination, destination_directory / "manifest.json"
    )
    candidate_records = _catalog_migration_records(candidate, candidate_directory / "manifest.json")
    if (
        not _trees_match_without_catalog_migration(destination_directory, candidate_directory)
        or destination_records != candidate_records[: len(destination_records)]
    ):
        raise CatalogPublishError(
            f"Catalog taxon {destination.get('taxon_id')} conflicts with immutable local approval"
        )


def _catalog_sync_should_replace(destination: Path, source: Path) -> bool:
    """Validate migration ancestry and return whether source is newer."""
    if _trees_match_without_catalog_migration(destination, source):
        try:
            _validate_catalog_migration_convergence(destination, source)
        except CatalogPublishError as forward_error:
            try:
                _validate_catalog_migration_convergence(source, destination)
            except CatalogPublishError as reverse_error:
                raise forward_error from reverse_error
            return False
        return True

    try:
        _validate_catalog_replacement(destination, source, allow_ancestor=True)
    except CatalogPublishError as forward_error:
        try:
            _validate_catalog_replacement(source, destination, allow_ancestor=True)
        except CatalogPublishError as reverse_error:
            raise forward_error from reverse_error
        return False
    return True


def _catalog_approvals_are_independent(destination: Path, source: Path) -> bool:
    """Return whether two validated, same-taxon approvals have no shared ancestry."""
    destination_manifest = read_json(destination / "manifest.json")
    source_manifest = read_json(source / "manifest.json")
    if not isinstance(destination_manifest, dict) or not isinstance(source_manifest, dict):
        raise CatalogPublishError("Catalog approval manifests must be JSON objects")
    identity_fields = ("taxon_id", "common_name", "scientific_name", "slug")
    if any(
        destination_manifest.get(field) != source_manifest.get(field) for field in identity_fields
    ):
        raise CatalogPublishError("Catalog approval changed the approved taxon identity")

    def approvals(manifest: dict[str, object], path: Path) -> set[tuple[str, str, str]]:
        history = {
            (approved_at, display_sha256, portrait_sha256)
            for _, approved_at, display_sha256, portrait_sha256 in _catalog_migration_records(
                manifest, path
            )
        }
        approved_at = manifest.get("approved_at")
        if not isinstance(approved_at, str):
            raise CatalogPublishError(f"Manifest has invalid approval timestamp: {path}")
        history.add(
            (
                approved_at,
                _manifest_asset_sha256(manifest, "display"),
                _manifest_asset_sha256(manifest, "portrait"),
            )
        )
        return history

    return approvals(destination_manifest, destination / "manifest.json").isdisjoint(
        approvals(source_manifest, source / "manifest.json")
    )


def validate_catalog_additions(
    base_catalog: Path,
    candidate_catalog: Path,
) -> list[CatalogEntry]:
    base_entries = validate_public_catalog(base_catalog)
    candidate_entries = validate_public_catalog(candidate_catalog)
    candidate_by_taxon = {entry.taxon_id: entry for entry in candidate_entries}

    for base_entry in base_entries:
        candidate_entry = candidate_by_taxon.get(base_entry.taxon_id)
        if candidate_entry is None:
            raise CatalogPublishError(
                f"Catalog contribution removed immutable taxon {base_entry.taxon_id}"
            )
        base_directory = base_catalog / "species" / f"{base_entry.taxon_id}-{base_entry.slug}"
        candidate_directory = (
            candidate_catalog / "species" / f"{candidate_entry.taxon_id}-{candidate_entry.slug}"
        )
        if base_directory.name != candidate_directory.name:
            raise CatalogPublishError(
                f"Catalog contribution changed immutable taxon {base_entry.taxon_id}"
            )
        if not _trees_match(base_directory, candidate_directory):
            _validate_catalog_replacement(base_directory, candidate_directory)

    base_taxa = {entry.taxon_id for entry in base_entries}
    additions = [entry for entry in candidate_entries if entry.taxon_id not in base_taxa]
    for entry in additions:
        manifest = read_json(
            candidate_catalog / "species" / f"{entry.taxon_id}-{entry.slug}" / "manifest.json"
        )
        if isinstance(manifest, dict) and manifest.get("catalog_migration") is not None:
            raise CatalogPublishError(
                f"New catalog taxon {entry.taxon_id} cannot declare a replacement migration"
            )
    return additions


def replace_public_catalog_taxon(
    source_catalog: Path,
    destination_catalog: Path,
    taxon_id: int,
    reason: str,
) -> dict[str, object]:
    replacement_reason = reason.strip()
    if not replacement_reason:
        raise ValueError("Catalog replacement requires a non-empty reason")
    source_entries = validate_public_catalog(source_catalog)
    _validate_catalog_root(destination_catalog, allow_create=False)
    _recover_catalog_transactions(destination_catalog)
    destination_entries = validate_public_catalog(destination_catalog)
    source_entry = next((entry for entry in source_entries if entry.taxon_id == taxon_id), None)
    destination_entry = next(
        (entry for entry in destination_entries if entry.taxon_id == taxon_id), None
    )
    if source_entry is None:
        raise CatalogPublishError(f"Source catalog does not contain taxon {taxon_id}")
    if destination_entry is None:
        raise CatalogPublishError(f"Destination catalog does not contain taxon {taxon_id}")
    if (
        source_entry.common_name,
        source_entry.scientific_name,
        source_entry.slug,
    ) != (
        destination_entry.common_name,
        destination_entry.scientific_name,
        destination_entry.slug,
    ):
        raise CatalogPublishError("Catalog replacement identity differs from the approved taxon")

    source = source_catalog / "species" / f"{taxon_id}-{source_entry.slug}"
    destination = destination_catalog / "species" / f"{taxon_id}-{destination_entry.slug}"
    destination_manifest = read_json(destination / "manifest.json")
    if not isinstance(destination_manifest, dict):
        raise CatalogPublishError(f"Invalid approved manifest: {destination / 'manifest.json'}")
    migration_history = _catalog_migration_records(
        destination_manifest, destination / "manifest.json"
    )
    replacement_record: dict[str, object] = {
        "reason": replacement_reason,
        "replaces": {
            "approved_at": destination_entry.approved_at,
            "display_sha256": destination_entry.display_sha256,
            "portrait_sha256": destination_entry.portrait_sha256,
        },
    }

    transaction = _new_catalog_transaction(destination_catalog)
    try:
        staged = transaction / "replacement" / source.name
        backup = transaction / "approved" / destination.name
        backup.parent.mkdir()
        shutil.copytree(source, staged)
        staged_manifest_path = staged / "manifest.json"
        staged_manifest = read_json(staged_manifest_path)
        if not isinstance(staged_manifest, dict):
            raise CatalogPublishError(f"Invalid replacement manifest: {staged_manifest_path}")
        if migration_history:
            replacement_record["history"] = [
                _catalog_migration_payload(record) for record in migration_history
            ]
        staged_manifest["catalog_migration"] = replacement_record
        write_json_atomic(staged_manifest_path, staged_manifest)
        _validate_species_directory(staged)
        _validate_catalog_replacement(destination, staged)

        destination.replace(backup)
        staged.replace(destination)
        rebuild_catalog_index(destination_catalog)
        validate_public_catalog(destination_catalog)
    except BaseException as error:
        _restore_catalog_replacements(transaction, destination_catalog / "species")
        rebuild_catalog_index(destination_catalog)
        transaction.mkdir(exist_ok=True)
        raise CatalogPublishError("Catalog replacement failed and was rolled back") from error
    _commit_catalog_transaction(transaction, destination_catalog)

    return {
        "replaced": {
            "taxon_id": source_entry.taxon_id,
            "common_name": source_entry.common_name,
            "scientific_name": source_entry.scientific_name,
            "slug": source_entry.slug,
        },
        "reason": replacement_reason,
        "replaces": replacement_record["replaces"],
    }


def _species_directories(species_root: Path) -> list[Path]:
    return sorted(path for path in species_root.iterdir() if path.name != _FINDER_METADATA)


def _restore_catalog_replacements(transaction: Path, destination_species: Path) -> None:
    backup_root = transaction / "approved"
    if not backup_root.exists():
        return
    if backup_root.is_symlink() or not backup_root.is_dir():
        raise CatalogPublishError(f"Invalid catalog sync backup path: {backup_root}")
    for backup in sorted(backup_root.iterdir()):
        if backup.name == _FINDER_METADATA:
            continue
        _validate_species_directory(backup)
        destination = destination_species / backup.name
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        elif destination.exists():
            shutil.rmtree(destination)
        backup.replace(destination)


def _catalog_transaction_prefix(destination_catalog: Path, *, committed: bool) -> str:
    catalog_digest = hashlib.sha256(str(destination_catalog.resolve()).encode()).hexdigest()[:16]
    state = "committed" if committed else "active"
    return f".catalog-sync-{state}-{catalog_digest}-"


def _catalog_transactions(
    destination_catalog: Path, *, committed: bool, transaction_dir: Path | None = None
) -> list[Path]:
    prefix = _catalog_transaction_prefix(destination_catalog, committed=committed)
    parent = transaction_dir if transaction_dir is not None else destination_catalog.parent
    return sorted(parent.glob(f"{prefix}*"))


def _catalog_transaction_directory(
    source_catalog: Path, destination_catalog: Path, requested: Path | None
) -> Path | None:
    if requested is None:
        return None
    if requested.is_symlink():
        raise CatalogPublishError("Catalog transaction directory must not be a symlink")
    directory = requested.resolve()
    if any(
        directory.is_relative_to(catalog.resolve())
        for catalog in (source_catalog, destination_catalog)
    ):
        raise CatalogPublishError("Catalog transaction directory must be outside both catalogs")
    if directory != destination_catalog.parent.resolve() and any(
        _catalog_transactions(destination_catalog, committed=committed)
        for committed in (False, True)
    ):
        raise CatalogPublishError(
            "Recover existing sibling catalog transactions before changing transaction directory"
        )
    custody = directory if directory.exists() else directory.parent
    try:
        info = custody.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022
            or stat.S_IMODE(info.st_mode) & 0o300 != 0o300
        ):
            raise CatalogPublishError(
                "Catalog transaction directory must be caller-owned, writable "
                "and not shared-writable"
            )
        for ancestor in directory.parents:
            parent_info = ancestor.lstat()
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid not in (0, os.geteuid())
                or (
                    stat.S_IMODE(parent_info.st_mode) & 0o022
                    and not parent_info.st_mode & stat.S_ISVTX
                )
            ):
                raise CatalogPublishError(
                    "Catalog transaction ancestors must not permit path replacement by other users"
                )
        target = destination_catalog
        while not target.exists():
            target = target.parent
        if info.st_dev != target.stat().st_dev:
            raise CatalogPublishError("Catalog transactions require the destination filesystem")
        directory.mkdir(mode=0o700, exist_ok=True)
        created = directory.lstat()
        if (
            not stat.S_ISDIR(created.st_mode)
            or created.st_uid != os.geteuid()
            or stat.S_IMODE(created.st_mode) & 0o022
            or stat.S_IMODE(created.st_mode) & 0o300 != 0o300
            or created.st_dev != target.stat().st_dev
        ):
            raise CatalogPublishError("Catalog transaction directory custody changed")
    except OSError as exc:
        raise CatalogPublishError("Catalog transaction directory is unavailable") from exc
    return directory


def _validate_catalog_transaction(transaction: Path, *, private: bool = False) -> None:
    if transaction.is_symlink() or not transaction.is_dir():
        raise CatalogPublishError(f"Invalid catalog sync transaction path: {transaction}")
    if private:
        info = transaction.lstat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise CatalogPublishError("Catalog transaction is not in caller custody")


def _recover_catalog_transactions(
    destination_catalog: Path, *, transaction_dir: Path | None = None
) -> bool:
    destination_species = destination_catalog / "species"
    committed_transactions = _catalog_transactions(
        destination_catalog, committed=True, transaction_dir=transaction_dir
    )
    for transaction in committed_transactions:
        _validate_catalog_transaction(transaction, private=transaction_dir is not None)
        shutil.rmtree(transaction)
    active_transactions = _catalog_transactions(
        destination_catalog, committed=False, transaction_dir=transaction_dir
    )
    for transaction in active_transactions:
        _validate_catalog_transaction(transaction, private=transaction_dir is not None)
        _restore_catalog_replacements(transaction, destination_species)
    if active_transactions:
        rebuild_catalog_index(destination_catalog)
        validate_public_catalog(destination_catalog)
        for transaction in active_transactions:
            shutil.rmtree(transaction)
    return bool(active_transactions)


def _new_catalog_transaction(
    destination_catalog: Path, *, transaction_dir: Path | None = None
) -> Path:
    return Path(
        mkdtemp(
            prefix=_catalog_transaction_prefix(destination_catalog, committed=False),
            dir=transaction_dir if transaction_dir is not None else destination_catalog.parent,
        )
    )


def _commit_catalog_transaction(transaction: Path, destination_catalog: Path) -> None:
    committed_transaction = transaction.with_name(
        transaction.name.replace(
            _catalog_transaction_prefix(destination_catalog, committed=False),
            _catalog_transaction_prefix(destination_catalog, committed=True),
            1,
        )
    )
    transaction.replace(committed_transaction)
    shutil.rmtree(committed_transaction)


def sync_public_catalog(
    source_catalog: Path,
    destination_catalog: Path,
    *,
    taxon_ids: set[int] | None = None,
    allow_replacements: bool = False,
    retain_independent_approvals: bool = False,
    transaction_dir: Path | None = None,
) -> dict[str, object]:
    source_entries = validate_public_catalog(source_catalog)
    all_source_taxa = {entry.taxon_id for entry in source_entries}
    requested_taxa = all_source_taxa if taxon_ids is None else taxon_ids
    missing_taxa = requested_taxa - all_source_taxa
    if missing_taxa:
        missing = ", ".join(str(taxon_id) for taxon_id in sorted(missing_taxa))
        raise CatalogPublishError(f"Source catalog does not contain taxon {missing}")
    source_by_taxon = {
        entry.taxon_id: entry for entry in source_entries if entry.taxon_id in requested_taxa
    }

    transaction_dir = _catalog_transaction_directory(
        source_catalog, destination_catalog, transaction_dir
    )
    _validate_catalog_root(destination_catalog, allow_create=True)
    destination_species = destination_catalog / "species"
    recovered_transaction = _recover_catalog_transactions(
        destination_catalog, transaction_dir=transaction_dir
    )
    if (destination_catalog / "index.json").exists():
        try:
            validate_public_catalog(destination_catalog)
        except CatalogPublishError:
            if not recovered_transaction:
                raise
            for directory in _species_directories(destination_species):
                _validate_species_directory(directory)
            rebuild_catalog_index(destination_catalog)
            validate_public_catalog(destination_catalog)
    elif _species_directories(destination_species):
        for directory in _species_directories(destination_species):
            _validate_species_directory(directory)
        rebuild_catalog_index(destination_catalog)
        validate_public_catalog(destination_catalog)

    published: list[dict[str, object]] = []
    replaced: list[dict[str, object]] = []
    retained_newer: list[dict[str, object]] = []
    retained_independent: list[dict[str, object]] = []
    existing: list[int] = []

    transaction = _new_catalog_transaction(destination_catalog, transaction_dir=transaction_dir)
    try:
        for taxon_id, entry in sorted(source_by_taxon.items()):
            source = source_catalog / "species" / f"{taxon_id}-{entry.slug}"
            matches = sorted(destination_species.glob(f"{taxon_id}-*"))
            if len(matches) > 1:
                raise CatalogPublishError(
                    f"Catalog contains multiple directories for taxon {taxon_id}"
                )
            if matches:
                destination = matches[0]
                if destination.name != source.name:
                    raise CatalogPublishError(
                        f"Catalog taxon {taxon_id} conflicts with immutable local approval"
                    )
                if not _trees_match(source, destination):
                    retained_entry = {
                        "taxon_id": entry.taxon_id,
                        "common_name": entry.common_name,
                        "scientific_name": entry.scientific_name,
                        "slug": entry.slug,
                    }
                    if not allow_replacements:
                        if retain_independent_approvals and (
                            _catalog_approvals_are_independent(destination, source)
                        ):
                            retained_independent.append(retained_entry)
                            continue
                        raise CatalogPublishError(
                            f"Catalog taxon {taxon_id} conflicts with immutable local approval"
                        )
                    try:
                        should_replace = _catalog_sync_should_replace(destination, source)
                    except CatalogPublishError:
                        if not retain_independent_approvals or not (
                            _catalog_approvals_are_independent(destination, source)
                        ):
                            raise
                        retained_independent.append(retained_entry)
                        continue
                    if not should_replace:
                        retained_newer.append(
                            {
                                "taxon_id": entry.taxon_id,
                                "common_name": entry.common_name,
                                "scientific_name": entry.scientific_name,
                                "slug": entry.slug,
                            }
                        )
                        continue
                    staged = transaction / "replacement" / source.name
                    backup = transaction / "approved" / destination.name
                    backup.parent.mkdir(exist_ok=True)
                    shutil.copytree(source, staged)
                    _validate_species_directory(staged)
                    destination.replace(backup)
                    staged.replace(destination)
                    replaced.append(
                        {
                            "taxon_id": entry.taxon_id,
                            "common_name": entry.common_name,
                            "scientific_name": entry.scientific_name,
                            "slug": entry.slug,
                        }
                    )
                    continue
                existing.append(taxon_id)
                continue
            staged = transaction / source.name
            shutil.copytree(source, staged)
            _validate_species_directory(staged)
            staged.replace(destination_species / source.name)
            published.append(
                {
                    "taxon_id": entry.taxon_id,
                    "common_name": entry.common_name,
                    "scientific_name": entry.scientific_name,
                    "slug": entry.slug,
                }
            )

        rebuild_catalog_index(destination_catalog)
        validate_public_catalog(destination_catalog)
    except BaseException:
        _restore_catalog_replacements(transaction, destination_species)
        rebuild_catalog_index(destination_catalog)
        transaction.mkdir(exist_ok=True)
        raise
    _commit_catalog_transaction(transaction, destination_catalog)
    return {
        "published": published,
        "replaced": replaced,
        "retained_newer": retained_newer,
        "retained_independent": retained_independent,
        "already_present": existing,
    }


def _redact_command_output(value: str) -> str:
    return _CREDENTIALS_IN_URL.sub(r"\1[REDACTED]@", value.strip())


def _command_label(arguments: list[str]) -> str:
    program = Path(arguments[0]).name
    word_count = 1 if program == "git" else 2
    words: list[str] = []
    index = 1
    while index < len(arguments) and len(words) < word_count:
        item = arguments[index]
        if program == "git" and item in {"-C", "-c"}:
            index += 2
            continue
        if item.startswith("-"):
            break
        words.append(item)
        index += 1
    return " ".join([program, *words])


def _run(
    arguments: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    timeout: float = _COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    label = _command_label(arguments)
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=input_text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CatalogPublishError(f"{label} timed out after {timeout:g} seconds") from exc
    except OSError as exc:
        raise CatalogPublishError(f"Unable to run {label}") from exc
    if check and result.returncode != 0:
        detail = _redact_command_output(result.stderr or result.stdout)
        raise CatalogPublishError(f"{label} failed: {detail or 'unknown error'}")
    return result


def _git(
    repository: Path,
    *arguments: str,
    check: bool = True,
    timeout: float = _COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(repository), *arguments], check=check, timeout=timeout)


def _gh(
    publication: PublicCatalogConfig,
    *arguments: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run([str(publication.gh_path), *arguments], input_text=input_text, check=check)


def _remote_repository(remote_url: str) -> str | None:
    match = _GITHUB_REMOTE.fullmatch(remote_url.strip())
    return match.group("repository") if match is not None else None


def _validate_checkout(checkout: Path, publication: PublicCatalogConfig) -> str:
    if checkout.is_symlink() or not checkout.is_dir():
        raise CatalogPublishError(f"Repository checkout does not exist: {checkout}")
    repository = publication.repository
    if repository is None:
        raise CatalogPublishError("Catalog repository is not configured")
    top_level = _git(checkout, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(top_level).resolve() != checkout.resolve():
        raise CatalogPublishError("checkout_dir must be the Git repository root")
    if _GIT_REMOTE_NAME.fullmatch(publication.remote) is None:
        raise CatalogPublishError("Catalog remote has an invalid Git remote name")
    _git(checkout, "check-ref-format", "--branch", publication.base_branch)
    remote_url = _git(checkout, "remote", "get-url", publication.remote).stdout.strip()
    remote_repository = _remote_repository(remote_url)
    if remote_repository is None or remote_repository.casefold() != repository.casefold():
        raise CatalogPublishError("Catalog remote does not match the configured repository")

    owner = repository.split("/", maxsplit=1)[0]
    try:
        authenticated = _gh(publication, "api", "user", "--jq", ".login").stdout.strip()
    except CatalogPublishError as exc:
        auth_status = _gh(
            publication,
            "auth",
            "status",
            "--hostname",
            "github.com",
            check=False,
        )
        if auth_status.returncode != 0:
            detail = _redact_command_output(auth_status.stderr or auth_status.stdout)
            raise CatalogPublishError(
                f"GitHub CLI authentication check failed: {detail or 'unknown error'}"
            ) from exc
        raise
    if authenticated.casefold() != owner.casefold():
        raise CatalogPublishError(f"GitHub CLI must be authenticated as repository owner {owner!r}")
    return repository


def _commit_message(published: list[dict[str, object]]) -> str:
    if len(published) == 1:
        return f"Publish {published[0]['common_name']}"
    if published:
        return f"Publish {len(published)} bird plates"
    return "Rebuild catalog index"


def _catalog_digest(catalog_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in catalog_dir.rglob("*") if item.is_file()):
        relative = path.relative_to(catalog_dir).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _catalog_changes(worktree: Path, *diff_arguments: str) -> list[tuple[str, str]]:
    # NUL-separated, unquoted paths keep non-ASCII species slugs byte-exact.
    output = _git(
        worktree,
        "-c",
        "core.quotePath=false",
        "diff",
        "--name-status",
        "--no-renames",
        "-z",
        *diff_arguments,
    ).stdout
    fields = output.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2 != 0:
        raise CatalogPublishError("Unable to parse staged catalog changes")
    return list(zip(fields[0::2], fields[1::2], strict=True))


def _validate_catalog_changes(
    changes: list[tuple[str, str]],
    published: list[dict[str, object]],
) -> list[str]:
    expected_directories = {
        f"catalog/species/{item['taxon_id']}-{item['slug']}/"
        for item in published
        if isinstance(item.get("taxon_id"), int) and isinstance(item.get("slug"), str)
    }
    paths: list[str] = []
    for status, path in changes:
        paths.append(path)
        if path == "catalog/index.json":
            if status not in {"A", "M"}:
                raise CatalogPublishError("Catalog index may only be added or modified")
            continue
        if status != "A" or not any(path.startswith(prefix) for prefix in expected_directories):
            raise CatalogPublishError(f"Publication attempted an unexpected change: {path}")
        if Path(path).name not in _ALLOWED_SPECIES_FILES:
            raise CatalogPublishError(f"Publication attempted an unexpected species file: {path}")
    if published and "catalog/index.json" not in paths:
        raise CatalogPublishError("Catalog publication did not update catalog/index.json")
    return paths


def _validate_staged_catalog(
    worktree: Path,
    published: list[dict[str, object]],
) -> list[str]:
    return _validate_catalog_changes(_catalog_changes(worktree, "--cached"), published)


def _validate_publication_commit(
    worktree: Path,
    commit: str,
    *,
    base_commit: str,
    staged_tree: str,
    staged_paths: list[str],
    published: list[dict[str, object]],
) -> None:
    """Check the commit that will be pushed, not only the index it was made from."""
    paths = _validate_catalog_changes(_catalog_changes(worktree, base_commit, commit), published)
    if sorted(paths) != sorted(staged_paths):
        raise CatalogPublishError("Publication commit does not match the validated changes")
    parents = _git(worktree, "rev-list", "--parents", "-n", "1", commit).stdout.split()
    tree = _git(worktree, "rev-parse", f"{commit}^{{tree}}").stdout.strip()
    if parents != [commit, base_commit] or tree != staged_tree:
        raise CatalogPublishError("Publication commit does not match the validated changes")


def _remote_catalog_species(checkout: Path, remote_ref: str) -> list[dict[str, object]] | None:
    shown = _git(checkout, "show", f"{remote_ref}:catalog/index.json", check=False)
    if shown.returncode != 0:
        return None
    try:
        index = json.loads(shown.stdout)
    except json.JSONDecodeError:
        return None
    species = index.get("species") if isinstance(index, dict) else None
    if not isinstance(species, list) or any(not isinstance(item, dict) for item in species):
        return None
    return cast(list[dict[str, object]], species)


def _published_directory_names(remote_species: list[dict[str, object]] | None) -> set[str]:
    return {
        f"{item.get('taxon_id')}-{item.get('slug')}"
        for item in (remote_species or [])
        if isinstance(item.get("taxon_id"), int) and isinstance(item.get("slug"), str)
    }


def _already_published_taxa(
    catalog_dir: Path, remote_species: list[dict[str, object]] | None
) -> list[int] | None:
    """Return the local taxa when the remote index already holds every local approval."""
    if remote_species is None:
        return None
    try:
        # A damaged local approval must take the snapshot path, which reports it.
        validate_public_catalog(catalog_dir)
        local_index = read_json(catalog_dir / "index.json")
        directories = {path.name for path in _species_directories(catalog_dir / "species")}
    except (CatalogError, CatalogPublishError, OSError):
        return None
    local_species = local_index.get("species") if isinstance(local_index, dict) else None
    if not isinstance(local_species, list):
        return None
    if not directories <= _published_directory_names(remote_species) or any(
        item not in remote_species for item in local_species
    ):
        return None
    taxa = [item.get("taxon_id") for item in local_species if isinstance(item, dict)]
    if len(taxa) != len(local_species) or any(
        not isinstance(taxon_id, int) or isinstance(taxon_id, bool) for taxon_id in taxa
    ):
        return None
    return sorted(cast(list[int], taxa))


def _exclude_unpublishable_species(
    snapshot: Path,
    published_names: set[str],
    private_patterns: tuple[re.Pattern[str], ...],
) -> list[dict[str, str]]:
    """Drop local approvals that fail publication gates so the rest can still publish."""
    species_root = snapshot / "species"
    if species_root.is_symlink() or not species_root.is_dir():
        return []
    skipped: list[tuple[Path, dict[str, str]]] = []
    for directory in _species_directories(species_root):
        patterns = () if directory.name in published_names else private_patterns
        try:
            _validate_species_directory(directory, private_patterns=patterns)
        except (CatalogError, CatalogPublishError) as exc:
            reason = str(exc).replace(f"{snapshot}{os.sep}", "")
            skipped.append((directory, {"species": directory.name, "reason": reason}))
    for directory, _ in skipped:
        if directory.is_symlink() or not directory.is_dir():
            directory.unlink()
        else:
            shutil.rmtree(directory)
    if skipped:
        rebuild_catalog_index(snapshot)
    return [report for _, report in skipped]


def _cleanup_stale_publications(
    checkout: Path,
    publication: PublicCatalogConfig,
    repository: str,
    *,
    keep: str | None,
) -> list[str]:
    """Close the owner's abandoned publication PRs and delete their branches."""
    heads = _git(
        checkout,
        "ls-remote",
        "--heads",
        publication.remote,
        "refs/heads/catalog/publish-*",
    ).stdout
    branches: set[str] = set()
    for line in heads.splitlines():
        _, _, ref = line.partition("\t")
        name = ref.removeprefix("refs/heads/")
        if _PUBLICATION_BRANCH.fullmatch(name) is not None and name != keep:
            branches.add(name)

    raw = _gh(
        publication,
        "pr",
        "list",
        "--repo",
        repository,
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "url,headRefName,isCrossRepository,author",
    ).stdout
    try:
        pull_requests = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CatalogPublishError("GitHub CLI returned invalid pull request data") from exc
    if not isinstance(pull_requests, list) or any(
        not isinstance(item, dict) for item in pull_requests
    ):
        raise CatalogPublishError("GitHub CLI returned invalid pull request data")

    owner = repository.split("/", maxsplit=1)[0].casefold()
    owner_pull_requests: list[tuple[str, str]] = []
    protected: set[str] = set()
    for item in cast(list[dict[str, object]], pull_requests):
        head = item.get("headRefName")
        if (
            not isinstance(head, str)
            or _PUBLICATION_BRANCH.fullmatch(head) is None
            or head == keep
            or item.get("isCrossRepository") is not False
        ):
            continue
        author = item.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        url = item.get("url")
        if isinstance(login, str) and login.casefold() == owner and isinstance(url, str):
            owner_pull_requests.append((head, url))
        else:
            protected.add(head)

    cleaned: set[str] = set()
    for head, url in owner_pull_requests:
        if head in protected:
            continue
        _gh(publication, "pr", "close", url, "--repo", repository, "--delete-branch")
        cleaned.add(head)
    for name in sorted(branches - protected - cleaned):
        _git(
            checkout,
            "push",
            "--no-verify",
            publication.remote,
            "--delete",
            f"refs/heads/{name}",
            timeout=_TRANSFER_TIMEOUT_SECONDS,
        )
        cleaned.add(name)
    return sorted(cleaned)


def _pull_request(
    publication: PublicCatalogConfig,
    repository: str,
    branch: str,
    *,
    branch_existed: bool,
    commit: str,
) -> dict[str, object] | None:
    """Return the PR to reuse for this branch, or None when a new one is needed."""
    raw = _gh(
        publication,
        "pr",
        "list",
        "--repo",
        repository,
        "--head",
        branch,
        "--state",
        "all",
        "--limit",
        "20",
        "--json",
        "number,url,state,headRefOid",
    ).stdout
    try:
        pull_requests = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CatalogPublishError("GitHub CLI returned invalid pull request data") from exc
    if not isinstance(pull_requests, list) or any(
        not isinstance(item, dict) for item in pull_requests
    ):
        raise CatalogPublishError("GitHub CLI returned invalid pull request data")
    typed = cast(list[dict[str, object]], pull_requests)
    open_pull = next((item for item in typed if item.get("state") == "OPEN"), None)
    if open_pull is not None:
        return open_pull
    # The branch name repeats when the catalog returns to an earlier state on the same base.
    # A closed PR is current only when the branch survived and still points at its head;
    # one for a deleted or since-recreated branch, for example after stale cleanup, is past.
    if not branch_existed:
        return None
    return next((item for item in typed if item.get("headRefOid") == commit), None)


def _create_pull_request(
    publication: PublicCatalogConfig,
    repository: str,
    branch: str,
    title: str,
    published: list[dict[str, object]],
) -> str:
    birds = "\n".join(f"- {item['common_name']} ({item['scientific_name']})" for item in published)
    body = (
        "Automated catalog publication from the trusted controller.\n\n"
        "Validated additions:\n"
        f"{birds}\n\n"
        "Only immutable, location-neutral files under `catalog/` are included.\n"
    )
    return _gh(
        publication,
        "pr",
        "create",
        "--repo",
        repository,
        "--base",
        publication.base_branch,
        "--head",
        branch,
        "--title",
        title,
        "--body-file",
        "-",
        input_text=body,
    ).stdout.strip()


def run_catalog_publish(config: AppConfig, *, dry_run: bool = False) -> dict[str, object]:
    publication = config.public_catalog
    if not publication.enabled:
        raise CatalogPublishError("Catalog publishing is disabled")
    checkout = publication.checkout_dir
    if checkout is None:
        raise CatalogPublishError("Catalog checkout_dir is not configured")

    with exclusive_publish_lock(config.controller.state_dir):
        work_parent = config.controller.state_dir / "catalog-publish-work"
        work_parent.mkdir(parents=True, exist_ok=True)
        abandoned_work_directories = len(_abandoned_publish_work(work_parent))
        cleaned_work_directories = (
            0 if dry_run else _cleanup_abandoned_publish_work_locked(work_parent)
        )
        repository = _validate_checkout(checkout, publication)
        remote_ref = f"refs/remotes/{publication.remote}/{publication.base_branch}"
        _git(
            checkout,
            "fetch",
            "--prune",
            publication.remote,
            f"+refs/heads/{publication.base_branch}:{remote_ref}",
            timeout=_TRANSFER_TIMEOUT_SECONDS,
        )
        _git(checkout, "worktree", "prune")
        remote_species = _remote_catalog_species(checkout, remote_ref)
        result: dict[str, object] = {
            "published": [],
            "replaced": [],
            "retained_newer": [],
            "retained_independent": [],
            "already_present": [],
            "skipped": [],
            "changed": False,
            "dry_run": dry_run,
            "pushed": False,
            "merged": False,
            "commit": None,
            "pull_request": None,
            "abandoned_work_directories": abandoned_work_directories,
            "cleaned_work_directories": cleaned_work_directories,
            "cleaned_publication_branches": [],
        }
        with catalog_state_lock(config.controller.state_dir):
            already_published = _already_published_taxa(
                config.controller.catalog_dir, remote_species
            )
        if already_published is not None:
            result["already_present"] = already_published
            if not dry_run:
                result["cleaned_publication_branches"] = _cleanup_stale_publications(
                    checkout, publication, repository, keep=None
                )
            return result

        with TemporaryDirectory(prefix="publish-", dir=work_parent) as temporary:
            source_snapshot = Path(temporary) / "source-catalog"
            with catalog_state_lock(config.controller.state_dir):
                # Approval staging debris would fail the snapshot root check.
                clear_catalog_staging(config.controller.catalog_dir)
                try:
                    shutil.copytree(
                        config.controller.catalog_dir,
                        source_snapshot,
                        symlinks=True,
                    )
                except FileNotFoundError as exc:
                    raise CatalogPublishError("Approved local catalog does not exist") from exc
            result["skipped"] = _exclude_unpublishable_species(
                source_snapshot,
                _published_directory_names(remote_species),
                publication_privacy_patterns(config),
            )
            validate_public_catalog(source_snapshot)

            worktree = Path(temporary) / "checkout"
            _git(
                checkout,
                "worktree",
                "add",
                "--detach",
                str(worktree),
                remote_ref,
                timeout=_CHECKOUT_TIMEOUT_SECONDS,
            )
            try:
                return _publish_from_worktree(
                    checkout,
                    worktree,
                    source_snapshot,
                    publication,
                    repository,
                    result,
                    dry_run=dry_run,
                )
            finally:
                # Cleanup is best effort; the next run prunes, and it must not mask the outcome.
                with suppress(CatalogPublishError):
                    _git(
                        checkout,
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                        check=False,
                        timeout=_CHECKOUT_TIMEOUT_SECONDS,
                    )
                with suppress(CatalogPublishError):
                    _git(checkout, "worktree", "prune", check=False)


def _publish_from_worktree(
    checkout: Path,
    worktree: Path,
    source_snapshot: Path,
    publication: PublicCatalogConfig,
    repository: str,
    result: dict[str, object],
    *,
    dry_run: bool,
) -> dict[str, object]:
    sync = sync_public_catalog(source_snapshot, worktree / "catalog")
    published = sync["published"]
    if not isinstance(published, list) or any(not isinstance(item, dict) for item in published):
        raise CatalogPublishError("Publisher produced an invalid change summary")
    typed_published = cast(list[dict[str, object]], published)
    changed = bool(
        _git(
            worktree,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "catalog",
        ).stdout.strip()
    )
    result.update(sync)
    result["changed"] = changed
    if not changed:
        return result
    if not typed_published:
        raise CatalogPublishError("Catalog changed without adding an approved species")

    _git(worktree, "add", "--", "catalog")
    _git(worktree, "diff", "--cached", "--check")
    staged_paths = _validate_staged_catalog(worktree, typed_published)
    result["paths"] = staged_paths
    if dry_run:
        return result

    base_commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    staged_tree = _git(worktree, "write-tree").stdout.strip()
    branch = f"catalog/publish-{_catalog_digest(source_snapshot)[:8]}-{base_commit[:8]}"
    result["cleaned_publication_branches"] = _cleanup_stale_publications(
        checkout, publication, repository, keep=branch
    )
    remote_branch = _git(
        checkout,
        "ls-remote",
        "--heads",
        publication.remote,
        f"refs/heads/{branch}",
    ).stdout.strip()
    if remote_branch:
        commit = remote_branch.split(maxsplit=1)[0]
        branch_ref = f"refs/remotes/{publication.remote}/catalog-publication"
        _git(
            checkout,
            "fetch",
            publication.remote,
            f"+refs/heads/{branch}:{branch_ref}",
            timeout=_TRANSFER_TIMEOUT_SECONDS,
        )
        remote_tree = _git(checkout, "rev-parse", f"{branch_ref}^{{tree}}").stdout.strip()
        if remote_tree != staged_tree:
            raise CatalogPublishError(
                f"Existing publication branch {branch!r} has unexpected content"
            )
    else:
        _git(
            worktree,
            "-c",
            f"user.name={publication.commit_name}",
            "-c",
            f"user.email={publication.commit_email}",
            # Checkout hooks and signing settings must not change or block the validated tree.
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            _commit_message(typed_published),
        )
        commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    _validate_publication_commit(
        worktree,
        commit,
        base_commit=base_commit,
        staged_tree=staged_tree,
        staged_paths=staged_paths,
        published=typed_published,
    )
    if not remote_branch:
        _git(
            worktree,
            "push",
            "--no-verify",
            publication.remote,
            f"{commit}:refs/heads/{branch}",
            timeout=_TRANSFER_TIMEOUT_SECONDS,
        )
    result["pushed"] = True
    result["commit"] = commit
    result["branch"] = branch

    pull_request = _pull_request(
        publication, repository, branch, branch_existed=bool(remote_branch), commit=commit
    )
    if pull_request is None:
        pull_request_url = _create_pull_request(
            publication,
            repository,
            branch,
            _commit_message(typed_published),
            typed_published,
        )
    else:
        existing_url = pull_request.get("url")
        if not isinstance(existing_url, str):
            raise CatalogPublishError("Existing publication PR has no URL")
        pull_request_url = existing_url
        if pull_request.get("state") == "CLOSED":
            raise CatalogPublishError(
                "Existing publication PR was closed without merge; "
                f"delete branch {branch} to publish it again"
            )
        head_sha = pull_request.get("headRefOid")
        if head_sha != commit:
            raise CatalogPublishError("Existing publication PR has an unexpected head")
    result["pull_request"] = pull_request_url

    _gh(
        publication,
        "pr",
        "merge",
        pull_request_url,
        "--repo",
        repository,
        "--admin",
        "--squash",
        "--delete-branch",
        "--match-head-commit",
        commit,
    )
    state = _gh(
        publication,
        "pr",
        "view",
        pull_request_url,
        "--repo",
        repository,
        "--json",
        "state",
        "--jq",
        ".state",
    ).stdout.strip()
    if state != "MERGED":
        raise CatalogPublishError(f"Catalog pull request did not merge: {state}")
    result["merged"] = True
    return result
