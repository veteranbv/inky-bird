"""One configuration dataclass, loaded from an optional TOML file over built-in defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Final, Literal

from inky_bird_frame.config import ResearchConfig
from inky_bird_frame.errors import InkyBirdFrameError

JudgeKind = Literal["claude", "codex"]
JUDGE_KINDS: Final = ("claude", "codex")
DEFAULT_CLAUDE_MODEL: Final = "claude-fable-5-2"
DEFAULT_CODEX_MODEL: Final = "gpt-6-astra"
DEFAULT_MAX_CANDIDATE_CHARACTERS: Final = 9000
DEFAULT_MAX_MISSING_JUDGE_SHARE: Final = 0.1
PACKAGE_DIR: Final = Path(__file__).resolve().parent


class HarnessError(InkyBirdFrameError):
    """A harness input, run directory or tool result that cannot be used."""


class BudgetExhaustedError(HarnessError):
    """The generation or review cap for this command has been reached."""


@dataclass(frozen=True)
class JudgeSpec:
    name: str
    kind: JudgeKind
    # None leaves the model choice to the command-line tool; configuration
    # loading always names one (DEFAULT_CLAUDE_MODEL or DEFAULT_CODEX_MODEL).
    model: str | None = None


def _default_judges() -> tuple[JudgeSpec, ...]:
    return (
        JudgeSpec("fable-1", "claude", DEFAULT_CLAUDE_MODEL),
        JudgeSpec("fable-2", "claude", DEFAULT_CLAUDE_MODEL),
        JudgeSpec("codex-1", "codex", DEFAULT_CODEX_MODEL),
    )


@dataclass(frozen=True)
class EvalConfig:
    judges: tuple[JudgeSpec, ...] = field(default_factory=_default_judges)
    claude_executable: str = "claude"
    codex_executable: str = "codex"
    # None uses the Codex CLI's configured model for plate generation and review.
    generation_model: str | None = None
    reflection_model: str = DEFAULT_CLAUDE_MODEL
    generation_timeout_seconds: int = 1200
    judge_timeout_seconds: int = 900
    reflection_timeout_seconds: int = 600
    review_timeout_seconds: int = 1200
    judge_attempts: int = 2
    generation_attempts: int = 2
    samples_per_species: int = 3
    bootstrap_iterations: int = 10000
    confidence: float = 0.95
    rubric_path: str | None = None
    anchor_accuracy_threshold: float = 0.8
    planted_recall_threshold: float = 0.8
    planted_preference_threshold: float = 0.8
    swap_consistency_threshold: float = 0.8
    # A decision is refused when any judge is missing on more of its pairs than this.
    max_missing_judge_share: float = DEFAULT_MAX_MISSING_JUDGE_SHARE
    planted_species: int = 6
    # A decision rests on at least this many judges that passed calibration,
    # with at least one of each configured kind when require_each_provider is set.
    min_calibrated_judges: int = 2
    require_each_provider: bool = True
    max_candidate_characters: int = DEFAULT_MAX_CANDIDATE_CHARACTERS
    tune_minibatch_size: int = 3
    anatomy_weight: float = 0.5
    lettering_weight: float = 0.3
    acceptable_weight: float = 0.2
    allowed_domains: tuple[str, ...] = ResearchConfig().allowed_domains

    def rubric(self) -> str:
        path = Path(self.rubric_path) if self.rubric_path else PACKAGE_DIR / "rubric.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HarnessError(f"The rubric file could not be read: {path.name}") from exc

    def validate(self) -> EvalConfig:
        names = [judge.name for judge in self.judges]
        if not names:
            raise HarnessError("At least one judge is required")
        if len(set(names)) != len(names):
            raise HarnessError("Judge names must be unique")
        for judge in self.judges:
            if judge.kind not in JUDGE_KINDS:
                raise HarnessError(f"Unknown judge kind: {judge.kind}")
            if not judge.name or not all(c.isalnum() or c == "-" for c in judge.name):
                raise HarnessError("Judge names may contain only letters, digits and hyphens")
        for name in (
            "generation_timeout_seconds",
            "judge_timeout_seconds",
            "reflection_timeout_seconds",
            "review_timeout_seconds",
            "judge_attempts",
            "generation_attempts",
            "samples_per_species",
            "bootstrap_iterations",
            "planted_species",
            "min_calibrated_judges",
            "max_candidate_characters",
            "tune_minibatch_size",
        ):
            if getattr(self, name) < 1:
                raise HarnessError(f"{name} must be at least 1")
        if not 0 < self.confidence < 1:
            raise HarnessError("confidence must be between 0 and 1")
        for name in (
            "anchor_accuracy_threshold",
            "planted_recall_threshold",
            "planted_preference_threshold",
            "swap_consistency_threshold",
        ):
            if not 0 < getattr(self, name) <= 1:
                raise HarnessError(f"{name} must be above 0 and at most 1")
        if not 0 <= self.max_missing_judge_share < 1:
            raise HarnessError("max_missing_judge_share must be at least 0 and below 1")
        weights = (self.anatomy_weight, self.lettering_weight, self.acceptable_weight)
        if min(weights) < 0 or abs(sum(weights) - 1) > 1e-9:
            raise HarnessError("The tuning score weights must be non-negative and sum to 1")
        if len(set(self.allowed_domains)) < 2:
            raise HarnessError("allowed_domains must contain at least two domains")
        return self


def _judge(raw: object) -> JudgeSpec:
    if not isinstance(raw, dict) or set(raw) - {"name", "kind", "model"}:
        raise HarnessError("Each [[judges]] entry takes only name, kind and model")
    name, kind, model = raw.get("name"), raw.get("kind"), raw.get("model")
    if not isinstance(name, str) or kind not in JUDGE_KINDS:
        raise HarnessError("Each judge needs a name and a kind of claude or codex")
    if model is not None and not isinstance(model, str):
        raise HarnessError("A judge model must be a string")
    if kind == "claude":
        return JudgeSpec(name, "claude", model or DEFAULT_CLAUDE_MODEL)
    return JudgeSpec(name, "codex", model or DEFAULT_CODEX_MODEL)


def _value(raw: dict[str, object], key: str, kind: type[object], default: object) -> object:
    if key not in raw:
        return default
    value = raw[key]
    if kind is float and type(value) is int:
        return float(value)
    if type(value) is not kind:
        raise HarnessError(f"{key} must be a {kind.__name__}")
    return value


def _int(raw: dict[str, object], key: str, default: int) -> int:
    value = _value(raw, key, int, default)
    assert isinstance(value, int)
    return value


def _float(raw: dict[str, object], key: str, default: float) -> float:
    value = _value(raw, key, float, default)
    assert isinstance(value, float)
    return value


def _bool(raw: dict[str, object], key: str, default: bool) -> bool:
    value = _value(raw, key, bool, default)
    assert isinstance(value, bool)
    return value


def _str(raw: dict[str, object], key: str, default: str) -> str:
    value = _value(raw, key, str, default)
    assert isinstance(value, str)
    return value


def _optional_str(raw: dict[str, object], key: str, default: str | None) -> str | None:
    value = raw.get(key, default)
    if value is not None and not isinstance(value, str):
        raise HarnessError(f"{key} must be a string")
    return value


def load_config(path: Path | None) -> EvalConfig:
    defaults = EvalConfig()
    if path is None:
        return defaults.validate()
    try:
        raw: dict[str, object] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HarnessError(f"The configuration file could not be read: {path.name}") from exc
    unknown = set(raw) - {item.name for item in fields(EvalConfig)}
    if unknown:
        raise HarnessError(f"Unknown configuration key: {sorted(unknown)[0]}")
    judges = defaults.judges
    if "judges" in raw:
        listed = raw["judges"]
        if not isinstance(listed, list):
            raise HarnessError("judges must be an array of tables")
        judges = tuple(_judge(item) for item in listed)
    domains = defaults.allowed_domains
    if "allowed_domains" in raw:
        listed = raw["allowed_domains"]
        if not isinstance(listed, list) or not all(isinstance(item, str) for item in listed):
            raise HarnessError("allowed_domains must be a list of strings")
        domains = tuple(str(item) for item in listed)
    d = defaults
    return EvalConfig(
        judges=judges,
        claude_executable=_str(raw, "claude_executable", d.claude_executable),
        codex_executable=_str(raw, "codex_executable", d.codex_executable),
        generation_model=_optional_str(raw, "generation_model", d.generation_model),
        reflection_model=_str(raw, "reflection_model", d.reflection_model),
        generation_timeout_seconds=_int(
            raw, "generation_timeout_seconds", d.generation_timeout_seconds
        ),
        judge_timeout_seconds=_int(raw, "judge_timeout_seconds", d.judge_timeout_seconds),
        reflection_timeout_seconds=_int(
            raw, "reflection_timeout_seconds", d.reflection_timeout_seconds
        ),
        review_timeout_seconds=_int(raw, "review_timeout_seconds", d.review_timeout_seconds),
        judge_attempts=_int(raw, "judge_attempts", d.judge_attempts),
        generation_attempts=_int(raw, "generation_attempts", d.generation_attempts),
        samples_per_species=_int(raw, "samples_per_species", d.samples_per_species),
        bootstrap_iterations=_int(raw, "bootstrap_iterations", d.bootstrap_iterations),
        confidence=_float(raw, "confidence", d.confidence),
        rubric_path=_optional_str(raw, "rubric_path", d.rubric_path),
        anchor_accuracy_threshold=_float(
            raw, "anchor_accuracy_threshold", d.anchor_accuracy_threshold
        ),
        planted_recall_threshold=_float(
            raw, "planted_recall_threshold", d.planted_recall_threshold
        ),
        planted_preference_threshold=_float(
            raw, "planted_preference_threshold", d.planted_preference_threshold
        ),
        swap_consistency_threshold=_float(
            raw, "swap_consistency_threshold", d.swap_consistency_threshold
        ),
        max_missing_judge_share=_float(raw, "max_missing_judge_share", d.max_missing_judge_share),
        planted_species=_int(raw, "planted_species", d.planted_species),
        min_calibrated_judges=_int(raw, "min_calibrated_judges", d.min_calibrated_judges),
        require_each_provider=_bool(raw, "require_each_provider", d.require_each_provider),
        max_candidate_characters=_int(raw, "max_candidate_characters", d.max_candidate_characters),
        tune_minibatch_size=_int(raw, "tune_minibatch_size", d.tune_minibatch_size),
        anatomy_weight=_float(raw, "anatomy_weight", d.anatomy_weight),
        lettering_weight=_float(raw, "lettering_weight", d.lettering_weight),
        acceptable_weight=_float(raw, "acceptable_weight", d.acceptable_weight),
        allowed_domains=domains,
    ).validate()
