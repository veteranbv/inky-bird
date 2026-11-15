from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageChops
from plate_tuning.calibrate import (
    DUPLICATE_BOX,
    LETTERING_BOX,
    STRETCH_BAND,
    altered_digits,
    calibrate,
    plant_defect,
    planted_items,
    score_judge,
)
from plate_tuning.config import HarnessError
from plate_tuning.judges import (
    PLATE_FILES,
    BlindVerdict,
    Choice,
    Defect,
    JudgeRequest,
    PlateAssessment,
)
from plate_tuning.panel import frozen_references
from plate_tuning_fixtures import config, make_run


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "approved.png"
    image = Image.new("RGB", (120, 160), (235, 220, 185))
    for x in range(120):
        for y in range(160):
            if (x // 6 + y // 6) % 2:
                image.putpixel((x, y), (60, 40, 20))
    image.save(path)
    return path


def _changed_rows(before: Path, after: Path) -> tuple[int, int]:
    with Image.open(before) as left, Image.open(after) as right:
        box = ImageChops.difference(left.convert("RGB"), right.convert("RGB")).getbbox()
    assert box is not None
    return box[1], box[3]


def test_a_planted_defect_that_changes_nothing_is_refused(tmp_path: Path) -> None:
    flat = tmp_path / "flat.png"
    Image.new("RGB", (120, 160), (200, 200, 200)).save(flat)

    with pytest.raises(HarnessError, match="unchanged"):
        plant_defect(flat, tmp_path / "out.png", "duplicate", "12 cm")
    assert not (tmp_path / "out.png").exists()


def test_altered_digits_always_change_the_value() -> None:
    assert altered_digits("12-14 cm") == "45-47 cm"
    assert altered_digits("7 in") == "0 in"
    assert altered_digits("small") == "small 99"


def test_each_planted_defect_changes_only_its_region(tmp_path: Path) -> None:
    source = _source(tmp_path)
    height = 160
    for kind, (top, bottom) in {
        "stretch": (STRETCH_BAND[0], 1.0),
        "duplicate": (DUPLICATE_BOX[1], DUPLICATE_BOX[3]),
        "lettering": (LETTERING_BOX[1], LETTERING_BOX[3]),
    }.items():
        planted = tmp_path / f"{kind}.png"
        plant_defect(source, planted, kind, "12-14 cm")
        with Image.open(planted) as image:
            assert image.size == (120, 160)
        first, last = _changed_rows(source, planted)
        assert first >= round(top * height) - 1, kind
        assert last <= round(bottom * height) + 1, kind


class CalibrationJudge:
    """Answers from the plate pixels: a good judge, a blind one or a position-biased one."""

    def __init__(self, name: str, approved: dict[str, bytes], *, mode: str = "good") -> None:
        self.name = name
        self.approved = approved
        self.mode = mode

    def judge(self, request: JudgeRequest) -> BlindVerdict:
        a = (request.blind_dir / PLATE_FILES["A"]).read_bytes()
        approved_side: Choice = "A" if a in self.approved.values() else "B"
        if self.mode == "biased":
            approved_side = "A"
        winner: Choice = "tie" if self.mode == "blind" else approved_side
        found = (
            ()
            if self.mode == "blind"
            else tuple(
                Defect(category, "region", "reference-01.jpg", "major", "planted")
                for category in ("proportions", "legs", "lettering")
            )
        )
        approved = PlateAssessment((), True)
        planted = PlateAssessment(found, False)
        return BlindVerdict(
            plate_a=approved if approved_side == "A" else planted,
            plate_b=approved if approved_side == "B" else planted,
            anatomy=winner,
            lettering=winner,
        )


def test_calibration_scores_good_blind_and_biased_judges(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    species = [(entry, frozen_references(run, entry)) for entry in panel[:2]]
    anchors = tmp_path / "anchors"
    (anchors / "101").mkdir(parents=True)
    Image.new("RGB", (120, 160), (10, 10, 10)).save(anchors / "101" / "rejected-first.png")
    # The judge recognises the approved plate by its re-encoded bytes.
    approved: dict[str, bytes] = {}
    for entry, _refs in species:
        reencoded = tmp_path / f"reencoded-{entry.key}.png"
        with Image.open(entry.portrait_path) as image:
            image.convert("RGB").save(reencoded, format="PNG")
        approved[entry.key] = reencoded.read_bytes()
    judges = [
        CalibrationJudge("fake-1", approved),
        CalibrationJudge("fake-2", approved, mode="blind"),
        CalibrationJudge("fake-3", approved, mode="biased"),
    ]

    results = {
        result.judge: result
        for result in calibrate(run, config(), judges, species, anchors=anchors)
    }

    good, blind, biased = results["fake-1"], results["fake-2"], results["fake-3"]
    assert (good.anchor_accuracy, good.planted_recall, good.swap_consistency) == (1.0, 1.0, 1.0)
    assert good.passed
    assert good.judged == 2 * (1 + 2 * 3)
    assert blind.planted_recall == 0.0 and blind.anchor_accuracy == 0.0
    assert not blind.passed
    assert biased.swap_consistency == 0.0
    assert biased.anchor_accuracy == 0.5
    assert not biased.passed
    summary = json.loads(run.path("calibration", "summary.json").read_text())
    assert summary["anchors_supplied"] is True
    assert [judge["judge"] for judge in summary["judges"]] == ["fake-1", "fake-2", "fake-3"]


def test_without_anchors_a_judge_cannot_pass(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    species = [(panel[0], frozen_references(run, panel[0]))]
    items = planted_items(run, species)

    result = score_judge("fake-1", [], config())

    assert len(items) == 3
    assert result.anchor_accuracy is None
    assert not result.passed
    assert "anchor_accuracy not measured" in result.failures
