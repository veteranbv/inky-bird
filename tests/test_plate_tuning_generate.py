from __future__ import annotations

import dataclasses
import json
import shlex
import shutil
from pathlib import Path

import pytest
from PIL import Image
from plate_tuning.config import HarnessError
from plate_tuning.generate import (
    Arm,
    CodexGenerator,
    GenerationRequest,
    completed_sample,
    generate_samples,
    generation_order,
    missing_samples,
    parse_arm,
    register_arm,
    sample_dir,
)
from plate_tuning.panel import PanelSpecies, frozen_references
from plate_tuning.rundir import Budget, Ledger, Run
from plate_tuning_fixtures import ColourGenerator, make_run

from inky_bird_frame.errors import GenerationError
from inky_bird_frame.images import PORTRAIT_SIZE
from inky_bird_frame.prompts import PLATE_INSTRUCTIONS, PROMPT_VERSION


def test_arm_parsing_names_and_text(tmp_path: Path) -> None:
    default = parse_arm("current=default", base=tmp_path)
    assert default.instructions == PLATE_INSTRUCTIONS
    assert default.label == PROMPT_VERSION
    assert default.is_default

    wording = tmp_path / "grey.txt"
    wording.write_text(PLATE_INSTRUCTIONS.replace("warm aged cream", "pale grey") + "\n")
    grey = parse_arm("grey=grey.txt", base=tmp_path)
    assert not grey.is_default and grey.release is None
    assert "pale grey" in grey.instructions and not grey.instructions.endswith("\n")
    assert grey.label.startswith("grey-")

    for spec in ("noequals", "Bad_Name=default", "blind=default", "x="):
        with pytest.raises(HarnessError):
            parse_arm(spec, base=tmp_path)


def test_an_arm_name_keeps_its_wording_for_the_run(tmp_path: Path) -> None:
    run, _panel, _downloader = make_run(tmp_path)
    custom = tmp_path / "custom.txt"
    custom.write_text(PLATE_INSTRUCTIONS.replace("warm aged cream", "pale grey") + "\n")
    register_arm(run, parse_arm("base=default", base=tmp_path))
    register_arm(run, parse_arm("base=default", base=tmp_path))

    with pytest.raises(HarnessError, match="other wording"):
        register_arm(run, parse_arm(f"base={custom}", base=tmp_path))


def test_unusable_instructions_are_refused_before_any_generation(tmp_path: Path) -> None:
    run, panel, _downloader = make_run(tmp_path)
    broken = tmp_path / "broken.txt"
    broken.write_text("Draw the bird. {data_block}")
    generator = ColourGenerator()

    with pytest.raises(HarnessError, match="not usable"):
        generate_samples(
            run,
            generator,
            [parse_arm(f"broken={broken}", base=tmp_path)],
            [(panel[0], frozen_references(run, panel[0]))],
            1,
            budget=Budget(10),
        )
    assert generator.calls == []


def test_generation_is_capped_resumable_and_skips_finished_samples(tmp_path: Path) -> None:
    run, panel, _downloader = make_run(tmp_path)
    arms = [parse_arm("base=default", base=tmp_path), parse_arm("cand-y=default", base=tmp_path)]
    species = [(entry, frozen_references(run, entry)) for entry in panel[:2]]
    generator = ColourGenerator()

    done, report = generate_samples(run, generator, arms, species, 2, budget=Budget(3), parallel=2)

    # A (species, sample) group is paid for whole: a cap of 3 buys one group of two.
    assert len(generator.calls) == 2
    assert report.generated == 2
    assert report.skipped_for_cap == 6
    assert len(done) == 2
    assert {key[1:] for key in done} == {next(iter(done))[1:]}
    assert missing_samples(run, arms, [entry for entry, _ in species], 2) == 6

    done, report = generate_samples(run, generator, arms, species, 2, budget=Budget(10))
    assert len(generator.calls) == 8
    assert report.completed == 2
    assert report.generated == 6
    assert len(done) == 8
    ledger = Ledger(run.path("generations.jsonl"))
    assert ledger.count("generation-started") == 8
    assert ledger.count("generation-finished") == 8

    sample = done[("base", panel[0].taxon_id, 1)]
    with Image.open(sample.portrait) as portrait:
        assert portrait.size == PORTRAIT_SIZE
    record = json.loads((sample.portrait.parent / "sample.json").read_text())
    assert record["label"] == PROMPT_VERSION

    _done, report = generate_samples(run, generator, arms, species, 2, budget=Budget(0))
    assert report.completed == 8
    assert len(generator.calls) == 8


