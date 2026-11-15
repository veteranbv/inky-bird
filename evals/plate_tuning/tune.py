"""Reflective tuning of the plate prompt (with generation) and the review prompt (without).

The optimiser is reached through the Optimizer protocol; the real one is GEPA,
isolated in gepa_integration.py. The adapters here follow GEPA's adapter
contract: evaluate a candidate on a batch and return per-item scores in [0, 1]
plus the judges' feedback text for reflection.
"""

from __future__ import annotations

import functools
import threading
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from inky_bird_frame.errors import GenerationError
from inky_bird_frame.prompts import (
    PLATE_INSTRUCTION_FIELDS,
    PLATE_INSTRUCTIONS,
    PROMPT_VERSION,
    REVIEW_INSTRUCTION_FIELDS,
    REVIEW_INSTRUCTIONS,
)

from .blind import JudgedPair, Vote, judge_pairs
from .compare import compare, pair_specs
from .config import BudgetExhaustedError, EvalConfig, HarnessError
from .gate import CalibrationGate
from .generate import (
    Arm,
    Generator,
    InstructedCodexRunner,
    Sample,
    generate_samples,
    missing_samples,
)
from .judges import (
    CATEGORIES,
    CLAUDE_ISOLATION,
    Judge,
    JudgeError,
    Usage,
    claude_envelope,
    claude_usage,
    codex_usage,
    find_executable,
    run_bounded,
    scratch_folder,
)
from .panel import FrozenReferences, PanelSpecies, frozen_references
from .rundir import (
    Budget,
    Ledger,
    Run,
    read_json,
    read_object,
    resolve_recorded,
    run_tasks,
    sha256_file,
    sha256_text,
    write_json,
    write_text,
)
from .safety import CandidateRejectedError, Target, validate_candidate
from .stats import decide_improvement, majority_flag, pair_outcome, win_score

PLATE_COMPONENT: Final = "plate_instructions"
REVIEW_COMPONENT: Final = "review_instructions"
BASELINE_ARM: Final = "baseline"
REVIEWS_LEDGER: Final = "reviews.jsonl"

ReflectionLM = Callable[[str], str]


def constraints(target: Target, max_characters: int) -> str:
    fields = PLATE_INSTRUCTION_FIELDS if target == "plate" else REVIEW_INSTRUCTION_FIELDS
    names = ", ".join("{" + name + "}" for name in sorted(fields))
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
            validate_candidate(
                text, target="plate", max_characters=self.config.max_candidate_characters
            )
        except CandidateRejectedError as exc:
            return _rejected(batch, str(exc), capture_traces)
        digest = sha256_text(text)[:12]
        arm = Arm(f"cand-{digest}", text, f"candidate-{digest}")
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
                # The candidate's plate failed every attempt; it scores nothing.
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
            self.component, eval_batch, constraints("plate", self.config.max_candidate_characters)
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


