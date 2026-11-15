"""Blind judges: the verdict schema, its parser and the two command-line judges."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

from inky_bird_frame.codex_runner import CodexRunner
from inky_bird_frame.errors import GenerationError
from inky_bird_frame.prompts import data_block

from .config import EvalConfig, HarnessError, JudgeSpec

Side = Literal["A", "B"]
Choice = Literal["A", "B", "tie"]
Severity = Literal["fatal", "major", "minor"]
CATEGORIES: Final = (
    "legs",
    "wings",
    "head_bill",
    "tail",
    "proportions",
    "plumage",
    "lettering",
    "ruler",
)
ANATOMY_CATEGORIES: Final = frozenset({"legs", "wings", "head_bill", "tail", "proportions"})
SEVERITIES: Final = ("fatal", "major", "minor")
CHOICES: Final = ("A", "B", "tie")
MAX_DEFECTS: Final = 40
MAX_TEXT: Final = 2000
PLATE_FILES: Final = {"A": "plate-A.png", "B": "plate-B.png"}
RUBRIC_FILE: Final = "RUBRIC.md"
REFERENCES_DIR: Final = "references"

_DEFECT_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "location": {"type": "string"},
        "reference": {"type": "string"},
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "description": {"type": "string"},
    },
    "required": ["category", "location", "reference", "severity", "description"],
    "additionalProperties": False,
}
_PLATE_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "defects": {"type": "array", "items": _DEFECT_SCHEMA},
        "acceptable": {"type": "boolean"},
    },
    "required": ["defects", "acceptable"],
    "additionalProperties": False,
}
VERDICT_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "plate_a": _PLATE_SCHEMA,
        "plate_b": _PLATE_SCHEMA,
        "more_correct_anatomy": {"type": "string", "enum": list(CHOICES)},
        "more_correct_lettering": {"type": "string", "enum": list(CHOICES)},
    },
    "required": ["plate_a", "plate_b", "more_correct_anatomy", "more_correct_lettering"],
    "additionalProperties": False,
}


class JudgeError(HarnessError):
    """A judge did not return a usable verdict."""


@dataclass(frozen=True)
class Defect:
    category: str
    location: str
    reference: str
    severity: str
    description: str

    def as_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "location": self.location,
            "reference": self.reference,
            "severity": self.severity,
            "description": self.description,
        }


@dataclass(frozen=True)
class PlateAssessment:
    defects: tuple[Defect, ...]
    acceptable: bool

    @property
    def fatal(self) -> bool:
        return any(defect.severity == "fatal" for defect in self.defects)

    @property
    def categories(self) -> frozenset[str]:
        return frozenset(defect.category for defect in self.defects)

    def as_dict(self) -> dict[str, object]:
        return {
            "defects": [defect.as_dict() for defect in self.defects],
            "acceptable": self.acceptable,
        }


@dataclass(frozen=True)
class BlindVerdict:
    """A verdict in the judge's own A/B terms, before unblinding."""

    plate_a: PlateAssessment
    plate_b: PlateAssessment
    anatomy: Choice
    lettering: Choice

    def plate(self, side: Side) -> PlateAssessment:
        return self.plate_a if side == "A" else self.plate_b

    def as_dict(self) -> dict[str, object]:
        return {
            "plate_a": self.plate_a.as_dict(),
            "plate_b": self.plate_b.as_dict(),
            "more_correct_anatomy": self.anatomy,
            "more_correct_lettering": self.lettering,
        }


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise JudgeError(f"Verdict field {field} must be a string")
    return " ".join(value.split())[:MAX_TEXT]


def _choice(value: object, field: str) -> Choice:
    if value == "A":
        return "A"
    if value == "B":
        return "B"
    if value == "tie":
        return "tie"
    raise JudgeError(f"Verdict field {field} must be A, B or tie")


