"""Synthetic catalog, panel, generator and judges for the plate tuning harness tests."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from PIL import Image, ImageStat
from plate_tuning.blind import JudgedPair
from plate_tuning.config import EvalConfig, JudgeSpec
from plate_tuning.generate import GenerationRequest
from plate_tuning.judges import (
    PLATE_FILES,
    BlindVerdict,
    Choice,
    Defect,
    JudgeError,
    JudgeRequest,
    PlateAssessment,
)
from plate_tuning.panel import PanelSpecies, load_panel
from plate_tuning.rundir import Run

from inky_bird_frame.models import ReferencePhoto, SpeciesProfileData
from inky_bird_frame.references import ReferenceCandidate

# (taxon, common name, scientific name, stratum, split)
SPECIES = (
    (101, "Example Finch", "Exemplum finchi", "perching-songbird", "dev"),
    (102, "Example Duck", "Exemplum anas", "swimmer", "dev"),
    (103, "Example Heron", "Exemplum ardea", "wader", "holdout"),
    (104, "Example Hawk", "Exemplum buteo", "raptor", "holdout"),
)
RED = (220, 30, 30)
BLUE = (30, 30, 220)


def profile(taxon: int, name: str, scientific: str) -> SpeciesProfileData:
    return SpeciesProfileData(
        taxon_id=taxon,
        common_name=name,
        scientific_name=scientific,
        family="Exemplidae",
        measurements={"length": "12-14 cm", "wingspan": "20-22 cm", "weight": "15 g"},
        field_marks=["red cap", "pale belly", "dark tail", "short bill"],
        habitat="Example woodland",
        behavior="Forages in pairs",
        palette=["red", "cream", "brown"],
        sources=[
            {"title": "One", "url": "https://birds.example/one"},
            {"title": "Two", "url": "https://field.example/two"},
        ],
    )


def textured_plate(path: Path, *, seed: int) -> None:
    """A small patterned plate, so copying or stretching any region changes pixels."""
    image = Image.new("RGB", (120, 160), (235, 220, 185))
    for x in range(120):
        for y in range(160):
            if (x // 6 + y // 7 + seed) % 3 == 0:
                image.putpixel((x, y), (60 + seed % 50, 40, 20 + y))
    image.save(path)


def write_catalog(root: Path) -> tuple[Path, Path]:
    """A catalog with approved portraits, and a panel naming its four species."""
    catalog = root / "catalog"
    index: list[dict[str, object]] = []
    for taxon, name, scientific, _stratum, _split in SPECIES:
        slug = name.lower().replace(" ", "-")
        directory = catalog / "species" / f"{taxon}-{slug}"
        directory.mkdir(parents=True)
        textured_plate(directory / "portrait.png", seed=taxon)
        manifest = {
            "status": "approved",
            "taxon_id": taxon,
            "profile": profile(taxon, name, scientific),
            "references": [
                {
                    "attribution": "no rights reserved",
                    "filename": f"0{index_}-{taxon}{index_}.jpg",
                    "height": 900,
                    "image_url": f"https://photos.example.test/{taxon}{index_}/large.jpg",
                    "license_code": "cc0",
                    "observation_id": taxon * 10 + index_,
                    "observer": f"observer-{index_}",
                    "photo_id": taxon * 100 + index_,
                    "sha256": "0" * 64,
                    "source_url": f"https://observations.example.test/{taxon}{index_}",
                    "width": 1200,
                }
                for index_ in (1, 2)
            ],
        }
        (directory / "manifest.json").write_text(json.dumps(manifest))
        index.append(
            {
                "taxon_id": taxon,
                "common_name": name,
                "scientific_name": scientific,
                "portrait_path": f"species/{taxon}-{slug}/portrait.png",
            }
        )
    (catalog / "index.json").write_text(json.dumps({"species": index}))
    panel = root / "panel.json"
    panel.write_text(
        json.dumps(
            {
                "species": [
                    {"taxon_id": taxon, "common_name": name, "stratum": stratum, "split": split}
                    for taxon, name, _scientific, stratum, split in SPECIES
                ]
            }
        )
    )
    return catalog, panel


class FakeDownloader:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self, candidates: list[ReferenceCandidate], destination: Path
    ) -> list[ReferencePhoto]:
        self.calls += 1
        destination.mkdir(parents=True, exist_ok=True)
        photos: list[ReferencePhoto] = []
        for index, candidate in enumerate(candidates, start=1):
            filename = f"{index:02d}-{candidate.photo_id}.jpg"
            path = destination / filename
            Image.new("RGB", (16, 12), (index * 40, 90, 60)).save(path, format="JPEG")
            photos.append(
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
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
        return photos


class ColourGenerator:
    """Paints each arm's plate one flat colour so judges can tell arms apart by pixels."""

    def __init__(self, colours: dict[str, tuple[int, int, int]] | None = None) -> None:
        self.colours = colours or {}
        self.calls: list[tuple[str, int]] = []
        self.instructions: list[str] = []
        self._lock = threading.Lock()

    def generate(self, request: GenerationRequest) -> None:
        with self._lock:
            self.calls.append((request.arm.name, request.species.taxon_id))
            self.instructions.append(request.arm.instructions)
        colour = self.colours.get(request.arm.name, RED if "cand" in request.arm.name else BLUE)
        Image.new("RGB", (30, 40), colour).save(request.output_path, format="PNG")


