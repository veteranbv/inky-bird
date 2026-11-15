"""Blind pairs: anonymised folders, the hidden side key, judging and unblinding.

A pair is two plates in a fixed order, "first" and "second". Each judge sees
them only as plate-A.png and plate-B.png in a blind folder of its own; which is
which is drawn independently for every (pair, judge) and kept in a key file
under unblinding/, named so it cannot be found from a folder's name. Judges
never run inside the run directory at all: each gets a private copy of its
folder (see judges.isolated_pair).

A verdict is stamped with the rubric's digest and the judge's kind and model;
a change to any of them makes the pair be judged again.
"""

from __future__ import annotations

import functools
import os
import random
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from PIL import Image

from inky_bird_frame.models import SpeciesProfileData

from .config import EvalConfig, HarnessError, JudgeSpec
from .judges import (
    FIXED_MTIME,
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
    judge_contract_sha256,
    parse_verdict,
)
from .panel import FrozenReferences
from .rundir import (
    Ledger,
    Run,
    read_object,
    replace_directory,
    run_relative,
    run_tasks,
    sha256_file,
    sha256_text,
    staging_directory,
    write_json,
    write_text,
)

Role = Literal["first", "second"]
Outcome = Literal["first", "second", "tie"]
RULER_NOTE: Final = "SCHEMATIC — NOT TO SCALE"
KEYS_DIR: Final = "unblinding"


# Tests that inspect a judged folder set this; runs leave it off to bound disk use.
KEEP_JUDGED_FOLDERS = False


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
    # Forces which letter the first plate gets for a judge; any other judge's
    # letter is drawn from the run seed.
    first_sides: Mapping[str, Side] = field(default_factory=dict)


@dataclass(frozen=True)
class Vote:
    """One judge's verdict on a pair, unblinded to first/second."""

    judge: str
    anatomy: Outcome
    lettering: Outcome
    first: PlateAssessment
    second: PlateAssessment
    # The letter this judge saw the first plate under, for position-bias checks.
    first_side: Side = "A"


@dataclass(frozen=True)
class JudgedPair:
    spec: PairSpec
    pair_id: str
    first_sides: Mapping[str, Side]
    votes: tuple[Vote, ...]
    errors: tuple[tuple[str, str], ...]


def other(side: Side) -> Side:
    return "B" if side == "A" else "A"


def unblind_choice(choice: Choice, first_side: Side) -> Outcome:
    if choice == "tie":
        return "tie"
    return "first" if choice == first_side else "second"


def blind_choice(outcome: Outcome, first_side: Side) -> Choice:
    """The letter a judge chose, recovered from its unblinded outcome."""
    if outcome == "tie":
        return "tie"
    return first_side if outcome == "first" else other(first_side)


def unblind(judge: str, verdict: BlindVerdict, first_side: Side) -> Vote:
    return Vote(
        judge=judge,
        anatomy=unblind_choice(verdict.anatomy, first_side),
        lettering=unblind_choice(verdict.lettering, first_side),
        first=verdict.plate(first_side),
        second=verdict.plate(other(first_side)),
        first_side=first_side,
    )


def _anonymised_copy(source: Path, destination: Path) -> None:
    """Re-encode from bare pixels, so only IHDR, IDAT and IEND reach the judge.

    A copy of the decoded image would keep its info (an ICC profile, text,
    resolution or EXIF), which can differ between how two arms' plates were made.
    """
    try:
        with Image.open(source) as image:
            rgb = image.convert("RGB")
            bare = Image.frombytes("RGB", rgb.size, rgb.tobytes())
        bare.save(destination, format="PNG")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HarnessError(f"A plate could not be read: {source.name}") from exc


def reference_names(references: FrozenReferences) -> list[tuple[Path, str]]:
    return [
        (path, f"reference-{index:02d}{path.suffix.lower() or '.jpg'}")
        for index, path in enumerate(references.paths, start=1)
    ]


def key_path(run: Run, identity: str) -> Path:
    return run.path(KEYS_DIR, f"{run.opaque_id(f'key|{identity}')}.json")