def holdout_reservation(
    run: Run, baseline: Arm, holdout: Sequence[PanelSpecies], samples: int
) -> int:
    """Generations the held-out test needs: every candidate plate, plus missing baselines.

    The candidate is not known yet, so none of its plates can be cached.
    """
    return len(holdout) * samples + missing_samples(run, [baseline], holdout, samples)


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
    every dev species is tested.
    """
    dev = [entry for entry in panel if entry.split == "dev"]
    holdout = [entry for entry in panel if entry.split == "holdout"]
    references = {entry.taxon_id: frozen_references(run, entry) for entry in panel}
    adapter = PlateTuningAdapter(
        run, config, generator, judges, references, budget=budget, parallel=parallel
    )
    reserved = holdout_reservation(run, adapter.baseline, holdout, holdout_samples)
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
            best_score = adapter.scores.mean(best_text, [entry.key for entry in dev])
            if best_score is None:
                # The optimiser's pick was never scored on every dev species here.
                best_text, best_score = adapter.scores.best(
                    [entry.key for entry in dev], PLATE_INSTRUCTIONS
                )
        except BudgetExhaustedError as exc:
            stopped = str(exc)
            best_text, best_score = adapter.scores.best(
                [entry.key for entry in dev], PLATE_INSTRUCTIONS
            )
    validate_candidate(best_text, target="plate", max_characters=config.max_candidate_characters)
    write_text(run.path("tune", "best-plate-instructions.txt"), best_text + "\n")
    holdout_result: dict[str, object] | None = None
    decision: dict[str, object] | None = None
    note: str | None = None
    if best_text == PLATE_INSTRUCTIONS:
        note = "tuning found nothing better than the current prompt on the dev species"
        if not exploratory:
            decision = {"accept": False, "reasons": [note]}
    else:
        digest = sha256_text(best_text)[:12]
        result = compare(
            run,
            config,
            generator,
            judges,
            Arm(f"cand-{digest}", best_text, f"candidate-{digest}"),
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


# ---- review prompt tuning -------------------------------------------------

CATEGORY_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "legs": ("leg", "foot", "feet", "toe", "tarsus", "talon", "claw"),
    "wings": ("wing", "primar", "secondar", "covert", "tertial"),
    "head_bill": ("head", "bill", "beak", "mandible", "eye", "iris", "pupil", "neck", "gape"),
    "tail": ("tail", "rectri"),
    "proportions": ("proportion", "posture", "silhouette", "stretch", "body shape", "duplicat"),
    "plumage": ("plumage", "colour", "color", "feather", "pattern", "streak"),
    "lettering": ("text", "label", "spell", "letter", "typo", "misprint", "wingspan", "weight"),
    "ruler": ("ruler", "tick", "schematic", "endpoint"),
}


def categorise(findings: Sequence[str]) -> frozenset[str]:
    """Map free-text review corrections to the judges' defect categories by keyword."""
    found: set[str] = set()
    for finding in findings:
        text = finding.casefold()
        for category, words in CATEGORY_KEYWORDS.items():
            if any(word in text for word in words):
                found.add(category)
    return frozenset(found)


@dataclass(frozen=True)
class ReviewItem:
    """An existing plate with the judge panel's verdict on it as its label."""

    plate_id: str
    taxon_id: int
    plate_path: Path
    acceptable: bool
    categories: frozenset[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "plate_id": self.plate_id,
            "taxon_id": self.taxon_id,
            "plate_path": str(self.plate_path),
            "acceptable": self.acceptable,
            "categories": sorted(self.categories),
        }