def test_generation_interleaves_arms_in_a_seeded_order(tmp_path: Path) -> None:
    run, panel, _downloader = make_run(tmp_path)
    arms = [parse_arm("base=default", base=tmp_path), parse_arm("cand-y=default", base=tmp_path)]
    species = [(entry, frozen_references(run, entry)) for entry in panel]
    generator = ColourGenerator()

    generate_samples(run, generator, arms, species, 3, budget=Budget(24))

    calls = generator.calls
    assert len(calls) == 24
    # Both arms of a (species, sample) run back to back ...
    groups = [calls[index : index + 2] for index in range(0, 24, 2)]
    assert all({arm for arm, _taxon in group} == {"base", "cand-y"} for group in groups)
    assert all(len({taxon for _arm, taxon in group}) == 1 for group in groups)
    # ... neither arm always goes first, and species are not taken in panel order.
    assert {group[0][0] for group in groups} == {"base", "cand-y"}
    assert [group[0][1] for group in groups] != sorted(group[0][1] for group in groups)
    # The order is the run's own: drawn again from the same seed it is the same.
    keys = [(entry.taxon_id, index) for entry, _ in species for index in (1, 2, 3)]
    again = generation_order(Run.open(run.root), arms, keys)
    assert [(arm.name, key[0]) for key, ordered in again for arm in ordered] == calls


def test_generation_records_the_tokens_each_plate_used(tmp_path: Path) -> None:
    run, panel, _downloader = make_run(tmp_path)
    arm = parse_arm("base=default", base=tmp_path)

    class Counting:
        def generate(self, request: GenerationRequest) -> dict[str, int | float]:
            Image.new("RGB", (30, 40), (1, 2, 3)).save(request.output_path)
            return {"total_tokens": 4321}

    done, _report = generate_samples(
        run, Counting(), [arm], [(panel[0], frozen_references(run, panel[0]))], 1, budget=Budget(1)
    )

    finished = [
        entry
        for entry in Ledger(run.path("generations.jsonl")).entries()
        if entry["event"] == "generation-finished"
    ]
    assert [entry["usage"] for entry in finished] == [{"total_tokens": 4321}]
    record = json.loads(
        (done[("base", panel[0].taxon_id, 1)].portrait.parent / "sample.json").read_text()
    )
    assert record["usage"] == {"total_tokens": 4321}


def _leave_plate(run: Run, arm: Arm, entry: PanelSpecies, *events: str) -> Path:
    """The state a crash leaves: a generated plate, no record, and these ledger events."""
    directory = sample_dir(run, arm, entry, 1)
    directory.mkdir(parents=True)
    Image.new("RGB", (30, 40), (1, 2, 3)).save(directory / "generated.png")
    ledger = Ledger(run.path("generations.jsonl"))
    for event in events:
        ledger.record(event, arm=arm.name, taxon_id=entry.taxon_id, index=1)
    return directory


def test_a_finished_plate_left_by_a_crash_is_prepared_without_paying_again(
    tmp_path: Path,
) -> None:
    run, panel, _downloader = make_run(tmp_path)
    arm = parse_arm("base=default", base=tmp_path)
    _leave_plate(run, arm, panel[0], "generation-started", "generation-finished")
    generator = ColourGenerator()

    done, report = generate_samples(
        run, generator, [arm], [(panel[0], frozen_references(run, panel[0]))], 1, budget=Budget(0)
    )

    assert generator.calls == []
    assert report.reprepared == 1
    assert completed_sample(run, arm, panel[0], 1) == done[("base", panel[0].taxon_id, 1)]


