"""Blind pairs: anonymised folders, the hidden side key, judging and unblinding.

A pair is two plates in a fixed order, "first" and "second". Each judge sees
them only as plate-A.png and plate-B.png; which is which is drawn per pair and
kept in a key file outside the judged folder, under keys/.
"""

from __future__ import annotations

import functools
import random
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from PIL import Image

from inky_bird_frame.models import SpeciesProfileData

from .config import EvalConfig, HarnessError
from .judges import (
    PLATE_FILES,
    REFERENCES_DIR,
    RUBRIC_FILE,
    BlindVerdict,
    Choice,
    Judge,
    JudgeError,
    JudgeRequest,
    PlateAssessment,
    Side,
    parse_verdict,
)
from .panel import FrozenReferences
from .rundir import (
    Ledger,
    Run,
    read_object,
    replace_directory,
    run_tasks,
    sha256_file,
    staging_directory,
    write_json,
    write_text,
)

Role = Literal["first", "second"]
Outcome = Literal["first", "second", "tie"]
RULER_NOTE: Final = "SCHEMATIC — NOT TO SCALE"


def expected_text(profile: SpeciesProfileData) -> dict[str, object]:
    """The lettering a correct plate carries, with the labels the prompt asks for."""
    measurements = profile["measurements"]
    return {
        "common_name": profile["common_name"],
        "scientific_name": profile["scientific_name"],
        "family": profile["family"],
        "labelled_values": {
            "Length:": measurements["length"],
            "Wingspan:": measurements["wingspan"],
            "Weight:": measurements["weight"],
            "Habitat:": profile["habitat"],
            "Behavior:": profile["behavior"],
        },
        "field_marks_heading": "Field marks",
        "field_marks": list(profile["field_marks"]),
        "color_swatch_names": list(profile["palette"]),
        "ruler_label": f"BODY LENGTH: {measurements['length']}",
        "ruler_note": RULER_NOTE,
    }


@dataclass(frozen=True)
class PlateRef:
    path: Path
    label: str


@dataclass(frozen=True)
class PairSpec:
    identity: str
    taxon_id: int
    first: PlateRef
    second: PlateRef
    references: FrozenReferences
    expected_text: dict[str, object]
    # Forces which letter the first plate gets; None draws it from the run seed.
    first_side: Side | None = None


@dataclass(frozen=True)
class Vote:
    """One judge's verdict on a pair, unblinded to first/second."""

    judge: str
    anatomy: Outcome
    lettering: Outcome
    first: PlateAssessment
    second: PlateAssessment


@dataclass(frozen=True)
class JudgedPair:
    spec: PairSpec
    pair_id: str
    first_side: Side
    votes: tuple[Vote, ...]
    errors: tuple[tuple[str, str], ...]


def other(side: Side) -> Side:
    return "B" if side == "A" else "A"


def unblind_choice(choice: Choice, first_side: Side) -> Outcome:
    if choice == "tie":
        return "tie"
    return "first" if choice == first_side else "second"


def unblind(judge: str, verdict: BlindVerdict, first_side: Side) -> Vote:
    return Vote(
        judge=judge,
        anatomy=unblind_choice(verdict.anatomy, first_side),
        lettering=unblind_choice(verdict.lettering, first_side),
        first=verdict.plate(first_side),
        second=verdict.plate(other(first_side)),
    )


def _anonymised_copy(source: Path, destination: Path) -> None:
    """Re-encode so no file name, timestamp or embedded text travels with the plate."""
    try:
        with Image.open(source) as image:
            image.convert("RGB").save(destination, format="PNG")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HarnessError(f"A plate could not be read: {source.name}") from exc


def _reference_names(references: FrozenReferences) -> list[tuple[Path, str]]:
    return [
        (path, f"reference-{index:02d}{path.suffix.lower() or '.jpg'}")
        for index, path in enumerate(references.paths, start=1)
    ]


