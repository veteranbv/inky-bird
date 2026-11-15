"""Prompt arms and resumable, capped plate generation."""

from __future__ import annotations

import functools
import importlib.util
import inspect
import random
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.codex_runner import REVIEW_SCHEMA, CodexRunner, _parse_review
from inky_bird_frame.errors import GenerationError
from inky_bird_frame.images import prepare_generated_plate
from inky_bird_frame.models import QualityReview, ReferencePhoto, SpeciesProfileData
from inky_bird_frame.prompts import (
    PLATE_INSTRUCTIONS,
    PROMPT_VERSION,
    plate_prompt,
    review_prompt,
)

from .config import (
    DEFAULT_MAX_CANDIDATE_CHARACTERS,
    PACKAGE_DIR,
    BudgetExhaustedError,
    HarnessError,
)
from .judges import Usage, codex_usage
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
RELEASE_PREFIX: Final = "ref:"
_ARM_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
_GIT_REF: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,199}")
PROMPTS_SOURCE: Final = "src/inky_bird_frame/prompts.py"
REPOSITORY: Final = PACKAGE_DIR.parents[1]
GENERATED: Final = "generated.png"
PORTRAIT: Final = "portrait.png"
DISPLAY: Final = "display.png"
SAMPLE_RECORD: Final = "sample.json"
GENERATIONS_LEDGER: Final = "generations.jsonl"


@dataclass(frozen=True)
class ReleasePrompt:
    """The prompt module exactly as an earlier commit shipped it."""

    ref: str
    commit: str
    source: str

    @functools.cached_property
    def module(self) -> ModuleType:
        """Load the source as an isolated module of the application package.

        Its relative imports resolve to the installed package's birds and
        models, which the rendered-prompt hash test shows are still compatible
        for the pinned v8 release.
        """
        name = f"inky_bird_frame._release_prompts_{sha256_text(self.source)[:16]}"
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_loader(name, loader=None)
        if spec is None:
            raise HarnessError(f"The prompt module of {self.ref} could not be loaded")
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "inky_bird_frame"
        sys.modules[name] = module
        try:
            exec(
                compile(self.source, f"<{self.commit[:12]}:{PROMPTS_SOURCE}>", "exec"), vars(module)
            )
        except Exception as exc:
            del sys.modules[name]
            raise HarnessError(f"The prompt module of {self.ref} could not be loaded") from exc
        if not callable(getattr(module, "plate_prompt", None)):
            del sys.modules[name]
            raise HarnessError(f"The prompt module of {self.ref} has no plate_prompt")
        return module

    @property
    def version(self) -> str:
        return str(getattr(self.module, "PROMPT_VERSION", "unknown"))

    def plate_prompt(
        self,
        species: BirdSpecies,
        profile: SpeciesProfileData,
        references: list[ReferencePhoto],
        output: Path,
        workspace: Path,
    ) -> str:
        """A first attempt's prompt, called the way that release's runner called it."""
        render: Callable[..., object] = self.module.plate_prompt
        options: dict[str, object] = {}
        if "workspace" in inspect.signature(render).parameters:
            options["workspace"] = workspace
        prompt = render(species, profile, references, output, **options)
        if not isinstance(prompt, str):
            raise HarnessError(f"The plate_prompt of {self.ref} did not return text")
        return prompt


