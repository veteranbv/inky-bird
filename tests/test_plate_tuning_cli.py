from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from PIL import Image
from plate_tuning.cli import Tools, main, spread
from plate_tuning.config import EvalConfig
from plate_tuning.generate import Generator
from plate_tuning.judges import Judge
from plate_tuning.panel import load_panel
from plate_tuning.rundir import Run
from plate_tuning.tune import Optimizer, ReflectionLM, ReviewEvaluator
from plate_tuning_fixtures import (
    ColourGenerator,
    FakeDownloader,
    PixelJudge,
    write_catalog,
)

from inky_bird_frame.prompts import PLATE_INSTRUCTIONS


class Offline(Tools):
    """Tools that never start a real command-line agent."""

    def __init__(self) -> None:
        self.colour = ColourGenerator({"v9": (220, 30, 30), "v8": (30, 30, 220)})
        self.fetcher = FakeDownloader()

        def generator(config: EvalConfig) -> Generator:
            return self.colour

        def judges(config: EvalConfig) -> Sequence[Judge]:
            return [PixelJudge(spec.name) for spec in config.judges]

        def unused_reflector(run: Run, config: EvalConfig) -> ReflectionLM:
            raise AssertionError("no reflection expected")

        def unused_optimizer() -> Optimizer:
            raise AssertionError("no optimiser expected")

        def unused_reviewer(config: EvalConfig) -> ReviewEvaluator:
            raise AssertionError("no review expected")

        super().__init__(
            generator=generator,
            judges=judges,
            reflector=unused_reflector,
            optimizer=unused_optimizer,
            review_evaluator=unused_reviewer,
            downloader=self.fetcher,
        )


def _invoke(
    args: list[str], tools: Tools, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, object]]:
    code = main(args, tools=tools)
    return code, json.loads(capsys.readouterr().out)


def _at(value: object, *keys: str) -> object:
    for key in keys:
        assert isinstance(value, dict), value
        value = value[key]
    return value


def _common(tmp_path: Path) -> list[str]:
    catalog, panel = write_catalog(tmp_path)
    config = tmp_path / "eval.toml"
    config.write_text(
        "bootstrap_iterations = 200\n"
        '[[judges]]\nname = "fake-1"\nkind = "claude"\n'
        '[[judges]]\nname = "fake-2"\nkind = "claude"\n'
        '[[judges]]\nname = "fake-3"\nkind = "codex"\n'
    )
    return [
        "--run-dir",
        str(tmp_path / "run"),
        "--catalog",
        str(catalog),
        "--panel",
        str(panel),
        "--config",
        str(config),
    ]


def test_paid_commands_refuse_to_start_without_a_cap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = _common(tmp_path)
    tools = Offline()

    code, out = _invoke(["compare", *common, "v9=default", "v8=default"], tools, capsys)
    assert code == 1
    assert "--max-generations is required" in str(out["error"])
    code, out = _invoke(["tune", *common, "--budget", "5"], tools, capsys)
    assert code == 1 and "--max-generations" in str(out["error"])
    code, out = _invoke(
        ["tune-review", *common, "--budget", "5", "--labels-from", str(tmp_path)], tools, capsys
    )
    assert code == 1 and "--max-reviews" in str(out["error"])
    assert tools.colour.calls == []
    assert main(["compare"], tools=tools) == 2


def test_cache_compare_and_report_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = _common(tmp_path)
    tools = Offline()
    v8 = tmp_path / "v8.txt"
    v8.write_text(PLATE_INSTRUCTIONS.replace("never in flight", "possibly in flight"))
    arms = ["v9=default", f"v8={v8}"]

    code, out = _invoke(["compare", *common, "--max-generations", "40", *arms], tools, capsys)
    assert code == 1 and "cache-references" in str(out["error"])

    code, out = _invoke(["cache-references", *common], tools, capsys)
    assert code == 0 and out["data"] == {"frozen": {"101": 2, "102": 2, "103": 2, "104": 2}}

    code, out = _invoke(
        ["compare", *common, "--max-generations", "40", "--samples", "2", "--dry-run", *arms],
        tools,
        capsys,
    )
    assert out["data"] == {
        "plan": {
            "generations_needed": 16,
            "pairs_possible": 8,
            "judgments_needed_at_most": 24,
        },
        "cap": 40,
    }
    assert tools.colour.calls == []

    code, out = _invoke(
        ["compare", *common, "--max-generations", "40", "--samples", "2", *arms], tools, capsys
    )
    assert code == 0, out
    assert _at(out, "data", "generation", "generated") == 16
    assert _at(out, "data", "decision") == {"accept": True, "reasons": []}
    assert _at(out, "data", "decision_species") == "holdout"
    assert _at(out, "data", "decision_summary", "judged_species") == [103, 104]
    assert _at(out, "data", "summary", "anatomy_win_rate", "point") == 1.0

    code, out = _invoke(["report", *common], tools, capsys)
    assert code == 0
    report = (tmp_path / "run" / "report" / "report.md").read_text()
    assert "## compare: v9-vs-v8" in report
    assert "Decision on held-out species: **replace the baseline**" in report
    sheets = sorted((tmp_path / "run" / "report" / "sheets").rglob("*.png"))
    assert len(sheets) == 4
    with Image.open(sheets[0]) as sheet:
        assert sheet.width > 700


def test_tune_refuses_a_cap_that_cannot_finish_the_holdout_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = _common(tmp_path)
    tools = Offline()

    code, out = _invoke(
        ["tune", *common, "--max-generations", "100", "--budget", "10", "--dry-run"],
        tools,
        capsys,
    )
    assert code == 0
    # 10 metric calls + 2 dev baselines + 2 holdout species x 3 samples x 2 arms.
    assert _at(out, "data", "plan", "generations_at_most") == 24

    code, out = _invoke(
        ["tune", *common, "--max-generations", "20", "--budget", "10"], tools, capsys
    )
    assert code == 1 and "below the 24" in str(out["error"])


def test_spread_takes_one_species_per_body_plan_first(tmp_path: Path) -> None:
    panel = load_panel(*write_catalog(tmp_path))

    assert [entry.stratum for entry in spread(panel, 3)] == [
        "perching-songbird",
        "swimmer",
        "wader",
    ]
    assert len(spread(panel, 10)) == 4
