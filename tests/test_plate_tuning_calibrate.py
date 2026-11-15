from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageChops
from plate_tuning.blind import Vote
from plate_tuning.calibrate import (
    DUPLICATE_BOX,
    DUPLICATE_SHIFT,
    LETTERING_BOX,
    STRETCH_BOX,
    altered_digits,
    anchor_items,
    calibrate,
    flags_planted,
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
from plate_tuning.panel import freeze_references, frozen_references, load_panel, resolve_species
from plate_tuning.rundir import Run
from plate_tuning_fixtures import FakeDownloader, config, make_run, write_catalog


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "approved.png"
    image = Image.new("RGB", (120, 160), (235, 220, 185))
    for x in range(120):
        for y in range(160):
            if (x // 6 + y // 6) % 2:
                image.putpixel((x, y), (60, 40, 20))
    image.save(path)
    return path


def _changed_box(before: Path, after: Path) -> tuple[int, int, int, int]:
    with Image.open(before) as left, Image.open(after) as right:
        box = ImageChops.difference(left.convert("RGB"), right.convert("RGB")).getbbox()
    assert box is not None
    return box


def test_a_planted_defect_on_blank_paper_is_refused(tmp_path: Path) -> None:
    flat = tmp_path / "flat.png"
    Image.new("RGB", (120, 160), (200, 200, 200)).save(flat)

    for kind in ("stretch", "duplicate", "lettering"):
        with pytest.raises(HarnessError, match="too little drawing"):
            plant_defect(flat, tmp_path / "out.png", kind, "12 cm")
    assert not (tmp_path / "out.png").exists()


def test_a_duplicate_that_would_paste_paper_over_the_bird_is_refused(tmp_path: Path) -> None:
    # Drawing only where the copy would land: the old check saw changed pixels and passed.
    source = tmp_path / "offset.png"
    image = Image.new("RGB", (120, 160), (235, 220, 185))
    left, top, right, bottom = _scaled(DUPLICATE_BOX)
    for x in range(right, min(right + 30, 120)):
        for y in range(top, bottom):
            image.putpixel((x, y), (40, 30, 20))
    image.save(source)

    with pytest.raises(HarnessError, match="too little drawing"):
        plant_defect(source, tmp_path / "out.png", "duplicate", "12 cm")


def _scaled(box: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    return round(box[0] * 120), round(box[1] * 160), round(box[2] * 120), round(box[3] * 160)


def test_altered_digits_always_change_the_value() -> None:
    assert altered_digits("12-14 cm") == "45-47 cm"
    assert altered_digits("7 in") == "0 in"
    assert altered_digits("small") == "small 99"


def test_each_planted_defect_changes_only_its_region(tmp_path: Path) -> None:
    source = _source(tmp_path)
    shift = DUPLICATE_SHIFT
    for kind, box in {
        # The stretch stays in the bird's column: the margin lettering and the ruler are untouched.
        "stretch": STRETCH_BOX,
        "duplicate": (
            DUPLICATE_BOX[0],
            DUPLICATE_BOX[1],
            DUPLICATE_BOX[2] + shift,
            DUPLICATE_BOX[3],
        ),
        "lettering": LETTERING_BOX,
    }.items():
        planted = tmp_path / f"{kind}.png"
        plant_defect(source, planted, kind, "12-14 cm")
        with Image.open(planted) as image:
            assert image.size == (120, 160)
        left, top, right, bottom = _changed_box(source, planted)
        expected = _scaled(box)
        assert left >= expected[0] - 1 and right <= expected[2] + 1, kind
        assert top >= expected[1] - 1 and bottom <= expected[3] + 1, kind
    assert STRETCH_BOX[0] > LETTERING_BOX[2]


class CalibrationJudge:
    """Answers from the plate pixels.

    Modes: good; blind (sees nothing); biased (always A); nitpick (lists the same
    defects on every plate); contrary (finds the defect but prefers that plate);
    misplaced (reports the right categories at a part of the plate that was not changed).
    """

    def __init__(self, name: str, approved: dict[str, bytes], *, mode: str = "good") -> None:
        self.name = name
        self.approved = approved
        self.mode = mode

    def judge(self, request: JudgeRequest) -> BlindVerdict:
        a = (request.blind_dir / PLATE_FILES["A"]).read_bytes()
        approved_side: Choice = "A" if a in self.approved.values() else "B"
        if self.mode == "biased":
            approved_side = "A"
        winner: Choice = "tie" if self.mode in ("blind", "nitpick") else approved_side
        if self.mode == "contrary":
            winner = "B" if approved_side == "A" else "A"
        where = {
            "proportions": "primary bird, lower body stretched",
            "legs": "primary bird, legs",
            "lettering": "left margin, Length line",
        }
        found = (
            ()
            if self.mode == "blind"
            else tuple(
                Defect(
                    category,
                    "primary bird, crown" if self.mode == "misplaced" else where[category],
                    "reference-01.jpg",
                    "major",
                    "does not match the references",
                )
                for category in ("proportions", "legs", "lettering")
            )
        )
        approved = PlateAssessment(found if self.mode == "nitpick" else (), True)
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


def test_a_judge_that_lists_the_same_defects_on_every_plate_finds_nothing(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    species = [(panel[0], frozen_references(run, panel[0]))]
    judges = [CalibrationJudge("fake-1", {}, mode="nitpick")]

    (result,) = calibrate(run, config(), judges, species, anchors=None)

    assert result.planted_recall == 0.0
    assert "planted_recall 0.000 < 0.8" in result.failures


def test_a_judge_that_prefers_the_planted_plate_fails_calibration(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    species = [(panel[0], frozen_references(run, panel[0]))]
    anchors = tmp_path / "anchors"
    (anchors / str(panel[0].taxon_id)).mkdir(parents=True)
    Image.new("RGB", (120, 160), (10, 10, 10)).save(anchors / str(panel[0].taxon_id) / "w.png")
    reencoded = tmp_path / "reencoded.png"
    with Image.open(panel[0].portrait_path) as image:
        image.convert("RGB").save(reencoded, format="PNG")
    good = CalibrationJudge("fake-1", {"x": reencoded.read_bytes()})
    contrary = CalibrationJudge("fake-2", {"x": reencoded.read_bytes()}, mode="contrary")

    results = {
        r.judge: r for r in calibrate(run, config(), [good, contrary], species, anchors=anchors)
    }

    assert results["fake-1"].passed
    # It names the planted defect, but a plate it prefers was not found to be damaged.
    assert results["fake-2"].planted_recall == 0.0
    assert results["fake-2"].planted_preference == 0.0
    assert any(failure.startswith("planted_preference") for failure in results["fake-2"].failures)


def test_a_planted_defect_reported_elsewhere_on_the_plate_is_not_found(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    species = [(panel[0], frozen_references(run, panel[0]))]
    reencoded = tmp_path / "reencoded.png"
    with Image.open(panel[0].portrait_path) as image:
        image.convert("RGB").save(reencoded, format="PNG")
    approved = {"x": reencoded.read_bytes()}
    judges = [
        CalibrationJudge("fake-1", approved),
        CalibrationJudge("fake-2", approved, mode="misplaced"),
    ]

    results = {r.judge: r for r in calibrate(run, config(), judges, species, anchors=None)}

    assert results["fake-1"].planted_recall == 1.0
    # More defects of the right category on the planted plate, but at the crown.
    assert results["fake-2"].planted_recall == 0.0
    assert results["fake-2"].planted_preference == 1.0


def test_flags_planted_needs_the_category_rise_the_region_and_the_lost_comparison() -> None:
    stretched = Defect("proportions", "primary bird, lower body", "reference-01.jpg", "major", "x")
    crown = Defect("proportions", "primary bird, crown", "reference-01.jpg", "major", "x")
    clean, planted = PlateAssessment((), True), PlateAssessment((stretched,), False)

    assert flags_planted(Vote("j", "first", "tie", clean, planted), "stretch")
    # The original wins nothing: the defect did not cost the planted plate the comparison.
    assert not flags_planted(Vote("j", "tie", "first", clean, planted), "stretch")
    # Reported in the wrong place.
    wrong = PlateAssessment((crown,), False)
    assert not flags_planted(Vote("j", "first", "tie", clean, wrong), "stretch")
    # Reported on both plates alike.
    assert not flags_planted(Vote("j", "first", "tie", planted, planted), "stretch")
    length = Defect("lettering", "left margin, Length line", "expected text", "fatal", "x")
    lettered = PlateAssessment((length,), False)
    assert flags_planted(Vote("j", "tie", "first", clean, lettered), "lettering")
    assert not flags_planted(Vote("j", "first", "tie", clean, lettered), "lettering")


def test_anchors_may_name_any_approved_catalog_species(tmp_path: Path) -> None:
    catalog, panel_path = write_catalog(tmp_path)
    listed = json.loads(panel_path.read_text())
    listed["species"] = [entry for entry in listed["species"] if entry["taxon_id"] != 104]
    panel_path.write_text(json.dumps(listed))
    panel = load_panel(catalog, panel_path)
    run = Run.open(tmp_path / "run", seed=7)
    (off_panel,) = resolve_species(catalog, panel, [104])
    assert off_panel.split == "catalog" and off_panel.taxon_id not in {e.taxon_id for e in panel}
    for entry in (*panel, off_panel):
        freeze_references(run, entry, downloader=FakeDownloader())
    anchors = tmp_path / "anchors"
    (anchors / "104").mkdir(parents=True)
    Image.new("RGB", (120, 160), (10, 10, 10)).save(anchors / "104" / "past-rejection.png")
    reencoded = tmp_path / "reencoded.png"
    with Image.open(off_panel.portrait_path) as image:
        image.convert("RGB").save(reencoded, format="PNG")
    judge = CalibrationJudge("fake-1", {"x": reencoded.read_bytes()})

    (result,) = calibrate(
        run,
        config(),
        [judge],
        [(panel[0], frozen_references(run, panel[0]))],
        anchors=anchors,
        anchor_species=[(off_panel, frozen_references(run, off_panel))],
    )

    assert result.anchor_accuracy == 1.0
    items = json.loads(run.path("calibration", "summary.json").read_text())["items"]
    assert {"kind": "anchor", "taxon_id": 104} in items
    with pytest.raises(HarnessError, match="approved catalog"):
        resolve_species(catalog, panel, [999])
    (anchors / "999").mkdir()
    with pytest.raises(HarnessError, match="catalog entry"):
        anchor_items(anchors, [(off_panel, frozen_references(run, off_panel))])
