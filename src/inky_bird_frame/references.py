"""Licensed iNaturalist reference-photo acquisition."""

from __future__ import annotations

import hashlib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit

from .errors import DataSourceError, InsufficientReferencesError, MissingDependencyError
from .http import get_bytes, get_json, write_bytes_atomic
from .models import ReferencePhoto

ALLOWED_LICENSES: Final = ("cc0", "cc-by")
MIN_REFERENCE_EDGE: Final = 800
REFERENCE_FIELDS: Final = (
    "id",
    "uri",
    "user.login",
    "photos.id",
    "photos.attribution",
    "photos.license_code",
    "photos.url",
    "photos.original_dimensions.width",
    "photos.original_dimensions.height",
)


@dataclass(frozen=True)
class ReferenceCandidate:
    photo_id: int
    observation_id: int
    observer: str
    attribution: str
    license_code: str
    source_url: str
    image_url: str
    width: int
    height: int


class ReferenceCandidates(list[ReferenceCandidate]):
    """The selected candidates, plus further distinct-observer spares in rank order.

    Download falls back to a spare when a selected photo cannot be fetched or
    decoded, so one bad photo does not fail the species.
    """

    def __init__(
        self,
        selected: list[ReferenceCandidate],
        spares: list[ReferenceCandidate] | tuple[ReferenceCandidate, ...] = (),
    ) -> None:
        super().__init__(selected)
        self.spares = tuple(spares)


def photo_url_for_size(url: str, size: str = "large") -> str:
    if size not in {"small", "medium", "large", "original"}:
        raise ValueError(f"Unsupported iNaturalist photo size: {size}")
    parsed = urlsplit(url)
    filename = Path(parsed.path).name
    if "." not in filename:
        raise DataSourceError(f"Unexpected iNaturalist photo URL: {url}")
    suffix = filename.rsplit(".", 1)[1]
    parent = str(Path(parsed.path).parent)
    return urlunsplit((parsed.scheme, parsed.netloc, f"{parent}/{size}.{suffix}", parsed.query, ""))


def _parse_candidate(observation: object) -> list[ReferenceCandidate]:
    if not isinstance(observation, dict):
        return []
    observation_id = observation.get("id")
    source_url = observation.get("uri")
    user = observation.get("user")
    photos = observation.get("photos")
    if (
        not isinstance(observation_id, int)
        or not isinstance(source_url, str)
        or not isinstance(user, dict)
        or not isinstance(photos, list)
    ):
        return []
    observer = user.get("login")
    if not isinstance(observer, str):
        return []

    candidates: list[ReferenceCandidate] = []
    for photo in photos:
        if not isinstance(photo, dict):
            continue
        photo_id = photo.get("id")
        attribution = photo.get("attribution")
        license_code = photo.get("license_code")
        image_url = photo.get("url")
        dimensions = photo.get("original_dimensions")
        if (
            not isinstance(photo_id, int)
            or not isinstance(attribution, str)
            or not isinstance(license_code, str)
            or license_code.lower() not in ALLOWED_LICENSES
            or not isinstance(image_url, str)
            or not isinstance(dimensions, dict)
        ):
            continue
        width = dimensions.get("width")
        height = dimensions.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            continue
        if min(width, height) < MIN_REFERENCE_EDGE:
            continue
        candidates.append(
            ReferenceCandidate(
                photo_id=photo_id,
                observation_id=observation_id,
                observer=observer,
                attribution=attribution,
                license_code=license_code.lower(),
                source_url=source_url,
                image_url=photo_url_for_size(image_url),
                width=width,
                height=height,
            )
        )
    return candidates


def parse_reference_candidates(payload: object, count: int) -> ReferenceCandidates:
    if not isinstance(payload, dict):
        raise DataSourceError("iNaturalist observations response was not an object")
    results = payload.get("results")
    if not isinstance(results, list):
        raise DataSourceError("iNaturalist observations response did not include results")

    selected: list[ReferenceCandidate] = []
    observers: set[str] = set()
    for observation in results:
        candidates = _parse_candidate(observation)
        candidate = next((item for item in candidates if item.observer not in observers), None)
        if candidate is None:
            continue
        selected.append(candidate)
        observers.add(candidate.observer)

    if len(selected) < count:
        raise InsufficientReferencesError(
            f"Only {len(selected)} suitable licensed reference photos were found; {count} required"
        )
    return ReferenceCandidates(selected[:count], selected[count:])


