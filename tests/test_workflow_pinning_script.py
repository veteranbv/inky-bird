"""Regression tests for GitHub Actions workflow pinning enforcement."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast


class WorkflowPinningModule(Protocol):
    class Violation(Protocol):
        path: Path
        line: int
        value: str
        reason: str

    def main(self, argv: Sequence[str] | None = None) -> int: ...

    def scan_workflows(self, workflows_dir: Path) -> list[Violation]: ...


def _load_workflow_pinning() -> WorkflowPinningModule:
    script = (
        Path(__file__).resolve().parents[1] / ".github" / "scripts" / "check_workflow_pinning.py"
    )
    spec = importlib.util.spec_from_file_location("workflow_pinning_under_test", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load check_workflow_pinning.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(WorkflowPinningModule, module)


def _write_workflow(workflows_dir: Path, name: str, body: str) -> None:
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / name).write_text(body, encoding="utf-8")


def test_scan_workflows_accepts_pinned_and_local_actions(tmp_path: Path) -> None:
    workflow_pinning = _load_workflow_pinning()
    workflows_dir = tmp_path / "workflows"
    _write_workflow(
        workflows_dir,
        "ci.yml",
        """
name: ci
jobs:
  test:
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4
      - uses: ./.github/actions/local-action
""",
    )

    assert workflow_pinning.scan_workflows(workflows_dir) == []


def test_scan_workflows_rejects_tags_missing_refs_and_short_shas(tmp_path: Path) -> None:
    workflow_pinning = _load_workflow_pinning()
    workflows_dir = tmp_path / "workflows"
    _write_workflow(
        workflows_dir,
        "ci.yml",
        """
name: ci
jobs:
  test:
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv
      - uses: owner/repo@abc123
""",
    )

    violations = workflow_pinning.scan_workflows(workflows_dir)

    assert [(v.line, v.value, v.reason) for v in violations] == [
        (6, "actions/checkout@v4", "ref is not a 40-character commit SHA"),
        (7, "astral-sh/setup-uv", "missing @<commit-sha> ref"),
        (8, "owner/repo@abc123", "ref is not a 40-character commit SHA"),
    ]


def test_scan_workflows_rejects_quoted_keys_and_flow_mappings(tmp_path: Path) -> None:
    workflow_pinning = _load_workflow_pinning()
    workflows_dir = tmp_path / "workflows"
    _write_workflow(
        workflows_dir,
        "ci.yml",
        """
name: ci
jobs:
  test:
    steps:
      - "uses": actions/checkout@v4
      - { uses: astral-sh/setup-uv }
""",
    )

    violations = workflow_pinning.scan_workflows(workflows_dir)

    assert [(v.line, v.value, v.reason) for v in violations] == [
        (6, "actions/checkout@v4", "ref is not a 40-character commit SHA"),
        (7, "astral-sh/setup-uv", "missing @<commit-sha> ref"),
    ]


def test_current_workflows_are_pinned_and_github_hosted() -> None:
    workflow_pinning = _load_workflow_pinning()
    workflows_dir = Path(__file__).resolve().parents[1] / ".github" / "workflows"

    assert workflow_pinning.scan_workflows(workflows_dir) == []


def test_review_gate_runs_only_trusted_default_branch_code() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
    workflow = (workflows / "review-gate-trusted.yml").read_text(encoding="utf-8")

    assert not (workflows / "review-gate.yml").exists()
    assert "pull_request_target:" in workflow
    # One suite per head: reopen/ready reruns go through `homelab pr recheck`.
    assert "types: [opened, synchronize]" in workflow
    # The evaluator comes from the default-branch tip, never the PR head or
    # the base branch the PR chose.
    assert "ref: ${{ github.sha }}" in workflow
    assert "ref: ${{ github.event.pull_request.head" not in workflow
    assert "ref: ${{ github.event.pull_request.base.sha }}" not in workflow
    assert "persist-credentials: false" in workflow
    assert "python3 -I" in workflow
    assert "permissions: {}" in workflow
    assert ": write" not in workflow


def test_review_gate_retrigger_admits_only_codex_bot() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/review-gate-retrigger.yml"
    ).read_text(encoding="utf-8")

    triggers = workflow.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    # issue_comment only: a pull_request_review trigger would run the PR's copy.
    assert "issue_comment:" in triggers
    assert "pull_request_review" not in triggers
    assert workflow.count("user.login == 'chatgpt-codex-connector[bot]'") == 1
    assert "github.repository_owner" not in workflow
    assert "${{ github.event.comment.body }}" not in workflow
    # Only the trusted gate is re-run; the removed legacy workflow is not queried.
    assert "review-gate-trusted.yml/runs" in workflow
    assert "review-gate.yml/runs" not in workflow


DIGEST = "sha256:" + "0" * 64


def test_scan_workflows_requires_digests_for_docker_references(tmp_path: Path) -> None:
    workflow_pinning = _load_workflow_pinning()
    workflows_dir = tmp_path / "workflows"
    _write_workflow(
        workflows_dir,
        "ci.yml",
        f"""