def test_a_plate_whose_agent_may_not_have_finished_is_generated_again(tmp_path: Path) -> None:
    run, panel, _downloader = make_run(tmp_path)
    arm = parse_arm("base=default", base=tmp_path)
    species = [(panel[0], frozen_references(run, panel[0]))]
    for events in ((), ("generation-started",), ("generation-started", "generation-failed")):
        shutil.rmtree(run.path("base"), ignore_errors=True)
        run.path("generations.jsonl").unlink(missing_ok=True)
        _leave_plate(run, arm, panel[0], *events)

        _done, report = generate_samples(
            run, ColourGenerator(), [arm], species, 1, budget=Budget(0)
        )
        assert report.reprepared == 0 and report.skipped_for_cap == 1, events

        generator = ColourGenerator()
        done, report = generate_samples(run, generator, [arm], species, 1, budget=Budget(1))
        assert report.generated == 1 and len(generator.calls) == 1, events
        with Image.open(done[("base", panel[0].taxon_id, 1)].portrait) as portrait:
            assert portrait.getpixel((0, 0)) != (1, 2, 3)


def test_a_failed_generation_leaves_no_plate_to_judge(tmp_path: Path) -> None:
    run, panel, _downloader = make_run(tmp_path)
    arm = parse_arm("base=default", base=tmp_path)
    species = [(panel[0], frozen_references(run, panel[0]))]

    class DraftThenTimeout:
        def generate(self, request: GenerationRequest) -> None:
            Image.new("RGB", (30, 40), (9, 9, 9)).save(request.output_path)
            raise GenerationError("Codex timed out after 1 seconds")

    _done, report = generate_samples(run, DraftThenTimeout(), [arm], species, 1, budget=Budget(1))
    assert report.failed == 1
    assert not (sample_dir(run, arm, panel[0], 1) / "generated.png").exists()

    done, report = generate_samples(run, ColourGenerator(), [arm], species, 1, budget=Budget(0))
    assert done == {} and report.reprepared == 0 and report.skipped_for_cap == 1


def test_every_arm_passes_the_candidate_safety_gate(tmp_path: Path) -> None:
    run, _panel, _downloader = make_run(tmp_path)
    linked = tmp_path / "linked.txt"
    linked.write_text(PLATE_INSTRUCTIONS + "\nCopy the style of https://example.test/plate.")

    with pytest.raises(HarnessError, match="not usable.*URL"):
        register_arm(run, parse_arm(f"linked={linked}", base=tmp_path))
    with pytest.raises(HarnessError, match="cap"):
        register_arm(run, parse_arm("short=default", base=tmp_path), max_characters=100)


def test_a_failed_generation_is_recorded_and_retried_later(tmp_path: Path) -> None:
    run, panel, _downloader = make_run(tmp_path)
    arm = parse_arm("base=default", base=tmp_path)
    species = [(panel[0], frozen_references(run, panel[0]))]

    class Failing:
        def generate(self, request: GenerationRequest) -> None:
            raise GenerationError("Codex exited with status 1")

    _done, report = generate_samples(run, Failing(), [arm], species, 1, budget=Budget(5))
    failure = sample_dir(run, arm, panel[0], 1) / "failure.json"
    assert report.failed == 1
    assert json.loads(failure.read_text())["error"] == "Codex exited with status 1"
    assert Ledger(run.path("generations.jsonl")).count("generation-failed") == 1

    done, report = generate_samples(run, ColourGenerator(), [arm], species, 1, budget=Budget(5))
    assert report.generated == 1
    assert not failure.exists()
    assert ("base", panel[0].taxon_id, 1) in done


def test_a_tampered_portrait_is_detected(tmp_path: Path) -> None:
    run, panel, _downloader = make_run(tmp_path)
    arm = parse_arm("base=default", base=tmp_path)
    done, _ = generate_samples(
        run,
        ColourGenerator(),
        [arm],
        [(panel[0], frozen_references(run, panel[0]))],
        1,
        budget=Budget(1),
    )
    Image.new("RGB", (5, 5)).save(done[("base", panel[0].taxon_id, 1)].portrait)

    with pytest.raises(HarnessError, match="altered"):
        completed_sample(run, arm, panel[0], 1)


