"""The fixed species panel and its frozen reference photos."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.codex_runner import parse_species_profile
from inky_bird_frame.errors import GenerationError
from inky_bird_frame.models import ReferencePhoto, SpeciesProfileData
from inky_bird_frame.references import ReferenceCandidate, download_references

from .config import PACKAGE_DIR, HarnessError
from .rundir import Run, read_object, sha256_file, staging_directory, write_json

# "catalog" marks an approved catalog species loaded only for calibration anchors.
Split = Literal["dev", "holdout", "catalog"]
CATALOG_STRATUM: Final = "catalog"
STRATA: Final = (
    "perching-songbird",
    "swimmer",
    "clinger",
    "wader",
    "raptor",
    "long-billed",
    "aerial",
    "ground",
    "tiny",
    "large",
)
DEFAULT_PANEL: Final = PACKAGE_DIR / "panel.json"
MIN_REFERENCES: Final = 2

Downloader = Callable[[list[ReferenceCandidate], Path], list[ReferencePhoto]]


@dataclass(frozen=True)
class PanelSpecies:
    taxon_id: int
    common_name: str
    scientific_name: str
    stratum: str
    split: Split
    catalog_dir: Path
    profile: SpeciesProfileData
    catalog_references: tuple[ReferencePhoto, ...]

    @property
    def key(self) -> str:
        return str(self.taxon_id)

    @property
    def portrait_path(self) -> Path:
        return self.catalog_dir / "portrait.png"

    def bird(self) -> BirdSpecies:
        return BirdSpecies(self.taxon_id, self.common_name, self.scientific_name, 1, "panel")


@dataclass(frozen=True)
class FrozenReferences:
    directory: Path
    photos: tuple[ReferencePhoto, ...]

    @property
    def paths(self) -> list[Path]:
        return [self.directory / photo.filename for photo in self.photos]


def _reference(raw: object) -> ReferencePhoto:
    if not isinstance(raw, dict):
        raise HarnessError("A catalog reference entry must be an object")
    try:
        return ReferencePhoto(
            photo_id=int(raw["photo_id"]),
            observation_id=int(raw["observation_id"]),
            observer=str(raw["observer"]),
            attribution=str(raw["attribution"]),
            license_code=str(raw["license_code"]),
            source_url=str(raw["source_url"]),
            image_url=str(raw["image_url"]),
            width=int(raw["width"]),
            height=int(raw["height"]),
            filename=str(raw["filename"]),
            sha256=str(raw["sha256"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HarnessError("A catalog reference entry is incomplete") from exc


def _approved_index(catalog: Path) -> dict[int, dict[str, object]]:
    index = read_object(catalog / "index.json")
    listed = index.get("species")
    if not isinstance(listed, list):
        raise HarnessError("catalog/index.json has no species list")
    approved: dict[int, dict[str, object]] = {}
    for item in listed:
        if isinstance(item, dict):
            indexed_id = item.get("taxon_id")
            if isinstance(indexed_id, int):
                approved[indexed_id] = item
    return approved


def _catalog_species(
    catalog: Path,
    approved: dict[int, dict[str, object]],
    taxon_id: int,
    stratum: str,
    split: Split,
    name: object = None,
) -> PanelSpecies:
    """One approved catalog species, checked against its manifest."""
    indexed = approved.get(taxon_id)
    if indexed is None:
        raise HarnessError(f"Species {taxon_id} is not in the approved catalog")
    if name is not None and indexed.get("common_name") != name:
        raise HarnessError(f"Panel species {taxon_id} name does not match the catalog")
    portrait = indexed.get("portrait_path")
    if not isinstance(portrait, str):
        raise HarnessError(f"Catalog entry {taxon_id} has no portrait path")
    species_dir = (catalog / portrait).parent
    manifest = read_object(species_dir / "manifest.json")
    if manifest.get("status") != "approved":
        raise HarnessError(f"Catalog species {taxon_id} is not approved")
    try:
        profile = parse_species_profile(manifest.get("profile"))
    except GenerationError as exc:
        raise HarnessError(f"Catalog profile {taxon_id} is not valid: {exc}") from exc
    if profile["taxon_id"] != taxon_id:
        raise HarnessError(f"Catalog profile {taxon_id} names a different taxon")
    raw_references = manifest.get("references")
    if not isinstance(raw_references, list) or len(raw_references) < MIN_REFERENCES:
        raise HarnessError(f"Catalog species {taxon_id} has too few reference photos")
    return PanelSpecies(
        taxon_id=taxon_id,
        common_name=str(indexed.get("common_name")),
        scientific_name=str(indexed.get("scientific_name")),
        stratum=stratum,
        split=split,
        catalog_dir=species_dir,
        profile=profile,
        catalog_references=tuple(_reference(item) for item in raw_references),
    )


def load_panel(catalog: Path, panel_path: Path = DEFAULT_PANEL) -> tuple[PanelSpecies, ...]:
    """Load the panel and check every entry against the committed catalog."""
    approved = _approved_index(catalog)
    entries = read_object(panel_path).get("species")
    if not isinstance(entries, list) or not entries:
        raise HarnessError("The panel lists no species")
    panel: list[PanelSpecies] = []
    seen: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise HarnessError("Each panel entry must be an object")
        taxon_id, name = entry.get("taxon_id"), entry.get("common_name")
        stratum, split = entry.get("stratum"), entry.get("split")
        if not isinstance(taxon_id, int) or taxon_id in seen:
            raise HarnessError(f"Panel taxon ids must be unique integers: {taxon_id}")
        seen.add(taxon_id)
        if stratum not in STRATA:
            raise HarnessError(f"Panel species {taxon_id} has an unknown stratum")
        if split not in ("dev", "holdout"):
            raise HarnessError(f"Panel species {taxon_id} must be split dev or holdout")
        panel.append(
            _catalog_species(
                catalog,
                approved,
                taxon_id,
                str(stratum),
                "dev" if split == "dev" else "holdout",
                name,
            )
        )
    return tuple(panel)


def resolve_species(
    catalog: Path, panel: Sequence[PanelSpecies], taxa: Sequence[int]
) -> tuple[PanelSpecies, ...]:
    """Each taxon as its panel entry, or else as an approved catalog species off the panel."""
    on_panel = {entry.taxon_id: entry for entry in panel}
    approved: dict[int, dict[str, object]] | None = None
    found: list[PanelSpecies] = []
    for taxon in dict.fromkeys(taxa):
        if taxon in on_panel:
            found.append(on_panel[taxon])
            continue
        approved = _approved_index(catalog) if approved is None else approved
        found.append(_catalog_species(catalog, approved, taxon, CATALOG_STRATUM, "catalog"))
    return tuple(found)


def select(
    panel: Sequence[PanelSpecies],
    *,
    split: str = "all",
    taxa: Sequence[int] = (),
) -> tuple[PanelSpecies, ...]:
    chosen = [item for item in panel if split == "all" or item.split == split]
    if taxa:
        wanted = set(taxa)
        unknown = wanted - {item.taxon_id for item in panel}
        if unknown:
            raise HarnessError(f"Not on the panel: {sorted(unknown)[0]}")
        chosen = [item for item in chosen if item.taxon_id in wanted]
    if not chosen:
        raise HarnessError("No panel species match the selection")
    return tuple(chosen)


def _candidate(photo: ReferencePhoto) -> ReferenceCandidate:
    return ReferenceCandidate(
        photo_id=photo.photo_id,
        observation_id=photo.observation_id,
        observer=photo.observer,
        attribution=photo.attribution,
        license_code=photo.license_code,
        source_url=photo.source_url,
        image_url=photo.image_url,
        width=photo.width,
        height=photo.height,
    )


def _load_frozen(directory: Path) -> FrozenReferences:
    manifest = read_object(directory / "manifest.json")
    raw = manifest.get("photos")
    if not isinstance(raw, list) or not raw:
        raise HarnessError("A frozen reference manifest lists no photos")
    photos = tuple(_reference(item) for item in raw)
    for photo in photos:
        path = directory / photo.filename
        if not path.is_file() or sha256_file(path) != photo.sha256:
            raise HarnessError(
                f"Frozen reference {photo.filename} changed or is missing; "
                "start a new run directory rather than refetching"
            )
    return FrozenReferences(directory, photos)


def freeze_references(
    run: Run,
    species: PanelSpecies,
    *,
    downloader: Downloader = download_references,
) -> FrozenReferences:
    """Return this run's reference photos, downloading the catalog's set only once.

    The frozen directory is published by one rename after every file and the
    manifest are written, so a crash leaves either nothing or a complete set.
    """
    final = run.path("references", species.key)
    if final.exists():
        return _load_frozen(final)
    staging = staging_directory(final)
    try:
        photos = downloader([_candidate(photo) for photo in species.catalog_references], staging)
        if len(photos) < MIN_REFERENCES:
            raise HarnessError(f"Too few reference photos were downloaded for {species.key}")
        expected = {photo.photo_id: photo.sha256 for photo in species.catalog_references}
        write_json(
            staging / "manifest.json",
            {
                "taxon_id": species.taxon_id,
                "photos": [photo.as_dict() for photo in photos],
                "matches_catalog": all(
                    expected.get(photo.photo_id) == photo.sha256 for photo in photos
                ),
            },
        )
        staging.replace(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return _load_frozen(final)


def frozen_references(run: Run, species: PanelSpecies) -> FrozenReferences:
    """The already frozen photos; never downloads."""
    final = run.path("references", species.key)
    if not final.exists():
        raise HarnessError(
            f"References for {species.key} are not cached; run cache-references first"
        )
    return _load_frozen(final)
