"""The calibration gate: only judges that passed calibration under the same rubric decide.

A calibration record (calibration/summary.json) names the rubric, the judging
contract, the thresholds and each judge's kind and model it was measured
with. A later decision may rest only on judges whose current configuration
matches that record exactly and who passed; any change to the rubric, the
judging prompt, a threshold or a model makes the record stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .config import EvalConfig, HarnessError
from .judges import judge_contract_sha256
from .rundir import Run, read_object, sha256_text

CALIBRATION_SUMMARY: Final = ("calibration", "summary.json")
THRESHOLD_FIELDS: Final = (
    "anchor_accuracy_threshold",
    "planted_recall_threshold",
    "planted_preference_threshold",
    "swap_consistency_threshold",
)


def judged_with(config: EvalConfig) -> dict[str, object]:
    """The rubric, judging contract and judges a summary's verdicts came from."""
    return {
        "rubric_sha256": sha256_text(config.rubric()),
        "judge_contract_sha256": judge_contract_sha256(),
        "judges": [
            {"name": judge.name, "kind": judge.kind, "model": judge.model}
            for judge in config.judges
        ],
    }


def thresholds(config: EvalConfig) -> dict[str, float]:
    return {name: float(getattr(config, name)) for name in THRESHOLD_FIELDS}


@dataclass(frozen=True)
class CalibrationGate:
    """Which configured judges may decide, and why a decision cannot be made, if it cannot."""

    calibrated: tuple[str, ...]
    excluded: dict[str, str]
    reasons: tuple[str, ...]
    source: str | None
    rubric_sha256: str
    judge_contract_sha256: str
    # Kind and model of every calibrated judge, for matching earlier runs' verdicts.
    specs: dict[str, tuple[str, str | None]]

    @property
    def passed(self) -> bool:
        return not self.reasons

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "source": self.source,
            "calibrated_judges": list(self.calibrated),
            "excluded_judges": self.excluded,
            "reasons": list(self.reasons),
        }


def load_calibration(run: Run, path: Path | None) -> tuple[dict[str, object] | None, str | None]:
    """The record given on the command line, or else the run directory's own."""
    if path is not None:
        if not path.is_file():
            raise HarnessError(f"The calibration record {path.name} does not exist")
        return read_object(path), path.name
    own = run.path(*CALIBRATION_SUMMARY)
    if not own.exists():
        return None, None
    return read_object(own), "/".join(CALIBRATION_SUMMARY)


def _recorded_judges(record: dict[str, object]) -> dict[str, dict[str, object]]:
    judged = record.get("judged_with")
    listed = judged.get("judges") if isinstance(judged, dict) else None
    return {
        str(item.get("name")): item
        for item in (listed if isinstance(listed, list) else [])
        if isinstance(item, dict)
    }


def _results(record: dict[str, object]) -> dict[str, dict[str, object]]:
    listed = record.get("judges")
    return {
        str(item.get("judge")): item
        for item in (listed if isinstance(listed, list) else [])
        if isinstance(item, dict)
    }


def calibration_gate(
    config: EvalConfig, record: dict[str, object] | None, *, source: str | None
) -> CalibrationGate:
    rubric = sha256_text(config.rubric())
    contract = judge_contract_sha256()
    if record is None:
        return CalibrationGate(
            calibrated=(),
            excluded={judge.name: "not calibrated" for judge in config.judges},
            reasons=("no calibration record: run calibrate (or pass --calibration) first",),
            source=None,
            rubric_sha256=rubric,
            judge_contract_sha256=contract,
            specs={},
        )
    reasons: list[str] = []
    excluded: dict[str, str] = {}
    calibrated: list[str] = []
    judged = record.get("judged_with")
    judged = judged if isinstance(judged, dict) else {}
    if judged.get("rubric_sha256") != rubric:
        reasons.append("the calibration was made with another rubric")
    if judged.get("judge_contract_sha256") != contract:
        reasons.append("the calibration was made with another judging prompt or schema")
    if record.get("thresholds") != thresholds(config):
        reasons.append("the calibration was scored against other thresholds")
    recorded, results = _recorded_judges(record), _results(record)
    for judge in config.judges:
        spec = recorded.get(judge.name)
        result = results.get(judge.name)
        if spec is None or result is None:
            excluded[judge.name] = "not in the calibration record"
        elif (spec.get("kind"), spec.get("model")) != (judge.kind, judge.model):
            excluded[judge.name] = "calibrated with another kind or model"
        elif result.get("passed") is not True:
            excluded[judge.name] = "failed calibration"
        else:
            calibrated.append(judge.name)
    if reasons:
        # A stale record calibrates nobody.
        calibrated = []
    elif len(calibrated) < config.min_calibrated_judges:
        reasons.append(
            f"{len(calibrated)} calibrated judges; a decision needs at least"
            f" {config.min_calibrated_judges}"
        )
    if config.require_each_provider and calibrated:
        passing = {judge.kind for judge in config.judges if judge.name in calibrated}
        for kind in sorted({judge.kind for judge in config.judges}):
            if kind not in passing:
                reasons.append(f"no calibrated {kind} judge; each configured kind needs one")
    specs: dict[str, tuple[str, str | None]] = {
        judge.name: (str(judge.kind), judge.model)
        for judge in config.judges
        if judge.name in calibrated
    }
    return CalibrationGate(
        calibrated=tuple(calibrated),
        excluded=excluded,
        reasons=tuple(dict.fromkeys(reasons)),
        source=source,
        rubric_sha256=rubric,
        judge_contract_sha256=contract,
        specs=specs,
    )
