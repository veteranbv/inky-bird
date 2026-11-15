from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from PIL import Image
from plate_tuning.compare import compare
from plate_tuning.config import JudgeSpec
from plate_tuning.gate import calibration_gate
from plate_tuning.generate import Arm, GenerationRequest, parse_arm
from plate_tuning.rundir import Budget
from plate_tuning_fixtures import (
    BLUE,
    RED,
    PixelJudge,
    calibration_record,
    config,
    make_run,
    passing_gate,
)

from inky_bird_frame.prompts import PLATE_INSTRUCTIONS

GREY = (128, 128, 128)


class SplitGenerator:
    """The candidate wins on dev species and loses on held-out ones."""

    def generate(self, request: GenerationRequest) -> None:
        if request.arm.name == "baseline":
            colour = GREY
        else:
            colour = RED if request.species.split == "dev" else BLUE
        Image.new("RGB", (30, 40), colour).save(request.output_path)


def test_the_decision_uses_only_held_out_species_when_they_are_compared(
    tmp_path: Path,
) -> None:
    run, panel, _ = make_run(tmp_path)
    candidate_text = tmp_path / "candidate.txt"
    candidate_text.write_text(PLATE_INSTRUCTIONS.replace("warm aged cream", "pale grey"))
    judges = [PixelJudge("fake-1"), PixelJudge("fake-2"), PixelJudge("fake-3")]

    result = compare(
        run,
        config(),
        SplitGenerator(),
        judges,
        parse_arm(f"candidate={candidate_text}", base=tmp_path),
        parse_arm("baseline=default", base=tmp_path),
        panel,
        samples=2,
        budget=Budget(16),
        gate=passing_gate(),
    )

    assert result.summary is not None and result.decision_summary is not None
    assert result.summary.anatomy.point == 0.5
    assert result.decision_species == "holdout"
    assert result.decision_summary.judged_species == (103, 104)
    assert result.decision_summary.anatomy.point == 0.0
    assert result.decision is not None and not result.decision.accept
    saved = json.loads(run.path("compare", "candidate-vs-baseline", "summary.json").read_text())
    assert saved["decision_species"] == "holdout"
    assert saved["judged_with"]["judges"][0] == {
        "name": "fake-1",
        "kind": "claude",
        "model": "model-x",
    }
    assert len(saved["judged_with"]["rubric_sha256"]) == 64
    records = json.loads(run.path("compare", "candidate-vs-baseline", "pairs.json").read_text())
    assert len(records) == 8
    # Plates inside the run are recorded run-relative, so no home folder is written down.
    for record in records:
        for role in ("first", "second"):
            recorded = Path(record[role]["path"])
            assert not recorded.is_absolute()
            assert (run.root / recorded).is_file()
    assert {vote["anatomy"] for record in records for vote in record["votes"]} == {
        "first",
        "second",
    }


