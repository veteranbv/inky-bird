"""The Linux controller installer's system-service mode, against the fakes.

Run as root with ``--service-account``, the installer verifies an account the
administrator made, keeps the code root's own and runs everything that reads
the account's data or runs the application as that account. The fakes model
the account database, file owners (``chown``), ``setpriv`` and ``pgrep``, and
record who ran each call, so a call made as the wrong identity, or a removal
outside the install root, shows. The installer matrix fails and stops every
step of these runs; these tests cover each refusal and hostile layout.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from test_installer_behaviour import TIMER_UNITS, Host, fake_tools, run_contained

SCRIPT = "install-controller-systemd.sh"
MARKER = "# Inky Bird system service"


def system_host(tmp_path: Path, account: str = "inky-bird", uid: int = 2001) -> Host:
    host = Host(tmp_path, "Linux", account)
    host.make_system(account, uid)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/keep").write_text("outside\n")
    return host


def run_system(
    host: Host, *options: str, uid: str = "0", system: bool = True
) -> subprocess.CompletedProcess[str]:
    script = host.script(SCRIPT)
    environment = host.environment(FAKE_UNITS=TIMER_UNITS, FAKE_UID=uid)
    chosen = host.system_options() if system else ()
    result = run_contained(["bash", str(script), *chosen, *options], environment)
    # Cycles the timers started run to their end before anything else happens.
    processes = host.state_of("processes")
    host.save_state(
        "processes",
        {name: value for name, value in processes.items() if not fake_tools._oneshot(name)},
    )
    host.save_state("busy", {})
    return result


def identities(host: Host) -> dict[str, set[str]]:
    """Who ran each kind of call."""
    found: dict[str, set[str]] = {}
    for call in host.calls():
        if call[0] == "as":
            what = call[2] if call[2] != "heredoc" else f"heredoc {call[3]}"
            found.setdefault(what, set()).add(call[1])
    return found


def machine_state(host: Host) -> tuple[object, ...]:
    """What a refusal must leave as it was."""
    units = sorted(path.name for path in host.units.iterdir())
    app = sorted(str(path) for path in host.app.rglob("*")) if host.app.is_dir() else None
    return units, app, host.state_of("systemd"), host.state_of("owners")


def assert_outside_intact(host: Host) -> None:
    assert (host.root / "outside/keep").read_text() == "outside\n"


def test_a_fresh_install_keeps_the_code_root_s_and_runs_the_application_as_the_account(
    tmp_path: Path,
) -> None:
    host = system_host(tmp_path)
    assert host.install_root is not None
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    ran = identities(host)
    for what in ("inky-bird", "upgrade-gate", "heredoc sync"):
        assert ran[what] == {"account"}, what
    for what in ("uv", "systemctl", "rsync", "chown", "heredoc render"):
        assert ran[what] == {"root"}, what
    # As root, nothing asks for sudo.
    assert not [call for call in host.calls() if call[0] == "sudo"]
    service = (host.units / "inky-bird-controller.service").read_text()
    assert service.startswith(f"{MARKER}\n")
    assert "User=inky-bird\nExecStart=" in service and "Group=inky-bird" in service
    assert f"INKY_BIRD_ENV_FILE={host.support}/private.env" in service
    private = host.support / "private.env"
    assert private.is_file() and private.stat().st_mode & 0o077 == 0
    owners = host.state_of("owners")
    for name in ("config", "home", "state", "logs"):
        assert str(host.install_root / name) in owners, name
    assert not [path for path in owners if path.startswith(f"{host.app}/")]
    assert os.path.realpath(host.app / ".venv").startswith(f"{host.app}/.runtimes/")
    assert_outside_intact(host)


def test_an_existing_private_environment_file_is_never_rewritten(tmp_path: Path) -> None:
    host = system_host(tmp_path)
    private = host.support / "private.env"
    private.write_text("GH_TOKEN=synthetic-environment-value\n")
    private.chmod(0o600)
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    assert private.read_text() == "GH_TOKEN=synthetic-environment-value\n"
    assert "synthetic-environment-value" not in result.stdout + result.stderr


REFUSALS = [
    ("name", "--service-account must be inky-bird"),
    ("missing", "does not exist; create it"),
    ("root-uid", "has root's user or group ID"),
    ("administrator", "is in an administrator group"),
    ("login", "can log in; give it the shell /usr/sbin/nologin"),
    ("home", "must be"),
    ("relative-root", "--install-root must be an absolute path"),
    ("root-writable", "can be changed by someone other than root"),
    ("root-link", "can be changed by someone other than root"),
    ("source-writable", "can change the source checkout"),
    ("code-link", "must be a folder, not a link"),
    ("runtimes-link", "must be a folder, not a link"),
    ("data-not-the-account-s", "must be a folder, not a link"),
    ("not-root", "Install system services as root"),
    ("root-without-account", "--install-root needs --service-account"),
]


@pytest.mark.parametrize(("case", "message"), REFUSALS)
def test_each_refusal_is_one_line_and_changes_nothing(
    tmp_path: Path, case: str, message: str
) -> None:
    host = system_host(tmp_path)
    assert host.install_root is not None
    options: list[str] = []
    uid = "0"
    system = True
    if case == "name":
        host.account = "birds"
    elif case == "missing":
        host.account = "inky-bird-qa"
    elif case in ("root-uid", "administrator", "login", "home"):
        changes: dict[str, dict[str, object]] = {
            "root-uid": {"uid": 0},
            "administrator": {"groups": ["sudo"]},
            "login": {"shell": "/bin/bash"},
            "home": {"home": str(tmp_path / "elsewhere")},
        }
        accounts = host.state_of("accounts")
        for name, value in changes[case].items():
            accounts["inky-bird"][name] = value
        host.save_state("accounts", accounts)
    elif case == "relative-root":
        options = ["--install-root", "srv/inky-bird"]
    elif case == "root-writable":
        host.give(host.install_root.parent, "inky-bird")
    elif case == "root-link":
        # The root resolves to a folder the account owns.
        real = tmp_path / "account-root"
        real.mkdir()
        host.give(real, "inky-bird")
        (host.install_root / "config").rename(real / "config")
        host.install_root.rmdir()
        host.install_root.symlink_to(real)
    elif case == "source-writable":
        host.give(host.source / "src/inky_bird_frame", "inky-bird")
    elif case == "code-link":
        host.app.symlink_to(tmp_path / "outside")
    elif case == "runtimes-link":
        host.app.mkdir()
        (host.app / ".runtimes").symlink_to(tmp_path / "outside")
    elif case == "data-not-the-account-s":
        (host.install_root / "state").mkdir()
        (host.install_root / "state/root-s").write_text("")
    elif case == "not-root":
        uid = "1000"
    elif case == "root-without-account":
        uid = "1000"
        system = False
        options = ["--install-root", str(host.install_root)]
    before = machine_state(host)
    result = run_system(host, *options, uid=uid, system=system)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert len(output.splitlines()) == 1 and message in output, output
    assert output.rstrip().endswith("No changes made."), output
    assert machine_state(host) == before
    assert not [call for call in host.calls() if call[0] in ("systemctl", "uv", "as-account")]
    assert_outside_intact(host)


def test_root_without_the_option_names_the_account_of_installed_system_services(
    tmp_path: Path,
) -> None:
    host = system_host(tmp_path)
    assert run_system(host).returncode == 0
    result = run_system(host, system=False)
    assert result.returncode == 1
    assert result.stderr == (
        "inky-bird-controller.service is a system service of inky-bird. Run the installer "
        "again with --service-account inky-bird. No changes made.\n"
    )
    # Without system services, root is refused as before.
    for unit in host.units.iterdir():
        unit.unlink()
    result = run_system(host, system=False)
    assert result.stderr == (
        "Run the installer as the account that will own the controller, not as root. "
        "No changes made.\n"
    )


def test_each_installer_points_at_the_other_s_services(tmp_path: Path) -> None:
    host = system_host(tmp_path)
    assert run_system(host).returncode == 0
    result = run_contained(
        ["bash", str(host.script(SCRIPT))], host.environment(FAKE_UNITS=TIMER_UNITS)
    )
    assert result.returncode == 1
    assert result.stderr.startswith(
        "inky-bird-controller.service is a system service of inky-bird. Upgrade it as root: sudo "
    )
    assert result.stderr.endswith("--service-account inky-bird. No changes made.\n")
    # Units the user installer made lack the system service's first line.
    for unit in host.units.iterdir():
        unit.write_text(unit.read_text().replace(f"{MARKER}\n", ""))
    before = machine_state(host)
    result = run_system(host)
    assert result.returncode == 1
    assert "was installed by the user installer for inky-bird; this installer does not move" in (
        result.stderr
    )
    assert machine_state(host) == before


def test_account_processes_left_after_the_stop_fail_the_upgrade_before_the_hook(
    tmp_path: Path,
) -> None:
    host = system_host(tmp_path)
    assert run_system(host).returncode == 0
    (host.fake / "log").write_text("")
    hook = host.stopped_hook()
    host.fail("account-process")
    result = run_system(host, "--stopped-hook", str(hook))
    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        "Processes of inky-bird still run after its services stopped; the upgrade did not switch "
        "to the new version." in result.stderr
    )
    assert "the services it stopped were started again on the previous version" in result.stderr
    assert not [call for call in host.calls() if call[0] == "stopped-hook"]
    assert host.state_of("systemd")["inky-bird-controller.service"]["active"] is True


def test_the_hook_runs_as_root_and_a_failed_health_check_switches_back(tmp_path: Path) -> None:
    host = system_host(tmp_path)
    assert run_system(host).returncode == 0
    active = os.path.realpath(host.app / ".venv")
    (host.fake / "log").write_text("")
    host.fail("health-fail")
    result = run_system(host, "--stopped-hook", str(host.stopped_hook()))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "its web service failed its health check" in result.stderr
    assert os.path.realpath(host.app / ".venv") == active
    ran = identities(host)
    assert ran["stopped-hook"] == {"root"}
    assert ran["upgrade-gate"] == {"account"}


def test_a_link_where_root_removes_takes_only_the_link(tmp_path: Path) -> None:
    """An older runtime that is a link out of the install root, and a left
    over link swap: root removes each link, never what it names."""
    host = system_host(tmp_path)
    assert run_system(host).returncode == 0
    runtimes = host.app / ".runtimes"
    (runtimes / "runtime.planted").symlink_to(tmp_path / "outside")
    (host.app / ".venv.next").symlink_to(tmp_path / "outside")
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (runtimes / "runtime.planted").is_symlink()
    assert not (host.app / ".venv.next").is_symlink()
    assert_outside_intact(host)


def test_code_the_account_could_change_stops_the_upgrade_before_the_switch(
    tmp_path: Path,
) -> None:
    host = system_host(tmp_path)
    assert run_system(host).returncode == 0
    active = os.path.realpath(host.app / ".venv")
    runtime = Path(active).parent
    # An earlier runtime the account was given after it was installed.
    host.give(runtime / "src", "inky-bird")
    result = run_system(host)
    assert result.returncode == 1
    assert f"inky-bird could change {runtime}/src; the upgrade did not switch " in result.stderr
    assert os.path.realpath(host.app / ".venv") == active
    assert "The upgrade failed; the services it stopped were started again on the previous" in (
        result.stderr
    )


def test_two_installations_under_different_accounts_do_not_touch_each_other(
    tmp_path: Path,
) -> None:
    host = system_host(tmp_path)
    assert run_system(host).returncode == 0
    first_root = host.install_root
    assert first_root is not None
    first_units = {path.name: path.read_text() for path in host.units.iterdir()}
    first_app = sorted(str(path) for path in (first_root / "app").rglob("*"))
    # A rehearsal beside it, under its own account and root.
    host.make_system("inky-bird-qa", 2002)
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    added = sorted({path.name for path in host.units.iterdir()} - set(first_units))
    assert added and all(name.startswith("inky-bird-qa-") for name in added)
    assert "User=inky-bird-qa\n" in (host.units / "inky-bird-qa-controller.service").read_text()
    # Its presence check counts only its own account's processes.
    result = run_system(host, "--stopped-hook", str(host.stopped_hook()))
    assert result.returncode == 0, result.stdout + result.stderr
    assert {name: (host.units / name).read_text() for name in first_units} == first_units
    assert sorted(str(path) for path in (first_root / "app").rglob("*")) == first_app
    assert host.state_of("systemd")["inky-bird-controller.service"]["active"] is True
    assert_outside_intact(host)


def test_helpers_run_as_the_account_lead_their_own_group_and_are_stopped_by_it(
    tmp_path: Path,
) -> None:
    """The lock holder and the health check, run through setpriv, keep the
    process ID the installer started, which leads its group; one that ignores
    a polite stop is still stopped, by its group, within the bound."""
    host = system_host(tmp_path)
    assert run_system(host).returncode == 0
    (host.fake / "log").write_text("")
    result = run_system(host, "--stopped-hook", str(host.stopped_hook()))
    assert result.returncode == 0, result.stdout + result.stderr
    background = [
        call
        for call in host.calls()
        if call[0] == "as-account" and ("hold-writer-locks" in call or "check-serve" in call)
    ]
    # Three holds (before the first stop, around the web service's stop, for
    # the hook) and the health check.
    assert len(background) == 4 and all(call[-1] == "leader" for call in background)
    host.fail("holder-ignores-term", "new:refresh")
    started = time.monotonic()
    result = run_system(host, "--stopped-hook", str(host.stopped_hook()))
    assert time.monotonic() - started < 90
    assert result.returncode == 1
    assert host.state_of("systemd")["inky-bird-controller.service"]["active"] is True
    listing = subprocess.run(
        ["/bin/ps", "-ww", "-ax", "-o", "command="], capture_output=True, text=True, check=True
    ).stdout
    assert str(tmp_path) not in listing


def assert_units_start_real_commands(host: Host, account: str) -> None:
    """Every program a unit starts exists and runs: the account-named units
    start the installed command, not one named after the account."""
    started = 0
    for unit in host.units.glob(f"{account}-*.service"):
        for line in unit.read_text().splitlines():
            if line.startswith("ExecStart="):
                program = Path(line.partition("=")[2].split()[0].strip('"'))
                assert program.is_file() and os.access(program, os.X_OK), (unit.name, program)
                started += 1
    assert started


@pytest.mark.parametrize("account", ["inky-bird", "inky-bird-qa"])
def test_every_unit_starts_a_command_the_installed_runtime_has(
    tmp_path: Path, account: str
) -> None:
    host = system_host(tmp_path, account)
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    assert_units_start_real_commands(host, account)


def test_the_staged_runtime_is_readable_by_the_account_and_root_s_alone_to_change(
    tmp_path: Path,
) -> None:
    """mktemp makes a folder 0700: the installer opens the new runtime to the
    account (0755) before the account checks the configuration with it."""
    host = system_host(tmp_path)
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    runtime = Path(os.path.realpath(host.app / ".venv")).parent
    assert runtime.stat().st_mode & 0o777 == 0o755


def test_code_folders_the_account_cannot_read_are_refused_before_any_change(
    tmp_path: Path,
) -> None:
    host = system_host(tmp_path)
    (host.app / ".runtimes").mkdir(parents=True)
    (host.app / ".runtimes").chmod(0o700)
    before = machine_state(host)
    result = run_system(host)
    assert result.returncode == 1
    assert result.stderr.endswith(
        f"inky-bird cannot read or search {host.app}/.runtimes; give its folders mode 0755 "
        "(chmod go+rX). No changes made.\n"
    )
    assert machine_state(host) == before


def test_a_program_the_account_cannot_reach_is_refused_by_setpriv(tmp_path: Path) -> None:
    """The fake setpriv runs only what the account could reach by real modes."""
    host = system_host(tmp_path)
    hidden = tmp_path / "hidden"
    (hidden / "bin").mkdir(parents=True)
    program = hidden / "bin/tool"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    hidden.chmod(0o700)
    environment = host.environment(FAKE_UID="0")
    command = [
        str(host.bin / "setpriv"),
        "--reuid=2001",
        "--regid=2001",
        "--init-groups",
        "--",
        "env",
        "-i",
        "PATH=/usr/bin:/bin",
        str(program),
    ]
    denied = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert denied.returncode == 126 and "Permission denied" in denied.stderr
    hidden.chmod(0o755)
    allowed = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert allowed.returncode == 0, allowed.stderr


def test_a_checkout_only_root_can_read_still_gives_the_account_a_readable_runtime(
    tmp_path: Path,
) -> None:
    """A root shell's umask of 077 leaves the checkout's files 0600; the copies
    the account reads are made 0644 (folders 0755), and root's."""
    host = system_host(tmp_path)
    private = host.source / "src/inky_bird_frame/config.py"
    private.chmod(0o600)
    (host.source / "catalog").chmod(0o700)
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    runtime = Path(os.path.realpath(host.app / ".venv")).parent
    assert (runtime / "src/inky_bird_frame/config.py").stat().st_mode & 0o777 == 0o644
    assert (host.app / "catalog").stat().st_mode & 0o777 == 0o755