def fetch_reference_candidates(
    taxon_id: int,
    count: int,
    timeout_seconds: float = 20.0,
) -> ReferenceCandidates:
    if taxon_id <= 0:
        raise ValueError("taxon_id must be greater than zero")
    if count <= 0:
        raise ValueError("count must be greater than zero")
    params = urlencode(
        {
            "taxon_id": str(taxon_id),
            "photos": "true",
            "quality_grade": "research",
            "photo_licensed": "true",
            "photo_license": ",".join(ALLOWED_LICENSES),
            "order_by": "votes",
            "order": "desc",
            "per_page": str(max(30, count * 10)),
            "fields": ",".join(REFERENCE_FIELDS),
        }
    )
    payload = get_json(f"https://api.inaturalist.org/v2/observations?{params}", timeout_seconds)
    return parse_reference_candidates(payload, count)


def _validate_image(path: Path) -> None:
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise MissingDependencyError("Pillow is required to validate reference images") from exc
    try:
        with warnings.catch_warnings():
            # A reference photo is never large enough to warrant Pillow's
            # decompression-bomb warning; treat it as invalid.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.verify()
            # verify() checks structure only; a full decode rejects truncated
            # or undecodable pixel data.
            with Image.open(path) as image:
                image.load()
    # Pillow reports malformed input with many exception types (SyntaxError,
    # struct.error, ValueError, DecompressionBombError, ...).
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise DataSourceError(f"Downloaded reference is not a valid image: {path.name}") from exc


PHOTO_GONE_STATUSES: Final = frozenset({404, 410})


def _photo_specific(error: DataSourceError) -> bool:
    """Whether a download failure concerns this photo rather than the photo host.

    The downloader raises its own size and URL refusals without a cause, and
    wraps every transport failure in its urllib or socket cause.
    """
    cause = error.__cause__
    if isinstance(cause, HTTPError):
        return cause.code in PHOTO_GONE_STATUSES
    return cause is None


def download_references(
    candidates: list[ReferenceCandidate],
    destination: Path,
    timeout_seconds: float = 30.0,
) -> list[ReferencePhoto]:
    destination.mkdir(parents=True, exist_ok=True)
    spares = list(candidates.spares) if isinstance(candidates, ReferenceCandidates) else []
    # At most one spare per selected photo, so a run of bad photos cannot turn
    # into dozens of slow downloads.
    fallbacks = len(candidates)
    references: list[ReferencePhoto] = []
    download_failure: DataSourceError | None = None
    invalid = 0

    def next_spare() -> ReferenceCandidate | None:
        nonlocal fallbacks
        if not spares or fallbacks <= 0:
            return None
        fallbacks -= 1
        return spares.pop(0)

    for index, selected in enumerate(candidates, start=1):
        candidate: ReferenceCandidate | None = selected
        while candidate is not None:
            suffix = Path(urlsplit(candidate.image_url).path).suffix.lower() or ".jpg"
            filename = f"{index:02d}-{candidate.photo_id}{suffix}"
            path = destination / filename
            try:
                content = get_bytes(candidate.image_url, timeout_seconds)
            except DataSourceError as exc:
                # Rate limits and outages affect every photo; stop so the
                # job's normal retry backoff applies.
                if not _photo_specific(exc):
                    raise
                download_failure = exc
                candidate = next_spare()
                continue
            write_bytes_atomic(path, content)
            try:
                _validate_image(path)
            except DataSourceError:
                invalid += 1
                candidate = next_spare()
                continue
            references.append(
                ReferencePhoto(
                    photo_id=candidate.photo_id,
                    observation_id=candidate.observation_id,
                    observer=candidate.observer,
                    attribution=candidate.attribution,
                    license_code=candidate.license_code,
                    source_url=candidate.source_url,
                    image_url=candidate.image_url,
                    width=candidate.width,
                    height=candidate.height,
                    filename=filename,
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
            break
    if len(references) < len(candidates):
        message = f"Only {len(references)} of {len(candidates)} reference photos could be used"
        if download_failure is not None:
            raise DataSourceError(f"{message}; last download error: {download_failure}") from (
                download_failure
            )
        raise InsufficientReferencesError(f"{message}; {invalid} were not valid images")
    return references
