"""The run report: markdown tables, unblinded contact sheets and a JSON summary.

It reads only what earlier commands saved, so it can be rerun at any time
after judging; it never generates or judges.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import HarnessError
from .rundir import Run, read_json, read_object, resolve_recorded, write_json, write_text

THUMBNAIL: Final = (360, 480)
MARGIN: Final = 16
HEADER: Final = 56
WINNER: Final = (34, 139, 34)
LOSER: Final = (200, 200, 200)
TIE: Final = (218, 165, 32)


def _records(path: Path) -> list[dict[str, object]]:
    value = read_json(path)
    if not isinstance(value, list):
        raise HarnessError(f"{path.name} must hold a list")
    return [item for item in value if isinstance(item, dict)]


def _majority(values: list[str]) -> str:
    for outcome in ("first", "second", "tie"):
        if 2 * values.count(outcome) > len(values):
            return outcome
    return "tie"


def _thumbnail(path: Path) -> Image.Image:
    try:
        with Image.open(path) as image:
            return ImageOps.contain(image.convert("RGB"), THUMBNAIL)
    except OSError:
        return Image.new("RGB", THUMBNAIL, (90, 90, 90))


def contact_sheet(
    records: list[dict[str, object]], destination: Path, title: str, root: Path
) -> None:
    """One row per pair: the two plates side by side, the anatomy winner framed green."""
    font = ImageFont.load_default(size=18)
    row_height = THUMBNAIL[1] + HEADER + MARGIN
    width = 2 * THUMBNAIL[0] + 3 * MARGIN
    sheet = Image.new("RGB", (width, HEADER + row_height * len(records)), (250, 248, 240))
    draw = ImageDraw.Draw(sheet)
    draw.text((MARGIN, MARGIN), title, fill=(0, 0, 0), font=font)
    for row, record in enumerate(records):
        listed = record.get("votes")
        votes = (
            [vote for vote in listed if isinstance(vote, dict)] if isinstance(listed, list) else []
        )
        anatomy = _majority([str(vote.get("anatomy")) for vote in votes]) if votes else "unjudged"
        lettering = _majority([str(vote.get("lettering")) for vote in votes]) if votes else "-"
        top = HEADER + row * row_height
        for column, role in enumerate(("first", "second")):
            plate = record.get(role)
            if not isinstance(plate, dict):
                continue
            left = MARGIN + column * (THUMBNAIL[0] + MARGIN)
            colour = TIE if anatomy == "tie" else WINNER if anatomy == role else LOSER
            draw.rectangle(
                (
                    left - 4,
                    top + HEADER - 4,
                    left + THUMBNAIL[0] + 4,
                    top + HEADER + THUMBNAIL[1] + 4,
                ),
                outline=colour,
                width=6,
            )
            sheet.paste(
                _thumbnail(resolve_recorded(root, str(plate.get("path")))), (left, top + HEADER)
            )
            draw.text((left, top + 4), str(plate.get("label")), fill=(0, 0, 0), font=font)
        draw.text(
            (MARGIN, top + 28),
            f"anatomy: {anatomy}   lettering: {lettering}   judges: {len(votes)}",
            fill=(60, 60, 60),
            font=font,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, format="PNG")


def _rate(value: object) -> str:
    if not isinstance(value, dict):
        return "-"
    return f"{value.get('point', 0):.3f} [{value.get('low', 0):.3f}, {value.get('high', 0):.3f}]"


def _number(value: object) -> str:
    return f"{value:.3f}" if isinstance(value, float | int) else "-"


def _arm_lines(run: Run, summary: dict[str, object]) -> list[str]:
    """What each arm is, so a wording-only arm is never read as a shipped prompt version."""
    lines: list[str] = []
    for role in ("first", "second"):
        arm = summary.get(role)
        name = arm.get("name") if isinstance(arm, dict) else None
        if not isinstance(name, str):
            continue
        path = run.path("arms", f"{name}.json")
        record = read_object(path) if path.exists() else {}
        what = (
            "the shipped plate instructions"
            if record.get("is_default") is True
            else "replacement instruction wording inside the current prompt template; only the"
            " wording differs from the shipped prompt, not the data block or output lines"
        )
        lines.append(f"- {role.capitalize()} arm `{name}` ({record.get('label', '-')}): {what}.")
    return [*lines, ""] if lines else []


def _judge_notes(summary: dict[str, object]) -> list[str]:
    """Say when judges share a model and prompt, since their votes then move together."""
    judged_with = summary.get("judged_with")
    judges = judged_with.get("judges") if isinstance(judged_with, dict) else None
    groups: dict[tuple[object, object], list[str]] = defaultdict(list)
    for judge in judges if isinstance(judges, list) else []:
        if isinstance(judge, dict):
            groups[(judge.get("kind"), judge.get("model"))].append(str(judge.get("name")))
    return [
        f"Judges {', '.join(names)} use the same model and prompt, so their votes are strongly"
        " correlated: the majority leans on that model, and agreement and kappa overstate how"
        " independent the panel is."
        for names in groups.values()
        if len(names) > 1
    ]


def _comparison_section(run: Run, name: str, summary: dict[str, object]) -> list[str]:
    stats = summary.get("summary")
    decision = summary.get("decision")
    lines = [f"## {name}", "", *_arm_lines(run, summary)]
    if not isinstance(stats, dict):
        return [*lines, "No pair has been judged yet.", ""]
    agreement = stats.get("agreement")
    anatomy_agreement = agreement.get("anatomy") if isinstance(agreement, dict) else None
    lines.extend(
        [
            "| Measure | Value |",
            "| --- | --- |",
            f"| Pairs judged | {stats.get('pairs')} |",
            f"| Anatomy win rate of first (95% CI) | {_rate(stats.get('anatomy_win_rate'))} |",
            f"| Lettering win rate of first (95% CI) | {_rate(stats.get('lettering_win_rate'))} |",
            f"| Fatal-defect rate, first / second | {_number(stats.get('first_fatal_rate'))}"
            f" / {_number(stats.get('second_fatal_rate'))} |",
            f"| Acceptable rate, first / second | {_number(stats.get('first_acceptable_rate'))}"
            f" / {_number(stats.get('second_acceptable_rate'))} |",
        ]
    )
    if isinstance(anatomy_agreement, dict):
        lines.append(
            f"| Anatomy Fleiss' kappa | {_number(anatomy_agreement.get('fleiss_kappa'))} |"
        )
        pairwise = anatomy_agreement.get("pairwise")
        if isinstance(pairwise, dict):
            for judges, value in pairwise.items():
                lines.append(f"| Anatomy agreement {judges} | {_number(value)} |")
    coverage = stats.get("judge_coverage")
    if isinstance(coverage, dict):
        for judge, counts in coverage.items():
            if isinstance(counts, dict):
                lines.append(
                    f"| Verdicts from {judge} | {counts.get('votes')}"
                    f" (missing {counts.get('missing')}) |"
                )
    notes = _judge_notes(summary)
    if notes:
        lines.extend(["", *notes])
    if isinstance(decision, dict):
        verdict = "replace the baseline" if decision.get("accept") else "keep the baseline"
        on = "held-out species" if summary.get("decision_species") == "holdout" else "all species"
        lines.extend(["", f"Decision on {on}: **{verdict}**."])
        reasons = decision.get("reasons")
        if isinstance(reasons, list):
            lines.extend(f"- {reason}" for reason in reasons)
    return [*lines, ""]


def _calibration_section(summary: dict[str, object]) -> list[str]:
    lines = [
        "## Judge calibration",
        "",
        "| Judge | Anchor accuracy | Planted recall | Swap consistency | Passed |",
        "| --- | --- | --- | --- | --- |",
    ]
    judges = summary.get("judges")
    for judge in judges if isinstance(judges, list) else []:
        if isinstance(judge, dict):
            lines.append(
                f"| {judge.get('judge')} | {_number(judge.get('anchor_accuracy'))}"
                f" | {_number(judge.get('planted_recall'))}"
                f" | {_number(judge.get('swap_consistency'))}"
                f" | {'yes' if judge.get('passed') else 'no'} |"
            )
    return [*lines, ""]


def write_report(run: Run) -> dict[str, object]:
    out = run.path("report")
    lines = ["# Plate prompt evaluation report", ""]
    produced: dict[str, object] = {"comparisons": [], "sheets": []}
    calibration = run.path("calibration", "summary.json")
    if calibration.exists():
        calibration_summary = read_object(calibration)
        lines.extend(_calibration_section(calibration_summary))
        produced["calibration"] = calibration_summary
    comparisons: list[dict[str, object]] = []
    sheets: list[str] = []
    for summary_path in sorted(run.root.glob("*/*/summary.json")):
        scope, name = summary_path.parent.parent.name, summary_path.parent.name
        if scope not in {"compare", "tune"}:
            continue
        summary = read_object(summary_path)
        lines.extend(_comparison_section(run, f"{scope}: {name}", summary))
        comparisons.append({"scope": scope, "name": name, **summary})
        records = _records(summary_path.parent / "pairs.json")
        by_species: dict[int, list[dict[str, object]]] = defaultdict(list)
        for record in records:
            taxon = record.get("taxon_id")
            if isinstance(taxon, int):
                by_species[taxon].append(record)
        for taxon, species_records in sorted(by_species.items()):
            sheet = out / "sheets" / scope / name / f"{taxon}.png"
            contact_sheet(species_records, sheet, f"{name} — taxon {taxon}", run.root)
            sheets.append(str(sheet.relative_to(run.root)))
            lines.append(f"- [{taxon}](sheets/{scope}/{name}/{taxon}.png)")
        lines.append("")
    for tuned in ("tune", "tune-review"):
        path = run.path(tuned, "summary.json")
        if path.exists():
            lines.extend([f"## {tuned} result", "", "```json", path.read_text().strip(), "```", ""])
            produced[tuned] = read_object(path)
    produced["comparisons"] = comparisons
    produced["sheets"] = sheets
    write_text(out / "report.md", "\n".join(lines))
    write_json(out / "summary.json", produced)
    return {"report": str((out / "report.md").relative_to(run.root)), "sheets": len(sheets)}
