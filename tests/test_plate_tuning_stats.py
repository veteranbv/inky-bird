from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from plate_tuning.blind import JudgedPair, Outcome, PairSpec, PlateRef, Vote
from plate_tuning.judges import Defect, PlateAssessment, Side
from plate_tuning.panel import FrozenReferences
from plate_tuning.stats import (
    agreement,
    cluster_bootstrap,
    decide,
    decide_improvement,
    fleiss_kappa,
    majority,
    majority_flag,
    position_bias,
    summarize,
)

GOOD = PlateAssessment((), True)
FATAL = PlateAssessment(
    (Defect("legs", "left leg", "reference-01.jpg", "fatal", "detached"),), False
)

# The worked example from Fleiss (1971) as reproduced in most references:
# 10 subjects, 14 raters, 5 categories; kappa = 0.210.
FLEISS_EXAMPLE = [
    [0, 0, 0, 0, 14],
    [0, 2, 6, 4, 2],
    [0, 0, 3, 5, 6],
    [0, 3, 9, 2, 0],
    [2, 2, 8, 1, 1],
    [7, 7, 0, 0, 0],
    [3, 2, 6, 3, 0],
    [2, 5, 3, 2, 2],
    [6, 5, 2, 1, 0],
    [0, 2, 2, 3, 7],
]


def _pair(
    taxon: int,
    anatomy: list[Outcome],
    *,
    lettering: list[Outcome] | None = None,
    first: PlateAssessment = GOOD,
    second: PlateAssessment = GOOD,
) -> JudgedPair:
    spec = PairSpec(
        identity=f"{taxon}|{len(anatomy)}",
        taxon_id=taxon,
        first=PlateRef(Path("a.png"), "a"),
        second=PlateRef(Path("b.png"), "b"),
        references=FrozenReferences(Path("refs"), ()),
        expected_text={},
    )
    votes = tuple(
        Vote(f"j{index}", outcome, (lettering or anatomy)[index], first, second)
        for index, outcome in enumerate(anatomy)
    )
    return JudgedPair(spec, f"id{taxon}", {}, votes, ())


def test_majority_needs_more_than_half() -> None:
    assert majority(["first", "first", "second"]) == "first"
    assert majority(["first", "second", "tie"]) == "tie"
    assert majority(["second", "second"]) == "second"
    assert majority(["first", "second"]) == "tie"
    assert majority([]) is None
    assert majority_flag([True, True, False]) is True
    assert majority_flag([True, False]) is False
    assert majority_flag([]) is None


def test_fleiss_kappa_matches_the_published_example() -> None:
    kappa = fleiss_kappa(FLEISS_EXAMPLE)

    assert kappa == pytest.approx(0.20993, abs=1e-4)


def test_fleiss_kappa_edge_cases() -> None:
    assert fleiss_kappa([[3, 0, 0], [0, 3, 0], [0, 0, 3]]) == pytest.approx(1.0)
    assert fleiss_kappa([[3, 0, 0], [3, 0, 0]]) is None
    assert fleiss_kappa([[1, 0, 0]]) is None
    assert fleiss_kappa([[2, 1, 0], [1, 1, 0]]) is None
    assert fleiss_kappa([]) is None
    # Systematic disagreement is below chance.
    disagreement = fleiss_kappa([[1, 1, 0], [1, 1, 0], [0, 1, 1], [1, 0, 1]])
    assert disagreement is not None and disagreement < 0


def test_pairwise_agreement_counts_only_shared_items() -> None:
    rows: list[dict[str, Outcome]] = [
        {"a": "first", "b": "first", "c": "second"},
        {"a": "tie", "b": "first"},
        {"a": "second", "b": "second", "c": "second"},
    ]

    result = agreement(rows, ["a", "b", "c"])

    assert result.pairwise == {"a~b": pytest.approx(2 / 3), "a~c": 0.5, "b~c": 0.5}
    assert result.items_rated_by_all == 2


def test_cluster_bootstrap_resamples_species_not_pairs() -> None:
    # One species always wins, one always loses: pair-level resampling of 40
    # pairs would give a narrow interval; species-level gives the full spread.
    groups = {1: [1.0] * 20, 2: [0.0] * 20}

    low, high = cluster_bootstrap(groups, iterations=2000, seed=3, confidence=0.95)

    assert low == 0.0
    assert high == 1.0
    single = cluster_bootstrap({1: [0.7, 0.7]}, iterations=50, seed=1, confidence=0.95)
    assert single[0] == pytest.approx(0.7)
    assert single[1] == pytest.approx(0.7)
    assert cluster_bootstrap(groups, iterations=300, seed=9, confidence=0.9) == (
        cluster_bootstrap(groups, iterations=300, seed=9, confidence=0.9)
    )
    with pytest.raises(ValueError):
        cluster_bootstrap({}, iterations=10, seed=1, confidence=0.95)


def _clear_win(species: int = 8, fatal_first: bool = False) -> list[JudgedPair]:
    return [
        _pair(
            taxon,
            ["first", "first", "second"],
            first=FATAL if fatal_first else GOOD,
            second=FATAL,
        )
        for taxon in range(1, species + 1)
        for _sample in range(3)
    ]


def test_summary_uses_the_judge_majority_and_plate_majorities() -> None:
    summary = summarize(_clear_win(), ["j0", "j1", "j2"], iterations=500, seed=1)

    assert summary.pairs == 24
    assert summary.anatomy.point == 1.0
    assert summary.anatomy.low == 1.0
    assert summary.first_fatal_rate == 0.0
    assert summary.second_fatal_rate == 1.0
    assert summary.first_acceptable_rate == 1.0
    assert summary.anatomy_agreement.pairwise["j0~j1"] == 1.0
    assert summary.anatomy_agreement.pairwise["j0~j2"] == 0.0
    assert summary.per_species[1]["pairs"] == 3


