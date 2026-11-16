"""The private environment file named by INKY_BIRD_ENV_FILE."""

from __future__ import annotations

import io
import json
import logging
import os
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from inky_bird_frame.cli import main
from inky_bird_frame.errors import ConfigurationError
from inky_bird_frame.installation import (
    PRIVATE_ENVIRONMENT_FILE_ENV,
    InstallationRole,
    doctor,
    load_private_environment,
    read_private_environment,
)

SECRET = "sensitive-test-value"

CONFIG = """
[discovery]
zip_code = "12345"
radius_km = 8
species_limit = 50
window = "last-30-days"

[controller]
workspace_dir = "workspace"
catalog_dir = "catalog"
state_dir = "controller-state"
codex_path = "{codex}"
bind_host = "127.0.0.1"
port = 8793
references_per_species = 4
generations_per_cycle = 1
max_generation_attempts = 3

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display-state"

[notifications]
enabled = true

[[notifications.destinations]]
name = "synthetic-env"
url_env = "PRIVATE_TEST_NOTIFY_URL"
events = ["terminal_error"]
"""


def _private_file(directory: Path, content: str, mode: int = 0o600) -> Path:
    path = directory / "private.env"
    path.write_text(content)
    path.chmod(mode)
    return path


def _config(directory: Path) -> Path:
    codex = directory / "codex"
    codex.touch(mode=0o700)
    path = directory / "config.toml"
    path.write_text(CONFIG.format(codex=codex))
    path.chmod(0o600)
    return path


def _run(argv: list[str]) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(argv)
    return code, json.loads(output.getvalue())


def test_absent_setting_changes_nothing(tmp_path: Path) -> None:
    _private_file(tmp_path, f"PRIVATE_TEST_TOKEN={SECRET}\n")
    for unset in ({}, {PRIVATE_ENVIRONMENT_FILE_ENV: ""}):
        with patch.dict(os.environ, unset, clear=True):
            before = dict(os.environ)
            assert load_private_environment() == []
            assert dict(os.environ) == before


def test_process_environment_wins_and_values_are_read_literally(tmp_path: Path) -> None:
    path = _private_file(
        tmp_path,
        "# owner secrets\n"
        "\n"
        "PRIVATE_TEST_KEPT=from-file\n"
        "PRIVATE_TEST_PLAIN=a=b$HOME`x`\n"
        'PRIVATE_TEST_DOUBLE="with spaces \\" and \\\\ \\u2603"\n'
        "PRIVATE_TEST_SINGLE='literal $HOME \\n'\n"
        "PRIVATE_TEST_EMPTY=\n"
        "  PRIVATE_TEST_SPACED=  trimmed  \n",
    )
    with patch.dict(
        os.environ,
        {PRIVATE_ENVIRONMENT_FILE_ENV: str(path), "PRIVATE_TEST_KEPT": "from-process"},
        clear=True,
    ):
        added = load_private_environment()
        assert os.environ["PRIVATE_TEST_KEPT"] == "from-process"
        assert os.environ["PRIVATE_TEST_PLAIN"] == "a=b$HOME`x`"
        assert os.environ["PRIVATE_TEST_DOUBLE"] == 'with spaces " and \\ \u2603'
        assert os.environ["PRIVATE_TEST_SINGLE"] == "literal $HOME \\n"
        assert os.environ["PRIVATE_TEST_EMPTY"] == ""
        assert os.environ["PRIVATE_TEST_SPACED"] == "trimmed"
    assert "PRIVATE_TEST_KEPT" not in added
    assert set(added) == {
        "PRIVATE_TEST_PLAIN",
        "PRIVATE_TEST_DOUBLE",
        "PRIVATE_TEST_SINGLE",
        "PRIVATE_TEST_EMPTY",
        "PRIVATE_TEST_SPACED",
    }


def test_relative_path_is_refused(tmp_path: Path) -> None:
    with (
        patch.dict(os.environ, {PRIVATE_ENVIRONMENT_FILE_ENV: "private.env"}, clear=True),
        pytest.raises(ConfigurationError, match="absolute path"),
    ):
        load_private_environment()


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "symlink",
        "hardlink",
        "directory",
        "fifo",
        "group-readable",
        "world-readable",
        "group-writable",
        "world-writable",
        "other-owner",
        "shared-folder",
        "oversized",
        "not-utf8",
    ],
)
def test_unsafe_files_are_refused_without_their_values(tmp_path: Path, case: str) -> None:
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    path = _private_file(folder, f"PRIVATE_TEST_TOKEN={SECRET}\n")
    original = folder / "original.env"
    if case == "missing":
        path.unlink()
    elif case in {"symlink", "hardlink"}:
        path.rename(original)
        if case == "symlink":
            path.symlink_to(original)
        else:
            os.link(original, path)
    elif case == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    elif case == "fifo":
        path.unlink()
        os.mkfifo(path, mode=0o600)
    elif case.endswith(("-readable", "-writable")):
        bits = {
            "group-readable": 0o040,
            "world-readable": 0o004,
            "group-writable": 0o020,
            "world-writable": 0o002,
        }[case]
        path.chmod(0o600 | bits)
    elif case == "shared-folder":
        folder.chmod(0o777)
    elif case == "oversized":
        path.write_text(f"PRIVATE_TEST_TOKEN={SECRET}{'x' * 64 * 1024}\n")
    elif case == "not-utf8":
        path.write_bytes(f"PRIVATE_TEST_TOKEN={SECRET}\xff\n".encode("latin-1"))
    uid = os.getuid() + 1 if case == "other-owner" else os.getuid()
    with (
        patch.dict(os.environ, {PRIVATE_ENVIRONMENT_FILE_ENV: str(path)}, clear=True),
        patch("inky_bird_frame.installation.os.getuid", return_value=uid),
        pytest.raises(ConfigurationError) as error,
    ):
        load_private_environment()
    message = str(error.value)
    assert str(path) in message
    assert SECRET not in message
    assert "PRIVATE_TEST_TOKEN" not in os.environ


