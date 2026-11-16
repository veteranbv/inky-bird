"""Pairwise aggregation, species-clustered bootstrap, judge agreement and the decision rule.

Pairs are "first" (the candidate) against "second" (the baseline). A pair's
outcome is the majority of its judges; a plate's fatal or acceptable flag is
likewise a strict majority. Everything here is a pure function of the votes.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Final

from .blind import JudgedPair, Outcome, Vote, blind_choice
from .config import DEFAULT_MAX_MISSING_JUDGE_SHARE

OUTCOMES: Final[tuple[Outcome, ...]] = ("first", "second", "tie")
NO_BETTER: Final = 0.5


def majority(outcomes: Sequence[Outcome]) -> Outcome | None:
    """The outcome more than half the judges chose; a tie when none did."""
    if not outcomes:
        return None
    for outcome in OUTCOMES:
        if 2 * outcomes.count(outcome) > len(outcomes):
            return outcome
    return "tie"


def majority_flag(flags: Sequence[bool]) -> bool | None:
    if not flags:
        return None
    return 2 * sum(flags) > len(flags)


def win_score(outcome: Outcome) -> float:
    return {"first": 1.0, "tie": 0.5, "second": 0.0}[outcome]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def cluster_bootstrap(
    groups: Mapping[int, Sequence[float]],
    *,
    iterations: int,
    seed: int,
    confidence: float,
) -> tuple[float, float]:
    """Percentile interval for the pooled mean, resampling whole species.

    Samples of one species share its profile and references, so they are not
    independent; resampling species keeps that correlation in the interval.
    """
    clusters = [list(values) for values in groups.values() if values]
    if not clusters:
        raise ValueError("No scored pairs to bootstrap")
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(iterations):
        drawn = [rng.choice(clusters) for _ in clusters]
        pooled = [value for cluster in drawn for value in cluster]
        estimates.append(_mean(pooled))
    estimates.sort()
    tail = (1 - confidence) / 2
    low_index = int(tail * (iterations - 1))
    high_index = int(round((1 - tail) * (iterations - 1)))
    return estimates[low_index], estimates[high_index]


def fleiss_kappa(table: Sequence[Sequence[int]]) -> float | None:
    """Fleiss' kappa for items rated by the same number of raters.

    Each row counts how many raters chose each category for one item. Returns
    None when it is undefined: fewer than two raters, or no items, or every
    rating in one category (chance agreement is then total).
    """
    if not table:
        return None
    raters = sum(table[0])
    if raters < 2 or any(sum(row) != raters for row in table):
        return None
    items = len(table)
    observed = _mean(
        [(sum(count * count for count in row) - raters) / (raters * (raters - 1)) for row in table]
    )
    shares = [
        sum(row[column] for row in table) / (items * raters) for column in range(len(table[0]))
    ]
    expected = sum(share * share for share in shares)
    if expected >= 1:
        return None
    return (observed - expected) / (1 - expected)


@dataclass(frozen=True)
class Agreement:
    pairwise: dict[str, float | None]
    fleiss_kappa: float | None
    items_rated_by_all: int

    def as_dict(self) -> dict[str, object]:
        return {
            "pairwise": self.pairwise,
            "fleiss_kappa": self.fleiss_kappa,
            "items_rated_by_all": self.items_rated_by_all,
        }


def agreement(rows: Sequence[Mapping[str, Outcome]], judges: Sequence[str]) -> Agreement:
    pairwise: dict[str, float | None] = {}
    for left, right in combinations(judges, 2):
        shared = [row for row in rows if left in row and right in row]
        pairwise[f"{left}~{right}"] = (
            _mean([float(row[left] == row[right]) for row in shared]) if shared else None
        )
    complete = [row for row in rows if all(judge in row for judge in judges)]
    table = [
        [sum(row[judge] == outcome for judge in judges) for outcome in OUTCOMES] for row in complete
    ]
    return Agreement(pairwise, fleiss_kappa(table), len(complete))


def _a_share(choices: Sequence[str]) -> float | None:
    decided = [choice for choice in choices if choice != "tie"]
    return decided.count("A") / len(decided) if decided else None


def position_bias(
    pairs: Sequence[JudgedPair], judges: Sequence[str]
) -> dict[str, dict[str, float | int | None]]:
    """Each judge's share of decided choices that went to letter A.

    Sides are drawn at random per pair and judge, so an unbiased judge sits
    near 0.5; a judge far from it is choosing by position.
    """
    report: dict[str, dict[str, float | int | None]] = {}
    for judge in judges:
        votes = [vote for pair in pairs for vote in pair.votes if vote.judge == judge]
        anatomy = [blind_choice(vote.anatomy, vote.first_side) for vote in votes]
        lettering = [blind_choice(vote.lettering, vote.first_side) for vote in votes]
        report[judge] = {
            "verdicts": len(votes),
            "anatomy_a_share": _a_share(anatomy),
            "lettering_a_share": _a_share(lettering),
            "anatomy_ties": anatomy.count("tie"),
            "lettering_ties": lettering.count("tie"),
        }
    return report


@dataclass(frozen=True)
class Rate:
    point: float
    low: float
    high: float

    def as_dict(self) -> dict[str, float]:
        return {"point": self.point, "low": self.low, "high": self.high}


@dataclass(frozen=True)
class Summary:
    pairs: int
    unjudged_pairs: int
    judged_species: tuple[int, ...]
    anatomy: Rate
    lettering: Rate
    first_fatal_rate: float
    second_fatal_rate: float
    first_acceptable_rate: float
    second_acceptable_rate: float
    anatomy_agreement: Agreement
    lettering_agreement: Agreement
    per_species: dict[int, dict[str, float | int]]
    # Pairs (judged or not) each judge gave no verdict on.
    judge_missing: dict[str, int]
    position_bias: dict[str, dict[str, float | int | None]]

    @property
    def total_pairs(self) -> int:
        return self.pairs + self.unjudged_pairs

    def as_dict(self) -> dict[str, object]:
        return {
            "pairs": self.pairs,
            "unjudged_pairs": self.unjudged_pairs,
            "judge_coverage": {
                judge: {"votes": self.total_pairs - missing, "missing": missing}
                for judge, missing in self.judge_missing.items()
            },
            "judged_species": list(self.judged_species),
            "anatomy_win_rate": self.anatomy.as_dict(),
            "lettering_win_rate": self.lettering.as_dict(),
            "first_fatal_rate": self.first_fatal_rate,
            "second_fatal_rate": self.second_fatal_rate,
            "first_acceptable_rate": self.first_acceptable_rate,
            "second_acceptable_rate": self.second_acceptable_rate,
            "agreement": {
                "anatomy": self.anatomy_agreement.as_dict(),
                "lettering": self.lettering_agreement.as_dict(),
            },
            "per_species": {str(taxon): values for taxon, values in self.per_species.items()},
            "position_bias": self.position_bias,
        }


@dataclass(frozen=True)
class PairOutcome:
    """The panel's majority view of one pair."""

    anatomy: Outcome
    lettering: Outcome
    first_fatal: bool
    second_fatal: bool
    first_acceptable: bool
    second_acceptable: bool


