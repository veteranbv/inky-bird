from __future__ import annotations

import errno
import io
import json
import os
import stat
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from inky_bird_frame.cli import (
    access_recover_command,
    build_parser,
    config_install_command,
    main,
    notifications_test_command,
)
from inky_bird_frame.errors import ConfigurationError
from inky_bird_frame.http import write_bytes_atomic

VALID = """
[discovery]
zip_code = "12345"
radius_km = 8
species_limit = 50
window = "last-30-days"

[controller]
workspace_dir = "workspace"
catalog_dir = "catalog"
state_dir = "state"
codex_path = "codex"
bind_host = "0.0.0.0"
port = 8793
references_per_species = 4
generations_per_cycle = 1

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display-state"
"""

APP_OWNED = VALID.replace(
    'workspace_dir = "workspace"',
    'configuration_ownership = "app"\nworkspace_dir = "workspace"',
)


def _stdin(content: bytes) -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(content), encoding="utf-8")


def _install(destination: Path, content: bytes) -> dict[str, object]:
    output = io.StringIO()
    with patch("sys.stdin", _stdin(content)), redirect_stdout(output):
        assert config_install_command(build_parser().parse_args(_arguments(destination))) == 0
    payload = json.loads(output.getvalue())
    assert payload["ok"] is True
    data = payload["data"]
    assert isinstance(data, dict)
    return data


def _arguments(destination: Path) -> list[str]:
    return ["config", "install", "--destination", str(destination)]


def test_install_writes_a_private_validated_copy(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"

    result = _install(destination, VALID.encode())

    assert destination.read_text(encoding="utf-8") == VALID
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert result["installed"] is True
    assert result["previous_config_kept_as"] is None
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.toml"]


def test_install_replaces_a_valid_managed_copy_without_keeping_it_aside(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text(VALID)
    replacement = VALID.replace("radius_km = 8", "radius_km = 9")

    result = _install(destination, replacement.encode())

    assert destination.read_text(encoding="utf-8") == replacement
    assert result["previous_config_kept_as"] is None
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.toml"]


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        (b"not = [valid", "Invalid TOML in standard input"),
        (VALID.replace("[controller]", "[controllers]").encode(), r"Missing \[controller\]"),
        (VALID.replace("port = 8793", "port = 0").encode(), "port must be"),
        (VALID.replace("12345", "1234\xe9").encode("latin-1"), "not valid UTF-8"),
    ],
)
def test_invalid_candidate_leaves_valid_destination_unchanged(
    tmp_path: Path, candidate: bytes, message: str
) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text(VALID)
    destination.chmod(0o600)

    with (
        patch("sys.stdin", _stdin(candidate)),
        redirect_stdout(io.StringIO()),
        pytest.raises(ConfigurationError, match=message),
    ):
        config_install_command(build_parser().parse_args(_arguments(destination)))

    assert destination.read_text(encoding="utf-8") == VALID
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.toml"]


@pytest.mark.parametrize(
    "existing",
    [
        b"damaged = [",
        b"\xff\xfe not text",
        VALID.replace("[display_node]", "[display]").encode(),
        APP_OWNED.replace("port = 8793", "port = 0").encode(),
    ],
)
def test_unreadable_destination_is_kept_aside_and_replaced(tmp_path: Path, existing: bytes) -> None:
    destination = tmp_path / "config.toml"
    destination.write_bytes(existing)

    result = _install(destination, VALID.encode())

    assert destination.read_text(encoding="utf-8") == VALID
    kept = result["previous_config_kept_as"]
    assert isinstance(kept, str)
    assert kept.startswith("config.toml.invalid-")
    assert "/" not in kept
    assert (tmp_path / kept).read_bytes() == existing
    assert stat.S_IMODE((tmp_path / kept).stat().st_mode) == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(["config.toml", kept])
    assert result["installed"] is True


@pytest.mark.parametrize(
    ("existing", "browser_note"),
    [
        (APP_OWNED.replace("port = 8793", "port = 0").encode(), True),
        (b"damaged = [", True),
        (VALID.replace("port = 8793", "port = 0").encode(), False),
    ],
)
def test_repair_explains_what_was_kept_and_replaced(
    tmp_path: Path, existing: bytes, browser_note: bool
) -> None:
    destination = tmp_path / "config.toml"
    destination.write_bytes(existing)

    result = _install(destination, VALID.encode())

    message = result["message"]
    assert isinstance(message, str)
    assert "could not be used" in message
    assert str(result["previous_config_kept_as"]) in message
    assert "supplied file was installed" in message
    assert ("browser" in message) is browser_note


def test_install_without_repair_has_no_repair_message(tmp_path: Path) -> None:
    result = _install(tmp_path / "config.toml", VALID.encode())

    assert "message" not in result