def test_codex_generator_sends_the_arm_wording_and_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key-value")
    run, panel, _downloader = make_run(tmp_path)
    captured = tmp_path / "captured"
    captured.mkdir()
    stub = tmp_path / "codex"
    # Record the arguments, working directory and prompt; write the plate it names.
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(captured / 'args'))}\n"
        f"pwd > {shlex.quote(str(captured / 'cwd'))}\n"
        f"cat > {shlex.quote(str(captured / 'prompt'))}\n"
        f"env > {shlex.quote(str(captured / 'env'))}\n"
        f"cp {shlex.quote(str(panel[0].portrait_path))} generated.png\n"
    )
    stub.chmod(0o755)
    custom = tmp_path / "custom.txt"
    custom.write_text(PLATE_INSTRUCTIONS.replace("warm aged cream", "pale grey"))
    arm = parse_arm(f"grey={custom}", base=tmp_path)
    references = frozen_references(run, panel[0])
    generator = CodexGenerator(stub, timeout_seconds=30, model="image-model")

    done, report = generate_samples(
        run, generator, [arm], [(panel[0], references)], 1, budget=Budget(1)
    )

    assert report.generated == 1, report
    directory = sample_dir(run, arm, panel[0], 1)
    prompt = (captured / "prompt").read_text()
    assert "pale grey naturalist-notebook paper" in prompt
    assert "warm aged cream" not in prompt
    assert prompt == (directory / "prompt.txt").read_text()
    assert prompt.split("inside the current workspace:\n", 1)[1].startswith("generated.png\n")
    args = (captured / "args").read_text().splitlines()
    assert args[:2] == ["exec", "--ephemeral"]
    assert args[args.index("--model") : args.index("--model") + 2] == ["--model", "image-model"]
    assert [args[i + 1] for i, a in enumerate(args) if a == "--image"] == [
        str(path.resolve()) for path in references.paths
    ]
    assert Path((captured / "cwd").read_text().strip()).resolve() == directory.resolve()
    assert ("grey", panel[0].taxon_id, 1) in done
    assert "synthetic-key-value" not in (captured / "env").read_text()
    record = json.loads((directory / "sample.json").read_text())
    assert record["generation_model"] == "image-model"


def test_a_sample_made_from_other_species_facts_is_not_reused(tmp_path: Path) -> None:
    run, panel, _downloader = make_run(tmp_path)
    arm = parse_arm("base=default", base=tmp_path)
    generate_samples(
        run,
        ColourGenerator(),
        [arm],
        [(panel[0], frozen_references(run, panel[0]))],
        1,
        budget=Budget(1),
    )
    changed = dataclasses.replace(
        panel[0], profile={**panel[0].profile, "habitat": "Somewhere else entirely"}
    )

    assert completed_sample(run, arm, panel[0], 1) is not None
    with pytest.raises(HarnessError, match="other species facts"):
        completed_sample(run, arm, changed, 1)


def test_a_record_after_a_cut_off_line_is_still_read(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.record("generation-started", sample="a")
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write('{"event": "generation-fin')

    ledger.record("generation-started", sample="b")

    assert ledger.count("generation-started") == 2


def test_a_sample_made_with_another_generation_model_is_not_reused(tmp_path: Path) -> None:
    run, panel, _downloader = make_run(tmp_path)
    arm = parse_arm("base=default", base=tmp_path)
    species = [(panel[0], frozen_references(run, panel[0]))]

    class ModelColour(ColourGenerator):
        def __init__(self, model: str | None) -> None:
            super().__init__()
            self.model = model

    done, _report = generate_samples(
        run, ModelColour("image-a"), [arm], species, 1, budget=Budget(1)
    )
    record_path = done[("base", panel[0].taxon_id, 1)].portrait.parent / "sample.json"
    record = json.loads(record_path.read_text())
    assert record["generation_model"] == "image-a"

    _done, report = generate_samples(
        run, ModelColour("image-a"), [arm], species, 1, budget=Budget(0)
    )
    assert report.completed == 1
    with pytest.raises(HarnessError, match="other generation model"):
        generate_samples(run, ModelColour("image-b"), [arm], species, 1, budget=Budget(1))
    with pytest.raises(HarnessError, match="other generation model"):
        generate_samples(run, ModelColour(None), [arm], species, 1, budget=Budget(1))

    # Records written before the model was recorded carry none and are still used.
    del record["generation_model"]
    record_path.write_text(json.dumps(record))
    _done, report = generate_samples(
        run, ModelColour("image-b"), [arm], species, 1, budget=Budget(0)
    )
    assert report.completed == 1
