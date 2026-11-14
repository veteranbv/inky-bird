"""Small derived copies of approved plates for owner grids.

Thumbnails are rebuildable and live under the state directory, keyed by the
approved portrait checksum. The approved catalog is never written.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from .errors import CatalogError, MissingDependencyError
from .http import write_bytes_atomic
from .journal import JournalError, JournalService

THUMBNAIL_SIZE: Final = (480, 640)
THUMBNAIL_QUALITY: Final = 85
CACHE_DIRECTORY: Final = "plate-thumbnails"
# Pillow format, Pillow feature name, file extension and media type; first supported wins.
_ENCODERS: Final = (
    ("JPEG", "jpg", "jpg", "image/jpeg"),
    ("WEBP", "webp", "webp", "image/webp"),
)


@dataclass(frozen=True)
class Thumbnail:
    body: bytes
    media_type: str
    portrait_sha256: str


def _encoder() -> tuple[str, str, str]:
    from PIL import features

    for name, feature, extension, media_type in _ENCODERS:
        if features.check(feature):
            return name, extension, media_type
    raise MissingDependencyError("Pillow needs JPEG or WebP support to make plate thumbnails")


def _encode(portrait: bytes, name: str) -> bytes:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(portrait)) as source:
            source.load()
            image = source.convert("RGB")
        image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format=name, quality=THUMBNAIL_QUALITY)
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise CatalogError("This approved plate could not be read to make a thumbnail") from exc
    return output.getvalue()


class PlateThumbnails:
    def __init__(self, state_dir: Path, journal: JournalService) -> None:
        self.directory = state_dir / CACHE_DIRECTORY
        self.journal = journal
        # Writing and pruning share one lock so pruning never removes a
        # temporary file that another thread is about to rename into place.
        self._lock = threading.Lock()

    def _catalog_hashes(self) -> dict[int, str]:
        return {
            cast(int, entry["taxon_id"]): cast(str, entry["portrait_sha256"])
            for entry in self.journal.catalog()
        }

    def cached(self, taxon_id: int) -> Thumbnail | None:
        digest = self._catalog_hashes().get(taxon_id)
        if digest is None:
            raise JournalError("This species has no approved plate in the local catalog")
        _, extension, media_type = _encoder()
        try:
            body = (self.directory / f"{digest}.{extension}").read_bytes()
        except FileNotFoundError:
            return None
        return Thumbnail(body, media_type, digest)

    def generate(self, taxon_id: int) -> Thumbnail:
        existing = self.cached(taxon_id)
        if existing is not None:
            return existing
        name, extension, media_type = _encoder()
        portrait = self.journal.portrait(taxon_id)
        digest = hashlib.sha256(portrait).hexdigest()
        body = _encode(portrait, name)
        try:
            with self._lock:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                write_bytes_atomic(self.directory / f"{digest}.{extension}", body, mode=0o600)
                current = set(self._catalog_hashes().values()) | {digest}
                keep = {f"{value}.{extension}" for value in current}
                for path in self.directory.iterdir():
                    if path.name not in keep and path.is_file():
                        path.unlink(missing_ok=True)
        except OSError:
            # The thumbnail is still correct; only the rebuildable cache failed.
            logging.getLogger(__name__).warning(
                "Plate thumbnail cache could not be updated; check the state directory"
            )
        return Thumbnail(body, media_type, digest)
