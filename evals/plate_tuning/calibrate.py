"""Judge calibration: anchors, planted defects and side-swap consistency.

Every calibration pair has a known better plate (the approved catalog
portrait) as "first". Each pair is judged twice, the second time with the
sides swapped, so position bias shows up as inconsistency.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .blind import JudgedPair, PairSpec, PlateRef, Vote, expected_text, judge_pairs, other
from .config import EvalConfig, HarnessError
from .judges import ANATOMY_CATEGORIES, Judge, Side
from .panel import FrozenReferences, PanelSpecies
from .rundir import Run, write_json

PLANTED_KINDS: Final = ("stretch", "duplicate", "lettering")
# The categories a judge may use to report each planted defect.
EXPECTED_CATEGORIES: Final[dict[str, frozenset[str]]] = {
    "stretch": frozenset({"proportions", "legs", "tail"}),
    "duplicate": frozenset(ANATOMY_CATEGORIES),
    "lettering": frozenset({"lettering"}),
}
# Fractions of the page. The bird sits large and centered-right with its
# legs and tail in the lower half; the left margin carries the measurements.
STRETCH_BAND: Final = (0.50, 0.80)
STRETCH_FACTOR: Final = 1.35
DUPLICATE_BOX: Final = (0.45, 0.55, 0.70, 0.78)
DUPLICATE_SHIFT: Final = 0.14
LETTERING_BOX: Final = (0.03, 0.12, 0.40, 0.22)
_DIGIT: Final = re.compile(r"\d")


def altered_digits(value: str) -> str:
    """Shift every digit by three, so the printed value is certainly wrong."""
    altered = _DIGIT.sub(lambda match: str((int(match.group()) + 3) % 10), value)
    return altered if altered != value else f"{value} 99"


def _box(
    size: tuple[int, int], fractions: tuple[float, float, float, float]
) -> tuple[int, int, int, int]:
    width, height = size
    left, top, right, bottom = fractions
    return (round(left * width), round(top * height), round(right * width), round(bottom * height))


def plant_defect(source: Path, destination: Path, kind: str, length: str) -> None:
    """Write a copy of an approved plate with one deliberate, known defect."""
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    if kind == "stretch":
        top, bottom = round(STRETCH_BAND[0] * height), round(STRETCH_BAND[1] * height)
        band = image.crop((0, top, width, bottom))
        stretched = band.resize((width, round((bottom - top) * STRETCH_FACTOR)))
        image.paste(stretched, (0, top))
    elif kind == "duplicate":
        box = _box(image.size, DUPLICATE_BOX)
        region = image.crop(box)
        image.paste(region, (box[0] + round(DUPLICATE_SHIFT * width), box[1]))
    elif kind == "lettering":
        box = _box(image.size, LETTERING_BOX)
        paper = image.getpixel((round(0.02 * width), round(0.02 * height)))
        # Drawn on a patch of the box's size, so the text cannot spill outside it.
        patch = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), paper)
        font = ImageFont.load_default(size=max(height // 40, 8))
        ImageDraw.Draw(patch).text(
            (4, 4), f"Length: {altered_digits(length)}", fill=(40, 30, 20), font=font
        )
        image.paste(patch, box[:2])
    else:
        raise HarnessError(f"Unknown planted defect: {kind}")
    with Image.open(source) as original:
        if ImageChops.difference(original.convert("RGB"), image).getbbox() is None:
            raise HarnessError(f"The planted {kind} defect left {source.name} unchanged")
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG")


@dataclass(frozen=True)
class CalibrationItem:
    """One known-better/known-worse pair, judged in both orders."""

    kind: str
    taxon_id: int
    spec: PairSpec


def anchor_items(
    anchors: Path,
    species: Sequence[tuple[PanelSpecies, FrozenReferences]],
) -> list[CalibrationItem]:
    """Anchors are ANCHORS/<taxon_id>/<name>.png, each a known-worse plate for that species."""
    by_taxon = {entry.taxon_id: (entry, references) for entry, references in species}
    items: list[CalibrationItem] = []
    for directory in sorted(path for path in anchors.iterdir() if path.is_dir()):
        if not directory.name.isdigit() or int(directory.name) not in by_taxon:
            raise HarnessError(f"Anchor folder {directory.name} is not a panel taxon id")
        entry, references = by_taxon[int(directory.name)]
        for worse in sorted(directory.glob("*.png")):
            items.append(
                CalibrationItem(
                    "anchor",
                    entry.taxon_id,
                    PairSpec(
                        identity=f"calibration|anchor|{entry.taxon_id}|{worse.name}",
                        taxon_id=entry.taxon_id,
                        first=PlateRef(entry.portrait_path, "approved catalog plate"),
                        second=PlateRef(worse, f"known worse: {worse.name}"),
                        references=references,
                        expected_text=expected_text(entry.profile),
                    ),
                )
            )
    if not items:
        raise HarnessError("The anchor folder holds no plates")
    return items


def planted_items(
    run: Run, species: Sequence[tuple[PanelSpecies, FrozenReferences]]
) -> list[CalibrationItem]:
    items: list[CalibrationItem] = []
    for entry, references in species:
        if not entry.portrait_path.is_file():
            raise HarnessError(f"The approved plate for {entry.key} is not checked out")
        for kind in PLANTED_KINDS:
            planted = run.path("calibration", "planted", entry.key, f"{kind}.png")
            if not planted.exists():
                plant_defect(
                    entry.portrait_path, planted, kind, entry.profile["measurements"]["length"]
                )
            items.append(
                CalibrationItem(
                    kind,
                    entry.taxon_id,
                    PairSpec(
                        identity=f"calibration|planted|{entry.taxon_id}|{kind}",
                        taxon_id=entry.taxon_id,
                        first=PlateRef(entry.portrait_path, "approved catalog plate"),
                        second=PlateRef(planted, f"planted {kind}"),
                        references=references,
                        expected_text=expected_text(entry.profile),
                    ),
                )
            )
    return items


def swapped(spec: PairSpec, first_side: Side) -> PairSpec:
    return PairSpec(
        identity=f"{spec.identity}|swapped",
        taxon_id=spec.taxon_id,
        first=spec.first,
        second=spec.second,
        references=spec.references,
        expected_text=spec.expected_text,
        first_side=other(first_side),
    )


def prefers_first(vote: Vote) -> bool:
    """Wins at least one axis for the known-better plate and loses neither."""
    outcomes = (vote.anatomy, vote.lettering)
    return "first" in outcomes and "second" not in outcomes


def flags_planted(vote: Vote, kind: str) -> bool:
    return bool(vote.second.categories & EXPECTED_CATEGORIES[kind])


@dataclass(frozen=True)
class JudgeCalibration:
    judge: str
    anchor_accuracy: float | None
    planted_recall: float | None
    planted_preference: float | None
    swap_consistency: float | None
    judged: int
    passed: bool
    failures: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "judge": self.judge,
            "anchor_accuracy": self.anchor_accuracy,
            "planted_recall": self.planted_recall,
            "planted_preference": self.planted_preference,
            "swap_consistency": self.swap_consistency,
            "judgments": self.judged,
            "passed": self.passed,
            "failures": list(self.failures),
        }


def _share(values: Sequence[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def score_judge(
    judge: str,
    items: Sequence[tuple[CalibrationItem, JudgedPair, JudgedPair]],
    config: EvalConfig,
) -> JudgeCalibration:
    anchors: list[bool] = []
    recall: list[bool] = []
    preference: list[bool] = []
    consistent: list[bool] = []
    judged = 0
    for item, original, repeat in items:
        votes = [vote for pair in (original, repeat) for vote in pair.votes if vote.judge == judge]
        judged += len(votes)
        for vote in votes:
            if item.kind == "anchor":
                anchors.append(prefers_first(vote))
            else:
                recall.append(flags_planted(vote, item.kind))
                preference.append(prefers_first(vote))
        if len(votes) == 2:
            first, second = votes
            consistent.append(
                (first.anatomy, first.lettering) == (second.anatomy, second.lettering)
            )
    result = {
        "anchor_accuracy": (_share(anchors), config.anchor_accuracy_threshold),
        "planted_recall": (_share(recall), config.planted_recall_threshold),
        "swap_consistency": (_share(consistent), config.swap_consistency_threshold),
    }
    failures = tuple(
        f"{name} not measured" if value is None else f"{name} {value:.3f} < {threshold}"
        for name, (value, threshold) in result.items()
        if value is None or value < threshold
    )
    return JudgeCalibration(
        judge=judge,
        anchor_accuracy=result["anchor_accuracy"][0],
        planted_recall=result["planted_recall"][0],
        planted_preference=_share(preference),
        swap_consistency=result["swap_consistency"][0],
        judged=judged,
        passed=not failures,
        failures=failures,
    )


def calibrate(
    run: Run,
    config: EvalConfig,
    judges: Sequence[Judge],
    species: Sequence[tuple[PanelSpecies, FrozenReferences]],
    *,
    anchors: Path | None,
    parallel: int = 1,
) -> list[JudgeCalibration]:
    items = planted_items(run, species)
    if anchors is not None:
        items = anchor_items(anchors, species) + items
    originals = judge_pairs(run, [item.spec for item in items], judges, config, parallel=parallel)
    repeats = judge_pairs(
        run,
        [swapped(item.spec, pair.first_side) for item, pair in zip(items, originals, strict=True)],
        judges,
        config,
        parallel=parallel,
    )
    triples = list(zip(items, originals, repeats, strict=True))
    results = [score_judge(judge.name, triples, config) for judge in judges]
    write_json(
        run.path("calibration", "summary.json"),
        {
            "anchors_supplied": anchors is not None,
            "items": [{"kind": item.kind, "taxon_id": item.taxon_id} for item in items],
            "judges": [result.as_dict() for result in results],
        },
    )
    return results


def calibration_plan(species: int, anchors: int, judges: int) -> dict[str, int]:
    pairs = (species * len(PLANTED_KINDS) + anchors) * 2
    return {"pairs": pairs, "judgments_at_most": pairs * judges, "generations": 0}
