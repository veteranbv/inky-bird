"""inky-plate-eval: cache references, calibrate judges, compare, tune and report."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from inky_bird_frame.errors import InkyBirdFrameError
from inky_bird_frame.prompts import PLATE_INSTRUCTIONS, PROMPT_VERSION
from inky_bird_frame.references import download_references

from .calibrate import anchor_taxa, calibrate, calibration_plan
from .compare import compare, plan
from .config import EvalConfig, HarnessError, load_config
from .gate import CalibrationGate, calibration_gate, load_calibration
from .generate import (
    GENERATIONS_LEDGER,
    Arm,
    CodexGenerator,
    Generator,
    missing_samples,
    parse_arm,
)
from .judges import Judge, build_judges, find_executable
from .panel import (
    DEFAULT_PANEL,
    Downloader,
    PanelSpecies,
    freeze_references,
    frozen_references,
    load_panel,
    resolve_species,
    select,
)
from .report import write_report
from .rundir import Budget, Ledger, Run, locked, read_object
from .smoke import judge_smoke
from .tune import (
    BASELINE_ARM,
    ClaudeReflector,
    Optimizer,
    ReflectionLM,
    bind_evaluation,
    candidate_generations,
    evaluation_fingerprint,
    holdout_reservation,
    selected_candidate,
    tune_plate,
)

EXIT_ERROR: Final = 1
EXIT_USAGE: Final = 2


def _real_optimizer() -> Optimizer:
    from .gepa_integration import gepa_optimizer

    return gepa_optimizer


@dataclass
class Tools:
    """Everything that costs money or touches the network, replaceable in tests."""

    generator: Callable[[EvalConfig], Generator] = field(
        default=lambda config: CodexGenerator(
            find_executable(config.codex_executable),
            timeout_seconds=config.generation_timeout_seconds,
            model=config.generation_model,
        )
    )
    judges: Callable[[EvalConfig], Sequence[Judge]] = build_judges
    reflector: Callable[[Run, EvalConfig], ReflectionLM] = ClaudeReflector
    optimizer: Callable[[], Optimizer] = _real_optimizer
    downloader: Downloader = download_references


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inky-plate-eval",
        description="Blind evaluation and tuning of the plate prompt.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text)
        sub.add_argument("--run-dir", type=Path, required=True, help="run directory")
        sub.add_argument("--config", type=Path, help="TOML configuration file")
        sub.add_argument("--catalog", type=Path, default=Path("catalog"), help="catalog root")
        sub.add_argument("--panel", type=Path, default=DEFAULT_PANEL, help="panel JSON file")
        sub.add_argument("--parallel", type=int, default=1, help="concurrent paid calls")
        sub.add_argument("--seed", type=int, help="seed for a new run directory")
        return sub

    def gated(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--calibration", type=Path, help="calibration/summary.json from another run directory"
        )
        sub.add_argument(
            "--allow-uncalibrated",
            action="store_true",
            help="explore without a passing calibration; no decision is made",
        )

    cache = command("cache-references", "Download and freeze each species' reference photos")
    cache.add_argument(
        "--species",
        type=int,
        nargs="*",
        default=[],
        help="taxon ids: panel species, or approved catalog species used as anchors",
    )

    calibrate_command = command("calibrate", "Measure each judge against known answers")
    calibrate_command.add_argument("--anchors", type=Path, help="ANCHORS/<taxon>/<plate>.png")
    calibrate_command.add_argument("--planted-species", type=int, help="species for planting")
    calibrate_command.add_argument("--dry-run", action="store_true")

    smoke = command("judge-smoke", "Send one known pair to each judge once and show the answers")
    smoke.add_argument("--species", type=int, help="panel taxon id (default: the first)")
    smoke.add_argument("--judge", action="append", default=[], help="only this judge")
    smoke.add_argument("--dry-run", action="store_true")

    compare_command = command("compare", "Blind paired comparison of two arms")
    compare_command.add_argument(
        "first", help="candidate arm: NAME=default, NAME=ref:GITREF or NAME=PATH"
    )
    compare_command.add_argument(
        "second", help="baseline arm: NAME=default, NAME=ref:GITREF or NAME=PATH"
    )
    compare_command.add_argument("--max-generations", type=int, help="hard generation cap")
    compare_command.add_argument("--samples", type=int, help="plates per species per arm")
    compare_command.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    compare_command.add_argument("--species", type=int, nargs="*", default=[])
    compare_command.add_argument("--dry-run", action="store_true")
    gated(compare_command)

    tune_command = command("tune", "Tune the plate instructions, then test on held-out species")
    tune_command.add_argument("--max-generations", type=int, help="hard generation cap")
    tune_command.add_argument("--budget", type=int, required=True, help="optimiser metric calls")
    tune_command.add_argument("--holdout-samples", type=int, help="plates per held-out species")
    tune_command.add_argument("--dry-run", action="store_true")
    gated(tune_command)

    command("report", "Write the markdown report and contact sheets")
    return parser


def spread(panel: Sequence[PanelSpecies], count: int) -> list[PanelSpecies]:
    """Up to count species, taking one per body plan before repeating any."""
    chosen: list[PanelSpecies] = []
    remaining = list(panel)
    while remaining and len(chosen) < count:
        strata: set[str] = set()
        for entry in list(remaining):
            if entry.stratum not in strata and len(chosen) < count:
                strata.add(entry.stratum)
                chosen.append(entry)
                remaining.remove(entry)
    return chosen


def _require_cap(value: int | None, flag: str) -> int:
    if value is None:
        raise HarnessError(f"{flag} is required: this command pays for every call it makes")
    if value < 0:
        raise HarnessError(f"{flag} must not be negative")
    return value


def _run_budget(run: Run, cap: int) -> Budget:
    """The cap covers the whole run directory: calls earlier commands started count too."""
    ledger = Ledger(run.path(GENERATIONS_LEDGER))
    return Budget.for_run(cap, ledger, "generation-started", what="generation")


def _cap_report(budget: Budget) -> dict[str, int]:
    return {"cap": budget.limit, "used": budget.used, "remaining": budget.remaining}


def _gate(args: argparse.Namespace, run: Run, config: EvalConfig) -> CalibrationGate:
    record, source = load_calibration(run, args.calibration)
    return calibration_gate(config, record, source=source)


def _require_gate(args: argparse.Namespace, gate: CalibrationGate) -> bool:
    """Whether this is an exploratory run; refuses a gated run without a passing calibration."""
    if gate.passed:
        return False
    if args.allow_uncalibrated:
        return True
    raise HarnessError(
        "No passing calibration for these judges and this rubric ("
        + "; ".join(gate.reasons)
        + "); run calibrate first, or pass --allow-uncalibrated to explore without a decision"
    )


def _calibrated_only(config: EvalConfig, gate: CalibrationGate) -> EvalConfig:
    """The configuration with only the judges that passed, so no call is paid for the others."""
    return replace(
        config, judges=tuple(judge for judge in config.judges if judge.name in gate.calibrated)
    )


def _cache_references(
    args: argparse.Namespace,
    run: Run,
    panel: Sequence[PanelSpecies],
    tools: Tools,
) -> dict[str, object]:
    frozen: dict[str, int] = {}
    chosen = resolve_species(args.catalog, panel, args.species) if args.species else panel
    for entry in chosen:
        references = freeze_references(run, entry, downloader=tools.downloader)
        frozen[entry.key] = len(references.photos)
    return {"frozen": frozen}


def _calibrate(
    args: argparse.Namespace,
    run: Run,
    config: EvalConfig,
    panel: Sequence[PanelSpecies],
    tools: Tools,
) -> dict[str, object]:
    chosen = spread(panel, args.planted_species or config.planted_species)
    anchors = sum(1 for _ in args.anchors.glob("*/*.png")) if args.anchors is not None else 0
    anchor_species = (
        resolve_species(args.catalog, panel, anchor_taxa(args.anchors))
        if args.anchors is not None
        else ()
    )
    estimate = calibration_plan(len(chosen), anchors, len(config.judges), config.judge_attempts)
    if args.dry_run:
        return {
            "plan": estimate,
            "species": [entry.taxon_id for entry in chosen],
            "anchor_species": [entry.taxon_id for entry in anchor_species],
        }
    results = calibrate(
        run,
        config,
        tools.judges(config),
        [(entry, frozen_references(run, entry)) for entry in chosen],
        anchors=args.anchors,
        anchor_species=[(entry, frozen_references(run, entry)) for entry in anchor_species],
        parallel=args.parallel,
    )
    summary = read_object(run.path("calibration", "summary.json"))
    return {
        "plan": estimate,
        "judges": [result.as_dict() for result in results],
        "position_bias": summary.get("position_bias"),
        "gate": calibration_gate(config, summary, source="calibration/summary.json").as_dict(),
    }


def _judge_smoke(
    args: argparse.Namespace,
    run: Run,
    config: EvalConfig,
    panel: Sequence[PanelSpecies],
    tools: Tools,
) -> dict[str, object]:
    entry = select(panel, taxa=[args.species])[0] if args.species is not None else panel[0]
    names = args.judge or [judge.name for judge in config.judges]
    unknown = set(names) - {judge.name for judge in config.judges}
    if unknown:
        raise HarnessError(f"Judge {sorted(unknown)[0]} is not in the configuration")
    if args.dry_run:
        return {"plan": {"judge_calls": len(names), "generations": 0}, "taxon_id": entry.taxon_id}
    chosen = replace(config, judges=tuple(j for j in config.judges if j.name in names))
    results = judge_smoke(run, chosen, tools.judges(chosen), entry, frozen_references(run, entry))
    return {"taxon_id": entry.taxon_id, "judges": results}


def _compare(
    args: argparse.Namespace,
    run: Run,
    config: EvalConfig,
    panel: Sequence[PanelSpecies],
    tools: Tools,
) -> dict[str, object]:
    cap = _require_cap(args.max_generations, "--max-generations")
    first = parse_arm(args.first, base=Path.cwd())
    second = parse_arm(args.second, base=Path.cwd())
    if first.name == second.name:
        raise HarnessError("The two arms need different names")
    species = select(panel, split=args.split, taxa=args.species)
    samples = args.samples or config.samples_per_species
    estimate = plan(
        run, first, second, species, samples, len(config.judges), config.judge_attempts
    ).as_dict()
    budget = _run_budget(run, cap)
    gate = _gate(args, run, config)
    exploratory = not gate.passed and args.allow_uncalibrated
    if args.dry_run:
        return {"plan": estimate, **_cap_report(budget), "calibration": gate.as_dict()}
    result = compare(
        run,
        config,
        tools.generator(config),
        tools.judges(config),
        first,
        second,
        species,
        samples=samples,
        budget=budget,
        gate=gate,
        exploratory=exploratory,
        required_holdout=[entry.taxon_id for entry in panel if entry.split == "holdout"],
        parallel=args.parallel,
    )
    return {"plan": estimate, **_cap_report(budget), **result.as_dict()}


def _tune(
    args: argparse.Namespace,
    run: Run,
    config: EvalConfig,
    panel: Sequence[PanelSpecies],
    tools: Tools,
) -> dict[str, object]:
    cap = _require_cap(args.max_generations, "--max-generations")
    if args.budget < 1:
        raise HarnessError("--budget must be at least 1")
    dev = [entry for entry in panel if entry.split == "dev"]
    holdout = [entry for entry in panel if entry.split == "holdout"]
    holdout_samples = args.holdout_samples or config.samples_per_species
    baseline = Arm(BASELINE_ARM, PLATE_INSTRUCTIONS, PROMPT_VERSION)
    gate = _gate(args, run, config)
    # Without a passing calibration tuning either refuses below or explores with every judge.
    judging = _calibrated_only(config, gate) if gate.passed else config
    bind_evaluation(run, evaluation_fingerprint(judging, panel, holdout_samples), record=False)
    # The held-out test's plates are set aside first; the optimiser gets the rest.
    # Its share is planned as one plate per metric call plus the dev baselines.
    # A resumed tune already paid for its earlier candidate plates; they count as used.
    # Once a candidate is selected the optimiser does not run again, and only
    # the held-out plates still missing are needed.
    selection = selected_candidate(run)
    selected = None if selection is None else selection[0]
    reserved = holdout_reservation(run, baseline, holdout, holdout_samples, selected)
    done = min(candidate_generations(run, dev), args.budget)
    optimiser = (
        0 if selected is not None else args.budget - done + missing_samples(run, [baseline], dev, 1)
    )
    needed = optimiser + reserved
    estimate = {
        "generations_at_most": needed,
        "holdout_generations_reserved": reserved,
        "metric_calls": args.budget,
        "candidate_generations_done": done,
        "candidate_selected": selected is not None,
        "dev_species": len(dev),
        "holdout_species": len(holdout),
    }
    budget = _run_budget(run, cap)
    if args.dry_run:
        return {"plan": estimate, **_cap_report(budget), "calibration": gate.as_dict()}
    if budget.remaining < needed:
        raise HarnessError(
            f"--max-generations {cap} leaves {budget.remaining} for this run, below the"
            f" {needed} this tuning may need, so the held-out test could not finish;"
            " raise the cap or lower --budget"
        )
    exploratory = _require_gate(args, gate)
    for entry in panel:
        frozen_references(run, entry)
    result = tune_plate(
        run,
        judging,
        tools.generator(judging),
        tools.judges(judging),
        panel,
        optimizer=tools.optimizer(),
        reflection_lm=tools.reflector(run, judging),
        budget=budget,
        metric_calls=args.budget,
        holdout_samples=holdout_samples,
        gate=gate,
        exploratory=exploratory,
        parallel=args.parallel,
    )
    return {
        "plan": estimate,
        **_cap_report(budget),
        "calibration": gate.as_dict(),
        **result.as_dict(),
    }


def _run(args: argparse.Namespace, tools: Tools) -> dict[str, object]:
    config = load_config(args.config)
    if args.parallel < 1:
        raise HarnessError("--parallel must be at least 1")
    with locked(args.run_dir.resolve()):
        run = Run.open(args.run_dir, seed=args.seed)
        if args.command == "report":
            return write_report(run)
        panel = load_panel(args.catalog, args.panel)
        if args.command == "cache-references":
            return _cache_references(args, run, panel, tools)
        commands = {
            "calibrate": _calibrate,
            "judge-smoke": _judge_smoke,
            "compare": _compare,
            "tune": _tune,
        }
        return commands[args.command](args, run, config, panel, tools)


def main(argv: Sequence[str] | None = None, *, tools: Tools | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:
        return EXIT_USAGE if exit_request.code else 0
    try:
        data = _run(args, tools or Tools())
    except InkyBirdFrameError as exc:
        print(json.dumps({"ok": False, "error": {"message": str(exc)}}, indent=2))
        return EXIT_ERROR
    print(json.dumps({"ok": True, "command": args.command, "data": data}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