def _git(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(REPOSITORY), *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError("git could not be run to read an earlier release") from exc
    if completed.returncode != 0:
        raise HarnessError(f"git could not read {args[-1]!r} from this repository")
    return completed.stdout


def load_release(ref: str) -> ReleasePrompt:
    """Read an earlier commit's prompt module with git, read-only, from this repository."""
    if not _GIT_REF.fullmatch(ref):
        raise HarnessError(f"{ref!r} is not a usable git reference")
    commit = _git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip()
    return ReleasePrompt(ref, commit, _git("show", f"{commit}:{PROMPTS_SOURCE}"))


@dataclass(frozen=True)
class Arm:
    """A named plate prompt: instruction wording in the current template, or an earlier release.

    "default" is the shipped wording; a release arm renders the plate prompt
    with that release's own module, byte for byte what it would have sent.
    """

    name: str
    instructions: str
    label: str
    release: ReleasePrompt | None = None

    @property
    def sha256(self) -> str:
        return sha256_text(self.release.source if self.release else self.instructions)

    @property
    def is_default(self) -> bool:
        return self.release is None and self.instructions == PLATE_INSTRUCTIONS

    def render(
        self,
        species: BirdSpecies,
        profile: SpeciesProfileData,
        references: list[ReferencePhoto],
        output: Path,
        workspace: Path,
    ) -> str:
        if self.release is not None:
            return self.release.plate_prompt(species, profile, references, output, workspace)
        return plate_prompt(
            species,
            profile,
            references,
            output,
            workspace=workspace,
            instructions=self.instructions,
        )


def check_arm_name(name: str) -> str:
    if not _ARM_NAME.fullmatch(name) or name in RESERVED_NAMES:
        raise HarnessError(
            f"Arm name {name!r} must be lowercase letters, digits and hyphens and not a"
            " harness directory name"
        )
    return name


def parse_arm(spec: str, *, base: Path) -> Arm:
    """Parse NAME=default, NAME=ref:GITREF (an earlier release) or NAME=PATH to wording."""
    name, separator, source = spec.partition("=")
    if not separator or not source:
        raise HarnessError("An arm is NAME=default, NAME=ref:GITREF or NAME=PATH")
    check_arm_name(name)
    if source == DEFAULT_ARM:
        return Arm(name, PLATE_INSTRUCTIONS, PROMPT_VERSION)
    if source.startswith(RELEASE_PREFIX):
        release = load_release(source.removeprefix(RELEASE_PREFIX))
        return Arm(name, "", f"{release.version}@{release.commit[:12]}", release)
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
    """Gate and record the arm's exact prompt; a name keeps it for the life of the run.

    Every wording arm, typed on the command line or proposed by the tuner,
    passes the same safety gate before Codex ever sees it. A release arm is
    what that release sent, so it is recorded (its module beside the record)
    rather than gated; it names the plate by absolute path, as that release
    did, so its run directory must lie outside the home folder.
    """
    if arm.release is None:
        try:
            validate_candidate(arm.instructions, target="plate", max_characters=max_characters)
        except CandidateRejectedError as exc:
            raise HarnessError(f"Arm {arm.name} instructions are not usable: {exc}") from exc
    elif run.root.is_relative_to(Path.home().resolve()):
        raise HarnessError(
            f"Arm {arm.name} sends the plate's absolute path, as that release did; use a run"
            " directory outside your home folder"
        )
    path = run.path("arms", f"{arm.name}.json")
    if path.exists():
        recorded = read_object(path)
        if recorded.get("sha256") != arm.sha256:
            raise HarnessError(f"Arm {arm.name} already exists in this run with other wording")
        return arm
    record: dict[str, object] = {
        "name": arm.name,
        "label": arm.label,
        "sha256": arm.sha256,
        "is_default": arm.is_default,
    }
    if arm.release is None:
        record["instructions"] = arm.instructions
    else:
        source = run.path("arms", f"{arm.name}.prompts.py")
        write_text(source, arm.release.source)
        record.update(
            kind="release",
            ref=arm.release.ref,
            commit=arm.release.commit,
            prompt_version=arm.release.version,
            source=source.name,
        )
    write_json(path, record)
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
    def generate(self, request: GenerationRequest) -> Usage | None:
        """Write request.output_path, or raise GenerationError; return the tokens it reported."""


class InstructedCodexRunner(CodexRunner):
    """The application's Codex runner with an instruction override for evaluation.

    These mirror generate_plate and review_plate for a first attempt; the
    application's methods take no instruction argument.
    """

    def generate_with_instructions(
        self, request: GenerationRequest, prompt_path: Path, log_path: Path
    ) -> Usage | None:
        output = request.output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        command = self._base_command(writable=True)
        for image in request.references.paths:
            command.extend(["--image", str(image.resolve())])
        command.append("-")
        prompt = request.arm.render(
            request.species.bird(),
            request.species.profile,
            list(request.references.photos),
            output.resolve(),
            self.workspace,
        )
        write_text(prompt_path, prompt)
        finished = self._run(command, prompt, log_path)
        if not output.is_file() or output.stat().st_size == 0:
            raise GenerationError(f"Codex did not create the requested plate: {output.name}")
        return codex_usage(f"{finished.stdout}\n{finished.stderr}")

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

    def generate(self, request: GenerationRequest) -> Usage | None:
        runner = InstructedCodexRunner(
            self.executable,
            request.sample_dir,
            self.timeout_seconds,
            model=self.model,
        )
        return runner.generate_with_instructions(
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


def _finish(run: Run, request: GenerationRequest, index: int, usage: Usage | None) -> Sample:
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
            "usage": None if usage is None else dict(usage),
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
    reserved: bool,
) -> Sample | None:
    """Produce one sample. Each crash point leaves a state the next run resumes from:

    no record -> generate again; a plate and a ledger entry saying the agent
    finished, but no record -> prepare it again without paying; a record -> done.
    A plate whose generation failed, or whose agent may not have finished (a
    started entry and nothing after it), is never used. reserved says the
    call was already taken from the cap with the rest of its group.
    """
    directory = request.sample_dir
    if finished and (directory / GENERATED).is_file():
        try:
            _prepare(directory)
        except GenerationError:
            (directory / GENERATED).unlink(missing_ok=True)
        else:
            report.add("reprepared")
            return _finish(run, request, index, None)
    if not reserved:
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
        usage = generator.generate(request)
        _prepare(directory)
    except GenerationError as exc:
        # A timed-out or failed agent can still have written a draft.
        _discard_plates(directory)
        ledger.record("generation-failed", error=str(exc), **identity)
        write_json(directory / "failure.json", {"error": str(exc)})
        report.add("failed")
        return None
    recorded = None if usage is None else dict(usage)
    ledger.record("generation-finished", usage=recorded, **identity)
    report.add("generated")
    return _finish(run, request, index, usage)


def generation_order(
    run: Run, arms: Sequence[Arm], keys: Sequence[tuple[int, int]]
) -> list[tuple[tuple[int, int], list[Arm]]]:
    """(species, sample) groups in a seeded random order, each with its arms shuffled.

    Plates of all arms for one species and sample run next to each other and
    groups are not taken arm by arm, so drift over a long run, or the point
    where a cap stops it, does not line up with an arm.
    """
    names = ",".join(sorted(arm.name for arm in arms))
    rng = random.Random(run.derived(f"generation-order|{names}"))
    order = sorted(keys)
    rng.shuffle(order)
    grouped: list[tuple[tuple[int, int], list[Arm]]] = []
    for key in order:
        shuffled = list(arms)
        rng.shuffle(shuffled)
        grouped.append((key, shuffled))
    return grouped


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
    """Make every missing (arm, species, sample) plate, within the cap.

    The cap is taken per (species, sample) group, for every arm's plate at
    once, so a cap that cannot pay for a whole group leaves it untouched
    instead of making a plate with nothing to be compared against.
    """
    report = GenerationReport()
    done: dict[tuple[str, int, int], Sample] = {}
    for arm in arms:
        register_arm(run, arm, max_characters=max_characters)
    by_taxon = {entry.taxon_id: (entry, references) for entry, references in species}
    ledger = Ledger(run.path(GENERATIONS_LEDGER))
    latest = ledger.latest("arm", "taxon_id", "index")
    keys = [(entry.taxon_id, index) for entry, _ in species for index in range(1, samples + 1)]
    tasks: list[tuple[GenerationRequest, int, Callable[[], Sample | None]]] = []
    for (taxon, index), ordered in generation_order(run, arms, keys):
        entry, references = by_taxon[taxon]
        group: list[tuple[GenerationRequest, bool]] = []
        for arm in ordered:
            existing = completed_sample(run, arm, entry, index)
            if existing is not None:
                done[(arm.name, taxon, index)] = existing
                report.completed += 1
                continue
            request = GenerationRequest(arm, entry, references, sample_dir(run, arm, entry, index))
            finished = latest.get((arm.name, taxon, index)) == "generation-finished"
            group.append((request, finished and (request.sample_dir / GENERATED).is_file()))
        paid = sum(1 for _request, free in group if not free)
        try:
            budget.reserve(paid)
            affordable = True
        except BudgetExhaustedError:
            affordable = False
        for request, free in group:
            if not free and not affordable:
                report.skipped_for_cap += 1
                continue
            task = functools.partial(
                _one, run, generator, budget, ledger, request, index, report, free, not free
            )
            tasks.append((request, index, task))
    results = run_tasks([task for _request, _index, task in tasks], parallel)
    for (request, index, _task), sample in zip(tasks, results, strict=True):
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