def prepare_pair(run: Run, spec: PairSpec, rubric: str) -> tuple[str, Side]:
    """Create (or reuse) the pair's key and blind folder; return its id and first side.

    The key is written before the folder; the folder is published by one
    rename. A crash before the rename leaves a key and no folder, and the
    next run rebuilds the folder from the key.
    """
    pair_id = run.opaque_id(spec.identity)
    key_path = run.path("keys", f"{pair_id}.json")
    plates = {"first": spec.first, "second": spec.second}
    if key_path.exists():
        key = read_object(key_path)
        if key.get("identity") != spec.identity:
            raise HarnessError(f"Key {pair_id} belongs to another pair")
        recorded = key.get("first_side")
        if recorded not in ("A", "B"):
            raise HarnessError(f"Key {pair_id} has no side")
        first_side: Side = "A" if recorded == "A" else "B"
        for role, plate in plates.items():
            entry = key.get(role)
            if not isinstance(entry, dict) or entry.get("sha256") != sha256_file(plate.path):
                raise HarnessError(f"The {role} plate of pair {pair_id} changed since it was keyed")
    else:
        if spec.first_side is None:
            draw = random.Random(run.derived(spec.identity)).random()
            first_side = "A" if draw < 0.5 else "B"
        else:
            first_side = spec.first_side
        write_json(
            key_path,
            {
                "identity": spec.identity,
                "taxon_id": spec.taxon_id,
                "first_side": first_side,
                **{
                    role: {
                        "label": plate.label,
                        "path": str(plate.path.relative_to(run.root))
                        if plate.path.is_relative_to(run.root)
                        else str(plate.path),
                        "sha256": sha256_file(plate.path),
                    }
                    for role, plate in plates.items()
                },
            },
        )
    final = run.path("blind", pair_id)
    if not final.exists():
        staging = staging_directory(final)
        try:
            by_side = {
                first_side: spec.first.path,
                other(first_side): spec.second.path,
            }
            for side, source in by_side.items():
                _anonymised_copy(source, staging / PLATE_FILES[side])
            (staging / REFERENCES_DIR).mkdir()
            for source, name in _reference_names(spec.references):
                shutil.copyfile(source, staging / REFERENCES_DIR / name)
            write_text(staging / RUBRIC_FILE, rubric)
            replace_directory(staging, final)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return pair_id, first_side


def _verdict_path(run: Run, pair_id: str, judge: str) -> Path:
    return run.path("verdicts", pair_id, f"{judge}.json")


def _judge_once(
    run: Run,
    judge: Judge,
    pair_id: str,
    spec: PairSpec,
    attempts: int,
    ledger: Ledger,
) -> None:
    request = JudgeRequest(
        blind_dir=run.path("blind", pair_id),
        reference_files=tuple(name for _path, name in _reference_names(spec.references)),
        expected_text=spec.expected_text,
        work_dir=run.path("verdicts", pair_id, f"{judge.name}-work"),
    )
    error_path = run.path("verdicts", pair_id, f"{judge.name}.error.json")
    message = "no attempt was made"
    for attempt in range(1, attempts + 1):
        ledger.record("judgment-started", pair=pair_id, judge=judge.name, attempt=attempt)
        try:
            verdict = judge.judge(request)
        except JudgeError as exc:
            message = str(exc)
            ledger.record("judgment-failed", pair=pair_id, judge=judge.name, error=message)
            continue
        write_json(_verdict_path(run, pair_id, judge.name), verdict.as_dict())
        error_path.unlink(missing_ok=True)
        ledger.record("judgment-finished", pair=pair_id, judge=judge.name)
        return
    write_json(error_path, {"error": message, "attempts": attempts})


def judge_pairs(
    run: Run,
    specs: Sequence[PairSpec],
    judges: Sequence[Judge],
    config: EvalConfig,
    *,
    parallel: int = 1,
) -> list[JudgedPair]:
    """Judge every pair with every judge, skipping verdicts already on disk."""
    rubric = config.rubric()
    prepared = [(spec, *prepare_pair(run, spec, rubric)) for spec in specs]
    ledger = Ledger(run.path("judgments.jsonl"))
    run_tasks(
        [
            functools.partial(_judge_once, run, judge, pair_id, spec, config.judge_attempts, ledger)
            for spec, pair_id, _side in prepared
            for judge in judges
            if not _verdict_path(run, pair_id, judge.name).exists()
        ],
        parallel,
    )
    return [load_judged(run, spec, pair_id, side, judges) for spec, pair_id, side in prepared]


def load_judged(
    run: Run, spec: PairSpec, pair_id: str, first_side: Side, judges: Sequence[Judge]
) -> JudgedPair:
    votes: list[Vote] = []
    errors: list[tuple[str, str]] = []
    for judge in judges:
        path = _verdict_path(run, pair_id, judge.name)
        if path.exists():
            votes.append(unblind(judge.name, parse_verdict(read_object(path)), first_side))
            continue
        error_path = run.path("verdicts", pair_id, f"{judge.name}.error.json")
        reason = read_object(error_path).get("error") if error_path.exists() else "missing"
        errors.append((judge.name, str(reason)))
    return JudgedPair(spec, pair_id, first_side, tuple(votes), tuple(errors))


def pending_judgments(run: Run, specs: Sequence[PairSpec], judges: Sequence[Judge]) -> int:
    return sum(
        1
        for spec in specs
        for judge in judges
        if not _verdict_path(run, run.opaque_id(spec.identity), judge.name).exists()
    )
