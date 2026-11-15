"""One known pair through each judge, to check the command lines before a large run."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Final

from .blind import (
    PairSpec,
    PlateRef,
    blind_folder,
    expected_text,
    prepare_pair,
    reference_names,
    unblind,
)
from .calibrate import plant_defect
from .config import EvalConfig
from .judges import Judge, JudgeError, JudgeRequest
from .panel import FrozenReferences, PanelSpecies
from .rundir import Ledger, Run, run_relative, write_json

SMOKE_KIND: Final = "stretch"
ROLES: Final = {"first": "approved", "second": "planted", "tie": "tie"}


def smoke_pair(run: Run, entry: PanelSpecies, references: FrozenReferences) -> PairSpec:
    """The approved plate against a copy with its lower body stretched."""
    planted = run.path("calibration", "planted", entry.key, f"{SMOKE_KIND}.png")
    if not planted.exists():
        plant_defect(
            entry.portrait_path, planted, SMOKE_KIND, entry.profile["measurements"]["length"]
        )
    return PairSpec(
        identity=f"smoke|{entry.taxon_id}|{SMOKE_KIND}",
        taxon_id=entry.taxon_id,
        first=PlateRef(entry.portrait_path, "approved catalog plate"),
        second=PlateRef(planted, f"planted {SMOKE_KIND}"),
        references=references,
        expected_text=expected_text(entry.profile),
    )


def judge_smoke(
    run: Run,
    config: EvalConfig,
    judges: Sequence[Judge],
    entry: PanelSpecies,
    references: FrozenReferences,
) -> list[dict[str, object]]:
    """Ask every judge once, unconditionally, and report what each returned and how long it took.

    The verdicts are not stored as verdicts and never count in a comparison
    or calibration; each call is still logged in judgments.jsonl.
    """
    spec = smoke_pair(run, entry, references)
    pair_id, sides = prepare_pair(run, spec, config.rubric(), [judge.name for judge in judges])
    ledger = Ledger(run.path("judgments.jsonl"))
    specs = {judge.name: judge for judge in config.judges}
    results: list[dict[str, object]] = []
    for judge in judges:
        work = run.path("smoke", pair_id, judge.name)
        request = JudgeRequest(
            blind_dir=blind_folder(run, spec.identity, judge.name),
            reference_files=tuple(name for _path, name in reference_names(references)),
            expected_text=spec.expected_text,
            work_dir=work,
        )
        configured = specs.get(judge.name)
        result: dict[str, object] = {
            "judge": judge.name,
            "kind": None if configured is None else configured.kind,
            "model": None if configured is None else configured.model,
            "approved_plate_side": sides[judge.name],
            "log": run_relative(run, work / "judge.log"),
        }
        ledger.record("judgment-started", pair=pair_id, judge=judge.name, smoke=True)
        started = time.monotonic()
        try:
            verdict = judge.judge(request)
        except JudgeError as exc:
            result.update(ok=False, seconds=round(time.monotonic() - started, 1), error=str(exc))
            ledger.record("judgment-failed", pair=pair_id, judge=judge.name, error=str(exc))
            results.append(result)
            continue
        seconds = round(time.monotonic() - started, 1)
        usage = None if verdict.usage is None else dict(verdict.usage)
        ledger.record("judgment-finished", pair=pair_id, judge=judge.name, usage=usage, smoke=True)
        vote = unblind(judge.name, verdict, sides[judge.name])
        result.update(
            ok=True,
            seconds=seconds,
            usage=usage,
            verdict=verdict.as_dict(),
            preferred={"anatomy": ROLES[vote.anatomy], "lettering": ROLES[vote.lettering]},
        )
        results.append(result)
    write_json(run.path("smoke", "summary.json"), {"taxon_id": entry.taxon_id, "judges": results})
    return results