def test_decision_rule_accepts_a_clear_holdout_win() -> None:
    summary = summarize(_clear_win(), ["j0", "j1", "j2"], iterations=500, seed=1)

    decision = decide(summary, list(range(1, 9)))

    assert decision.accept
    assert decision.reasons == ()


def test_decision_rule_rejects_each_failed_condition() -> None:
    judges = ["j0", "j1", "j2"]
    missing = decide(summarize(_clear_win(), judges, iterations=200, seed=1), [1, 2, 99])
    assert not missing.accept and "required species" in missing.reasons[0]

    ties = [_pair(taxon, ["tie", "tie", "tie"]) for taxon in range(1, 9)]
    no_better = decide(summarize(ties, judges, iterations=200, seed=1), [])
    assert not no_better.accept
    assert any("does not exclude" in reason for reason in no_better.reasons)

    worse_lettering = [
        _pair(taxon, ["first"] * 3, lettering=["second"] * 3) for taxon in range(1, 9)
    ]
    lettering = decide(summarize(worse_lettering, judges, iterations=200, seed=1), [])
    assert [reason for reason in lettering.reasons if "lettering" in reason]
    assert not lettering.accept

    fatal = decide(
        summarize(
            _clear_win(fatal_first=True)[:6] + [_pair(9, ["first"] * 3, first=FATAL, second=GOOD)],
            judges,
            iterations=200,
            seed=1,
        ),
        [],
    )
    assert not fatal.accept
    assert any("fatal-defect rate" in reason for reason in fatal.reasons)


def test_a_noisy_mixed_result_is_not_accepted() -> None:
    # Half the species favour the candidate and half the baseline.
    pairs = [_pair(taxon, ["first"] * 3 if taxon % 2 else ["second"] * 3) for taxon in range(1, 9)]

    decision = decide(summarize(pairs, ["j0", "j1", "j2"], iterations=1000, seed=5), [])

    assert not decision.accept


def test_improvement_decision_requires_an_interval_above_zero() -> None:
    rate, decision = decide_improvement(
        {1: [0.2, 0.3], 2: [0.1], 3: [0.25]}, iterations=500, seed=2
    )
    assert decision.accept and rate.low > 0

    rate, decision = decide_improvement({1: [0.5], 2: [-0.5]}, iterations=500, seed=2)
    assert not decision.accept and rate.low < 0

    _rate, decision = decide_improvement({}, iterations=10, seed=2)
    assert not decision.accept


def test_summary_reports_each_judges_coverage() -> None:
    pairs = _clear_win()
    # The third judge answered only half of the pairs.
    thinned = [
        replace(pair, votes=pair.votes[:2]) if index % 2 else pair
        for index, pair in enumerate(pairs)
    ]

    summary = summarize(thinned, ["j0", "j1", "j2"], iterations=200, seed=1)

    assert summary.judge_missing == {"j0": 0, "j1": 0, "j2": 12}
    assert summary.as_dict()["judge_coverage"] == {
        "j0": {"votes": 24, "missing": 0},
        "j1": {"votes": 24, "missing": 0},
        "j2": {"votes": 12, "missing": 12},
    }


def test_decision_refuses_when_a_judge_is_missing_on_too_many_pairs() -> None:
    judges = ["j0", "j1", "j2"]
    pairs = _clear_win()
    one_gap = [replace(pairs[0], votes=pairs[0].votes[:2]), *pairs[1:]]
    many_gaps = [replace(pair, votes=pair.votes[:2]) for pair in pairs[:6]] + pairs[6:]

    assert decide(summarize(one_gap, judges, iterations=200, seed=1), [], 0.1).accept
    refused = decide(summarize(many_gaps, judges, iterations=200, seed=1), [], 0.1)
    assert not refused.accept
    assert refused.reasons == ("judge j2 gave no verdict on 6 of 24 pairs (above 10%)",)


SIDES: tuple[Side, ...] = ("A", "B") * 4


def test_position_bias_reports_each_judges_share_of_side_a() -> None:
    def vote(judge: str, anatomy: Outcome, side: Side) -> Vote:
        return Vote(judge, anatomy, "tie", GOOD, GOOD, side)

    # "always-a" picks letter A every time, whichever plate sat there;
    # "fair" picks the first plate wherever it landed.
    pairs = [
        JudgedPair(
            replace(_pair(taxon, ["first"]).spec, identity=f"bias|{taxon}"),
            f"b{taxon}",
            {"always-a": side, "fair": side},
            (
                vote("always-a", "first" if side == "A" else "second", side),
                vote("fair", "first", side),
            ),
            (),
        )
        for taxon, side in zip(range(1, 9), SIDES, strict=True)
    ]

    bias = position_bias(pairs, ["always-a", "fair", "silent"])

    assert bias["always-a"] == {
        "verdicts": 8,
        "anatomy_a_share": 1.0,
        "lettering_a_share": None,
        "anatomy_ties": 0,
        "lettering_ties": 8,
    }
    assert bias["fair"]["anatomy_a_share"] == 0.5
    assert bias["silent"]["verdicts"] == 0
    summary = summarize(pairs, ["always-a", "fair"], iterations=50, seed=1)
    assert summary.as_dict()["position_bias"] == {
        "always-a": bias["always-a"],
        "fair": bias["fair"],
    }
