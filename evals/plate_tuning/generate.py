"""Prompt arms and resumable, capped plate generation."""

from __future__ import annotations

import functools
import re
import shutil
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from inky_bird_frame.codex_runner import REVIEW_SCHEMA, CodexRunner, _parse_review
from inky_bird_frame.errors import GenerationError
from inky_bird_frame.images import prepare_generated_plate
from inky_bird_frame.models import QualityReview
from inky_bird_frame.prompts import (
    PLATE_INSTRUCTIONS,
    PROMPT_VERSION,
    plate_prompt,
    review_prompt,
)

from .config import DEFAULT_MAX_CANDIDATE_CHARACTERS, BudgetExhaustedError, HarnessError
from .panel import FrozenReferences, PanelSpecies
from .rundir import (
    RESERVED_NAMES,
    Budget,
    Ledger,
    Run,
    read_object,
    run_tasks,
    sha256_file,
    sha256_text,
    write_json,
    write_text,
)
from .safety import CandidateRejectedError, validate_candidate

DEFAULT_ARM: Final = "default"
_ARM_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
GENERATED: Final = "generated.png"
PORTRAIT: Final = "portrait.png"
DISPLAY: Final = "display.png"
SAMPLE_RECORD: Final = "sample.json"
GENERATIONS_LEDGER: Final = "generations.jsonl"


@dataclass(frozen=True)
class Arm:
    """A named plate-instruction text; "default" is the shipped wording."""

    name: str
    instructions: str
    label: str

    @property
    def sha256(self) -> str:
        return sha256_text(self.instructions)

    @property
    def is_default(self) -> bool:
        return self.instructions == PLATE_INSTRUCTIONS


def check_arm_name(name: str) -> str:
    if not _ARM_NAME.fullmatch(name) or name in RESERVED_NAMES:
        raise HarnessError(
            f"Arm name {name!r} must be lowercase letters, digits and hyphens and not a"
            " harness directory name"
        )
    return name


def parse_arm(spec: str, *, base: Path) -> Arm:
    """Parse NAME=default or NAME=PATH to an instruction text file."""
    name, separator, source = spec.partition("=")
    if not separator or not source:
        raise HarnessError("An arm is NAME=default or NAME=PATH")
    check_arm_name(name)
    if source == DEFAULT_ARM:
        return Arm(name, PLATE_INSTRUCTIONS, PROMPT_VERSION)
    path = Path(source)
    path = path if path.is_absolute() else base / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarnessError(f"Arm instructions could not be read: {path.name}") from exc
    # Text files end with a newline the template does not carry.
    text = text.removesuffix("\n")
    return Arm(name, text, f"{path.stem}-{sha256_text(text)[:12]}")


def register_arm(
    run: Run, arm: Arm, *, max_characters: int = DEFAULT_MAX_CANDIDATE_CHARACTERS
) -> Arm:
    """Gate and record the arm's exact text; a name keeps its text for the life of the run.

    Every arm, typed on the command line or proposed by the tuner, passes the
    same safety gate before Codex ever sees it.
    """
    try:
        validate_candidate(arm.instructions, target="plate", max_characters=max_characters)
    except CandidateRejectedError as exc:
        raise HarnessError(f"Arm {arm.name} instructions are not usable: {exc}") from exc
    path = run.path("arms", f"{arm.name}.json")
    if path.exists():
        recorded = read_object(path)
        if recorded.get("sha256") != arm.sha256:
            raise HarnessError(f"Arm {arm.name} already exists in this run with other wording")
        return arm
    write_json(
        path,
        {
            "name": arm.name,
            "label": arm.label,
            "sha256": arm.sha256,
            "instructions": arm.instructions,
            "is_default": arm.is_default,
        },
    )
    return arm


@dataclass(frozen=True)
class GenerationRequest:
    arm: Arm
    species: PanelSpecies
    references: FrozenReferences
    sample_dir: Path

    @property
    def output_path(self) -> Path:
        return self.sample_dir / GENERATED


