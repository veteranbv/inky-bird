"""Judge calibration: anchors, planted defects and side-swap consistency.

Every calibration pair has a known better plate (the approved catalog
portrait) as "first". Each pair is judged twice, the second time with the
sides swapped, so position bias shows up as inconsistency.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .blind import JudgedPair, PairSpec, PlateRef, Vote, expected_text, judge_pairs, other
from .config import EvalConfig, HarnessError
from .gate import judged_with, thresholds
from .judges import ANATOMY_CATEGORIES, Defect, Judge, PlateAssessment, Side
from .panel import FrozenReferences, PanelSpecies
from .rundir import Run, write_json
from .stats import position_bias

PLANTED_KINDS: Final = ("stretch", "duplicate", "lettering")
# The categories a judge may use to report each planted defect. A defect is
# found only when one of them is reported more often on the planted plate than
# on the untouched original in the same verdict, a defect of that category on
# the planted plate names the altered region, and the planted plate loses the
# comparison on the axis the defect damages.
EXPECTED_CATEGORIES: Final[dict[str, frozenset[str]]] = {
    "stretch": frozenset({"proportions", "legs", "tail"}),
    "duplicate": frozenset(ANATOMY_CATEGORIES),
    "lettering": frozenset({"lettering"}),
}
# Words that place a reported defect in the altered region: the lower body,
# legs and tail for the two drawing defects, the Length line for the lettering one.
REGION_WORDS: Final[dict[str, tuple[str, ...]]] = {
    "stretch": (
        "leg",
        "foot",
        "feet",
        "toe",
        "tarsus",
        "tail",
        "lower",
        "belly",
        "stretch",
        "elongat",
        "long",
    ),
    "duplicate": (
        "leg",
        "foot",
        "feet",
        "toe",
        "tarsus",
        "lower",
        "belly",
        "duplicat",
        "extra",
        "copied",
        "repeat",
        "double",
        "second",
    ),
    "lettering": ("length",),
}
DAMAGED_AXIS: Final[dict[str, str]] = {
    "stretch": "anatomy",
    "duplicate": "anatomy",
    "lettering": "lettering",
}
# Fractions of the page (left, top, right, bottom). The bird sits large in the
# middle with its legs and tail in the lower half; the left margin carries the
# measurements, the right edge the ruler and the bottom margin the studies.
# The stretch stays inside the bird's column, so it touches no lettering or ruler.
STRETCH_BOX: Final = (0.42, 0.50, 0.86, 0.78)
STRETCH_FACTOR: Final = 1.35
DUPLICATE_BOX: Final = (0.45, 0.55, 0.70, 0.78)
DUPLICATE_SHIFT: Final = 0.14
LETTERING_BOX: Final = (0.03, 0.12, 0.40, 0.22)
# A pixel is drawing, not paper, when a channel differs from the paper by this much.
PAPER_TOLERANCE: Final = 48
# Below these shares of the region a planted defect could be all but invisible.
MIN_DRAWING_SHARE: Final = 0.05
MIN_CHANGED_SHARE: Final = 0.05
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


def _share_differing(image: Image.Image, reference: Image.Image) -> float:
    """The share of pixels where some channel differs by more than the paper tolerance."""
    difference = ImageChops.difference(image, reference)
    strongest = functools.reduce(ImageChops.lighter, difference.split())
    mask = strongest.point(lambda value: 255 if value > PAPER_TOLERANCE else 0)
    return mask.histogram()[255] / (image.width * image.height)


def _require_drawing(image: Image.Image, box: tuple[int, int, int, int], kind: str) -> None:
    region = image.crop(box)
    paper = image.getpixel((round(0.02 * image.width), round(0.02 * image.height)))
    if _share_differing(region, Image.new("RGB", region.size, paper)) < MIN_DRAWING_SHARE:
        raise HarnessError(
            f"The {kind} region of this plate holds too little drawing to plant a visible defect"
        )


def plant_defect(source: Path, destination: Path, kind: str, length: str) -> None:
    """Write a copy of an approved plate with one deliberate, known, visible defect."""
    with Image.open(source) as opened:
        original = opened.convert("RGB")
    image = original.copy()
    width, height = image.size
    if kind == "stretch":
        box = _box(image.size, STRETCH_BOX)
        _require_drawing(image, box, kind)
        region = image.crop(box)
        # Taller by the factor from the top of the box, cut back to the box.
        stretched = region.resize((region.width, round(region.height * STRETCH_FACTOR)))
        image.paste(stretched.crop((0, 0, region.width, region.height)), box[:2])
        changed = box
    elif kind == "duplicate":
        box = _box(image.size, DUPLICATE_BOX)
        _require_drawing(image, box, kind)
        offset = round(DUPLICATE_SHIFT * width)
        image.paste(image.crop(box), (box[0] + offset, box[1]))
        changed = (box[0] + offset, box[1], min(box[2] + offset, width), box[3])
    elif kind == "lettering":
        box = _box(image.size, LETTERING_BOX)
        _require_drawing(image, box, kind)
        paper = image.getpixel((round(0.02 * width), round(0.02 * height)))
        # Drawn on a patch of the box's size, so the text cannot spill outside it.
        patch = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), paper)
        font = ImageFont.load_default(size=max(height // 40, 8))
        ImageDraw.Draw(patch).text(
            (4, 4), f"Length: {altered_digits(length)}", fill=(40, 30, 20), font=font
        )
        image.paste(patch, box[:2])
        changed = box
    else:
        raise HarnessError(f"Unknown planted defect: {kind}")
    if _share_differing(image.crop(changed), original.crop(changed)) < MIN_CHANGED_SHARE:
        raise HarnessError(f"The planted {kind} defect would barely change {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG")


@dataclass(frozen=True)
class CalibrationItem:
    """One known-better/known-worse pair, judged in both orders."""

    kind: str
    taxon_id: int
    spec: PairSpec


def anchor_taxa(anchors: Path) -> list[int]:
    """The taxon ids an anchor folder holds plates for."""
    if not anchors.is_dir():
        raise HarnessError(f"The anchor folder {anchors.name} does not exist")
    taxa: list[int] = []
    for directory in sorted(path for path in anchors.iterdir() if path.is_dir()):
        if not directory.name.isdigit():
            raise HarnessError(f"Anchor folder {directory.name} is not a taxon id")
        taxa.append(int(directory.name))
    return taxa


def anchor_items(
    anchors: Path,
    species: Sequence[tuple[PanelSpecies, FrozenReferences]],
) -> list[CalibrationItem]:
    """Anchors are ANCHORS/<taxon_id>/<name>.png, each a known-worse plate for that species.

    The species may be any approved catalog species, on the panel or not, so
    pairs made from the owner's past decisions can be used; species lists
    each one with its frozen references.
    """
    by_taxon = {entry.taxon_id: (entry, references) for entry, references in species}
    items: list[CalibrationItem] = []
    for taxon in anchor_taxa(anchors):
        if taxon not in by_taxon:
            raise HarnessError(f"Anchor folder {taxon} names a species without its catalog entry")
        entry, references = by_taxon[taxon]
        directory = anchors / str(taxon)
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
    skipped = 0
    for entry, references in species:
        if not entry.portrait_path.is_file():
            raise HarnessError(f"The approved plate for {entry.key} is not checked out")
        for kind in PLANTED_KINDS:
            planted = run.path("calibration", "planted", entry.key, f"{kind}.png")
            if not planted.exists():
                try:
                    plant_defect(
                        entry.portrait_path, planted, kind, entry.profile["measurements"]["length"]
                    )
                except HarnessError:
                    # This plate's layout leaves the defect's region blank, so nothing
                    # visible can be planted there; the other kinds still apply.
                    skipped += 1
                    continue
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
    if skipped * 3 > len(items) + skipped:
        raise HarnessError(
            "Too few planted defects could be made for these species; choose other species"
        )
    return items


def swapped(spec: PairSpec, first_sides: Mapping[str, Side]) -> PairSpec:
    """The same pair again, with every judge seeing the sides it saw the first time swapped."""
    return PairSpec(
        identity=f"{spec.identity}|swapped",
        taxon_id=spec.taxon_id,
        first=spec.first,
        second=spec.second,
        references=spec.references,
        expected_text=spec.expected_text,
        first_sides={judge: other(side) for judge, side in first_sides.items()},
    )


def prefers_first(vote: Vote) -> bool:
    """Wins at least one axis for the known-better plate and loses neither."""
    outcomes = (vote.anatomy, vote.lettering)
    return "first" in outcomes and "second" not in outcomes


def _count(plate: PlateAssessment, category: str) -> int:
    return sum(1 for defect in plate.defects if defect.category == category)


def _names_region(defect: Defect, kind: str) -> bool:
    text = f"{defect.location} {defect.description}".casefold()
    return any(word in text for word in REGION_WORDS[kind])


def flags_planted(vote: Vote, kind: str) -> bool:
    """True when the judge found the planted defect where it was planted.

    It must report an expected category more often on the planted plate than
    on the untouched original (a complaint it makes about both says nothing),
    place one such defect in the altered region, and prefer the original on
    the axis the defect damages.
    """
    rising = {
        category
        for category in EXPECTED_CATEGORIES[kind]
        if _count(vote.second, category) > _count(vote.first, category)
    }
    located = any(
        defect.category in rising and _names_region(defect, kind) for defect in vote.second.defects
    )
    damaged = vote.anatomy if DAMAGED_AXIS[kind] == "anatomy" else vote.lettering
    return located and damaged == "first"


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
        # Each item is shown twice. A verdict the judge never returned counts
        # against it, so a judge cannot pass on the few pairs it answered.
        missing = [False] * (2 - len(votes))
        if item.kind == "anchor":
            anchors.extend([prefers_first(vote) for vote in votes] + missing)
        else:
            recall.extend([flags_planted(vote, item.kind) for vote in votes] + missing)
            preference.extend([prefers_first(vote) for vote in votes] + missing)
        consistent.append(
            len(votes) == 2
            and (votes[0].anatomy, votes[0].lettering) == (votes[1].anatomy, votes[1].lettering)
        )
    result = {
        "anchor_accuracy": (_share(anchors), config.anchor_accuracy_threshold),
        "planted_recall": (_share(recall), config.planted_recall_threshold),
        "planted_preference": (_share(preference), config.planted_preference_threshold),
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
        planted_preference=result["planted_preference"][0],
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
    anchor_species: Sequence[tuple[PanelSpecies, FrozenReferences]] = (),
    parallel: int = 1,
) -> list[JudgeCalibration]:
    """Judge every known pair in both orders and score each judge.

    species are the planted-defect species; anchor_species are the approved
    catalog species the anchor folder names (planted species count too).
    """
    items = planted_items(run, species)
    if anchors is not None:
        items = anchor_items(anchors, [*species, *anchor_species]) + items
    originals = judge_pairs(run, [item.spec for item in items], judges, config, parallel=parallel)
    repeats = judge_pairs(
        run,
        [swapped(item.spec, pair.first_sides) for item, pair in zip(items, originals, strict=True)],
        judges,
        config,
        parallel=parallel,
    )
    triples = list(zip(items, originals, repeats, strict=True))
    results = [score_judge(judge.name, triples, config) for judge in judges]
    write_json(
        run.path("calibration", "summary.json"),
        {
            "judged_with": judged_with(config),
            "thresholds": thresholds(config),
            "position_bias": position_bias([*originals, *repeats], [j.name for j in judges]),
            "anchors_supplied": anchors is not None,
            "items": [{"kind": item.kind, "taxon_id": item.taxon_id} for item in items],
            "judges": [result.as_dict() for result in results],
        },
    )
    return results


def calibration_plan(species: int, anchors: int, judges: int, attempts: int) -> dict[str, int]:
    """Judgments are one per pair and judge; each may take up to attempts paid calls."""
    pairs = (species * len(PLANTED_KINDS) + anchors) * 2
    return {
        "pairs": pairs,
        "judgments_at_most": pairs * judges,
        "judge_calls_at_most": pairs * judges * attempts,
        "generations": 0,
    }
