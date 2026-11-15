from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
from plate_tuning.compare import compare
from plate_tuning.generate import GenerationRequest, parse_arm
from plate_tuning.rundir import Budget
from plate_tuning_fixtures import BLUE, RED, PixelJudge, config, make_run

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


def test_dev_only_comparisons_decide_on_every_compared_species(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    candidate_text = tmp_path / "candidate.txt"
    candidate_text.write_text(PLATE_INSTRUCTIONS.replace("warm aged cream", "pale grey"))
    judges = [PixelJudge("fake-1")]

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
    )

    assert result.decision_species == "all"
    assert result.decision_summary is not None
    assert result.decision_summary.judged_species == (101, 102)
