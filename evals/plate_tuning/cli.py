"""inky-plate-eval: cache references, calibrate judges, compare, tune and report."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from inky_bird_frame.errors import InkyBirdFrameError
from inky_bird_frame.prompts import PLATE_INSTRUCTIONS, PROMPT_VERSION
from inky_bird_frame.references import download_references

from .calibrate import calibrate, calibration_plan
from .compare import compare, plan
from .config import EvalConfig, HarnessError, load_config
from .generate import Arm, CodexGenerator, Generator, missing_samples, parse_arm
from .judges import Judge, build_judges, find_executable
from .panel import (
    DEFAULT_PANEL,
    Downloader,
    PanelSpecies,
    freeze_references,
    frozen_references,
    load_panel,
    select,
)
from .report import write_report
from .rundir import Budget, Run
from .tune import (
    BASELINE_ARM,
    ClaudeReflector,
    CodexReviewEvaluator,
    Optimizer,
    ReflectionLM,
    ReviewEvaluator,
    build_labels,
    tune_plate,
    tune_review,
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
    review_evaluator: Callable[[EvalConfig], ReviewEvaluator] = CodexReviewEvaluator
    downloader: Downloader = download_references


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inky-plate-eval",
        description="Blind evaluation and tuning of the plate and review prompts.",
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

    cache = command("cache-references", "Download and freeze each species' reference photos")
    cache.add_argument("--species", type=int, nargs="*", default=[], help="taxon ids")

    calibrate_command = command("calibrate", "Measure each judge against known answers")
    calibrate_command.add_argument("--anchors", type=Path, help="ANCHORS/<taxon>/<plate>.png")
    calibrate_command.add_argument("--planted-species", type=int, help="species for planting")
    calibrate_command.add_argument("--dry-run", action="store_true")

    compare_command = command("compare", "Blind paired comparison of two arms")
    compare_command.add_argument("first", help="candidate arm: NAME=default or NAME=PATH")
    compare_command.add_argument("second", help="baseline arm: NAME=default or NAME=PATH")
    compare_command.add_argument("--max-generations", type=int, help="hard generation cap")
    compare_command.add_argument("--samples", type=int, help="plates per species per arm")
    compare_command.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    compare_command.add_argument("--species", type=int, nargs="*", default=[])
    compare_command.add_argument("--dry-run", action="store_true")

    tune_command = command("tune", "Tune the plate instructions, then test on held-out species")
    tune_command.add_argument("--max-generations", type=int, help="hard generation cap")
    tune_command.add_argument("--budget", type=int, required=True, help="optimiser metric calls")
    tune_command.add_argument("--holdout-samples", type=int, help="plates per held-out species")
    tune_command.add_argument("--dry-run", action="store_true")

    review_command = command("tune-review", "Tune the review instructions on labelled plates")
    review_command.add_argument(
        "--labels-from", type=Path, action="append", required=True, help="earlier run directory"
    )
    review_command.add_argument("--max-reviews", type=int, help="hard review cap")
    review_command.add_argument("--budget", type=int, required=True, help="optimiser metric calls")
    review_command.add_argument("--dry-run", action="store_true")

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


def _cache_references(
    args: argparse.Namespace, run: Run, panel: Sequence[PanelSpecies], tools: Tools
) -> dict[str, object]:
    frozen: dict[str, int] = {}
    for entry in select(panel, taxa=args.species):
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
    estimate = calibration_plan(len(chosen), anchors, len(config.judges))
    if args.dry_run:
        return {"plan": estimate, "species": [entry.taxon_id for entry in chosen]}
    results = calibrate(
        run,
        config,
        tools.judges(config),
        [(entry, frozen_references(run, entry)) for entry in chosen],
        anchors=args.anchors,
        parallel=args.parallel,
    )
    return {"plan": estimate, "judges": [result.as_dict() for result in results]}


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
    estimate = plan(run, first, second, species, samples, len(config.judges)).as_dict()
    if args.dry_run:
        return {"plan": estimate, "cap": cap}
    result = compare(
        run,
        config,
        tools.generator(config),
        tools.judges(config),
        first,
        second,
        species,
        samples=samples,
        budget=Budget(cap),
        parallel=args.parallel,
    )
    return {"plan": estimate, **result.as_dict()}


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
    # Worst case: every metric call generates one plate, plus the baseline
    # plates for dev and both arms on the held-out species.
    needed = (
        args.budget
        + missing_samples(run, [baseline], dev, 1)
        + missing_samples(run, [baseline], holdout, holdout_samples)
        + len(holdout) * holdout_samples
    )
    estimate = {
        "generations_at_most": needed,
        "metric_calls": args.budget,
        "dev_species": len(dev),
        "holdout_species": len(holdout),
    }
    if args.dry_run:
        return {"plan": estimate, "cap": cap}
    if cap < needed:
        raise HarnessError(
            f"--max-generations {cap} is below the {needed} this tuning may need, so the"
            " held-out test could not finish; raise the cap or lower --budget"
        )
    for entry in panel:
        frozen_references(run, entry)
    result = tune_plate(
        run,
        config,
        tools.generator(config),
        tools.judges(config),
        panel,
        optimizer=tools.optimizer(),
        reflection_lm=tools.reflector(run, config),
        budget=Budget(cap),
        metric_calls=args.budget,
        holdout_samples=holdout_samples,
        parallel=args.parallel,
    )
    return {"plan": estimate, **result.as_dict()}


def _tune_review(
    args: argparse.Namespace,
    run: Run,
    config: EvalConfig,
    panel: Sequence[PanelSpecies],
    tools: Tools,
) -> dict[str, object]:
    cap = _require_cap(args.max_reviews, "--max-reviews")
    if args.budget < 1:
        raise HarnessError("--budget must be at least 1")
    labels = build_labels([path.resolve() for path in args.labels_from])
    splits = {entry.taxon_id: entry.split for entry in panel}
    held_out = sum(1 for item in labels if splits.get(item.taxon_id) == "holdout")
    needed = args.budget + 2 * held_out
    estimate = {"labelled_plates": len(labels), "reviews_at_most": needed}
    if args.dry_run:
        return {"plan": estimate, "cap": cap}
    if cap < needed:
        raise HarnessError(
            f"--max-reviews {cap} is below the {needed} this tuning may need;"
            " raise the cap or lower --budget"
        )
    summary = tune_review(
        run,
        config,
        tools.review_evaluator(config),
        panel,
        labels,
        optimizer=tools.optimizer(),
        reflection_lm=tools.reflector(run, config),
        budget=Budget(cap, what="review"),
        metric_calls=args.budget,
        parallel=args.parallel,
    )
    return {"plan": estimate, **summary}


def _run(args: argparse.Namespace, tools: Tools) -> dict[str, object]:
    config = load_config(args.config)
    if args.parallel < 1:
        raise HarnessError("--parallel must be at least 1")
    run = Run.open(args.run_dir, seed=args.seed)
    if args.command == "report":
        return write_report(run)
    panel = load_panel(args.catalog, args.panel)
    if args.command == "cache-references":
        return _cache_references(args, run, panel, tools)
    commands = {
        "calibrate": _calibrate,
        "compare": _compare,
        "tune": _tune,
        "tune-review": _tune_review,
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