def pair_outcome(votes: Sequence[Vote]) -> PairOutcome | None:
    anatomy = majority([vote.anatomy for vote in votes])
    lettering = majority([vote.lettering for vote in votes])
    if anatomy is None or lettering is None:
        return None
    return PairOutcome(
        anatomy=anatomy,
        lettering=lettering,
        first_fatal=bool(majority_flag([vote.first.fatal for vote in votes])),
        second_fatal=bool(majority_flag([vote.second.fatal for vote in votes])),
        first_acceptable=bool(majority_flag([vote.first.acceptable for vote in votes])),
        second_acceptable=bool(majority_flag([vote.second.acceptable for vote in votes])),
    )


def _rate(
    groups: Mapping[int, Sequence[float]], *, iterations: int, seed: int, confidence: float
) -> Rate:
    pooled = [value for values in groups.values() for value in values]
    low, high = cluster_bootstrap(groups, iterations=iterations, seed=seed, confidence=confidence)
    return Rate(_mean(pooled), low, high)


def summarize(
    pairs: Sequence[JudgedPair],
    judges: Sequence[str],
    *,
    iterations: int,
    seed: int,
    confidence: float = 0.95,
) -> Summary:
    anatomy: dict[int, list[float]] = defaultdict(list)
    lettering: dict[int, list[float]] = defaultdict(list)
    outcomes: list[PairOutcome] = []
    unjudged = 0
    for pair in pairs:
        outcome = pair_outcome(pair.votes)
        if outcome is None:
            unjudged += 1
            continue
        outcomes.append(outcome)
        anatomy[pair.spec.taxon_id].append(win_score(outcome.anatomy))
        lettering[pair.spec.taxon_id].append(win_score(outcome.lettering))
    if not outcomes:
        raise ValueError("No pair has a verdict yet")
    count = len(outcomes)
    per_species: dict[int, dict[str, float | int]] = {
        taxon: {
            "pairs": len(values),
            "anatomy_win_rate": _mean(values),
            "lettering_win_rate": _mean(lettering[taxon]),
        }
        for taxon, values in sorted(anatomy.items())
    }
    return Summary(
        pairs=count,
        unjudged_pairs=unjudged,
        judged_species=tuple(sorted(anatomy)),
        anatomy=_rate(anatomy, iterations=iterations, seed=seed, confidence=confidence),
        lettering=_rate(lettering, iterations=iterations, seed=seed + 1, confidence=confidence),
        first_fatal_rate=sum(item.first_fatal for item in outcomes) / count,
        second_fatal_rate=sum(item.second_fatal for item in outcomes) / count,
        first_acceptable_rate=sum(item.first_acceptable for item in outcomes) / count,
        second_acceptable_rate=sum(item.second_acceptable for item in outcomes) / count,
        anatomy_agreement=agreement(
            [{vote.judge: vote.anatomy for vote in pair.votes} for pair in pairs], judges
        ),
        lettering_agreement=agreement(
            [{vote.judge: vote.lettering for vote in pair.votes} for pair in pairs], judges
        ),
        per_species=per_species,
        judge_missing={
            judge: sum(1 for pair in pairs if judge not in {vote.judge for vote in pair.votes})
            for judge in judges
        },
        position_bias=position_bias(pairs, judges),
    )


