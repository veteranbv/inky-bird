from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest
from PIL import Image
from plate_tuning.cli import Tools, main, spread
from plate_tuning.config import EvalConfig, load_config
from plate_tuning.generate import Generator
from plate_tuning.judges import Judge
from plate_tuning.panel import load_panel
from plate_tuning.report import usage_totals
from plate_tuning.rundir import Ledger, Run
from plate_tuning.tune import Optimizer, ReflectionLM
from plate_tuning_fixtures import (
    ColourGenerator,
    FakeDownloader,
    PixelJudge,
    calibration_record,
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

        super().__init__(
            generator=generator,
            judges=judges,
            reflector=unused_reflector,
            optimizer=unused_optimizer,
            downloader=self.fetcher,
        )


def _invoke(
    args: list[str], tools: Tools, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, object]]:
    code = main(args, tools=tools)
    return code, json.loads(capsys.readouterr().out)


def _at(value: object, *keys: str) -> object:
    """A nested value; a digit key indexes a list."""
    for key in keys:
        if isinstance(value, list):
            value = value[int(key)]
            continue
        assert isinstance(value, dict), value
        value = value[key]
    return value


def _calibrated(tmp_path: Path, failed: tuple[str, ...] = ()) -> None:
    """Write the calibration record calibrate would leave when the named judges failed."""
    record = calibration_record(load_config(tmp_path / "eval.toml"), failed)
    path = tmp_path / "run" / "calibration" / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


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

    _calibrated(tmp_path)
    code, out = _invoke(
        ["compare", *common, "--max-generations", "40", "--samples", "2", "--dry-run", *arms],
        tools,
        capsys,
    )
    assert _at(out, "data", "plan") == {
        "generations_needed": 16,
        "pairs_possible": 8,
        "judgments_needed_at_most": 24,
    }
    assert {key: _at(out, "data", key) for key in ("cap", "used", "remaining")} == {
        "cap": 40,
        "used": 0,
        "remaining": 40,
    }
    assert _at(out, "data", "calibration", "passed") is True
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
    assert "First arm `v9` (" in report and "the shipped plate instructions" in report
    assert "Second arm `v8` (" in report and "only the wording differs" in report
    assert "Judges fake-1, fake-2 use the same model and prompt" in report
    assert "| Verdicts from fake-3 | 8 (missing 0) |" in report
    assert "| generations | 16 | 0 | - |" in report
    assert "Share of side A, fake-1" in report
    sheets = sorted((tmp_path / "run" / "report" / "sheets").rglob("*.png"))
    assert len(sheets) == 4
    with Image.open(sheets[0]) as sheet:
        assert sheet.width > 700


def test_tune_refuses_a_cap_that_cannot_finish_the_holdout_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = _common(tmp_path)
    tools = Offline()
    _calibrated(tmp_path)

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


def test_the_generation_cap_covers_the_whole_run_across_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = _common(tmp_path)
    tools = Offline()
    assert _invoke(["cache-references", *common], tools, capsys)[0] == 0
    v8 = tmp_path / "v8.txt"
    v8.write_text(PLATE_INSTRUCTIONS.replace("never in flight", "possibly in flight"))
    compare = ["compare", *common, "--samples", "2", "v9=default", f"v8={v8}"]

    # Plates are paid for a (species, sample) pair at a time: a cap of 3 buys one pair.
    code, out = _invoke([*compare, "--max-generations", "3"], tools, capsys)
    assert code == 0 and _at(out, "data", "generation", "generated") == 2
    code, out = _invoke([*compare, "--max-generations", "3"], tools, capsys)
    assert code == 0 and _at(out, "data", "generation", "generated") == 0
    assert len(tools.colour.calls) == 2

    code, out = _invoke([*compare, "--max-generations", "10", "--dry-run"], tools, capsys)
    assert {key: _at(out, "data", key) for key in ("cap", "used", "remaining")} == {
        "cap": 10,
        "used": 2,
        "remaining": 8,
    }
    code, out = _invoke([*compare, "--max-generations", "10"], tools, capsys)
    assert _at(out, "data", "generation", "generated") == 8
    assert len(tools.colour.calls) == 10


