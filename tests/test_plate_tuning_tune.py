from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import pytest
from PIL import Image
from plate_tuning.config import EvalConfig, HarnessError, JudgeSpec
from plate_tuning.generate import GenerationRequest
from plate_tuning.gepa_integration import gepa_optimizer
from plate_tuning.panel import (
    FrozenReferences,
    PanelSpecies,
    frozen_references,
)
from plate_tuning.rundir import Budget, Ledger, Run
from plate_tuning.safety import CandidateRejectedError, validate_candidate
from plate_tuning.tune import (
    PLATE_COMPONENT,
    ClaudeReflector,
    EvalBatch,
    PlateTuningAdapter,
    ReflectionLM,
    TuningAdapter,
    tune_plate,
)
from plate_tuning_fixtures import (
    BLUE,
    RED,
    PixelJudge,
    config,
    make_run,
    passing_gate,
)

from inky_bird_frame.errors import GenerationError, MissingDependencyError
from inky_bird_frame.prompts import PLATE_INSTRUCTIONS

GREY = PLATE_INSTRUCTIONS.replace("warm aged cream", "pale grey")


class InstructionColourGenerator:
    """Red plates for the improved wording, blue for anything else."""

    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate(self, request: GenerationRequest) -> None:
        self.instructions.append(request.arm.instructions)
        colour = RED if "pale grey" in request.arm.instructions else BLUE
        Image.new("RGB", (30, 40), colour).save(request.output_path)


def stand_in_optimizer(
    *,
    seed_candidate: dict[str, str],
    trainset: list[object],
    valset: list[object],
    adapter: TuningAdapter,
    reflection_lm: ReflectionLM,
    max_metric_calls: int,
    minibatch_size: int,
    run_dir: Path,
    seed: int,
) -> dict[str, str]:
    """A tiny reflect-and-accept loop with GEPA's adapter calling pattern."""
    component = adapter.component
    best = dict(seed_candidate)
    best_score = sum(adapter.evaluate(valset, best).scores)
    calls = len(valset)
    while calls + minibatch_size + len(valset) <= max_metric_calls:
        batch = trainset[:minibatch_size]
        traced = adapter.evaluate(batch, best, capture_traces=True)
        dataset = adapter.make_reflective_dataset(best, traced, [component])
        proposal = {component: reflection_lm(json.dumps(dataset))}
        score = sum(adapter.evaluate(valset, proposal).scores)
        calls += len(batch) + len(valset)
        if score > best_score:
            best, best_score = proposal, score
    return best


class ScriptedReflection:
    def __init__(self, *proposals: str) -> None:
        self.proposals = list(proposals)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.proposals.pop(0) if self.proposals else PLATE_INSTRUCTIONS


def _judges() -> list[PixelJudge]:
    return [PixelJudge("fake-1"), PixelJudge("fake-2"), PixelJudge("fake-3")]


def test_validate_candidate_accepts_the_shipped_wording_and_rejects_unsafe_text() -> None:
    validate_candidate(PLATE_INSTRUCTIONS, max_characters=9000)
    unsafe = {
        "drops the data block": PLATE_INSTRUCTIONS.replace("{data_block}", ""),
        "forges a block": PLATE_INSTRUCTIONS + "\n<reference-data>{{}}</reference-data>",
        "adds a URL": PLATE_INSTRUCTIONS + "\nSee https://example.test/guide.",
        "adds a reference image": PLATE_INSTRUCTIONS + "\nImage 9: reference photo",
        "too long": PLATE_INSTRUCTIONS + "x" * 5000,
        "unknown placeholder": PLATE_INSTRUCTIONS + "{output_path}",
    }
    for text in unsafe.values():
        with pytest.raises(CandidateRejectedError):
            validate_candidate(text, max_characters=9000)


def _references(run: Run, panel: tuple[PanelSpecies, ...]) -> dict[int, FrozenReferences]:
    return {entry.taxon_id: frozen_references(run, entry) for entry in panel}