def test_a_dev_only_comparison_makes_no_decision(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    candidate_text = tmp_path / "candidate.txt"
    candidate_text.write_text(PLATE_INSTRUCTIONS.replace("warm aged cream", "pale grey"))
    judges = [PixelJudge("fake-1"), PixelJudge("fake-2"), PixelJudge("fake-3")]

    result = compare(
        run,
        config(),
        SplitGenerator(),
        judges,
        parse_arm(f"candidate={candidate_text}", base=tmp_path),
        parse_arm("baseline=default", base=tmp_path),
        [entry for entry in panel if entry.split == "dev"],
        samples=1,
        budget=Budget(4),
        gate=passing_gate(),
    )

    # The candidate wins every dev pair, yet nothing decides without held-out species.
    assert result.summary is not None and result.summary.anatomy.point == 1.0
    assert result.decision_species == "none"
    assert result.decision is None and result.decision_summary is None
    assert result.decision_note is not None and "held-out" in result.decision_note


def _arms(tmp_path: Path) -> tuple[Arm, Arm]:
    candidate_text = tmp_path / "candidate.txt"
    candidate_text.write_text(PLATE_INSTRUCTIONS.replace("warm aged cream", "pale grey"))
    return (
        parse_arm(f"candidate={candidate_text}", base=tmp_path),
        parse_arm("baseline=default", base=tmp_path),
    )


class AlwaysRed:
    def generate(self, request: GenerationRequest) -> None:
        colour = RED if request.arm.name == "candidate" else GREY
        Image.new("RGB", (30, 40), colour).save(request.output_path)


def test_without_a_matching_calibration_there_is_no_decision(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    judges = [PixelJudge("fake-1"), PixelJudge("fake-2"), PixelJudge("fake-3")]
    rubric = tmp_path / "changed.md"
    rubric.write_text("a changed rubric")
    stale = calibration_gate(
        replace(config(), rubric_path=str(rubric)), calibration_record(config()), source="t"
    )
    cases = {
        "never calibrated": calibration_gate(config(), None, source=None),
        "rubric changed since": stale,
    }
    for name, gate in cases.items():
        first, second = _arms(tmp_path)
        result = compare(
            run,
            config() if name == "never calibrated" else replace(config(), rubric_path=str(rubric)),
            AlwaysRed(),
            judges,
            first,
            second,
            panel,
            samples=1,
            budget=Budget(8),
            gate=gate,
        )
        assert result.summary is not None and result.summary.anatomy.point == 1.0, name
        assert result.decision is None, name
        assert result.decision_note, name
    assert "another rubric" in str(stale.reasons)

    first, second = _arms(tmp_path)
    explored = compare(
        run,
        config(),
        AlwaysRed(),
        judges,
        first,
        second,
        panel,
        samples=1,
        budget=Budget(8),
        gate=calibration_gate(config(), None, source=None),
        exploratory=True,
    )
    assert explored.decision is None and explored.exploratory
    assert explored.exploratory_decision is not None and explored.exploratory_decision.accept
    saved = json.loads(run.path("compare", "candidate-vs-baseline", "summary.json").read_text())
    assert saved["exploratory"] is True and saved["decision"] is None


def test_only_calibrated_judges_decide(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    # The judge that failed calibration prefers the baseline; it must not sway the decision.
    judges = [PixelJudge("fake-1"), PixelJudge("fake-2", prefer_red=False), PixelJudge("fake-3")]
    gate = calibration_gate(config(), calibration_record(config(), ("fake-2",)), source="t")
    assert gate.passed and gate.calibrated == ("fake-1", "fake-3")
    assert gate.excluded == {"fake-2": "failed calibration"}
    first, second = _arms(tmp_path)

    result = compare(
        run,
        config(),
        AlwaysRed(),
        judges,
        first,
        second,
        panel,
        samples=1,
        budget=Budget(8),
        gate=gate,
    )

    assert result.summary is not None
    assert result.summary.judge_missing == {"fake-1": 0, "fake-2": 0, "fake-3": 0}
    assert result.decision_summary is not None
    assert set(result.decision_summary.judge_missing) == {"fake-1", "fake-3"}
    assert result.decision is not None and result.decision.accept


def test_the_gate_needs_two_calibrated_judges_of_every_configured_kind() -> None:
    record = calibration_record(config(), ("fake-3",))
    gate = calibration_gate(config(), record, source="t")
    assert not gate.passed
    assert "no calibrated codex judge; each configured kind needs one" in gate.reasons
    relaxed = replace(config(), require_each_provider=False)
    assert calibration_gate(relaxed, calibration_record(relaxed, ("fake-3",)), source="t").passed
    lonely = calibration_record(relaxed, ("fake-1", "fake-3"))
    assert "a decision needs at least 2" in str(
        calibration_gate(relaxed, lonely, source="t").reasons
    )
    other_model = replace(
        config(), judges=(JudgeSpec("fake-1", "claude", "model-z"), *config().judges[1:])
    )
    moved = calibration_gate(other_model, calibration_record(config()), source="t")
    assert moved.excluded == {"fake-1": "calibrated with another kind or model"}
    stricter = replace(config(), swap_consistency_threshold=0.9)
    assert "other thresholds" in str(
        calibration_gate(stricter, calibration_record(config()), source="t").reasons
    )


def test_pairs_the_cap_left_incomplete_are_reported_and_not_judged(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    judge = PixelJudge("fake-1")
    first, second = _arms(tmp_path)

    result = compare(
        run,
        config(),
        AlwaysRed(),
        [judge],
        first,
        second,
        panel,
        samples=2,
        budget=Budget(5),
        gate=passing_gate(),
    )

    # Groups are paid for whole: five plates buy two complete pairs, never a lone plate.
    assert result.generation.generated == 4
    assert len(result.pairs) == 2 and len(judge.requests) == 2
    assert len(result.dropped) == 6
    assert all(entry["missing"] == ["candidate", "baseline"] for entry in result.dropped)


def test_a_comparison_on_some_held_out_species_decides_nothing(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    first, second = _arms(tmp_path)

    result = compare(
        run,
        config(),
        AlwaysRed(),
        [PixelJudge("fake-1"), PixelJudge("fake-2"), PixelJudge("fake-3")],
        first,
        second,
        [entry for entry in panel if entry.taxon_id == 103],
        samples=1,
        budget=Budget(2),
        gate=passing_gate(),
        required_holdout=[103, 104],
    )

    assert result.decision is None
    assert result.decision_note == (
        "1 of the panel's held-out species were not compared; a decision needs all of them"
    )
