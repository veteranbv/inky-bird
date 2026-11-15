from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest
from PIL import Image
from plate_tuning.compare import compare
from plate_tuning.generate import GenerationRequest, parse_arm
from plate_tuning.gepa_integration import gepa_optimizer
from plate_tuning.panel import (
    FrozenReferences,
    PanelSpecies,
    freeze_references,
    frozen_references,
)
from plate_tuning.rundir import Budget, Ledger, Run
from plate_tuning.safety import CandidateRejectedError, validate_candidate
from plate_tuning.tune import (
    PLATE_COMPONENT,
    EvalBatch,
    PlateTuningAdapter,
    ReflectionLM,
    ReviewItem,
    ReviewOutcome,
    TuningAdapter,
    build_labels,
    categorise,
    review_score,
    tune_plate,
    tune_review,
)
from plate_tuning_fixtures import BLUE, RED, FakeDownloader, PixelJudge, config, make_run

from inky_bird_frame.errors import GenerationError, MissingDependencyError
from inky_bird_frame.prompts import PLATE_INSTRUCTIONS, REVIEW_INSTRUCTIONS

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
    validate_candidate(PLATE_INSTRUCTIONS, target="plate", max_characters=9000)
    validate_candidate(REVIEW_INSTRUCTIONS, target="review", max_characters=9000)
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
            validate_candidate(text, target="plate", max_characters=9000)
    with pytest.raises(CandidateRejectedError):
        validate_candidate("No domains.", target="review", max_characters=9000)


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
    )

    assert result.best_text == GREY
    assert result.best_dev_score == pytest.approx(1.0)
    assert result.decision["accept"] is True, result.decision
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

    # Seed evaluation needs four plates (two baseline, two seed); the fifth
    # generation, for the first proposal, is refused.
    result = tune_plate(
        run,
        config(),
        generator,
        _judges(),
        panel,
        optimizer=stand_in_optimizer,
        reflection_lm=ScriptedReflection(GREY),
        budget=Budget(4),
        metric_calls=12,
        holdout_samples=1,
    )

    assert result.stopped_early is not None and "cap of 4" in result.stopped_early
    assert result.best_text == PLATE_INSTRUCTIONS
    assert result.decision["accept"] is False
    assert len(generator.instructions) == 4


def test_review_categories_and_scores() -> None:
    assert categorise(["The left leg floats", "Misspelled habitat label"]) == {
        "legs",
        "lettering",
    }
    assert categorise(["Ruler endpoints are reversed"]) == {"ruler"}
    item = ReviewItem("p", 1, Path("p.png"), False, frozenset({"legs"}))
    assert review_score(ReviewOutcome(False, ("Fix the leg",)), item) == 1.0
    assert review_score(ReviewOutcome(True, ()), item) == 0.0
    assert review_score(ReviewOutcome(False, ("Fix the tail",)), item) == 0.5
    assert review_score(None, item) == 0.0
    clean = ReviewItem("q", 1, Path("q.png"), True, frozenset())
    assert review_score(ReviewOutcome(True, ()), clean) == 1.0


@dataclass
class FakeReviewer:
    calls: int = 0

    def review(
        self,
        item: ReviewItem,
        species: PanelSpecies,
        references: FrozenReferences,
        instructions: str,
        work_dir: Path,
    ) -> ReviewOutcome:
        self.calls += 1
        if "Be strict." not in instructions:
            return ReviewOutcome(True, ())
        with Image.open(item.plate_path) as image:
            red = image.convert("RGB").getpixel((0, 0)) == RED
        if item.taxon_id == 102 and not red:
            raise GenerationError("Codex review output was invalid")
        return ReviewOutcome(True, ()) if red else ReviewOutcome(False, ("The leg is detached",))


def test_review_tuning_uses_panel_verdicts_as_labels(tmp_path: Path) -> None:
    source, panel, _ = make_run(tmp_path / "source")
    generator = InstructionColourGenerator()
    compare(
        source,
        config(),
        generator,
        _judges(),
        parse_arm(f"grey={_write(tmp_path / 'grey.txt', GREY)}", base=tmp_path),
        parse_arm("current=default", base=tmp_path),
        panel,
        samples=1,
        budget=Budget(8),
    )
    labels = build_labels([source.root])
    assert len(labels) == 8
    assert {item.acceptable for item in labels if item.categories == {"legs"}} == {False}

    run = Run.open(tmp_path / "tuning", seed=5)
    for entry in panel:
        freeze_references(run, entry, downloader=FakeDownloader())
    reviewer = FakeReviewer()
    strict = REVIEW_INSTRUCTIONS + " Be strict."

    summary = tune_review(
        run,
        config(),
        reviewer,
        panel,
        labels,
        optimizer=stand_in_optimizer,
        reflection_lm=ScriptedReflection(strict),
        budget=Budget(40, what="review"),
        metric_calls=12,
    )

    assert summary["best_is_current_prompt"] is False
    assert summary["decision"] == {"accept": True, "reasons": []}
    assert run.path("tune-review", "best-review-instructions.txt").read_text() == strict + "\n"
    calls = reviewer.calls
    assert calls == Ledger(run.path("reviews.jsonl")).count("review-started")
    assert Ledger(run.path("reviews.jsonl")).count("review-failed") == 1


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


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
