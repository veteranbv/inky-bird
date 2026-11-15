from __future__ import annotations

import hashlib
import json
import shutil
import struct
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageCms, PngImagePlugin
from plate_tuning.blind import (
    KEYS_DIR,
    PairSpec,
    PlateRef,
    blind_folder,
    expected_text,
    judge_pairs,
    pending_judgments,
    prepare_pair,
    unblind_choice,
)
from plate_tuning.config import HarnessError, JudgeSpec
from plate_tuning.judges import FIXED_MTIME, judge_contract_sha256
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


def _chunks(path: Path) -> list[bytes]:
    """The chunk types of a PNG file, in order."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    types: list[bytes] = []
    offset = 8
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        types.append(data[offset + 4 : offset + 8])
        offset += 12 + length
    return types


def test_blind_folder_holds_only_anonymous_plates_references_and_rubric(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    spec = _specs(tmp_path, run, panel, 1)[0]

    pair_id, sides = prepare_pair(run, spec, "the rubric", ["fake-1"])

    blind = blind_folder(run, spec.identity, "fake-1")
    assert sorted(str(path.relative_to(blind)) for path in blind.rglob("*")) == [
        "RUBRIC.md",
        "plate-A.png",
        "plate-B.png",
        "references",
        "references/reference-01.jpg",
        "references/reference-02.jpg",
    ]
    assert (blind / "RUBRIC.md").read_text() == "the rubric"
    first_file = blind / f"plate-{sides['fake-1']}.png"
    with Image.open(first_file) as plate:
        assert plate.getpixel((0, 0)) == RED
        assert "Comment" not in plate.info
    assert b"candidate" not in first_file.read_bytes()
    for name in (pair_id, blind.name):
        assert "candidate" not in name and "test" not in name and "fake" not in name
    keys = sorted(run.path(KEYS_DIR).glob("*.json"))
    assert len(keys) == 1
    key = json.loads(keys[0].read_text())
    assert key["pair_id"] == pair_id
    assert key["sides"] == sides
    assert key["first"]["label"] == "candidate"
    # Nothing ties a folder's name to its key's name or place.
    assert pair_id not in keys[0].name and blind.name not in keys[0].name
    assert not run.path(KEYS_DIR).is_relative_to(blind)
    assert not run.path("keys").exists()
    # Every entry, the folder itself included, carries one fixed time.
    assert {path.stat().st_mtime for path in [blind, *blind.rglob("*")]} == {FIXED_MTIME}


def test_the_anonymised_plate_keeps_only_pixels_and_required_chunks(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    spec = _specs(tmp_path, run, panel, 1)[0]
    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "image tool 1.0")
    info.add_itxt("XML:com.adobe.xmp", "arm=candidate")
    Image.new("RGB", (30, 40), RED).save(
        spec.first.path,
        pnginfo=info,
        icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
        dpi=(300, 300),
        exif=Image.Exif(),
    )
    assert b"iCCP" in spec.first.path.read_bytes()

    _pair_id, sides = prepare_pair(run, spec, "r", ["fake-1"])

    plate = blind_folder(run, spec.identity, "fake-1") / f"plate-{sides['fake-1']}.png"
    assert set(_chunks(plate)) == {b"IHDR", b"IDAT", b"IEND"}
    with Image.open(plate) as image, Image.open(spec.first.path) as source:
        assert image.info == {}
        assert image.tobytes() == source.convert("RGB").tobytes()


def test_sides_are_drawn_per_pair_and_judge_and_stable_on_resume(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    specs = _specs(tmp_path, run, panel, 24)
    judges = ["fake-1", "fake-2", "fake-3"]

    sides = [prepare_pair(run, spec, "r", judges)[1] for spec in specs]
    again = [prepare_pair(Run.open(run.root), spec, "r", judges)[1] for spec in specs]

    assert sides == again
    for judge in judges:
        assert 4 <= [entry[judge] for entry in sides].count("A") <= 20
    # Judges are not shown one shared order: some pairs differ between them.
    assert sum(len(set(entry.values())) > 1 for entry in sides) >= 6
    for spec, entry in zip(specs, sides, strict=True):
        for judge in judges:
            folder = blind_folder(run, spec.identity, judge)
            with Image.open(folder / f"plate-{entry[judge]}.png") as plate:
                assert plate.getpixel((0, 0)) == RED


def test_a_judge_added_later_gets_its_own_draw_and_the_others_keep_theirs(
    tmp_path: Path,
) -> None:
    run, panel, _ = make_run(tmp_path)
    spec = _specs(tmp_path, run, panel, 1)[0]
    _pair_id, first = prepare_pair(run, spec, "r", ["fake-1"])

    _pair_id, both = prepare_pair(run, spec, "r", ["fake-1", "fake-2"])

    assert both["fake-1"] == first["fake-1"]
    key = json.loads(next(run.path(KEYS_DIR).glob("*.json")).read_text())
    assert key["sides"] == both


def test_a_forced_side_is_honoured_and_a_changed_plate_is_refused(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    spec = _specs(tmp_path, run, panel, 1)[0]
    forced = replace(spec, identity="forced", first_sides={"fake-1": "B", "fake-2": "A"})
    assert prepare_pair(run, forced, "r", ["fake-1", "fake-2"])[1] == {
        "fake-1": "B",
        "fake-2": "A",
    }

    prepare_pair(run, spec, "r", ["fake-1"])
    Image.new("RGB", (30, 40), (0, 255, 0)).save(spec.first.path)
    with pytest.raises(HarnessError, match="changed"):
        prepare_pair(run, spec, "r", ["fake-1"])


def test_a_key_without_its_folder_is_rebuilt_from_the_key(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    spec = _specs(tmp_path, run, panel, 1)[0]
    pair_id, sides = prepare_pair(run, spec, "r", ["fake-1"])
    shutil.rmtree(blind_folder(run, spec.identity, "fake-1"))

    assert prepare_pair(run, spec, "r", ["fake-1"]) == (pair_id, sides)
    folder = blind_folder(run, spec.identity, "fake-1")
    with Image.open(folder / f"plate-{sides['fake-1']}.png") as plate:
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

    assert {side for pair in pairs for side in pair.first_sides.values()} == {"A", "B"}
    for pair in pairs:
        assert [vote.first_side for vote in pair.votes] == [
            pair.first_sides[name] for name in ("fake-1", "fake-2", "fake-3")
        ]
        assert [vote.anatomy for vote in pair.votes] == ["first", "first", "second"]
        assert pair.votes[0].second.fatal and not pair.votes[0].first.fatal
        assert pair.votes[2].first.fatal
    assert pending_judgments(run, specs, judges, config()) == 0


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


def test_a_rubric_or_judge_model_change_rebuilds_the_folder_and_judges_again(
    tmp_path: Path,
) -> None:
    run, panel, _ = make_run(tmp_path)
    specs = _specs(tmp_path, run, panel, 2)
    judge = PixelJudge("fake-1")
    first_rubric, second_rubric = tmp_path / "one.md", tmp_path / "two.md"
    first_rubric.write_text("rubric one")
    second_rubric.write_text("rubric two")
    one = replace(config(), rubric_path=str(first_rubric))

    pairs = judge_pairs(run, specs, [judge], one)
    assert len(judge.requests) == 2
    judge_pairs(run, specs, [judge], one)
    assert len(judge.requests) == 2

    two = replace(config(), rubric_path=str(second_rubric))
    assert pending_judgments(run, specs, [judge], two) == 2
    judge_pairs(run, specs, [judge], two)
    assert len(judge.requests) == 4
    for pair in pairs:
        folder = blind_folder(run, pair.spec.identity, "fake-1")
        assert (folder / "RUBRIC.md").read_text() == "rubric two"
        record = json.loads(run.path("verdicts", pair.pair_id, "fake-1.json").read_text())
        assert record["stamp"] == {
            "rubric_sha256": hashlib.sha256(b"rubric two").hexdigest(),
            "judge_contract_sha256": judge_contract_sha256(),
            "judge_kind": "claude",
            "judge_model": "model-x",
        }

    other_model = replace(two, judges=(JudgeSpec("fake-1", "claude", "model-y"), *two.judges[1:]))
    judge_pairs(run, specs, [judge], other_model)
    assert len(judge.requests) == 6


def test_a_stale_verdict_is_never_counted_when_judging_it_again_fails(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    specs = _specs(tmp_path, run, panel, 1)
    judge_pairs(run, specs, [PixelJudge("fake-1")], config())
    rubric = tmp_path / "new.md"
    rubric.write_text("new rubric")

    pairs = judge_pairs(
        run, specs, [FailingJudge("fake-1")], replace(config(), rubric_path=str(rubric))
    )

    assert pairs[0].votes == ()
    assert pairs[0].errors == (("fake-1", "fake judge failed"),)


def test_a_judge_missing_from_the_configuration_is_refused(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    specs = _specs(tmp_path, run, panel, 1)

    with pytest.raises(HarnessError, match="not in the configuration"):
        judge_pairs(run, specs, [PixelJudge("stranger")], config())