def _plate(value: object, field: str) -> PlateAssessment:
    if not isinstance(value, dict):
        raise JudgeError(f"Verdict field {field} must be an object")
    raw_defects, acceptable = value.get("defects"), value.get("acceptable")
    if not isinstance(raw_defects, list) or len(raw_defects) > MAX_DEFECTS:
        raise JudgeError(f"Verdict field {field}.defects must be a list of at most {MAX_DEFECTS}")
    if not isinstance(acceptable, bool):
        raise JudgeError(f"Verdict field {field}.acceptable must be true or false")
    defects: list[Defect] = []
    for raw in raw_defects:
        if not isinstance(raw, dict):
            raise JudgeError(f"Each {field} defect must be an object")
        category, severity = raw.get("category"), raw.get("severity")
        if category not in CATEGORIES:
            raise JudgeError(f"Unknown defect category in {field}")
        if severity not in SEVERITIES:
            raise JudgeError(f"Unknown defect severity in {field}")
        defects.append(
            Defect(
                category=str(category),
                location=_text(raw.get("location"), "location"),
                reference=_text(raw.get("reference"), "reference"),
                severity=str(severity),
                description=_text(raw.get("description"), "description"),
            )
        )
    assessment = PlateAssessment(tuple(defects), acceptable)
    if assessment.fatal and acceptable:
        raise JudgeError(f"Verdict marks {field} acceptable despite a fatal defect")
    return assessment


def parse_verdict(raw: object) -> BlindVerdict:
    if not isinstance(raw, dict):
        raise JudgeError("A verdict must be a JSON object")
    return BlindVerdict(
        plate_a=_plate(raw.get("plate_a"), "plate_a"),
        plate_b=_plate(raw.get("plate_b"), "plate_b"),
        anatomy=_choice(raw.get("more_correct_anatomy"), "more_correct_anatomy"),
        lettering=_choice(raw.get("more_correct_lettering"), "more_correct_lettering"),
    )


@dataclass(frozen=True)
class JudgeRequest:
    """One blind pair. The folder holds only the two plates, references and rubric."""

    blind_dir: Path
    reference_files: tuple[str, ...]
    expected_text: dict[str, object]
    work_dir: Path

    def prompt(self) -> str:
        references = "\n".join(f"- {REFERENCES_DIR}/{name}" for name in self.reference_files)
        return f"""Judge the two field-journal plates in the current folder blind.

Read {RUBRIC_FILE} in this folder and follow it exactly. Then look at every reference photo:
{references}
and at {PLATE_FILES["A"]} and {PLATE_FILES["B"]} at full size. Use only files in this folder.

The text that should be lettered on each plate is the "expected_text" value below. Check every
printed label and value against it character for character.

{data_block({"expected_text": self.expected_text})}

Return only the JSON object the output schema asks for: the defects and acceptability of plate A
under "plate_a" and of plate B under "plate_b", and your two A, B or tie choices.
"""


class Judge(Protocol):
    name: str

    def judge(self, request: JudgeRequest) -> BlindVerdict:
        """Return a verdict, or raise JudgeError."""


def run_bounded(
    executable: Path,
    cwd: Path,
    timeout_seconds: int,
    command: list[str],
    prompt: str,
    log_path: Path,
) -> str:
    """Run a command-line agent with the application's bounded, group-killing supervisor.

    The child gets the application's environment allowlist: no API keys, the
    tools authenticate with their own logins.
    """
    runner = CodexRunner(executable, cwd, timeout_seconds)
    try:
        return runner._run(command, prompt, log_path).stdout
    except GenerationError as exc:
        # The supervisor names Codex in its messages whichever tool it ran.
        raise JudgeError(f"{executable.name} failed: {exc}") from exc


def find_executable(name: str) -> Path:
    found = shutil.which(name)
    if found is None:
        raise HarnessError(f"The {name} command-line tool was not found on PATH")
    return Path(found)


