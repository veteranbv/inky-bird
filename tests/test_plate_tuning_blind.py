from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin
from plate_tuning.blind import (
    PairSpec,
    PlateRef,
    expected_text,
    judge_pairs,
    pending_judgments,
    prepare_pair,
    unblind_choice,
)
from plate_tuning.config import HarnessError
from plate_tuning.panel import PanelSpecies, frozen_references
from plate_tuning.rundir import Ledger, Run
from plate_tuning_fixtures import BLUE, RED, FailingJudge, PixelJudge, config, make_run, profile


def _specs(tmp_path: Path, run: Run, panel: Sequence[PanelSpecies], count: int) -> list[PairSpec]:
    """Pairs whose first plate is red (with telltale metadata) and second is blue."""
    specs: list[PairSpec] = []
    for index in range(count):
        red, blue = tmp_path / f"red-{index}.png", tmp_path / f"blue-{index}.png"
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "arm=candidate")
        Image.new("RGB", (30, 40), RED).save(red, pnginfo=info)
        Image.new("RGB", (30, 40), BLUE).save(blue)
        entry = panel[index % 2]
        specs.append(
            PairSpec(
                identity=f"test|{index}",
                taxon_id=entry.taxon_id,
                first=PlateRef(red, "candidate"),
                second=PlateRef(blue, "baseline"),
                references=frozen_references(run, entry),
                expected_text=expected_text(entry.profile),
            )
        )
    return specs


def test_blind_folder_holds_only_anonymous_plates_references_and_rubric(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    spec = _specs(tmp_path, run, panel, 1)[0]

    pair_id, first_side = prepare_pair(run, spec, "the rubric")

    blind = run.path("blind", pair_id)
    assert sorted(str(path.relative_to(blind)) for path in blind.rglob("*")) == [
        "RUBRIC.md",
        "plate-A.png",
        "plate-B.png",
        "references",
        "references/reference-01.jpg",
        "references/reference-02.jpg",
    ]
    assert (blind / "RUBRIC.md").read_text() == "the rubric"
    first_file = blind / f"plate-{first_side}.png"
    with Image.open(first_file) as plate:
        assert plate.getpixel((0, 0)) == RED
        assert "Comment" not in plate.info
    assert b"candidate" not in first_file.read_bytes()
    assert "candidate" not in pair_id and "test" not in pair_id
    key = json.loads(run.path("keys", f"{pair_id}.json").read_text())
    assert key["first_side"] == first_side
    assert key["first"]["label"] == "candidate"
    assert not run.path("keys").is_relative_to(blind)


def test_sides_are_random_per_pair_but_stable_on_resume(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    specs = _specs(tmp_path, run, panel, 24)

    sides = [prepare_pair(run, spec, "r")[1] for spec in specs]
    again = [prepare_pair(Run.open(run.root), spec, "r")[1] for spec in specs]

    assert sides == again
    assert 4 <= sides.count("A") <= 20


def test_a_forced_side_is_honoured_and_a_changed_plate_is_refused(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    spec = _specs(tmp_path, run, panel, 1)[0]
    assert prepare_pair(run, replace(spec, identity="forced", first_side="B"), "r")[1] == "B"

    prepare_pair(run, spec, "r")
    Image.new("RGB", (30, 40), (0, 255, 0)).save(spec.first.path)
    with pytest.raises(HarnessError, match="changed"):
        prepare_pair(run, spec, "r")


def test_a_key_without_its_folder_is_rebuilt_from_the_key(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    spec = _specs(tmp_path, run, panel, 1)[0]
    pair_id, side = prepare_pair(run, spec, "r")
    for path in sorted(run.path("blind", pair_id).rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    run.path("blind", pair_id).rmdir()

    assert prepare_pair(run, spec, "r") == (pair_id, side)
    with Image.open(run.path("blind", pair_id, f"plate-{side}.png")) as plate:
        assert plate.getpixel((0, 0)) == RED


def test_unblinding_maps_letters_back_to_first_and_second() -> None:
    assert unblind_choice("A", "A") == "first"
    assert unblind_choice("A", "B") == "second"
    assert unblind_choice("B", "B") == "first"
    assert unblind_choice("tie", "B") == "tie"


def test_judging_unblinds_correctly_whatever_side_the_plate_landed_on(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    specs = _specs(tmp_path, run, panel, 12)
    judges = [PixelJudge("fake-1"), PixelJudge("fake-2"), PixelJudge("fake-3", prefer_red=False)]

    pairs = judge_pairs(run, specs, judges, config(), parallel=3)

    assert {pair.first_side for pair in pairs} == {"A", "B"}
    for pair in pairs:
        assert [vote.anatomy for vote in pair.votes] == ["first", "first", "second"]
        assert pair.votes[0].second.fatal and not pair.votes[0].first.fatal
        assert pair.votes[2].first.fatal
    assert pending_judgments(run, specs, judges) == 0


def test_judging_resumes_without_asking_again(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    specs = _specs(tmp_path, run, panel, 3)
    judge = PixelJudge("fake-1")
    judge_pairs(run, specs, [judge], config())
    asked = len(judge.requests)

    pairs = judge_pairs(run, specs, [judge], config())

    assert asked == 3
    assert len(judge.requests) == 3
    assert all(len(pair.votes) == 1 for pair in pairs)


def test_a_failing_judge_is_retried_then_recorded_and_left_out(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    specs = _specs(tmp_path, run, panel, 1)
    failing = FailingJudge("fake-2")

    pairs = judge_pairs(run, specs, [PixelJudge("fake-1"), failing], config())

    assert failing.calls == config().judge_attempts
    assert [vote.judge for vote in pairs[0].votes] == ["fake-1"]
    assert pairs[0].errors == (("fake-2", "fake judge failed"),)
    assert Ledger(run.path("judgments.jsonl")).count("judgment-failed") == 2
    pairs = judge_pairs(run, specs, [PixelJudge("fake-1"), PixelJudge("fake-2")], config())
    assert [vote.judge for vote in pairs[0].votes] == ["fake-1", "fake-2"]
    assert pairs[0].errors == ()


def test_expected_text_names_every_printed_label() -> None:
    text = expected_text(profile(1, "Example Finch", "Exemplum finchi"))

    assert text["labelled_values"] == {
        "Length:": "12-14 cm",
        "Wingspan:": "20-22 cm",
        "Weight:": "15 g",
        "Habitat:": "Example woodland",
        "Behavior:": "Forages in pairs",
    }
    assert text["ruler_label"] == "BODY LENGTH: 12-14 cm"
    assert text["field_marks"] == ["red cap", "pale belly", "dark tail", "short bill"]
    assert text["color_swatch_names"] == ["red", "cream", "brown"]