class Generator(Protocol):
    def generate(self, request: GenerationRequest) -> None:
        """Write request.output_path, or raise GenerationError."""


class InstructedCodexRunner(CodexRunner):
    """The application's Codex runner with an instruction override for evaluation.

    These mirror generate_plate and review_plate for a first attempt; the
    application's methods take no instruction argument.
    """

    def generate_with_instructions(
        self, request: GenerationRequest, prompt_path: Path, log_path: Path
    ) -> Path:
        output = request.output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        command = self._base_command(writable=True)
        for image in request.references.paths:
            command.extend(["--image", str(image.resolve())])
        command.append("-")
        prompt = plate_prompt(
            request.species.bird(),
            request.species.profile,
            list(request.references.photos),
            output.resolve(),
            workspace=self.workspace,
            instructions=request.arm.instructions,
        )
        write_text(prompt_path, prompt)
        self._run(command, prompt, log_path)
        if not output.is_file() or output.stat().st_size == 0:
            raise GenerationError(f"Codex did not create the requested plate: {output.name}")
        return output

    def review_with_instructions(
        self,
        species: PanelSpecies,
        references: FrozenReferences,
        plate_path: Path,
        output_path: Path,
        log_path: Path,
        *,
        allowed_domains: tuple[str, ...],
        instructions: str,
    ) -> QualityReview:
        raw = self._structured(
            review_prompt(
                species.bird(),
                species.profile,
                list(references.photos),
                allowed_domains,
                instructions=instructions,
            ),
            REVIEW_SCHEMA,
            [plate_path, *references.paths],
            output_path,
            log_path,
            search=True,
        )
        return _parse_review(raw, species.profile, allowed_domains)


class CodexGenerator:
    def __init__(self, executable: Path, *, timeout_seconds: int, model: str | None) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.model = model

    def generate(self, request: GenerationRequest) -> None:
        runner = InstructedCodexRunner(
            self.executable,
            request.sample_dir,
            self.timeout_seconds,
            model=self.model,
        )
        runner.generate_with_instructions(
            request,
            request.sample_dir / "prompt.txt",
            request.sample_dir / "codex.log",
        )


@dataclass(frozen=True)
class Sample:
    arm: str
    taxon_id: int
    index: int
    portrait: Path


def sample_dir(run: Run, arm: Arm, species: PanelSpecies, index: int) -> Path:
    return run.path(arm.name, species.key, str(index))


def completed_sample(run: Run, arm: Arm, species: PanelSpecies, index: int) -> Sample | None:
    directory = sample_dir(run, arm, species, index)
    record_path = directory / SAMPLE_RECORD
    portrait = directory / PORTRAIT
    if not record_path.is_file() or not portrait.is_file():
        return None
    record = read_object(record_path)
    if record.get("instructions_sha256") != arm.sha256:
        raise HarnessError(f"Sample {directory.name} of {arm.name} was made with other wording")
    if record.get("portrait_sha256") != sha256_file(portrait):
        raise HarnessError(f"Sample {arm.name}/{species.key}/{index} portrait was altered")
    return Sample(arm.name, species.taxon_id, index, portrait)


@dataclass
class GenerationReport:
    completed: int = 0
    generated: int = 0
    reprepared: int = 0
    failed: int = 0
    skipped_for_cap: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, name: str) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + 1)

    def as_dict(self) -> dict[str, int]:
        return {
            "completed": self.completed,
            "generated": self.generated,
            "reprepared": self.reprepared,
            "failed": self.failed,
            "skipped_for_cap": self.skipped_for_cap,
        }


def _prepare(directory: Path) -> None:
    prepare_generated_plate(directory / GENERATED, directory / PORTRAIT, directory / DISPLAY)


def _finish(run: Run, request: GenerationRequest, index: int) -> Sample:
    directory = request.sample_dir
    write_json(
        directory / SAMPLE_RECORD,
        {
            "arm": request.arm.name,
            "label": request.arm.label,
            "instructions_sha256": request.arm.sha256,
            "taxon_id": request.species.taxon_id,
            "index": index,
            "portrait_sha256": sha256_file(directory / PORTRAIT),
        },
    )
    return Sample(request.arm.name, request.species.taxon_id, index, directory / PORTRAIT)


