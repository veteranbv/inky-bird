"""Reflective tuning of the plate prompt.

The optimiser is reached through the Optimizer protocol; the real one is GEPA,
isolated in gepa_integration.py. The adapters here follow GEPA's adapter
contract: evaluate a candidate on a batch and return per-item scores in [0, 1]
plus the judges' feedback text for reflection.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from inky_bird_frame.prompts import PLATE_INSTRUCTION_FIELDS, PLATE_INSTRUCTIONS, PROMPT_VERSION

from .blind import JudgedPair, Vote, judge_pairs
from .compare import compare, pair_specs
from .config import BudgetExhaustedError, EvalConfig, HarnessError
from .gate import CalibrationGate, judged_with
from .generate import (
    GENERATIONS_LEDGER,
    Arm,
    Generator,
    Sample,
    generate_samples,
    missing_samples,
    profile_sha256,
)
from .judges import (
    CLAUDE_ISOLATION,
    Judge,
    JudgeError,
    claude_envelope,
    claude_usage,
    find_executable,
    run_bounded,
    scratch_folder,
)
from .panel import FrozenReferences, PanelSpecies, frozen_references
from .rundir import (
    Budget,
    Ledger,
    Run,
    read_object,
    sha256_text,
    write_json,
    write_text,
)
from .safety import CandidateRejectedError, validate_candidate
from .stats import pair_outcome, win_score

PLATE_COMPONENT: Final = "plate_instructions"
_CANDIDATE_ARM: Final = re.compile(r"cand-[0-9a-f]{12}")
BASELINE_ARM: Final = "baseline"
EVALUATION_RECORD: Final = ("tune", "evaluation.json")
SELECTION_RECORD: Final = ("tune", "selection.json")

ReflectionLM = Callable[[str], str]


def constraints(max_characters: int) -> str:
    names = ", ".join("{" + name + "}" for name in sorted(PLATE_INSTRUCTION_FIELDS))
    return (
        "Rules for any rewrite: the text is a Python format template. Keep each of these"
        f" placeholders exactly once: {names}. Write any other brace doubled. Stay under"
        f" {max_characters} characters, include no URLs, and do not add a data block, a tool"
        " request or an output path: the program adds those around the text."
    )


@dataclass
class EvalBatch:
    """GEPA's EvaluationBatch shape, without importing GEPA."""

    outputs: list[dict[str, object]]
    scores: list[float]
    trajectories: list[dict[str, object]] | None