name: ci
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: docker://alpine@{DIGEST}
      - uses: docker://alpine:3.20
      - uses: docker://ghcr.io/owner/image
      - uses: docker://alpine:3.20@sha256:abc
""",
    )

    violations = workflow_pinning.scan_workflows(workflows_dir)

    reason = "docker:// reference is not pinned to a sha256 digest"
    assert [(v.line, v.value, v.reason) for v in violations] == [
        (8, "docker://alpine:3.20", reason),
        (9, "docker://ghcr.io/owner/image", reason),
        (10, "docker://alpine:3.20@sha256:abc", reason),
    ]


def test_scan_workflows_accepts_one_literal_github_hosted_runner(tmp_path: Path) -> None:
    workflow_pinning = _load_workflow_pinning()
    workflows_dir = tmp_path / "workflows"
    _write_workflow(
        workflows_dir,
        "ci.yml",
        """
name: ci
jobs:
  a:
    runs-on: ubuntu-latest
  b:
    runs-on: "ubuntu-24.04-arm"  # native arm64
  c:
    runs-on: [windows-2025]
  d:
    runs-on:
      - macos-15
  e:
    uses: ./.github/workflows/reusable.yml
""",
    )

    assert workflow_pinning.scan_workflows(workflows_dir) == []


def test_scan_workflows_rejects_runners_that_are_not_github_hosted(tmp_path: Path) -> None:
    workflow_pinning = _load_workflow_pinning()
    workflows_dir = tmp_path / "workflows"
    _write_workflow(
        workflows_dir,
        "deploy.yml",
        """
name: deploy
jobs:
  a:
    runs-on: [self-hosted, macOS, ARM64, inky-bird-controller]
  b:
    runs-on: inky-bird-controller
  c:
    runs-on: ${{ matrix.runner }}
  d:
    runs-on:
      - self-hosted
  e:
    runs-on:
      group: private-runners
  f:
    runs-on: [ubuntu-latest, gpu]
  g: { runs-on: ubuntu-latest }
  h:
    "runs-on": Self-Hosted
""",
    )

    violations = workflow_pinning.scan_workflows(workflows_dir)

    assert [(v.line, v.reason) for v in violations] == [
        (5, "names a self-hosted runner"),
        (7, "inky-bird-controller is not a GitHub-hosted runner label"),
        (9, "runner must be a literal GitHub-hosted label, not an expression"),
        (11, "names a self-hosted runner"),
        (14, "runner groups and label mappings are not allowed"),
        (17, "a GitHub-hosted job names exactly one runner label"),
        (18, "runs-on inside a flow mapping cannot be checked; use block style"),
        (20, "names a self-hosted runner"),
    ]


def test_main_reports_runner_violations(tmp_path: Path) -> None:
    workflow_pinning = _load_workflow_pinning()
    workflows_dir = tmp_path / "workflows"
    _write_workflow(
        workflows_dir,
        "ci.yml",
        "jobs:\n  a:\n    runs-on: [self-hosted, linux]\n",
    )

    assert workflow_pinning.main([str(workflows_dir)]) == 1