def claude_command(executable: Path, model: str | None, schema: dict[str, object]) -> list[str]:
    command = [str(executable), "-p", "--output-format", "json"]
    if model is not None:
        command.extend(["--model", model])
    command.extend(
        [
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
            # Only file reading, and no configured MCP servers or network tools.
            "--tools",
            "Read",
            "--allowedTools",
            "Read",
            "--disallowedTools",
            "Bash,Edit,Write,WebFetch,WebSearch",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
    )
    return command


def parse_claude_output(stdout: str) -> object:
    """Read the structured result from `claude -p --output-format json`."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise JudgeError("Claude did not print a JSON result") from exc
    if not isinstance(envelope, dict):
        raise JudgeError("Claude printed an unexpected result")
    if envelope.get("is_error") is True:
        raise JudgeError("Claude reported an error result")
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result = envelope.get("result")
    if not isinstance(result, str):
        raise JudgeError("Claude printed no result text")
    text = result.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise JudgeError("Claude's result was not JSON") from exc


class ClaudeCliJudge:
    def __init__(self, name: str, model: str | None, executable: Path, timeout: int) -> None:
        self.name = name
        self.model = model
        self.executable = executable
        self.timeout = timeout

    def judge(self, request: JudgeRequest) -> BlindVerdict:
        request.work_dir.mkdir(parents=True, exist_ok=True)
        stdout = run_bounded(
            self.executable,
            request.blind_dir,
            self.timeout,
            claude_command(self.executable, self.model, VERDICT_SCHEMA),
            request.prompt(),
            request.work_dir / "judge.log",
        )
        return parse_verdict(parse_claude_output(stdout))


def codex_command(
    executable: Path,
    model: str | None,
    images: list[Path],
    schema_path: Path,
    output_path: Path,
) -> list[str]:
    command = [
        str(executable),
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
    ]
    if model is not None:
        command.extend(["--model", model])
    for image in images:
        command.extend(["--image", str(image)])
    command.extend(["--output-schema", str(schema_path), "-o", str(output_path), "-"])
    return command


class CodexCliJudge:
    def __init__(self, name: str, model: str | None, executable: Path, timeout: int) -> None:
        self.name = name
        self.model = model
        self.executable = executable
        self.timeout = timeout

    def judge(self, request: JudgeRequest) -> BlindVerdict:
        work = request.work_dir
        work.mkdir(parents=True, exist_ok=True)
        schema_path = work / "schema.json"
        output_path = work / "verdict.json"
        schema_path.write_text(json.dumps(VERDICT_SCHEMA, indent=2), encoding="utf-8")
        output_path.unlink(missing_ok=True)
        images = [request.blind_dir / PLATE_FILES["A"], request.blind_dir / PLATE_FILES["B"]]
        images.extend(request.blind_dir / REFERENCES_DIR / name for name in request.reference_files)
        run_bounded(
            self.executable,
            request.blind_dir,
            self.timeout,
            codex_command(self.executable, self.model, images, schema_path, output_path),
            request.prompt(),
            work / "judge.log",
        )
        try:
            raw = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JudgeError("Codex did not write a JSON verdict") from exc
        return parse_verdict(raw)


def build_judges(config: EvalConfig) -> list[Judge]:
    judges: list[Judge] = []
    executables: dict[str, Path] = {}
    for spec in config.judges:
        judges.append(_build(spec, config, executables))
    return judges


def _build(spec: JudgeSpec, config: EvalConfig, executables: dict[str, Path]) -> Judge:
    tool = config.claude_executable if spec.kind == "claude" else config.codex_executable
    if tool not in executables:
        executables[tool] = find_executable(tool)
    if spec.kind == "claude":
        return ClaudeCliJudge(
            spec.name, spec.model, executables[tool], config.judge_timeout_seconds
        )
    return CodexCliJudge(spec.name, spec.model, executables[tool], config.judge_timeout_seconds)