@dataclass(frozen=True)
class Decision:
    accept: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"accept": self.accept, "reasons": list(self.reasons)}


def decide(
    summary: Summary,
    required_species: Sequence[int],
    max_missing_share: float = DEFAULT_MAX_MISSING_JUDGE_SHARE,
) -> Decision:
    """The fixed rule: the candidate replaces the baseline only if all of these hold.

    0. No judge is missing on more than max_missing_share of the pairs, so the
       decision rests on the whole panel.
    1. Every required (held-out) species has at least one judged pair.
    2. The species-clustered interval for the anatomy win rate lies wholly
       above 0.5, so "no better" is excluded.
    3. The lettering win rate is at least 0.5 (not worse).
    4. The candidate's fatal-defect rate is no higher than the baseline's.
    """
    reasons: list[str] = []
    for judge, absent in summary.judge_missing.items():
        if absent > max_missing_share * summary.total_pairs:
            reasons.append(
                f"judge {judge} gave no verdict on {absent} of {summary.total_pairs} pairs"
                f" (above {max_missing_share:.0%})"
            )
    missing = sorted(set(required_species) - set(summary.judged_species))
    if missing:
        reasons.append(f"{len(missing)} required species have no judged pair")
    if summary.anatomy.low <= NO_BETTER:
        reasons.append(
            f"anatomy interval low {summary.anatomy.low:.3f} does not exclude no better (0.5)"
        )
    if summary.lettering.point < NO_BETTER:
        reasons.append(f"lettering win rate {summary.lettering.point:.3f} is below 0.5")
    if summary.first_fatal_rate > summary.second_fatal_rate:
        reasons.append(
            f"fatal-defect rate {summary.first_fatal_rate:.3f} exceeds the baseline's "
            f"{summary.second_fatal_rate:.3f}"
        )
    return Decision(not reasons, tuple(reasons))


def decide_improvement(
    differences: Mapping[int, Sequence[float]],
    *,
    iterations: int,
    seed: int,
    confidence: float = 0.95,
) -> tuple[Rate, Decision]:
    """For per-item score differences (candidate minus baseline), grouped by species.

    The candidate is accepted only when the species-clustered interval for the
    mean difference lies wholly above zero.
    """
    pooled = [value for values in differences.values() for value in values]
    if not pooled:
        return Rate(0.0, 0.0, 0.0), Decision(False, ("no held-out item was scored",))
    low, high = cluster_bootstrap(
        differences, iterations=iterations, seed=seed, confidence=confidence
    )
    rate = Rate(_mean(pooled), low, high)
    if low <= 0:
        return rate, Decision(False, (f"improvement interval low {low:.3f} does not exclude 0",))
    return rate, Decision(True, ())