def _discard_plates(directory: Path) -> None:
    for name in (GENERATED, PORTRAIT, DISPLAY):
        (directory / name).unlink(missing_ok=True)


def _one(
    run: Run,
    generator: Generator,
    budget: Budget,
    ledger: Ledger,
    request: GenerationRequest,
    index: int,
    report: GenerationReport,
    finished: bool,
) -> Sample | None:
    """Produce one sample. Each crash point leaves a state the next run resumes from:

    no record -> generate again; a plate and a ledger entry saying the agent
    finished, but no record -> prepare it again without paying; a record -> done.
    A plate whose generation failed, or whose agent may not have finished (a
    started entry and nothing after it), is never used.
    """
    directory = request.sample_dir
    if finished and (directory / GENERATED).is_file():
        try:
            _prepare(directory)
        except GenerationError:
            (directory / GENERATED).unlink(missing_ok=True)
        else:
            report.add("reprepared")
            return _finish(run, request, index)
    try:
        budget.reserve()
    except BudgetExhaustedError:
        report.add("skipped_for_cap")
        return None
    identity = {"arm": request.arm.name, "taxon_id": request.species.taxon_id, "index": index}
    # The directory may hold a failed attempt's log; a fresh attempt starts clean.
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    ledger.record("generation-started", **identity)
    try:
        generator.generate(request)
        _prepare(directory)
    except GenerationError as exc:
        # A timed-out or failed agent can still have written a draft.
        _discard_plates(directory)
        ledger.record("generation-failed", error=str(exc), **identity)
        write_json(directory / "failure.json", {"error": str(exc)})
        report.add("failed")
        return None
    ledger.record("generation-finished", **identity)
    report.add("generated")
    return _finish(run, request, index)


def generate_samples(
    run: Run,
    generator: Generator,
    arms: Sequence[Arm],
    species: Sequence[tuple[PanelSpecies, FrozenReferences]],
    samples: int,
    *,
    budget: Budget,
    parallel: int = 1,
    max_characters: int = DEFAULT_MAX_CANDIDATE_CHARACTERS,
) -> tuple[dict[tuple[str, int, int], Sample], GenerationReport]:
    """Make every missing (arm, species, sample) plate, within the cap."""
    report = GenerationReport()
    done: dict[tuple[str, int, int], Sample] = {}
    pending: list[tuple[GenerationRequest, int]] = []
    for arm in arms:
        register_arm(run, arm, max_characters=max_characters)
        for entry, references in species:
            for index in range(1, samples + 1):
                existing = completed_sample(run, arm, entry, index)
                if existing is not None:
                    done[(arm.name, entry.taxon_id, index)] = existing
                    report.completed += 1
                    continue
                request = GenerationRequest(
                    arm, entry, references, sample_dir(run, arm, entry, index)
                )
                pending.append((request, index))
    ledger = Ledger(run.path(GENERATIONS_LEDGER))
    latest = ledger.latest("arm", "taxon_id", "index")
    results = run_tasks(
        [
            functools.partial(
                _one,
                run,
                generator,
                budget,
                ledger,
                request,
                index,
                report,
                latest.get((request.arm.name, request.species.taxon_id, index))
                == "generation-finished",
            )
            for request, index in pending
        ],
        parallel,
    )
    for (request, index), sample in zip(pending, results, strict=True):
        if sample is not None:
            done[(request.arm.name, request.species.taxon_id, index)] = sample
    return done, report


def missing_samples(
    run: Run, arms: Sequence[Arm], species: Sequence[PanelSpecies], samples: int
) -> int:
    return sum(
        1
        for arm in arms
        for entry in species
        for index in range(1, samples + 1)
        if completed_sample(run, arm, entry, index) is None
    )