@pytest.mark.parametrize(
    "line",
    [
        f"{SECRET}",
        f"1BAD={SECRET}",
        f"export PRIVATE_TEST_TOKEN={SECRET}",
        f"PRIVATE_TEST_TOKEN ={SECRET}",
        f'PRIVATE_TEST_TOKEN="{SECRET}',
        f"PRIVATE_TEST_TOKEN='{SECRET}'x'",
        f'PRIVATE_TEST_TOKEN={SECRET}"',
        f'PRIVATE_TEST_TOKEN="{SECRET}\\u0007"',
        f"PRIVATE_TEST_TOKEN={SECRET}\tafter-tab",
        f"PRIVATE_TEST_OTHER=one\nPRIVATE_TEST_OTHER={SECRET}",
    ],
)
def test_malformed_lines_name_the_line_but_never_the_value(
    tmp_path: Path, line: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = _private_file(tmp_path, f"# comment\nPRIVATE_TEST_FIRST=ok\n{line}\n")
    expected_line = 4 if "\n" in line else 3
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(ConfigurationError, match=f"Line {expected_line} of") as error,
    ):
        read_private_environment(path)
    assert SECRET not in str(error.value)
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    "argv",
    [
        ["serve"],
        ["scheduler"],
        ["refresh"],
        ["generate"],
        ["catalog-publish"],
        ["notifications", "dispatch"],
        ["doctor", "controller"],
    ],
)
def test_every_entry_point_loads_the_file_and_removes_it_afterwards(
    tmp_path: Path, argv: list[str]
) -> None:
    path = _private_file(tmp_path, f"PRIVATE_TEST_TOKEN={SECRET}\n")
    seen: list[str | None] = []

    def command(args: Namespace) -> int:
        seen.append(os.environ.get("PRIVATE_TEST_TOKEN"))
        return 0

    handler = {
        "serve": "serve_command",
        "scheduler": "scheduler_command",
        "refresh": "refresh_command",
        "generate": "generate_command",
        "catalog-publish": "catalog_publish_command",
        "notifications": "notifications_dispatch_command",
        "doctor": "doctor_command",
    }[argv[0]]
    with (
        patch.dict(os.environ, {PRIVATE_ENVIRONMENT_FILE_ENV: str(path)}, clear=True),
        patch(f"inky_bird_frame.cli.{handler}", side_effect=command),
    ):
        assert main([*argv, "--config", str(tmp_path / "config.toml")]) == 0
        assert "PRIVATE_TEST_TOKEN" not in os.environ
    assert seen == [SECRET]


def test_unsafe_file_stops_the_command_with_a_configuration_error(tmp_path: Path) -> None:
    path = _private_file(tmp_path, f"PRIVATE_TEST_TOKEN={SECRET}\n", mode=0o644)
    with (
        patch.dict(os.environ, {PRIVATE_ENVIRONMENT_FILE_ENV: str(path)}, clear=True),
        patch("inky_bird_frame.cli.serve_command") as serve,
    ):
        code, payload = _run(["serve", "--config", str(tmp_path / "config.toml")])
    serve.assert_not_called()
    assert code != 0
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["type"] == "configuration"
    assert SECRET not in json.dumps(payload)


def test_config_validate_reads_the_file_first_and_reports_only_its_path(tmp_path: Path) -> None:
    config = _config(tmp_path)
    url = "json://example.test/private-hook"
    path = _private_file(tmp_path, f"PRIVATE_TEST_NOTIFY_URL={url}\n")
    with patch.dict(os.environ, {PRIVATE_ENVIRONMENT_FILE_ENV: str(path)}, clear=True):
        code, payload = _run(["config", "validate", "--config", str(config)])
    assert code == 0, payload
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["private_environment_file"] == str(path)
    assert url not in json.dumps(payload)
    with patch.dict(os.environ, {}, clear=True):
        code, payload = _run(["config", "validate", "--config", str(config)])
    assert code != 0


def test_config_validate_without_the_file_reports_none(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with patch.dict(
        os.environ, {"PRIVATE_TEST_NOTIFY_URL": "json://example.test/hook"}, clear=True
    ):
        code, payload = _run(["config", "validate", "--config", str(config)])
    assert code == 0, payload
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["private_environment_file"] is None


@pytest.mark.parametrize("safe", [True, False])
def test_doctor_reports_the_file_by_path_only(tmp_path: Path, safe: bool) -> None:
    config = _config(tmp_path)
    path = _private_file(
        tmp_path,
        f"PRIVATE_TEST_NOTIFY_URL=json://example.test/{SECRET}\n",
        mode=0o600 if safe else 0o644,
    )
    with (
        patch.dict(os.environ, {PRIVATE_ENVIRONMENT_FILE_ENV: str(path)}, clear=True),
        patch("inky_bird_frame.installation.platform.system", return_value="Linux"),
        patch("inky_bird_frame.installation.shutil.which", return_value=None),
        patch("inky_bird_frame.installation.get_json", side_effect=OSError("offline")),
    ):
        if safe:
            load_private_environment()
        report = doctor(InstallationRole.CONTROLLER, config).as_dict()
    checks = report["checks"]
    assert isinstance(checks, list)
    check = next(item for item in checks if item["id"] == "private_environment")
    assert check["status"] == ("pass" if safe else "fail")
    assert str(path) in json.dumps(check)
    assert SECRET not in json.dumps(report)