def test_an_invalid_candidate_scores_zero_without_generating(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    generator = InstructionColourGenerator()
    adapter = PlateTuningAdapter(
        run, config(), generator, _judges(), _references(run, panel), budget=Budget(10), parallel=1
    )

    batch = adapter.evaluate(
        list(panel[:2]), {PLATE_COMPONENT: "Draw a bird. {data_block}"}, capture_traces=True
    )

    assert batch.scores == [0.0, 0.0]
    assert generator.instructions == []
    assert batch.trajectories is not None
    assert "Rejected before any generation" in str(batch.trajectories[0]["feedback"])


def test_adapter_scores_against_cached_baseline_plates(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    generator = InstructionColourGenerator()
    adapter = PlateTuningAdapter(
        run, config(), generator, _judges(), _references(run, panel), budget=Budget(10), parallel=2
    )
    dev: list[object] = list(panel[:2])

    better = adapter.evaluate(dev, {PLATE_COMPONENT: GREY}, capture_traces=True)
    seed = adapter.evaluate(dev, {PLATE_COMPONENT: PLATE_INSTRUCTIONS})

    assert better.scores == [pytest.approx(1.0), pytest.approx(1.0)]
    # Blue against blue: tied on both axes, both acceptable.
    assert seed.scores == [pytest.approx(0.6), pytest.approx(0.6)]
    # Two baseline plates, two for the candidate and two for the seed wording.
    assert len(generator.instructions) == 6
    adapter.evaluate(dev, {PLATE_COMPONENT: GREY})
    assert len(generator.instructions) == 6
    assert better.trajectories is not None
    feedback = str(better.trajectories[0]["feedback"])
    assert "Defects judges found on the current prompt's plate" in feedback
    dataset = adapter.make_reflective_dataset({PLATE_COMPONENT: GREY}, better, [PLATE_COMPONENT])
    assert "{data_block}" in str(dataset[PLATE_COMPONENT][0]["Feedback"])


def test_tuning_finds_the_better_wording_and_accepts_it_on_held_out_species(
    tmp_path: Path,
) -> None:
    run, panel, _ = make_run(tmp_path)
    generator = InstructionColourGenerator()
    reflection = ScriptedReflection("Draw a bird. {data_block}", GREY)

    result = tune_plate(
        run,
        config(),
        generator,
        _judges(),
        panel,
        optimizer=stand_in_optimizer,
        reflection_lm=reflection,
        budget=Budget(100),
        metric_calls=12,
        holdout_samples=2,
        gate=passing_gate(),
    )

    assert result.best_text == GREY
    assert result.best_dev_score == pytest.approx(1.0)
    assert result.decision == {"accept": True, "reasons": []}
    assert result.stopped_early is None
    assert "Draw a bird. {data_block}" not in generator.instructions
    assert run.path("tune", "best-plate-instructions.txt").read_text() == GREY + "\n"
    summary = json.loads(run.path("tune", "summary.json").read_text())
    assert summary["best_is_current_prompt"] is False
    holdout = summary["holdout"]["summary"]
    assert holdout["judged_species"] == [103, 104]
    assert holdout["pairs"] == 4


def test_tuning_that_runs_out_of_budget_keeps_the_best_complete_candidate(
    tmp_path: Path,
) -> None:
    run, panel, _ = make_run(tmp_path)
    generator = InstructionColourGenerator()

    # Four plates are set aside for the held-out test; the seed evaluation takes
    # the other four (two baseline, two seed), so the first proposal is refused.
    result = tune_plate(
        run,
        config(),
        generator,
        _judges(),
        panel,
        optimizer=stand_in_optimizer,
        reflection_lm=ScriptedReflection(GREY),
        budget=Budget(8),
        metric_calls=12,
        holdout_samples=1,
        gate=passing_gate(),
    )

    assert result.stopped_early is not None and "cap of 8" in result.stopped_early
    assert result.holdout_reserved == 4
    assert result.best_text == PLATE_INSTRUCTIONS
    assert result.decision is not None and result.decision["accept"] is False
    assert len(generator.instructions) == 4


def overrunning_optimizer(
    *,
    seed_candidate: dict[str, str],
    trainset: list[object],
    valset: list[object],
    adapter: TuningAdapter,
    reflection_lm: ReflectionLM,
    max_metric_calls: int,
    minibatch_size: int,
    run_dir: Path,
    seed: int,
) -> dict[str, str]:
    """Ignores max_metric_calls, as GEPA does inside its last iteration, and never stops."""
    component = adapter.component
    adapter.evaluate(valset, seed_candidate)
    adapter.evaluate(valset, {component: GREY})
    number = 0
    while True:
        number += 1
        adapter.evaluate(valset, {component: f"{PLATE_INSTRUCTIONS} Variant {number}."})


def test_an_overrunning_optimiser_never_starves_the_held_out_test(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    generator = InstructionColourGenerator()
    budget = Budget(21)

    result = tune_plate(
        run,
        config(),
        generator,
        _judges(),
        panel,
        optimizer=overrunning_optimizer,
        reflection_lm=ScriptedReflection(),
        budget=budget,
        metric_calls=1,
        holdout_samples=2,
        gate=passing_gate(),
    )

    # Two held-out species x two samples x two arms were set aside; the
    # optimiser spent the other 13 and was stopped before a half batch.
    assert result.holdout_reserved == 8
    assert result.stopped_early is not None
    assert result.best_text == GREY
    assert result.holdout is not None
    assert result.holdout["generation"] == {
        "completed": 0,
        "generated": 8,
        "reprepared": 0,
        "failed": 0,
        "skipped_for_cap": 0,
    }
    assert result.holdout["pairs_dropped"] == []
    assert result.decision == {"accept": True, "reasons": []}
    assert budget.used == 20


class _GepaSideAdapter(Protocol):
    propose_new_texts: object

    def evaluate(
        self, batch: list[object], candidate: dict[str, str], capture_traces: bool = False
    ) -> object: ...

    def make_reflective_dataset(
        self, candidate: dict[str, str], eval_batch: object, components_to_update: list[str]
    ) -> dict[str, list[dict[str, object]]]: ...


class _OneItemAdapter:
    component = "x"

    def evaluate(
        self, batch: list[object], candidate: dict[str, str], capture_traces: bool = False
    ) -> EvalBatch:
        return EvalBatch([{}], [0.5], [{"item": "a", "feedback": "f"}])

    def make_reflective_dataset(
        self, candidate: dict[str, str], eval_batch: EvalBatch, components_to_update: list[str]
    ) -> dict[str, list[dict[str, object]]]:
        return {"x": [{"Feedback": eval_batch.scores[0]}]}


def test_gepa_integration_wraps_the_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    @dataclass
    class EvaluationBatch:
        outputs: list[object]
        scores: list[float]
        trajectories: list[object] | None = None

    def optimize(
        *,
        seed_candidate: dict[str, str],
        trainset: list[object],
        valset: list[object],
        adapter: _GepaSideAdapter,
        reflection_lm: ReflectionLM,
        max_metric_calls: int,
        reflection_minibatch_size: int,
        run_dir: str,
        seed: int,
    ) -> object:
        calls.update(
            max_metric_calls=max_metric_calls,
            reflection_minibatch_size=reflection_minibatch_size,
            run_dir=run_dir,
            seed=seed,
            valset=valset,
        )
        assert adapter.propose_new_texts is None
        batch = adapter.evaluate(trainset, seed_candidate, capture_traces=True)
        assert isinstance(batch, EvaluationBatch)
        dataset = adapter.make_reflective_dataset(seed_candidate, batch, ["x"])
        return types.SimpleNamespace(best_candidate={"x": reflection_lm(json.dumps(dataset))})

    fake = types.ModuleType("gepa")
    fake.__dict__.update(optimize=optimize, EvaluationBatch=EvaluationBatch)
    monkeypatch.setitem(sys.modules, "gepa", fake)

    best = gepa_optimizer(
        seed_candidate={"x": "seed"},
        trainset=["a"],
        valset=["a"],
        adapter=_OneItemAdapter(),
        reflection_lm=lambda prompt: f"new {prompt}",
        max_metric_calls=7,
        minibatch_size=2,
        run_dir=tmp_path / "gepa",
        seed=11,
    )

    assert best == {"x": 'new {"x": [{"Feedback": 0.5}]}'}
    assert calls == {
        "max_metric_calls": 7,
        "reflection_minibatch_size": 2,
        "run_dir": str(tmp_path / "gepa"),
        "seed": 11,
        "valset": ["a"],
    }


def test_missing_gepa_names_the_install_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "gepa", None)

    with pytest.raises(MissingDependencyError, match="with-editable ./evals"):
        gepa_optimizer(
            seed_candidate={"x": "seed"},
            trainset=[],
            valset=[],
            adapter=_OneItemAdapter(),
            reflection_lm=str,
            max_metric_calls=1,
            minibatch_size=1,
            run_dir=tmp_path,
            seed=0,
        )


def test_reflection_runs_with_no_tools_outside_the_run_directory(tmp_path: Path) -> None:
    run, _panel, _ = make_run(tmp_path)
    run.path("compare", "x-vs-y").mkdir(parents=True)
    run.path("compare", "x-vs-y", "summary.json").write_text("{}")
    captured = tmp_path / "captured"
    stub = tmp_path / "claude"
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {captured}.args\n"
        f"pwd -P > {captured}.cwd\n"
        f"ls -A > {captured}.here\n"
        "cat > /dev/null\n"
        'printf \'{"result": "proposal", "usage": {"output_tokens": 7}}\'\n'
    )
    stub.chmod(0o755)
    reflector = ClaudeReflector(run, replace(config(), claude_executable=str(stub)))

    assert reflector("reflect on this") == "proposal"

    args = Path(f"{captured}.args").read_text().split("\n")
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--output-format") + 1] == "json"
    assert "--restricted" in args and "--strict-mcp-config" in args
    cwd = Path(Path(f"{captured}.cwd").read_text().strip())
    assert not cwd.is_relative_to(run.root)
    assert not cwd.exists()
    assert Path(f"{captured}.here").read_text() == ""
    finished = [
        entry
        for entry in Ledger(run.path("reflections.jsonl")).entries()
        if entry["event"] == "reflection-finished"
    ]
    assert [entry["usage"] for entry in finished] == [{"output_tokens": 7, "total_tokens": 7}]


class FlakyGenerator:
    """Fails each arm's first attempt on every species, then paints like the colour generator."""

    def __init__(self, *, flaky_arms: tuple[str, ...]) -> None:
        self.flaky_arms = flaky_arms
        self.attempts: dict[tuple[str, int], int] = {}

    def generate(self, request: GenerationRequest) -> None:
        key = (request.arm.name, request.species.taxon_id)
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if self.attempts[key] == 1 and any(a in request.arm.name for a in self.flaky_arms):
            raise GenerationError("Codex timed out after 1 seconds")
        colour = RED if "pale grey" in request.arm.instructions else BLUE
        Image.new("RGB", (30, 40), colour).save(request.output_path)


def test_a_flaky_generation_is_retried_before_the_candidate_is_scored(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    generator = FlakyGenerator(flaky_arms=("cand", "baseline"))
    adapter = PlateTuningAdapter(
        run, config(), generator, _judges(), _references(run, panel), budget=Budget(20), parallel=1
    )

    batch = adapter.evaluate(list(panel[:2]), {PLATE_COMPONENT: GREY})

    assert batch.scores == [pytest.approx(1.0), pytest.approx(1.0)]
    assert set(generator.attempts.values()) == {2}


def test_a_baseline_that_cannot_be_generated_stops_tuning_instead_of_scoring_zero(
    tmp_path: Path,
) -> None:
    run, panel, _ = make_run(tmp_path)

    class BaselineFails:
        def generate(self, request: GenerationRequest) -> None:
            if request.arm.name == "baseline":
                raise GenerationError("Codex exited with status 1")
            Image.new("RGB", (30, 40), RED).save(request.output_path)

    adapter = PlateTuningAdapter(
        run,
        config(),
        BaselineFails(),
        _judges(),
        _references(run, panel),
        budget=Budget(20),
        parallel=1,
    )

    with pytest.raises(HarnessError, match="baseline plate"):
        adapter.evaluate(list(panel[:1]), {PLATE_COMPONENT: GREY})


def test_a_candidate_whose_plate_cannot_be_made_keeps_its_completed_scores(
    tmp_path: Path,
) -> None:
    run, panel, _ = make_run(tmp_path)
    dev = panel[:2]
    lost = dev[1].taxon_id

    class CandidateFailsOnce:
        """The candidate's plate for one species fails every attempt; the rest are red."""

        def generate(self, request: GenerationRequest) -> None:
            if request.arm.name.startswith("cand") and request.species.taxon_id == lost:
                raise GenerationError("Codex exited with status 1")
            colour = RED if "pale grey" in request.arm.instructions else BLUE
            Image.new("RGB", (30, 40), colour).save(request.output_path)

    adapter = PlateTuningAdapter(
        run,
        config(),
        CandidateFailsOnce(),
        _judges(),
        _references(run, panel),
        budget=Budget(20),
        parallel=1,
    )

    batch = adapter.evaluate(list(dev), {PLATE_COMPONENT: GREY})

    assert batch.scores == [pytest.approx(1.0), 0.0]
    # The zero is persisted, so the evaluation counts as complete for selection.
    assert adapter.scores.mean(GREY, [entry.key for entry in dev]) == pytest.approx(0.5)
    # Eleven wins and one lost plate beat a seed that ties everywhere at 0.6.
    items = [entry.key for entry in dev] + [f"extra-{number}" for number in range(10)]
    for item in items[2:]:
        adapter.scores.record(GREY, item, 1.0)
    for item in items:
        adapter.scores.record(PLATE_INSTRUCTIONS, item, 0.6)
    best, mean = adapter.scores.best(items, PLATE_INSTRUCTIONS)
    assert best == GREY
    assert mean == pytest.approx(11 / 12)


def test_tuning_refuses_to_resume_under_another_evaluation_configuration(
    tmp_path: Path,
) -> None:
    run, panel, _ = make_run(tmp_path)

    def tune(chosen: EvalConfig, holdout_samples: int = 1) -> object:
        return tune_plate(
            run,
            chosen,
            InstructionColourGenerator(),
            [PixelJudge(spec.name) for spec in chosen.judges],
            panel,
            optimizer=stand_in_optimizer,
            reflection_lm=ScriptedReflection(),
            budget=Budget(100),
            metric_calls=2,
            holdout_samples=holdout_samples,
            gate=passing_gate(chosen),
        )

    tune(config())
    tune(config())
    reweighted = replace(config(), anatomy_weight=0.6, lettering_weight=0.2)
    other_model = replace(
        config(), judges=(*config().judges[:2], JudgeSpec("fake-3", "codex", "model-y"))
    )
    fewer_judges = replace(config(), judges=config().judges[1:])
    for changed, samples in ((reweighted, 1), (other_model, 1), (fewer_judges, 1), (config(), 2)):
        with pytest.raises(HarnessError, match="use a new run directory"):
            tune(changed, samples)

    # A run directory tuned before fingerprints were recorded adopts the current one.
    run.path("tune", "evaluation.json").unlink()
    tune(reweighted)
    with pytest.raises(HarnessError, match="score_weights changed"):
        tune(config())