class Optimizer(Protocol):
    def __call__(
        self,
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
        """Return the best candidate found."""


class TuningAdapter(Protocol):
    component: str

    def evaluate(
        self, batch: list[object], candidate: dict[str, str], capture_traces: bool = False
    ) -> EvalBatch: ...

    def make_reflective_dataset(
        self, candidate: dict[str, str], eval_batch: EvalBatch, components_to_update: list[str]
    ) -> dict[str, list[dict[str, object]]]: ...


class ClaudeReflector:
    """The reflection model: one headless Claude call per proposal, with no tools.

    It runs in an empty temporary folder outside the run directory, with none of
    the user's settings, so nothing but the prompt can shape a proposal.
    """

    def __init__(self, run: Run, config: EvalConfig) -> None:
        self.executable = find_executable(config.claude_executable)
        self.model = config.reflection_model
        self.timeout = config.reflection_timeout_seconds
        self.work = run.path("tune", "reflection")
        self.ledger = Ledger(run.path("reflections.jsonl"))
        self._lock = threading.Lock()
        self._count = 0

    def command(self) -> list[str]:
        return [
            str(self.executable),
            "-p",
            # The JSON result carries the token counts as well as the text.
            "--output-format",
            "json",
            "--model",
            self.model,
            "--tools",
            "",
            *CLAUDE_ISOLATION,
        ]

    def __call__(self, prompt: str) -> str:
        with self._lock:
            self._count += 1
            number = self._count
        self.work.mkdir(parents=True, exist_ok=True)
        self.ledger.record("reflection-started", number=number)
        with scratch_folder("plate-reflect-") as folder:
            stdout = run_bounded(
                self.executable,
                folder,
                self.timeout,
                self.command(),
                prompt,
                self.work / f"reflection-{number:04d}.log",
            ).stdout
        envelope = claude_envelope(stdout)
        text = envelope.get("result")
        if not isinstance(text, str):
            raise JudgeError("Claude printed no reflection text")
        self.ledger.record("reflection-finished", number=number, usage=claude_usage(envelope))
        return text


def _defect_lines(votes: Sequence[Vote], role: str) -> list[str]:
    lines: list[str] = []
    for vote in votes:
        plate = vote.first if role == "first" else vote.second
        for defect in plate.defects:
            lines.append(
                f"- {defect.severity} {defect.category} at {defect.location}"
                f" ({defect.reference}): {defect.description}"
            )
    return lines


def plate_feedback(species: PanelSpecies, pair: JudgedPair) -> str:
    outcome = pair_outcome(pair.votes)
    words = {"first": "the candidate", "second": "the current prompt", "tie": "neither"}
    lines = [
        f"Species: {species.common_name} ({species.stratum}). Blind judges compared the"
        " candidate prompt's plate with the current prompt's plate.",
    ]
    if outcome is not None:
        lines.append(
            f"Anatomy majority preferred {words[outcome.anatomy]}; lettering majority preferred"
            f" {words[outcome.lettering]}. Candidate plate acceptable:"
            f" {'yes' if outcome.first_acceptable else 'no'}."
        )
    found = _defect_lines(pair.votes, "first")
    lines.append("Defects judges found on the candidate's plate:" if found else "No defects found.")
    lines.extend(found)
    baseline = _defect_lines(pair.votes, "second")
    if baseline:
        lines.append("Defects judges found on the current prompt's plate, for contrast:")
        lines.extend(baseline)
    return "\n".join(lines)


def plate_score(pair: JudgedPair, config: EvalConfig) -> float:
    outcome = pair_outcome(pair.votes)
    if outcome is None:
        raise HarnessError("No judge returned a verdict; tuning cannot score this candidate")
    return (
        config.anatomy_weight * win_score(outcome.anatomy)
        + config.lettering_weight * win_score(outcome.lettering)
        + config.acceptable_weight * float(outcome.first_acceptable)
    )


class _Scores:
    """Per-candidate, per-item scores, persisted so the best candidate survives a stop."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.Lock()

    def record(self, text: str, item: str, score: float) -> None:
        path = self.directory / f"{sha256_text(text)[:16]}.json"
        with self._lock:
            stored = read_object(path) if path.exists() else {"text": text, "scores": {}}
            scores = stored.get("scores")
            if not isinstance(scores, dict):
                raise HarnessError(f"{path.name} has no score table")
            scores[item] = score
            write_json(path, {"text": text, "scores": scores})

    def mean(self, text: str, items: Sequence[str]) -> float | None:
        path = self.directory / f"{sha256_text(text)[:16]}.json"
        if not path.exists():
            return None
        scores = read_object(path).get("scores")
        if not isinstance(scores, dict):
            return None
        values = [scores.get(item) for item in items]
        numbers = [float(value) for value in values if isinstance(value, float | int)]
        return sum(numbers) / len(items) if len(numbers) == len(items) else None

    def best(self, items: Sequence[str], seed_text: str) -> tuple[str, float | None]:
        """The candidate with the highest mean over every item; the seed wins ties."""
        best_text, best_mean = seed_text, None
        for path in sorted(self.directory.glob("*.json")):
            stored = read_object(path)
            text, scores = stored.get("text"), stored.get("scores")
            if not isinstance(text, str) or not isinstance(scores, dict):
                continue
            if not all(isinstance(scores.get(item), float | int) for item in items):
                continue
            mean = sum(float(scores[item]) for item in items) / len(items)
            if best_mean is None or mean > best_mean or (mean == best_mean and text == seed_text):
                best_text, best_mean = text, mean
        return best_text, best_mean


def _rejected(batch: Sequence[object], reason: str, capture_traces: bool) -> EvalBatch:
    feedback = f"Rejected before any generation: {reason}"
    return EvalBatch(
        outputs=[{"rejected": reason} for _ in batch],
        scores=[0.0 for _ in batch],
        trajectories=[{"feedback": feedback, "item": None} for _ in batch]
        if capture_traces
        else None,
    )


class PlateTuningAdapter:
    component = PLATE_COMPONENT

    def __init__(
        self,
        run: Run,
        config: EvalConfig,
        generator: Generator,
        judges: Sequence[Judge],
        references: dict[int, FrozenReferences],
        *,
        budget: Budget,
        parallel: int,
    ) -> None:
        self.run = run
        self.config = config
        self.generator = generator
        self.judges = judges
        self.references = references
        self.budget = budget
        self.parallel = parallel
        self.baseline = Arm(BASELINE_ARM, PLATE_INSTRUCTIONS, PROMPT_VERSION)
        self.scores = _Scores(run.path("tune", "candidates"))

    def evaluate(
        self, batch: list[object], candidate: dict[str, str], capture_traces: bool = False
    ) -> EvalBatch:
        species = [_species(item) for item in batch]
        text = candidate.get(self.component, "")
        try:
            validate_candidate(text, max_characters=self.config.max_candidate_characters)
        except CandidateRejectedError as exc:
            return _rejected(batch, str(exc), capture_traces)
        arm = candidate_arm(text)
        with_references = [(entry, self.references[entry.taxon_id]) for entry in species]
        done = self._generate(arm, with_references)
        specs = pair_specs("tune", arm, self.baseline, with_references, done, 1)
        judged = {
            pair.spec.taxon_id: pair
            for pair in judge_pairs(
                self.run, specs, self.judges, self.config, parallel=self.parallel
            )
        }
        outputs: list[dict[str, object]] = []
        scores: list[float] = []
        trajectories: list[dict[str, object]] = []
        for entry in species:
            pair = judged.get(entry.taxon_id)
            if pair is None:
                # The candidate's plate failed every attempt; it scores nothing,
                # recorded so the completed evaluation stays eligible for selection.
                self.scores.record(text, entry.key, 0.0)
                outputs.append({"taxon_id": entry.taxon_id, "generated": False})
                scores.append(0.0)
                trajectories.append(
                    {
                        "item": entry.common_name,
                        "feedback": "The plate could not be generated in"
                        f" {self.config.generation_attempts} attempts.",
                    }
                )
                continue
            score = plate_score(pair, self.config)
            self.scores.record(text, entry.key, score)
            outputs.append({"taxon_id": entry.taxon_id, "pair_id": pair.pair_id, "score": score})
            scores.append(score)
            trajectories.append(
                {
                    "item": f"{entry.common_name} ({entry.stratum})",
                    "feedback": plate_feedback(entry, pair),
                }
            )
        return EvalBatch(outputs, scores, trajectories if capture_traces else None)

    def _generate(
        self, arm: Arm, species: list[tuple[PanelSpecies, FrozenReferences]]
    ) -> dict[tuple[str, int, int], Sample]:
        """Both plates per species, retrying failures so flakiness is not read as quality.

        A batch the cap cannot pay for in full stops tuning before it starts,
        so no candidate is left half evaluated.
        """
        needed = missing_samples(self.run, [arm, self.baseline], [e for e, _r in species], 1)
        if needed > self.budget.available:
            raise BudgetExhaustedError(
                f"The generation cap of {self.budget.limit} leaves too little for this batch"
            )
        done: dict[tuple[str, int, int], Sample] = {}
        for _attempt in range(self.config.generation_attempts):
            done, report = generate_samples(
                self.run,
                self.generator,
                [arm, self.baseline],
                species,
                1,
                budget=self.budget,
                parallel=self.parallel,
                max_characters=self.config.max_candidate_characters,
            )
            if report.skipped_for_cap:
                raise BudgetExhaustedError(f"The generation cap of {self.budget.limit} was reached")
            if not report.failed:
                break
        for entry, _references in species:
            if (BASELINE_ARM, entry.taxon_id, 1) not in done:
                raise HarnessError(
                    f"The baseline plate for {entry.common_name} could not be generated in"
                    f" {self.config.generation_attempts} attempts; see generations.jsonl"
                )
        return done

    def make_reflective_dataset(
        self, candidate: dict[str, str], eval_batch: EvalBatch, components_to_update: list[str]
    ) -> dict[str, list[dict[str, object]]]:
        return _reflective(
            self.component, eval_batch, constraints(self.config.max_candidate_characters)
        )


def _reflective(
    component: str, eval_batch: EvalBatch, rules: str
) -> dict[str, list[dict[str, object]]]:
    records: list[dict[str, object]] = [
        {
            "Inputs": str(trajectory.get("item")),
            "Generated Outputs": f"score {score:.2f} of 1",
            "Feedback": f"{trajectory.get('feedback')}\n\n{rules}",
        }
        for trajectory, score in zip(eval_batch.trajectories or [], eval_batch.scores, strict=False)
    ]
    return {component: records}


def _species(item: object) -> PanelSpecies:
    if not isinstance(item, PanelSpecies):
        raise HarnessError("The plate tuner evaluates panel species")
    return item


@dataclass(frozen=True)
class TuneResult:
    best_text: str
    best_dev_score: float | None
    stopped_early: str | None
    holdout_reserved: int
    holdout: dict[str, object] | None
    decision: dict[str, object] | None
    decision_note: str | None
    exploratory: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "best_is_current_prompt": self.best_text == PLATE_INSTRUCTIONS,
            "best_sha256": sha256_text(self.best_text),
            "best_dev_score": self.best_dev_score,
            "stopped_early": self.stopped_early,
            "holdout_generations_reserved": self.holdout_reserved,
            "holdout": self.holdout,
            "exploratory": self.exploratory,
            "decision": self.decision,
            "decision_note": self.decision_note,
        }


def candidate_arm(text: str) -> Arm:
    digest = sha256_text(text)[:12]
    return Arm(f"cand-{digest}", text, f"candidate-{digest}")


def candidate_generations(run: Run, dev: Sequence[PanelSpecies]) -> int:
    """Candidate plates earlier tune commands in this run started on the dev species.

    Each is part of the optimiser's metric-call share and already counts as used
    against the cap, so a resumed tune must not plan for it again.
    """
    taxa = {entry.taxon_id for entry in dev}
    return sum(
        1
        for entry in Ledger(run.path(GENERATIONS_LEDGER)).entries()
        if entry.get("event") == "generation-started"
        and entry.get("taxon_id") in taxa
        and isinstance(entry.get("arm"), str)
        and _CANDIDATE_ARM.fullmatch(str(entry["arm"])) is not None
    )


def evaluation_fingerprint(
    config: EvalConfig, panel: Sequence[PanelSpecies], holdout_samples: int
) -> dict[str, object]:
    """Everything that changes what a tuning score, the optimiser's state or the selection means.

    config is the judging configuration: with a passing calibration, only its
    calibrated judges.
    """
    return {
        **judged_with(config),
        "score_weights": {
            "anatomy": config.anatomy_weight,
            "lettering": config.lettering_weight,
            "acceptable": config.acceptable_weight,
        },
        "generation_model": config.generation_model,
        "generation_attempts": config.generation_attempts,
        "max_candidate_characters": config.max_candidate_characters,
        "species": [
            {"key": entry.key, "split": entry.split, "profile_sha256": profile_sha256(entry)}
            for entry in panel
            if entry.split in ("dev", "holdout")
        ],
        "holdout_samples": holdout_samples,
    }


def bind_evaluation(run: Run, fingerprint: dict[str, object], *, record: bool) -> None:
    """Refuse to continue tuning in a run directory scored under another configuration.

    The first tune records the fingerprint (record=True); a run directory
    tuned before fingerprints were recorded adopts the current one.
    """
    path = run.path(*EVALUATION_RECORD)
    if not path.exists():
        if record:
            write_json(path, fingerprint)
        return
    stored = read_object(path)
    changed = sorted(
        key for key in {*stored, *fingerprint} if stored.get(key) != fingerprint.get(key)
    )
    if changed:
        raise HarnessError(
            "This run directory was tuned under another evaluation configuration"
            f" ({', '.join(changed)} changed), so its scores, optimiser state and selection"
            " do not apply; use a new run directory"
        )


def selected_candidate(run: Run) -> tuple[str, float | None, str | None] | None:
    """The candidate an earlier tune in this run selected for the held-out test, if any."""
    path = run.path(*SELECTION_RECORD)
    if not path.exists():
        return None
    stored = read_object(path)
    text, score, stopped = (
        stored.get("text"),
        stored.get("best_dev_score"),
        stored.get("stopped_early"),
    )
    if (
        not isinstance(text, str)
        or stored.get("sha256") != sha256_text(text)
        or not (score is None or isinstance(score, float | int))
        or not (stopped is None or isinstance(stopped, str))
    ):
        raise HarnessError(f"{path.name} is not a usable selection record")
    return text, None if score is None else float(score), stopped


def holdout_reservation(
    run: Run,
    baseline: Arm,
    holdout: Sequence[PanelSpecies],
    samples: int,
    selected: str | None = None,
) -> int:
    """Generations the held-out test still needs.

    Before a candidate is selected none of its plates can be cached, so every
    candidate plate is counted; once one is selected (selected), only its and
    the baseline's missing plates are. The current prompt needs no test.
    """
    if selected is None:
        return len(holdout) * samples + missing_samples(run, [baseline], holdout, samples)
    if selected == PLATE_INSTRUCTIONS:
        return 0
    return missing_samples(run, [candidate_arm(selected), baseline], holdout, samples)


def _optimise(
    run: Run,
    config: EvalConfig,
    adapter: PlateTuningAdapter,
    dev: Sequence[PanelSpecies],
    holdout: Sequence[PanelSpecies],
    *,
    optimizer: Optimizer,
    reflection_lm: ReflectionLM,
    budget: Budget,
    metric_calls: int,
    holdout_samples: int,
) -> tuple[str, float | None, str | None, int]:
    """Run the optimiser with the held-out test's generations held back.

    Returns the best text, its mean dev score, why tuning stopped early (or
    None) and the generations held back.
    """
    reserved = holdout_reservation(run, adapter.baseline, holdout, holdout_samples)
    keys = [entry.key for entry in dev]
    stopped: str | None = None
    with budget.holding(reserved):
        try:
            best = optimizer(
                seed_candidate={PLATE_COMPONENT: PLATE_INSTRUCTIONS},
                trainset=list(dev),
                valset=list(dev),
                adapter=adapter,
                reflection_lm=reflection_lm,
                max_metric_calls=metric_calls,
                minibatch_size=config.tune_minibatch_size,
                run_dir=run.path("gepa"),
                seed=run.derived("gepa") % 2**31,
            )
            best_text = best.get(PLATE_COMPONENT, PLATE_INSTRUCTIONS)
            best_score = adapter.scores.mean(best_text, keys)
            if best_score is None:
                # The optimiser's pick was never scored on every dev species here.
                best_text, best_score = adapter.scores.best(keys, PLATE_INSTRUCTIONS)
        except BudgetExhaustedError as exc:
            stopped = str(exc)
            best_text, best_score = adapter.scores.best(keys, PLATE_INSTRUCTIONS)
    return best_text, best_score, stopped, reserved


def tune_plate(
    run: Run,
    config: EvalConfig,
    generator: Generator,
    judges: Sequence[Judge],
    panel: Sequence[PanelSpecies],
    *,
    optimizer: Optimizer,
    reflection_lm: ReflectionLM,
    budget: Budget,
    metric_calls: int,
    holdout_samples: int,
    gate: CalibrationGate,
    exploratory: bool = False,
    parallel: int = 1,
) -> TuneResult:
    """Tune on the dev species within the cap, then test the best candidate on held-out ones.

    The held-out test's generations are set aside before the optimiser starts,
    so however the optimiser spends its share (GEPA can finish an iteration
    past max_metric_calls), the held-out comparison runs in full. When the
    optimiser's share runs out it stops, and the best candidate scored on
    every dev species is tested. The selection is recorded before the test,
    so a resumed tune goes straight to the test of the same candidate.
    """
    bind_evaluation(run, evaluation_fingerprint(config, panel, holdout_samples), record=True)
    dev = [entry for entry in panel if entry.split == "dev"]
    holdout = [entry for entry in panel if entry.split == "holdout"]
    references = {entry.taxon_id: frozen_references(run, entry) for entry in panel}
    adapter = PlateTuningAdapter(
        run, config, generator, judges, references, budget=budget, parallel=parallel
    )
    selection = selected_candidate(run)
    if selection is not None:
        best_text, best_score, stopped = selection
        reserved = holdout_reservation(run, adapter.baseline, holdout, holdout_samples, best_text)
    else:
        best_text, best_score, stopped, reserved = _optimise(
            run,
            config,
            adapter,
            dev,
            holdout,
            optimizer=optimizer,
            reflection_lm=reflection_lm,
            budget=budget,
            metric_calls=metric_calls,
            holdout_samples=holdout_samples,
        )
    validate_candidate(best_text, max_characters=config.max_candidate_characters)
    if selection is None:
        write_json(
            run.path(*SELECTION_RECORD),
            {
                "text": best_text,
                "sha256": sha256_text(best_text),
                "best_dev_score": best_score,
                "stopped_early": stopped,
            },
        )
    write_text(run.path("tune", "best-plate-instructions.txt"), best_text + "\n")
    holdout_result: dict[str, object] | None = None
    decision: dict[str, object] | None = None
    note: str | None = None
    if best_text == PLATE_INSTRUCTIONS:
        note = "tuning found nothing better than the current prompt on the dev species"
        if not exploratory:
            decision = {"accept": False, "reasons": [note]}
    else:
        result = compare(
            run,
            config,
            generator,
            judges,
            candidate_arm(best_text),
            adapter.baseline,
            holdout,
            samples=holdout_samples,
            budget=budget,
            gate=gate,
            exploratory=exploratory,
            parallel=parallel,
            scope="tune",
        )
        holdout_result = result.as_dict()
        decision = None if result.decision is None else result.decision.as_dict()
        note = result.decision_note
    outcome = TuneResult(
        best_text, best_score, stopped, reserved, holdout_result, decision, note, exploratory
    )
    write_json(run.path("tune", "summary.json"), outcome.as_dict())
    return outcome