def blind_folder(run: Run, identity: str, judge: str) -> Path:
    """One judge's folder for one pair; its name says nothing about either."""
    return run.path("blind", run.opaque_id(f"folder|{identity}|{judge}"))


def draw_side(run: Run, identity: str, judge: str) -> Side:
    draw = random.Random(run.derived(f"side|{identity}|{judge}")).random()
    return "A" if draw < 0.5 else "B"


def _build_folder(staging: Path, spec: PairSpec, first_side: Side, rubric: str) -> None:
    by_side = {first_side: spec.first.path, other(first_side): spec.second.path}
    # Written in A, B order with one fixed time, so file times say nothing about roles.
    for side in ("A", "B"):
        _anonymised_copy(by_side[side], staging / PLATE_FILES[side])
    (staging / REFERENCES_DIR).mkdir()
    for source, name in reference_names(spec.references):
        shutil.copyfile(source, staging / REFERENCES_DIR / name)
    write_text(staging / RUBRIC_FILE, rubric)
    for path in [*sorted(staging.rglob("*")), staging]:
        os.utime(path, (FIXED_MTIME, FIXED_MTIME))


def _publish_folder(final: Path, spec: PairSpec, first_side: Side, rubric: str) -> None:
    """Build the blind folder, replacing one made with another rubric.

    A crash after the old folder is moved aside leaves no folder, which the
    next run rebuilds from the key; the moved-aside copy is removed then.
    """
    retired = final.parent / f".{final.name}.retired"
    shutil.rmtree(retired, ignore_errors=True)
    if final.exists():
        current = final / RUBRIC_FILE
        if current.is_file() and sha256_file(current) == sha256_text(rubric):
            return
    staging = staging_directory(final)
    try:
        _build_folder(staging, spec, first_side, rubric)
        if final.exists():
            final.replace(retired)
        replace_directory(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(retired, ignore_errors=True)


def prepare_pair(
    run: Run, spec: PairSpec, rubric: str, judges: Sequence[str]
) -> tuple[str, dict[str, Side]]:
    """Create (or reuse) the pair's key and each judge's blind folder.

    Returns the pair id and, per judge, the letter the first plate carries.
    The key is written before any folder; each folder is published by one
    rename. A crash before a rename leaves a key and no folder, and the next
    run rebuilds the folder from the key. A judge added later gets its own
    draw, added to the key before its folder is built.
    """
    pair_id = run.opaque_id(spec.identity)
    sealed = key_path(run, spec.identity)
    plates = {"first": spec.first, "second": spec.second}
    sides: dict[str, Side] = {}
    if sealed.exists():
        key = read_object(sealed)
        if key.get("identity") != spec.identity or key.get("pair_id") != pair_id:
            raise HarnessError(f"Key {pair_id} belongs to another pair")
        recorded = key.get("sides")
        if not isinstance(recorded, dict):
            raise HarnessError(
                f"Key {pair_id} was written by an older harness; start a new run directory"
            )
        for judge, side in recorded.items():
            if side not in ("A", "B"):
                raise HarnessError(f"Key {pair_id} has no side for {judge}")
            sides[str(judge)] = "A" if side == "A" else "B"
        for role, plate in plates.items():
            entry = key.get(role)
            if not isinstance(entry, dict) or entry.get("sha256") != sha256_file(plate.path):
                raise HarnessError(f"The {role} plate of pair {pair_id} changed since it was keyed")
    else:
        key = {
            "identity": spec.identity,
            "pair_id": pair_id,
            "taxon_id": spec.taxon_id,
            **{
                role: {
                    "label": plate.label,
                    "path": run_relative(run, plate.path),
                    "sha256": sha256_file(plate.path),
                }
                for role, plate in plates.items()
            },
        }
    missing = [judge for judge in judges if judge not in sides]
    if missing or not sealed.exists():
        for judge in missing:
            sides[judge] = spec.first_sides.get(judge) or draw_side(run, spec.identity, judge)
        write_json(sealed, {**key, "sides": dict(sorted(sides.items()))})
    for judge in judges:
        _publish_folder(blind_folder(run, spec.identity, judge), spec, sides[judge], rubric)
    return pair_id, {judge: sides[judge] for judge in judges}


def _verdict_path(run: Run, pair_id: str, judge: str) -> Path:
    return run.path("verdicts", pair_id, f"{judge}.json")


def _judge_spec(config: EvalConfig, name: str) -> JudgeSpec:
    for spec in config.judges:
        if spec.name == name:
            return spec
    raise HarnessError(f"Judge {name} is not in the configuration")


def verdict_stamp(config: EvalConfig, judge: str, rubric: str) -> dict[str, object]:
    """What a verdict depends on besides the pair: the rubric, the judging prompt and model."""
    spec = _judge_spec(config, judge)
    return {
        "rubric_sha256": sha256_text(rubric),
        "judge_contract_sha256": judge_contract_sha256(),
        "judge_kind": spec.kind,
        "judge_model": spec.model,
    }


def _current_verdict(path: Path, stamp: dict[str, object]) -> dict[str, object] | None:
    if not path.exists():
        return None
    record = read_object(path)
    return record if record.get("stamp") == stamp else None


def _judge_once(
    run: Run,
    judge: Judge,
    pair_id: str,
    spec: PairSpec,
    attempts: int,
    ledger: Ledger,
    stamp: dict[str, object],
) -> None:
    request = JudgeRequest(
        blind_dir=blind_folder(run, spec.identity, judge.name),
        reference_files=tuple(name for _path, name in reference_names(spec.references)),
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
        usage = None if verdict.usage is None else dict(verdict.usage)
        write_json(
            _verdict_path(run, pair_id, judge.name),
            {"stamp": stamp, **verdict.as_dict(), "usage": usage},
        )
        error_path.unlink(missing_ok=True)
        ledger.record("judgment-finished", pair=pair_id, judge=judge.name, usage=usage)
        # The verdict is stored; the folder's plate copies are rebuilt from the
        # key if this pair is ever judged again.
        if not KEEP_JUDGED_FOLDERS:
            shutil.rmtree(request.blind_dir, ignore_errors=True)
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
    """Judge every pair with every judge, skipping current verdicts already on disk."""
    rubric = config.rubric()
    stamps = {judge.name: verdict_stamp(config, judge.name, rubric) for judge in judges}
    names = [judge.name for judge in judges]
    prepared = [(spec, *prepare_pair(run, spec, rubric, names)) for spec in specs]
    ledger = Ledger(run.path("judgments.jsonl"))
    run_tasks(
        [
            functools.partial(
                _judge_once,
                run,
                judge,
                pair_id,
                spec,
                config.judge_attempts,
                ledger,
                stamps[judge.name],
            )
            for spec, pair_id, _side in prepared
            for judge in judges
            if _current_verdict(_verdict_path(run, pair_id, judge.name), stamps[judge.name]) is None
        ],
        parallel,
    )
    return [
        load_judged(run, spec, pair_id, side, judges, stamps) for spec, pair_id, side in prepared
    ]


def load_judged(
    run: Run,
    spec: PairSpec,
    pair_id: str,
    first_sides: Mapping[str, Side],
    judges: Sequence[Judge],
    stamps: dict[str, dict[str, object]],
) -> JudgedPair:
    votes: list[Vote] = []
    errors: list[tuple[str, str]] = []
    for judge in judges:
        record = _current_verdict(_verdict_path(run, pair_id, judge.name), stamps[judge.name])
        if record is not None:
            votes.append(unblind(judge.name, parse_verdict(record), first_sides[judge.name]))
            continue
        error_path = run.path("verdicts", pair_id, f"{judge.name}.error.json")
        reason = read_object(error_path).get("error") if error_path.exists() else "missing"
        errors.append((judge.name, str(reason)))
    return JudgedPair(spec, pair_id, dict(first_sides), tuple(votes), tuple(errors))


def pending_judgments(
    run: Run, specs: Sequence[PairSpec], judges: Sequence[Judge], config: EvalConfig
) -> int:
    rubric = config.rubric()
    return sum(
        1
        for spec in specs
        for judge in judges
        if _current_verdict(
            _verdict_path(run, run.opaque_id(spec.identity), judge.name),
            verdict_stamp(config, judge.name, rubric),
        )
        is None
    )
