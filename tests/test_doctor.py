"""Doctor reports what an owner must act on without blocking readiness wrongly."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from inky_bird_frame.config import AppConfig, load_config
from inky_bird_frame.installation import (
    CheckStatus,
    CommandResult,
    DiagnosticCheck,
    InstallationRole,
    _launchd_checks,
    doctor,
)
from inky_bird_frame.native_names import NativeLayout

PASSING = DiagnosticCheck("mock", CheckStatus.PASS, "ready")

CONFIG = """
[discovery]
zip_code = "12345"
radius_km = 8
species_limit = 50
window = "last-30-days"
{discovery}

[controller]
workspace_dir = "workspace"
catalog_dir = "catalog"
state_dir = "controller-state"
codex_path = "{codex}"
bind_host = "0.0.0.0"
port = 8793
references_per_species = 4
generations_per_cycle = 1
max_generation_attempts = 3

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display-state"
{tail}
"""

NOTIFICATIONS = """
[notifications]
enabled = true

[[notifications.destinations]]
name = "synthetic"
url = "json://example.test/hook"
events = ["terminal_error"]
"""


@pytest.fixture(autouse=True)
def isolated_native_identity(tmp_path: Path) -> Iterator[None]:
    layout = NativeLayout("inky-bird", tmp_path, tmp_path, tmp_path / "config.toml")
    with patch("inky_bird_frame.installation.resolve_native_layout", return_value=layout):
        yield


def _write(tmp_path: Path, *, discovery: str = "", tail: str = "") -> Path:
    codex = tmp_path / "codex"
    codex.touch(mode=0o700)
    (tmp_path / "workspace").mkdir(exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(codex=codex, discovery=discovery, tail=tail))
    path.chmod(0o600)
    return path


def _doctor(path: Path) -> list[DiagnosticCheck]:
    with (
        patch("inky_bird_frame.installation.platform.system", return_value="Darwin"),
        patch("inky_bird_frame.installation._codex_auth_check", return_value=PASSING),
        patch("inky_bird_frame.installation._launchd_checks", return_value=[PASSING]),
        patch("inky_bird_frame.installation._controller_health_check", return_value=PASSING),
    ):
        report = doctor(InstallationRole.CONTROLLER, path)
    assert report.ready, [check.as_dict() for check in report.checks]
    return list(report.checks)


def test_unknown_keys_warn_with_paths_only_and_keep_readiness(tmp_path: Path) -> None:
    path = _write(tmp_path, discovery='zip_kode = "secret-looking-value"')
    path.write_text(path.read_text() + '\n[mystery]\nvalue = "another-private-value"\n')
    checks = _doctor(path)
    check = next(item for item in checks if item.check_id == "config_unknown_keys")
    assert check.status is CheckStatus.WARN
    rendered = str(check.as_dict())
    assert "discovery.zip_kode" in rendered
    assert "mystery" in rendered
    assert "secret-looking-value" not in rendered
    assert "another-private-value" not in rendered


def test_known_keys_report_no_unknown_key_check(tmp_path: Path) -> None:
    checks = _doctor(_write(tmp_path))
    assert "config_unknown_keys" not in {check.check_id for check in checks}


@pytest.mark.parametrize("count", [0, 2])
def test_notification_dead_letters_warn_without_blocking_readiness(
    tmp_path: Path, count: int
) -> None:
    path = _write(tmp_path, tail=NOTIFICATIONS)
    state = tmp_path / "controller-state" / "notifications.json"
    state.parent.mkdir()
    state.write_text("{}")
    status = {"enabled": True, "pending": 0, "dead_letters": count}
    with patch("inky_bird_frame.installation.notification_status", return_value=status) as read:
        checks = _doctor(path)
    read.assert_called_once()
    check = next(item for item in checks if item.check_id == "notification_dead_letters")
    if count:
        assert check.status is CheckStatus.WARN
        assert "2" in check.summary
        assert "notifications retry" in (check.remediation or "")
    else:
        assert check.status is CheckStatus.PASS


def test_notification_check_does_not_create_state(tmp_path: Path) -> None:
    path = _write(tmp_path, tail=NOTIFICATIONS)
    checks = _doctor(path)
    check = next(item for item in checks if item.check_id == "notification_dead_letters")
    assert check.status is CheckStatus.PASS
    assert not (tmp_path / "controller-state").exists()


def test_disabled_notifications_skip_the_dead_letter_check(tmp_path: Path) -> None:
    checks = _doctor(_write(tmp_path))
    assert "notification_dead_letters" not in {check.check_id for check in checks}


LAUNCHCTL_PRINT = """gui/501/com.inky-bird.{job} = {{
\tactive count = 0
\tpath = /Users/example/Library/LaunchAgents/com.inky-bird.{job}.plist
\ttype = LaunchAgent
\tstate = not running
\truns = {runs}
{last_exit}
}}"""


def _managed_config(tmp_path: Path) -> AppConfig:
    config = load_config(_write(tmp_path))
    return replace(config, controller=replace(config.controller, generation_enabled=True))


def _launchd(tmp_path: Path, exits: dict[str, str]) -> dict[str, DiagnosticCheck]:
    def launchctl(command: list[str], **_kwargs: object) -> CommandResult:
        job = command[-1].rsplit(".", 1)[-1]
        last_exit = exits.get(job, "\tlast exit code = 0")
        runs = "0" if "never exited" in last_exit else "4"
        return CommandResult(0, LAUNCHCTL_PRINT.format(job=job, runs=runs, last_exit=last_exit), "")

    with (
        patch(
            "inky_bird_frame.installation._launchd_target",
            side_effect=lambda label, uid: (f"gui/{uid}/{label}", "LaunchAgent"),
        ),
        patch("inky_bird_frame.installation._run", side_effect=launchctl),
    ):
        return {check.check_id: check for check in _launchd_checks(_managed_config(tmp_path))}


def test_launchd_doctor_reports_a_failed_last_run(tmp_path: Path) -> None:
    checks = _launchd(tmp_path, {"generate": "\tlast exit code = 78: EX_CONFIG"})
    assert checks["service_generate"].status is CheckStatus.PASS
    assert checks["job_generate"].status is CheckStatus.FAIL
    assert "exit code 78" in (checks["job_generate"].detail or "")
    assert checks["job_refresh"].status is CheckStatus.PASS


def test_launchd_doctor_reports_a_signalled_last_run(tmp_path: Path) -> None:
    checks = _launchd(
        tmp_path,
        {"refresh": "\tlast exit code = (never exited)\n\tlast terminating signal = Killed: 9"},
    )
    assert checks["job_refresh"].status is CheckStatus.FAIL


def test_launchd_doctor_accepts_a_job_that_has_not_run_yet(tmp_path: Path) -> None:
    checks = _launchd(tmp_path, {"generate": "\tlast exit code = (never exited)"})
    assert checks["job_generate"].status is CheckStatus.PASS
    assert "serve" not in {name.removeprefix("job_") for name in checks if name.startswith("job_")}