def test_kept_copy_is_durable_before_the_destination_is_replaced(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_bytes(b"damaged = [")
    events: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            events.append("directory synced")
        real_fsync(descriptor)

    def recording_write(path: Path, content: bytes, *, mode: int | None = None) -> None:
        events.append("destination replaced")
        write_bytes_atomic(path, content, mode=mode)

    with (
        patch("inky_bird_frame.cli.os.fsync", recording_fsync),
        patch("inky_bird_frame.cli.write_bytes_atomic", recording_write),
    ):
        _install(destination, VALID.encode())

    assert events[:2] == ["directory synced", "destination replaced"]


def test_failed_kept_copy_leaves_no_partial_file(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_bytes(b"damaged = [")

    def failing_fsync(descriptor: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    with (
        patch("sys.stdin", _stdin(VALID.encode())),
        patch("inky_bird_frame.cli.os.fsync", failing_fsync),
        redirect_stdout(io.StringIO()),
        pytest.raises(OSError, match="No space left"),
    ):
        config_install_command(build_parser().parse_args(_arguments(destination)))

    assert destination.read_bytes() == b"damaged = ["
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.toml"]


def test_repeated_repairs_keep_every_previous_copy(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    with patch("inky_bird_frame.cli.datetime") as clock:
        clock.now.return_value = moment
        destination.write_bytes(b"first = [")
        first = _install(destination, VALID.encode())
        destination.write_bytes(b"second = [")
        second = _install(destination, VALID.encode())

    assert first["previous_config_kept_as"] == "config.toml.invalid-20260102T030405Z"
    assert second["previous_config_kept_as"] == "config.toml.invalid-20260102T030405Z-1"
    assert (tmp_path / "config.toml.invalid-20260102T030405Z").read_bytes() == b"first = ["
    assert (tmp_path / "config.toml.invalid-20260102T030405Z-1").read_bytes() == b"second = ["


def test_invalid_candidate_does_not_touch_an_unreadable_destination(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_bytes(b"damaged = [")

    with (
        patch("sys.stdin", _stdin(b"also = [damaged")),
        redirect_stdout(io.StringIO()),
        pytest.raises(ConfigurationError, match="standard input"),
    ):
        config_install_command(build_parser().parse_args(_arguments(destination)))

    assert destination.read_bytes() == b"damaged = ["
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.toml"]


def test_app_owned_destination_is_preserved(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text(APP_OWNED)

    result = _install(destination, VALID.replace("radius_km = 8", "radius_km = 9").encode())

    assert destination.read_text(encoding="utf-8") == APP_OWNED
    assert result["installed"] is False
    assert result["preserved"] is True
    assert result["ownership"] == "app"
    assert "browser" in str(result["message"])
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.toml"]


def _validate(path: Path, environment: dict[str, str] | None = None) -> dict[str, object]:
    output = io.StringIO()
    with patch.dict("os.environ", environment or {}), redirect_stdout(output):
        assert main(["config", "validate", "--config", str(path)]) == 0
    payload = json.loads(output.getvalue())
    data = payload["data"]
    assert isinstance(data, dict)
    return data


def test_validate_reports_unknown_keys_and_stays_valid(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        VALID.replace("[controller]\n", "[controller]\ngeneration_enable = false\n")
        + "\n[notification]\nenabled = true\n"
    )

    result = _validate(path)

    assert result["valid"] is True
    assert result["unknown_keys"] == ["controller.generation_enable", "notification"]


def test_validate_reports_ownership_generation_and_directory_warnings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(APP_OWNED.replace('state_dir = "state"', 'state_dir = "workspace/state"'))

    result = _validate(path)

    assert result["unknown_keys"] == []
    assert result["configuration_ownership"] == "app"
    assert result["generation_enabled"] is False
    assert result["warnings"] == [
        {
            "code": "overlapping_directories",
            "settings": ["controller.workspace_dir", "controller.state_dir"],
            "message": "controller.workspace_dir and controller.state_dir overlap; "
            "keep the workspace, catalog and state directories separate.",
        }
    ]
    path.write_text(VALID)
    managed = _validate(path)
    assert managed["configuration_ownership"] == "managed"
    assert managed["generation_enabled"] is True
    assert managed["warnings"] == []


def test_validate_does_not_report_a_blank_environment_secret_as_configured(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID.replace("[discovery]\n", '[discovery]\nebird_api_key_env = "KEY"\n'))

    blank = _validate(path, {"KEY": "   "})["discovery"]
    present = _validate(path, {"KEY": "synthetic-key"})["discovery"]

    assert isinstance(blank, dict)
    assert isinstance(present, dict)
    assert blank["ebird_configured"] is False
    assert present["ebird_configured"] is True


def test_access_recover_prompts_on_stderr_so_stdout_stays_json(tmp_path: Path) -> None:
    args = build_parser().parse_args(["access", "recover", "--config", "instance.toml"])
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=["yes", "birder"]) as prompt,
        patch("getpass.getpass", side_effect=["synthetic pass", "synthetic pass"]),
        patch("inky_bird_frame.cli._config") as config,
        patch("inky_bird_frame.access.AccessStore"),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        config.return_value.controller.state_dir = tmp_path
        assert access_recover_command(args) == 0

    assert all(call.args == () for call in prompt.call_args_list)
    assert "Type yes to continue" in stderr.getvalue()
    assert json.loads(stdout.getvalue())["data"]["sessions_revoked"] is True


def test_notifications_test_names_the_destination_that_failed() -> None:
    result = {
        "delivered": ["kitchen"],
        "failures": [{"name": "phone", "error_type": "TimeoutError"}],
    }
    with (
        patch("inky_bird_frame.cli._config"),
        patch("inky_bird_frame.cli.send_notification_test", return_value=result),
        redirect_stdout(io.StringIO()),
        pytest.raises(ValueError, match=r"every configured destination: phone$"),
    ):
        notifications_test_command(
            build_parser().parse_args(["notifications", "test", "--config", "instance.toml"])
        )