def label_judges(summary: dict[str, object], gate: CalibrationGate | None) -> set[str] | None:
    """The judges whose votes in one earlier comparison may label plates.

    With a gate, only calibrated judges count, and only where that comparison
    used the same rubric, judging contract, kind and model; None means every
    judge (an exploratory run).
    """
    if gate is None:
        return None
    judged = summary.get("judged_with")
    if not isinstance(judged, dict):
        return set()
    if judged.get("rubric_sha256") != gate.rubric_sha256:
        return set()
    if judged.get("judge_contract_sha256") != gate.judge_contract_sha256:
        return set()
    listed = judged.get("judges")
    allowed: set[str] = set()
    for entry in listed if isinstance(listed, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name"))
        if gate.specs.get(name) == (entry.get("kind"), entry.get("model")):
            allowed.add(name)
    return allowed


def build_labels(sources: Sequence[Path], gate: CalibrationGate | None = None) -> list[ReviewItem]:
    """Label every plate judged in earlier runs' comparisons with the panel majority.

    Only votes label_judges admits count; a plate without one is unlabelled.
    """
    items: dict[str, ReviewItem] = {}
    for source in sources:
        for records_path in sorted(source.glob("*/*/pairs.json")):
            summary_path = records_path.parent / "summary.json"
            summary = read_object(summary_path) if summary_path.exists() else {}
            allowed = label_judges(summary, gate)
            records = read_object_list(records_path)
            for record in records:
                for role in ("first", "second"):
                    item = _label(source, record, role, allowed)
                    if item is not None:
                        items.setdefault(item.plate_id, item)
    return sorted(items.values(), key=lambda item: (item.taxon_id, item.plate_id))


def read_object_list(path: Path) -> list[dict[str, object]]:
    value = read_json(path)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise HarnessError(f"{path.name} must hold a list of pair records")
    return [item for item in value if isinstance(item, dict)]


def _label(
    source: Path, record: dict[str, object], role: str, allowed: set[str] | None
) -> ReviewItem | None:
    plate, votes, taxon = record.get(role), record.get("votes"), record.get("taxon_id")
    if not isinstance(plate, dict) or not isinstance(votes, list) or not isinstance(taxon, int):
        return None
    recorded = plate.get("path")
    if not isinstance(recorded, str):
        return None
    path = resolve_recorded(source, recorded)
    if not path.is_file():
        return None
    assessments = [
        vote.get(role)
        for vote in votes
        if isinstance(vote, dict) and (allowed is None or vote.get("judge") in allowed)
    ]
    usable = [item for item in assessments if isinstance(item, dict)]
    if not usable:
        return None
    acceptable = majority_flag([item.get("acceptable") is True for item in usable])
    counts: dict[str, int] = defaultdict(int)
    for item in usable:
        defects = item.get("defects")
        seen = {
            defect.get("category")
            for defect in (defects if isinstance(defects, list) else [])
            if isinstance(defect, dict)
        }
        for category in seen:
            if category in CATEGORIES:
                counts[str(category)] += 1
    return ReviewItem(
        plate_id=f"{taxon}-{sha256_file(path)[:16]}",
        taxon_id=taxon,
        plate_path=path,
        acceptable=bool(acceptable),
        categories=frozenset(name for name, count in counts.items() if 2 * count > len(usable)),
    )


@dataclass(frozen=True)
class ReviewOutcome:
    passed: bool
    corrections: tuple[str, ...]
    usage: Usage | None = field(default=None, compare=False)

    @property
    def categories(self) -> frozenset[str]:
        return categorise(self.corrections)


class ReviewEvaluator(Protocol):
    def review(
        self,
        item: ReviewItem,
        species: PanelSpecies,
        references: FrozenReferences,
        instructions: str,
        work_dir: Path,
    ) -> ReviewOutcome:
        """Run the pipeline review with these instructions, or raise GenerationError."""


class CodexReviewEvaluator:
    def __init__(self, config: EvalConfig) -> None:
        self.executable = find_executable(config.codex_executable)
        self.timeout = config.review_timeout_seconds
        self.model = config.generation_model
        self.domains = config.allowed_domains

    def review(
        self,
        item: ReviewItem,
        species: PanelSpecies,
        references: FrozenReferences,
        instructions: str,
        work_dir: Path,
    ) -> ReviewOutcome:
        work_dir.mkdir(parents=True, exist_ok=True)
        runner = InstructedCodexRunner(self.executable, work_dir, self.timeout, model=self.model)
        log = work_dir / "review.log"
        review = runner.review_with_instructions(
            species,
            references,
            item.plate_path,
            work_dir / "review.json",
            log,
            allowed_domains=self.domains,
            instructions=instructions,
        )
        try:
            usage = codex_usage(log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            usage = None
        return ReviewOutcome(review.passed, review.correction_findings, usage)


def review_score(outcome: ReviewOutcome | None, item: ReviewItem) -> float:
    """Half for agreeing on pass/fail, half for the overlap of defect categories."""
    if outcome is None:
        return 0.0
    agreement = float(outcome.passed == item.acceptable)
    found, expected = outcome.categories, item.categories
    union = found | expected
    overlap = 1.0 if not union else len(found & expected) / len(union)
    return 0.5 * agreement + 0.5 * overlap


def review_feedback(item: ReviewItem, outcome: ReviewOutcome | None, error: str | None) -> str:
    label = (
        f"The judge panel found this plate {'acceptable' if item.acceptable else 'not acceptable'}"
        f" with defects in: {', '.join(sorted(item.categories)) or 'none'}."
    )
    if outcome is None:
        return f"{label}\nThe review did not produce a usable result: {error}"
    missed = sorted(item.categories - outcome.categories)
    extra = sorted(outcome.categories - item.categories)
    corrections = "\n".join(f"- {line}" for line in outcome.corrections) or "- none"
    return (
        f"{label}\nThe review {'passed' if outcome.passed else 'failed'} it and asked for:\n"
        f"{corrections}\nCategories the review missed: {', '.join(missed) or 'none'}."
        f" Categories the panel did not see: {', '.join(extra) or 'none'}."
    )


class ReviewTuningAdapter:
    component = REVIEW_COMPONENT

    def __init__(
        self,
        run: Run,
        config: EvalConfig,
        evaluator: ReviewEvaluator,
        panel: dict[int, PanelSpecies],
        references: dict[int, FrozenReferences],
        *,
        budget: Budget,
        parallel: int,
    ) -> None:
        self.run = run
        self.config = config
        self.evaluator = evaluator
        self.panel = panel
        self.references = references
        self.budget = budget
        self.parallel = parallel
        self.ledger = Ledger(run.path(REVIEWS_LEDGER))
        self.scores = _Scores(run.path("tune-review", "candidates"))

    def outcome(self, item: ReviewItem, text: str) -> tuple[ReviewOutcome | None, str | None]:
        cache = self.run.path("reviews", sha256_text(text)[:16], f"{item.plate_id}.json")
        if cache.exists():
            stored = read_object(cache)
            if isinstance(stored.get("error"), str):
                return None, str(stored["error"])
            corrections = stored.get("corrections")
            if not isinstance(corrections, list):
                raise HarnessError(f"Cached review {cache.name} is malformed")
            return ReviewOutcome(stored.get("passed") is True, tuple(map(str, corrections))), None
        self.budget.reserve()
        self.ledger.record("review-started", plate=item.plate_id)
        try:
            outcome = self.evaluator.review(
                item,
                self.panel[item.taxon_id],
                self.references[item.taxon_id],
                text,
                cache.parent / f"{item.plate_id}-work",
            )
        except GenerationError as exc:
            self.ledger.record("review-failed", plate=item.plate_id, error=str(exc))
            write_json(cache, {"error": str(exc)})
            return None, str(exc)
        usage = None if outcome.usage is None else dict(outcome.usage)
        self.ledger.record("review-finished", plate=item.plate_id, usage=usage)
        write_json(cache, {"passed": outcome.passed, "corrections": list(outcome.corrections)})
        return outcome, None

    def evaluate(
        self, batch: list[object], candidate: dict[str, str], capture_traces: bool = False
    ) -> EvalBatch:
        items = [_review_item(item) for item in batch]
        text = candidate.get(self.component, "")
        try:
            validate_candidate(
                text, target="review", max_characters=self.config.max_candidate_characters
            )
        except CandidateRejectedError as exc:
            return _rejected(batch, str(exc), capture_traces)
        results = run_tasks(
            [functools.partial(self.outcome, item, text) for item in items], self.parallel
        )
        scores = [
            review_score(outcome, item) for item, (outcome, _e) in zip(items, results, strict=True)
        ]
        for item, score in zip(items, scores, strict=True):
            self.scores.record(text, item.plate_id, score)
        trajectories: list[dict[str, object]] = [
            {"item": f"plate {item.plate_id}", "feedback": review_feedback(item, outcome, error)}
            for item, (outcome, error) in zip(items, results, strict=True)
        ]
        outputs: list[dict[str, object]] = [
            {"plate_id": item.plate_id, "score": score}
            for item, score in zip(items, scores, strict=True)
        ]
        return EvalBatch(outputs, scores, trajectories if capture_traces else None)

    def make_reflective_dataset(
        self, candidate: dict[str, str], eval_batch: EvalBatch, components_to_update: list[str]
    ) -> dict[str, list[dict[str, object]]]:
        return _reflective(
            self.component, eval_batch, constraints("review", self.config.max_candidate_characters)
        )


def _review_item(item: object) -> ReviewItem:
    if not isinstance(item, ReviewItem):
        raise HarnessError("The review tuner evaluates labelled plates")
    return item


def tune_review(
    run: Run,
    config: EvalConfig,
    evaluator: ReviewEvaluator,
    panel: Sequence[PanelSpecies],
    labels: Sequence[ReviewItem],
    *,
    optimizer: Optimizer,
    reflection_lm: ReflectionLM,
    budget: Budget,
    metric_calls: int,
    exploratory: bool = False,
    parallel: int = 1,
) -> dict[str, object]:
    """Tune the review instructions on labelled plates, then test on held-out species.

    The labels must come from calibrated judges (see build_labels); an
    exploratory run reports what the rule would say but makes no decision.
    """
    by_taxon = {entry.taxon_id: entry for entry in panel}
    usable = [item for item in labels if item.taxon_id in by_taxon]
    dev = [item for item in usable if by_taxon[item.taxon_id].split == "dev"]
    holdout = [item for item in usable if by_taxon[item.taxon_id].split == "holdout"]
    if not dev or not holdout:
        raise HarnessError("Review tuning needs labelled plates of both dev and held-out species")
    references = {
        taxon: frozen_references(run, by_taxon[taxon])
        for taxon in {item.taxon_id for item in usable}
    }
    write_json(run.path("tune-review", "labels.json"), [item.as_dict() for item in usable])
    adapter = ReviewTuningAdapter(
        run, config, evaluator, by_taxon, references, budget=budget, parallel=parallel
    )
    stopped: str | None = None
    try:
        best = optimizer(
            seed_candidate={REVIEW_COMPONENT: REVIEW_INSTRUCTIONS},
            trainset=list(dev),
            valset=list(dev),
            adapter=adapter,
            reflection_lm=reflection_lm,
            max_metric_calls=metric_calls,
            minibatch_size=config.tune_minibatch_size,
            run_dir=run.path("gepa-review"),
            seed=run.derived("gepa-review") % 2**31,
        )
        best_text = best.get(REVIEW_COMPONENT, REVIEW_INSTRUCTIONS)
    except BudgetExhaustedError as exc:
        stopped = str(exc)
        best_text = adapter.scores.best([item.plate_id for item in dev], REVIEW_INSTRUCTIONS)[0]
    validate_candidate(best_text, target="review", max_characters=config.max_candidate_characters)
    write_text(run.path("tune-review", "best-review-instructions.txt"), best_text + "\n")
    summary: dict[str, object] = {
        "best_is_current_prompt": best_text == REVIEW_INSTRUCTIONS,
        "best_sha256": sha256_text(best_text),
        "stopped_early": stopped,
        "labelled_plates": {"dev": len(dev), "holdout": len(holdout)},
        "exploratory": exploratory,
    }
    if best_text == REVIEW_INSTRUCTIONS:
        note = "tuning found nothing better than the current review prompt"
        summary["decision"] = None if exploratory else {"accept": False, "reasons": [note]}
        summary["decision_note"] = note
    else:
        differences: dict[int, list[float]] = defaultdict(list)
        incomplete = 0
        for item in holdout:
            try:
                baseline, _ = adapter.outcome(item, REVIEW_INSTRUCTIONS)
                candidate, _ = adapter.outcome(item, best_text)
            except BudgetExhaustedError:
                incomplete += 1
                continue
            differences[item.taxon_id].append(
                review_score(candidate, item) - review_score(baseline, item)
            )
        rate, decision = decide_improvement(
            differences,
            iterations=config.bootstrap_iterations,
            seed=run.derived("bootstrap|tune-review"),
            confidence=config.confidence,
        )
        reasons = list(decision.reasons)
        if incomplete:
            reasons.append(f"{incomplete} held-out plates were not reviewed within the cap")
        summary["holdout_score_difference"] = rate.as_dict()
        verdict = {"accept": decision.accept and not incomplete, "reasons": reasons}
        if exploratory:
            summary["decision"] = None
            summary["exploratory_decision"] = verdict
            summary["decision_note"] = (
                "exploratory run without a passing calibration; no decision is made"
            )
        else:
            summary["decision"] = verdict
            summary["decision_note"] = None
    write_json(run.path("tune-review", "summary.json"), summary)
    return summary