def redness(path: Path) -> float:
    with Image.open(path) as image:
        red, _green, blue = ImageStat.Stat(image.convert("RGB")).mean
    return red - blue


class PixelJudge:
    """Prefers the redder plate; reports a fatal leg defect on the bluer one."""

    def __init__(self, name: str, *, prefer_red: bool = True, favour_a: bool = False) -> None:
        self.name = name
        self.prefer_red = prefer_red
        # A position-biased judge always answers A.
        self.favour_a = favour_a
        self.requests: list[JudgeRequest] = []
        self._lock = threading.Lock()

    def judge(self, request: JudgeRequest) -> BlindVerdict:
        with self._lock:
            self.requests.append(request)
        a = redness(request.blind_dir / PLATE_FILES["A"])
        b = redness(request.blind_dir / PLATE_FILES["B"])
        winner: Choice
        if self.favour_a:
            winner = "A"
        elif a == b:
            winner = "tie"
        else:
            winner = "A" if (a > b) == self.prefer_red else "B"
        bad = Defect("legs", "primary bird, left leg", "reference-01.jpg", "fatal", "detached")
        good = PlateAssessment((), True)
        poor = PlateAssessment((bad,), False)
        return BlindVerdict(
            plate_a=good if winner == "A" else poor if winner == "B" else good,
            plate_b=good if winner == "B" else poor if winner == "A" else good,
            anatomy=winner,
            lettering=winner,
        )


class FailingJudge:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def judge(self, request: JudgeRequest) -> BlindVerdict:
        self.calls += 1
        raise JudgeError("fake judge failed")


def config() -> EvalConfig:
    return EvalConfig(
        judges=(
            JudgeSpec("fake-1", "claude", "model-x"),
            JudgeSpec("fake-2", "claude", "model-x"),
            JudgeSpec("fake-3", "codex", None),
        ),
        bootstrap_iterations=200,
    ).validate()


def make_run(root: Path) -> tuple[Run, tuple[PanelSpecies, ...], FakeDownloader]:
    from plate_tuning.panel import freeze_references

    catalog, panel_path = write_catalog(root)
    panel = load_panel(catalog, panel_path)
    run = Run.open(root / "run", seed=1234)
    downloader = FakeDownloader()
    for entry in panel:
        freeze_references(run, entry, downloader=downloader)
    return run, panel, downloader


def votes_of(pair: JudgedPair) -> list[str]:
    return [vote.anatomy for vote in pair.votes]
