"""Fail when a workflow uses an unpinned reference or a runner that is not GitHub-hosted.

External `uses:` references must name a full commit SHA and `docker://` images a
sha256 digest. Every `runs-on:` must be one literal GitHub-hosted label: this
public repository registers no self-hosted runner, and a workflow that asks for
one, by the `self-hosted` label, a custom label or a runner group, fails here.

The check reads lines, not parsed YAML, so it is repository hygiene rather than
a security boundary: an escaped key or a remote reusable workflow can get past
it, and a pull request runs its own copy. The controls are that no runner is
registered and that outside contributors' workflows wait for approval.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

FULL_SHA_REF: Final = re.compile(r"^[0-9a-fA-F]{40}$")
USES_KEY: Final = re.compile(r"(?:^|[-{,])\s*(?:uses|'uses'|\"uses\")\s*:\s*(.+)$")
LOCAL_USES_PREFIXES: Final = ("./", "../")
DOCKER_PREFIX: Final = "docker://"
DOCKER_DIGEST_REF: Final = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")
RUNS_ON_BLOCK_KEY: Final = re.compile(r"^(\s*)(?:runs-on|'runs-on'|\"runs-on\")\s*:(.*)$")
RUNS_ON_ANY_KEY: Final = re.compile(r"(?:^|[\s{,])(?:runs-on|'runs-on'|\"runs-on\")\s*:")
# Standard GitHub-hosted runner images. Labels are matched case-insensitively.
GITHUB_HOSTED_LABEL: Final = re.compile(
    r"^(?:ubuntu-latest|ubuntu-\d{2}\.04(?:-arm)?"
    r"|windows-latest|windows-\d{4}|windows-11-arm"
    r"|macos-latest|macos-\d{2})$"
)


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    value: str
    reason: str


def workflow_files(workflows_dir: Path) -> list[Path]:
    return sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")])


def _strip_comment(value: str) -> str:
    return re.split(r"(?:^|\s)#", value, maxsplit=1)[0].strip()


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1].strip()
    return value


def _uses_value(line: str) -> str | None:
    match = USES_KEY.search(line)
    if match is None:
        return None
    return _unquote(match.group(1).split("#", 1)[0].strip().rstrip(",}"))


def _uses_reason(value: str) -> str | None:
    if value.startswith(LOCAL_USES_PREFIXES):
        return None
    if value.startswith(DOCKER_PREFIX):
        if DOCKER_DIGEST_REF.fullmatch(value):
            return None
        return "docker:// reference is not pinned to a sha256 digest"
    if "@" not in value:
        return "missing @<commit-sha> ref"
    if not FULL_SHA_REF.fullmatch(value.rsplit("@", 1)[1]):
        return "ref is not a 40-character commit SHA"
    return None


def _runner_labels(lines: Sequence[str], index: int, indent: int, inline: str) -> list[str] | None:
    """Return the labels of one `runs-on:` key, or None when it is a mapping."""
    if inline.startswith("["):
        return [_unquote(item) for item in inline.strip("[]").split(",") if item.strip()]
    if inline:
        return [_unquote(inline)]
    labels: list[str] = []
    for following in lines[index + 1 :]:
        content = _strip_comment(following)
        if not content:
            continue
        if len(following) - len(following.lstrip()) <= indent:
            break
        if not content.startswith("-"):
            return None
        labels.append(_unquote(content[1:]))
    return labels


def _runner_reason(labels: Sequence[str] | None) -> str | None:
    if labels is None:
        return "runner groups and label mappings are not allowed"
    if any("${{" in label for label in labels):
        return "runner must be a literal GitHub-hosted label, not an expression"
    if any(label.lower() == "self-hosted" for label in labels):
        return "names a self-hosted runner"
    # Any extra label routes the job to a self-hosted runner carrying those labels.
    if len(labels) != 1:
        return "a GitHub-hosted job names exactly one runner label"
    if not GITHUB_HOSTED_LABEL.fullmatch(labels[0].lower()):
        return f"{labels[0]} is not a GitHub-hosted runner label"
    return None


def scan_workflows(workflows_dir: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in workflow_files(workflows_dir):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            line_number = index + 1
            value = _uses_value(line)
            if value:
                reason = _uses_reason(value)
                if reason is not None:
                    violations.append(Violation(path, line_number, value, reason))
            runs_on = RUNS_ON_BLOCK_KEY.match(line)
            if runs_on is not None:
                inline = _strip_comment(runs_on.group(2))
                labels = _runner_labels(lines, index, len(runs_on.group(1)), inline)
                reason = _runner_reason(labels)
            elif RUNS_ON_ANY_KEY.search(_strip_comment(line)):
                reason = "runs-on inside a flow mapping cannot be checked; use block style"
            else:
                continue
            if reason is not None:
                violations.append(Violation(path, line_number, line.strip(), reason))
    return violations


def format_violations(violations: Sequence[Violation]) -> str:
    lines = ["Workflow policy violations found:"]
    lines.extend(f"- {item.path}:{item.line}: {item.value} ({item.reason})" for item in violations)
    lines.extend(
        (
            "",
            "Pin external actions to a full 40-character commit SHA and docker:// images to",
            "a sha256 digest. Run every job on one literal GitHub-hosted runner label.",
        )
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflows_dir", nargs="?", default=".github/workflows", type=Path)
    args = parser.parse_args(argv)
    if not args.workflows_dir.is_dir() or not workflow_files(args.workflows_dir):
        print(f"workflow directory is missing or empty: {args.workflows_dir}", file=sys.stderr)
        return 2
    violations = scan_workflows(args.workflows_dir)
    if violations:
        print(format_violations(violations), file=sys.stderr)
        return 1
    count = len(workflow_files(args.workflows_dir))
    print(f"workflow pinning and GitHub-hosted runner policy OK: {count} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
