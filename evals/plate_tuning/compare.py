"""Blind paired comparison of two prompt arms on panel species."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from .blind import JudgedPair, PairSpec, PlateRef, expected_text, judge_pairs
from .config import EvalConfig
from .generate import Arm, GenerationReport, Generator, Sample, generate_samples, missing_samples
from .judges import Judge
from .panel import FrozenReferences, PanelSpecies, frozen_references
from .rundir import Budget, Run, run_relative, sha256_text, write_json
from .stats import Decision, Summary, decide, summarize

COMPARE_DIR: Final = "compare"


def judged_with(config: EvalConfig) -> dict[str, object]:
    """The rubric and judges a summary's verdicts came from."""
    return {
        "rubric_sha256": sha256_text(config.rubric()),
        "judges": [
            {"name": judge.name, "kind": judge.kind, "model": judge.model}
            for judge in config.judges
        ],
    }


def pair_identity(scope: str, first: Arm, second: Arm, taxon_id: int, index: int) -> str:
    return f"{scope}|{first.name}|{second.name}|{taxon_id}|{index}"


def pair_specs(
    scope: str,
    first: Arm,
    second: Arm,
    species: Sequence[tuple[PanelSpecies, FrozenReferences]],
    samples: dict[tuple[str, int, int], Sample],
    count: int,
) -> list[PairSpec]:
    """Pair sample k of the first arm with sample k of the second, where both exist."""
    specs: list[PairSpec] = []
    for entry, references in species:
        for index in range(1, count + 1):
            left = samples.get((first.name, entry.taxon_id, index))
            right = samples.get((second.name, entry.taxon_id, index))
            if left is None or right is None:
                continue
            specs.append(
                PairSpec(
                    identity=pair_identity(scope, first, second, entry.taxon_id, index),
                    taxon_id=entry.taxon_id,
                    first=PlateRef(left.portrait, f"{first.name} sample {index}"),
                    second=PlateRef(right.portrait, f"{second.name} sample {index}"),
                    references=references,
                    expected_text=expected_text(entry.profile),
                )
            )
    return specs


@dataclass(frozen=True)
class ComparePlan:
    generations_needed: int
    pairs_possible: int
    judgments_needed: int

    def as_dict(self) -> dict[str, int]:
        return {
            "generations_needed": self.generations_needed,
            "pairs_possible": self.pairs_possible,
            "judgments_needed_at_most": self.judgments_needed,
        }


def plan(
    run: Run,
    first: Arm,
    second: Arm,
    species: Sequence[PanelSpecies],
    samples: int,
    judges: int,
) -> ComparePlan:
    pairs = len(species) * samples
    return ComparePlan(
        generations_needed=missing_samples(run, [first, second], species, samples),
        pairs_possible=pairs,
        judgments_needed=pairs * judges,
    )


@dataclass(frozen=True)
class CompareResult:
    first: Arm
    second: Arm
    generation: GenerationReport
    pairs: list[JudgedPair]
    summary: Summary | None
    decision_species: str
    decision_summary: Summary | None
    decision: Decision | None

    def as_dict(self) -> dict[str, object]:
        return {
            "first": {"name": self.first.name, "label": self.first.label},
            "second": {"name": self.second.name, "label": self.second.label},
            "generation": self.generation.as_dict(),
            "pairs_judged": len(self.pairs),
            "summary": None if self.summary is None else self.summary.as_dict(),
            "decision_species": self.decision_species,
            "decision_summary": None
            if self.decision_summary is None
            else self.decision_summary.as_dict(),
            "decision": None if self.decision is None else self.decision.as_dict(),
        }


def pair_records(run: Run, pairs: Sequence[JudgedPair]) -> list[dict[str, object]]:
    """The unblinded per-pair record kept beside the summary for re-analysis and reports.

    Plate paths inside the run are run-relative.
    """
    return [
        {
            "pair_id": pair.pair_id,
            "identity": pair.spec.identity,
            "taxon_id": pair.spec.taxon_id,
            "first": {
                "label": pair.spec.first.label,
                "path": run_relative(run, pair.spec.first.path),
            },
            "second": {
                "label": pair.spec.second.label,
                "path": run_relative(run, pair.spec.second.path),
            },
            "first_side": pair.first_side,
            "votes": [
                {
                    "judge": vote.judge,
                    "anatomy": vote.anatomy,
                    "lettering": vote.lettering,
                    "first": vote.first.as_dict(),
                    "second": vote.second.as_dict(),
                }
                for vote in pair.votes
            ],
            "errors": dict(pair.errors),
        }
        for pair in pairs
    ]


def _summary(
    pairs: Sequence[JudgedPair], judges: Sequence[str], config: EvalConfig, seed: int
) -> Summary | None:
    if not any(pair.votes for pair in pairs):
        return None
    return summarize(
        pairs,
        judges,
        iterations=config.bootstrap_iterations,
        seed=seed,
        confidence=config.confidence,
    )


def compare(
    run: Run,
    config: EvalConfig,
    generator: Generator,
    judges: Sequence[Judge],
    first: Arm,
    second: Arm,
    species: Sequence[PanelSpecies],
    *,
    samples: int,
    budget: Budget,
    parallel: int = 1,
    scope: str = COMPARE_DIR,
) -> CompareResult:
    """Generate what is missing, judge every complete pair blind, then unblind and decide.

    "first" is treated as the candidate and "second" as the baseline. The
    decision rule is applied to the held-out species when any are compared,
    otherwise to all of them; the summary always covers every pair.
    """
    with_references = [(entry, frozen_references(run, entry)) for entry in species]
    done, report = generate_samples(
        run, generator, [first, second], with_references, samples, budget=budget, parallel=parallel
    )
    specs = pair_specs(scope, first, second, with_references, done, samples)
    pairs = judge_pairs(run, specs, judges, config, parallel=parallel)
    names = [judge.name for judge in judges]
    seed = run.derived(f"bootstrap|{scope}|{first.name}|{second.name}")
    summary = _summary(pairs, names, config, seed)
    held_out = [entry.taxon_id for entry in species if entry.split == "holdout"]
    decision_taxa = held_out or [entry.taxon_id for entry in species]
    decision_summary = _summary(
        [pair for pair in pairs if pair.spec.taxon_id in set(decision_taxa)], names, config, seed
    )
    decision = (
        None
        if decision_summary is None
        else decide(decision_summary, decision_taxa, config.max_missing_judge_share)
    )
    result = CompareResult(
        first,
        second,
        report,
        pairs,
        summary,
        "holdout" if held_out else "all",
        decision_summary,
        decision,
    )
    out = run.path(scope, f"{first.name}-vs-{second.name}")
    write_json(out / "pairs.json", pair_records(run, pairs))
    write_json(out / "summary.json", {**result.as_dict(), "judged_with": judged_with(config)})
    return result
