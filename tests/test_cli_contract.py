"""The 1.x command-line contract: JSON envelope, error codes and exit codes."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from argparse import Namespace
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import UnidentifiedImageError

from inky_bird_frame.access import AccessDeniedError, AccessError
from inky_bird_frame.catalog import CandidateApprovalError
from inky_bird_frame.cli import main
from inky_bird_frame.errors import (
    BusyError,
    CatalogError,
    CatalogPublishError,
    ConfigurationError,
    DataSourceError,
    GenerationError,
    InkyBirdFrameError,
    InstallationError,
    MissingDependencyError,
    QualityReviewError,
    SpeciesStateError,
    TaxonomyMatchError,
    UnsupportedSpeciesError,
    UsageError,
)
from inky_bird_frame.journal import JournalError
from inky_bird_frame.oidc import OIDCError
from inky_bird_frame.owner_operations import OwnerOperationBusyError, OwnerOperationError
from inky_bird_frame.settings import SettingsError

ERROR_CODES = {
    "configuration",
    "usage",
    "busy",
    "data_source",
    "catalog",
    "generation",
    "publication",
    "installation",
    "access",
    "storage",
    "internal",
}


def _run(command: Callable[[Namespace], int]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch("inky_bird_frame.cli.build_parser") as parser,
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        parser.return_value.parse_args.return_value = Namespace(config=None, func=command)
        exit_code = main([])
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _raising(exc: BaseException) -> Callable[[Namespace], int]:
    def command(_args: Namespace) -> int:
        raise exc

    return command


def _busy(exc: Exception) -> Exception:
    exc.__cause__ = BlockingIOError(35, "Resource temporarily unavailable")
    return exc


def test_success_envelope_is_exact() -> None:
    from inky_bird_frame.cli import print_result

    def command(_args: Namespace) -> int:
        print_result({"value": 1})
        return 0

    exit_code, stdout, _ = _run(command)

    assert exit_code == 0
    assert json.loads(stdout) == {"ok": True, "data": {"value": 1}, "schema_version": 1}


@pytest.mark.parametrize(
    ("exc", "code", "exit_code"),
    [
        (ConfigurationError("Configuration example failed"), "configuration", 1),
        (SettingsError("Settings example failed"), "configuration", 1),
        (UsageError("--example requires --other"), "usage", 2),
        (ValueError("--example must be positive"), "usage", 2),
        (BusyError("Another scheduler is already running"), "busy", 3),
        (_busy(GenerationError("Another controller cycle is already running")), "busy", 3),
        (_busy(DataSourceError("Another observation refresh is already running")), "busy", 3),
        (_busy(CatalogPublishError("Another catalog publication is running")), "busy", 3),
        (_busy(CatalogError("Another display cycle is already running")), "busy", 3),
        (OwnerOperationBusyError("Another owner operation is running"), "busy", 3),
        (BlockingIOError(35, "Resource temporarily unavailable"), "busy", 3),
        (DataSourceError("Source example failed"), "data_source", 1),
        (TaxonomyMatchError("Match example failed"), "data_source", 1),
        (CatalogError("Catalog example failed"), "catalog", 1),
        (SpeciesStateError("Species example failed"), "catalog", 1),
        (CandidateApprovalError("Approval example failed"), "catalog", 1),
        (JournalError("Journal example failed"), "catalog", 1),
        (GenerationError("Generation example failed"), "generation", 1),
        (QualityReviewError("Review example failed"), "generation", 1),
        (UnsupportedSpeciesError("Renderer example failed"), "generation", 1),
        (CatalogPublishError("Publication example failed"), "publication", 1),
        (InstallationError("Installation example failed"), "installation", 1),
        (MissingDependencyError("Dependency example failed"), "installation", 1),
        (AccessError("Access example failed"), "access", 1),
        (AccessDeniedError("Denied example failed"), "access", 1),
        (OIDCError("Sign-in example failed"), "access", 1),
        (OwnerOperationError("Operation example failed"), "storage", 1),
        (PermissionError(13, "Permission denied"), "storage", 1),
        (OSError(28, "No space left on device"), "storage", 1),
        (UnidentifiedImageError("cannot identify image file"), "storage", 1),
        (InkyBirdFrameError("Unclassified example failed"), "internal", 1),
    ],
)
def test_error_envelope_and_exit_code(exc: Exception, code: str, exit_code: int) -> None:
    result, stdout, _ = _run(_raising(exc))

    assert result == exit_code
    payload = json.loads(stdout)
    assert payload == {
        "ok": False,
        "error": {"type": code, "message": str(exc)},
        "schema_version": 1,
    }
    assert code in ERROR_CODES


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("PRIVATE_DETAIL /private/path"),
        KeyError("PRIVATE_DETAIL"),
        json.JSONDecodeError("PRIVATE_DETAIL", "{", 0),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "PRIVATE_DETAIL"),
    ],
)
def test_unexpected_error_is_an_internal_envelope_without_traceback(exc: Exception) -> None:
    result, stdout, stderr = _run(_raising(exc))

    assert result == 1
    payload = json.loads(stdout)
    assert payload["ok"] is False
    assert payload["schema_version"] == 1
    assert payload["error"]["type"] == "internal"
    assert "PRIVATE_DETAIL" not in payload["error"]["message"]
    assert payload["error"]["message"]
    assert "Traceback" not in stdout + stderr


def test_error_code_set_is_the_documented_closed_set() -> None:
    from inky_bird_frame import cli

    assert set(cli.ERROR_CODES) == ERROR_CODES
    assert len(cli.ERROR_CODES) == len(ERROR_CODES)


def test_error_type_never_names_a_python_class() -> None:
    for exc in (ValueError("x"), RuntimeError("x"), ConfigurationError("x"), OSError("x")):
        _, stdout, _ = _run(_raising(exc))
        assert json.loads(stdout)["error"]["type"] in ERROR_CODES


def test_argparse_usage_error_exits_two() -> None:
    with redirect_stderr(io.StringIO()), pytest.raises(SystemExit) as raised:
        main(["status"])

    assert raised.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["seed", "--config", "{config}", "--window", "last-week", "--latitude", "1.0"],
        ["seed", "--config", "{config}", "--window", "last-week", "--end-date", "2026-01-02"],
        ["seed", "--config", "{config}", "--start-date", "2026-01-01"],
        ["birdbuddy", "logout", "--config", "{config}"],
        ["access", "prepare", "--config", "{config}"],
        [
            "catalog",
            "prepare",
            "--catalog",
            "{dir}",
            "--source-catalog",
            "{dir}",
            "1",
            "--reason",
            "example",
        ],
    ],
)
def test_hand_rolled_usage_checks_exit_two_with_json(argv: list[str], tmp_path: Path) -> None:
    config = tmp_path / "missing.toml"
    arguments = [
        item.replace("{config}", str(config)).replace("{dir}", str(tmp_path)) for item in argv
    ]
    output = io.StringIO()
    with redirect_stdout(output):
        result = main(arguments)

    assert result == 2
    payload = json.loads(output.getvalue())
    assert payload["ok"] is False
    assert payload["error"]["type"] == "usage"


def test_doctor_not_ready_is_an_ok_report_with_exit_one(tmp_path: Path) -> None:
    class Report:
        ready = False

        def as_dict(self) -> dict[str, object]:
            return {"ready": False, "checks": []}

    output = io.StringIO()
    with (
        patch("inky_bird_frame.cli.doctor", return_value=Report()),
        redirect_stdout(output),
    ):
        result = main(["doctor", "controller", "--config", str(tmp_path / "config.toml")])

    assert result == 1
    assert json.loads(output.getvalue()) == {
        "ok": True,
        "data": {"ready": False, "checks": []},
        "schema_version": 1,
    }


def test_doctor_ready_exits_zero(tmp_path: Path) -> None:
    class Report:
        ready = True

        def as_dict(self) -> dict[str, object]:
            return {"ready": True, "checks": []}

    with (
        patch("inky_bird_frame.cli.doctor", return_value=Report()),
        redirect_stdout(io.StringIO()),
    ):
        assert main(["doctor", "controller", "--config", str(tmp_path / "config.toml")]) == 0


@pytest.mark.parametrize(
    ("arguments", "exit_code", "code"),
    [
        (["status", "--config", "{missing}"], 1, "configuration"),
        (["birdbuddy", "logout", "--config", "{missing}"], 2, "usage"),
    ],
)
def test_module_entry_point_returns_the_contract_exit_code(
    arguments: list[str], exit_code: int, code: str, tmp_path: Path
) -> None:
    missing = str(tmp_path / "missing.toml")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "inky_bird_frame.cli",
            *(item.replace("{missing}", missing) for item in arguments),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == exit_code
    assert json.loads(result.stdout)["error"]["type"] == code
    assert "Traceback" not in result.stderr