def test_a_second_command_on_a_busy_run_directory_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = _common(tmp_path)
    tools = Offline()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    holder = os.open(run_dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, out = _invoke(
            ["compare", *common, "--max-generations", "40", "v9=default", "v8=default"],
            tools,
            capsys,
        )
    finally:
        os.close(holder)

    assert code == 1 and "Another command is using this run directory" in str(out["error"])
    assert tools.colour.calls == []
    assert not (run_dir / "run.json").exists()


def test_without_calibration_compare_decides_nothing_and_tuning_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = _common(tmp_path)
    tools = Offline()
    assert _invoke(["cache-references", *common], tools, capsys)[0] == 0
    v8 = tmp_path / "v8.txt"
    v8.write_text(PLATE_INSTRUCTIONS.replace("never in flight", "possibly in flight"))
    compare = ["compare", *common, "--max-generations", "40", "--samples", "1", "v9=default"]

    code, out = _invoke([*compare, f"v8={v8}"], tools, capsys)
    assert code == 0
    assert _at(out, "data", "summary", "anatomy_win_rate", "point") == 1.0
    assert _at(out, "data", "decision") is None
    assert "no calibration record" in str(_at(out, "data", "decision_note"))
    assert _at(out, "data", "exploratory") is False

    code, out = _invoke([*compare, f"v8={v8}", "--allow-uncalibrated"], tools, capsys)
    assert code == 0 and _at(out, "data", "exploratory") is True
    assert _at(out, "data", "decision") is None
    assert _at(out, "data", "exploratory_decision", "accept") is True

    code, out = _invoke(
        ["tune", *common, "--max-generations", "100", "--budget", "4"], tools, capsys
    )
    assert code == 1 and "--allow-uncalibrated" in str(out["error"])
    assert len(tools.colour.calls) == 8


def test_a_calibration_from_another_run_directory_can_be_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = _common(tmp_path)
    tools = Offline()
    assert _invoke(["cache-references", *common], tools, capsys)[0] == 0
    record = tmp_path / "elsewhere.json"
    record.write_text(json.dumps(calibration_record(load_config(tmp_path / "eval.toml"))))
    compare = [
        "compare",
        *common,
        "--max-generations",
        "40",
        "--samples",
        "1",
        "v9=default",
        "v8="
        + str(
            _write(
                tmp_path / "v8.txt",
                PLATE_INSTRUCTIONS.replace("never in flight", "possibly in flight"),
            )
        ),
    ]

    code, out = _invoke([*compare, "--calibration", str(record)], tools, capsys)

    assert code == 0
    assert _at(out, "data", "calibration", "source") == "elsewhere.json"
    assert _at(out, "data", "decision", "accept") is True
    assert _at(out, "data", "decision_summary", "position_bias", "fake-1", "verdicts") == 2


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_judge_smoke_sends_one_pair_to_each_judge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = _common(tmp_path)
    tools = Offline()
    assert _invoke(["cache-references", *common], tools, capsys)[0] == 0

    code, out = _invoke(["judge-smoke", *common, "--dry-run"], tools, capsys)
    assert code == 0 and _at(out, "data", "plan") == {"judge_calls": 3, "generations": 0}

    code, out = _invoke(["judge-smoke", *common], tools, capsys)

    assert code == 0
    results = _at(out, "data", "judges")
    assert isinstance(results, list)
    assert [result["judge"] for result in results] == ["fake-1", "fake-2", "fake-3"]
    for result in results:
        assert result["ok"] is True
        assert isinstance(result["seconds"], float)
        assert set(result["verdict"]) == {
            "plate_a",
            "plate_b",
            "more_correct_anatomy",
            "more_correct_lettering",
        }
        assert result["model"] in {"claude-fable-5-2", "gpt-6-astra"}
    assert tools.colour.calls == []
    code, out = _invoke(["judge-smoke", *common, "--judge", "fake-3"], tools, capsys)
    assert _at(out, "data", "judges") == [_at(out, "data", "judges", "0")]
    assert _at(out, "data", "judges", "0", "judge") == "fake-3"


def test_cache_references_takes_an_off_panel_catalog_species(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = _common(tmp_path)
    panel = Path(common[common.index("--panel") + 1])
    listed = json.loads(panel.read_text())
    listed["species"] = [entry for entry in listed["species"] if entry["taxon_id"] != 104]
    panel.write_text(json.dumps(listed))
    tools = Offline()

    code, out = _invoke(["cache-references", *common, "--species", "104"], tools, capsys)

    assert code == 0 and out["data"] == {"frozen": {"104": 2}}


def test_usage_totals_add_up_what_each_call_reported(tmp_path: Path) -> None:
    run = Run.open(tmp_path / "run", seed=3)
    generations = Ledger(run.path("generations.jsonl"))
    generations.record("generation-started", arm="a", taxon_id=1, index=1)
    generations.record(
        "generation-finished", arm="a", taxon_id=1, index=1, usage={"total_tokens": 900}
    )
    generations.record("generation-finished", arm="b", taxon_id=1, index=1, usage=None)
    judgments = Ledger(run.path("judgments.jsonl"))
    judgments.record(
        "judgment-finished", pair="p", judge="fable-1", usage={"total_tokens": 100, "cost_usd": 0.5}
    )
    judgments.record("judgment-finished", pair="q", judge="fable-1", usage={"total_tokens": 300})
    judgments.record("judgment-failed", pair="r", judge="fable-1", error="x")

    totals = usage_totals(run)

    assert totals["generations"] == {
        "calls": 2,
        "calls_with_usage": 1,
        "total_tokens": 900,
        "mean_total_tokens": 900.0,
    }
    assert _at(totals, "judgments", "by_judge", "fable-1") == {
        "calls": 2,
        "calls_with_usage": 2,
        "total_tokens": 400,
        "cost_usd": 0.5,
        "mean_total_tokens": 200.0,
    }
    assert totals["reflections"] == {"calls": 0, "calls_with_usage": 0}
