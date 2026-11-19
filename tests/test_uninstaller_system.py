"""The native controller uninstallers' system-service mode, against the fakes.

Run as root with ``--service-account``, an uninstaller stops and removes only
that account's own system services, with the writer locks held, removes the
code under the install root and keeps the account and all its data. The same
synthetic accounts, owners and service managers as the installers' tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import test_installer_system as linux
import test_installer_system_macos as macos
from test_installer_behaviour import MACOS_BASH, TIMER_UNITS, Host, run_contained

UNINSTALLERS = {"Linux": "uninstall-controller-systemd.sh", "Darwin": "uninstall-controller.sh"}
SYSTEMS = ["Linux", "Darwin"]


def installed(tmp_path: Path, system: str, account: str = "inky-bird", uid: int = 2001) -> Host:
    if system == "Linux":
        host = linux.system_host(tmp_path, account, uid)
    else:
        host = macos.system_host(tmp_path, account, uid)
    result = install(host)
    assert result.returncode == 0, result.stdout + result.stderr
    return host


def install(host: Host) -> subprocess.CompletedProcess[str]:
    if host.system == "Linux":
        return linux.run_system(host)
    return macos.run_system(host)


def uninstall(
    host: Host, *options: str, uid: str = "0", system: bool = True, **extra: str
) -> subprocess.CompletedProcess[str]:
    script = host.script(UNINSTALLERS[host.system])
    shell = "bash" if host.system == "Linux" else MACOS_BASH
    environment = host.environment(FAKE_UNITS=TIMER_UNITS, FAKE_UID=uid, **extra)
    chosen = host.system_options() if system else ()
    return run_contained([shell, str(script), *chosen, *options], environment)


def jobs(host: Host, account: str) -> list[str]:
    """The account's service definitions on the machine."""
    pattern = f"{account}-*" if host.system == "Linux" else f"com.{account}.*.plist"
    folder = host.units if host.system == "Linux" else host.agents
    return sorted(path.name for path in folder.glob(pattern))


def running(host: Host, account: str) -> set[str]:
    prefix = f"{account}-" if host.system == "Linux" else f"com.{account}."
    loaded = set(host.state_of("launchd")) if host.system == "Darwin" else set()
    active = {name for name, entry in host.state_of("systemd").items() if entry.get("active")}
    return {name for name in loaded | active if name.startswith(prefix)}


def code(host: Host) -> list[Path]:
    """The installed code; bytecode Python writes as it runs is not part of it."""
    return sorted(path for path in host.app.rglob("*") if "__pycache__" not in path.parts)


def kept(host: Host) -> dict[str, str]:
    """The account's data, which an uninstall keeps."""
    assert host.install_root is not None
    found = {}
    for name in ("config", "home", "state", "logs"):
        folder = host.install_root / name
        for path in sorted(folder.rglob("*")) if folder.is_dir() else ():
            # Holding the writer locks creates their empty files.
            if path.is_file() and path.suffix != ".lock":
                found[str(path)] = path.read_text(errors="replace")
        found[str(folder)] = "folder" if folder.is_dir() else "absent"
    return found


@pytest.mark.parametrize("system", SYSTEMS)
def test_an_uninstall_removes_the_code_and_jobs_and_keeps_the_account_s_data(
    tmp_path: Path, system: str
) -> None:
    host = installed(tmp_path, system)
    private = host.support / "private.env"
    private.write_text("GH_TOKEN=synthetic-environment-value\n")
    data = kept(host)
    (host.fake / "log").write_text("")
    result = uninstall(host)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert not jobs(host, "inky-bird") and not running(host, "inky-bird")
    assert not host.app.exists()
    assert kept(host) == data
    assert "synthetic-environment-value" not in output
    assert "Intentionally left in place:" in output
    assert "The account inky-bird" in output
    # The locks were held, as the account, before the code went.
    held = [
        call
        for call in host.calls()
        if call[:2] == ["as", "account"] and "hold-writer-locks" in call
    ]
    assert held
    # Everything it removed was inside the install root or the service manager's folder.
    linux.assert_outside_intact(host)
    # A second run finds nothing to do; a reinstall finds the kept data.
    again = uninstall(host)
    assert again.returncode == 0, again.stdout + again.stderr
    result = install(host)
    assert result.returncode == 0, result.stdout + result.stderr
    assert private.read_text() == "GH_TOKEN=synthetic-environment-value\n"


@pytest.mark.parametrize("system", SYSTEMS)
def test_uninstalling_one_account_leaves_the_other_intact_and_running(
    tmp_path: Path, system: str
) -> None:
    host = installed(tmp_path, system)
    first_root = host.install_root
    assert first_root is not None
    first_jobs = jobs(host, "inky-bird")
    first_running = running(host, "inky-bird")
    first_app = sorted(str(path) for path in (first_root / "app").rglob("*"))
    host.make_system("inky-bird-rehearsal", 2002)
    assert install(host).returncode == 0
    result = uninstall(host)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not jobs(host, "inky-bird-rehearsal")
    assert jobs(host, "inky-bird") == first_jobs
    assert running(host, "inky-bird") == first_running and first_running
    assert sorted(str(path) for path in (first_root / "app").rglob("*")) == first_app


REFUSALS = [
    ("not-root", "Remove system services as root"),
    ("root-without-account", "--install-root needs --service-account"),
    ("home", "must be"),
    ("missing", "does not exist"),
    ("user-units", "was installed by the user installer"),
    ("busy", "A plate is still being made. Run the uninstaller again after it finishes."),
    # The lock helper cannot load the config, so it holds nothing: a writer
    # still running with its old config could be stopped mid-write.
    ("unreadable-config", "The installed version cannot read its configuration"),
]


@pytest.mark.parametrize("system", SYSTEMS)
@pytest.mark.parametrize(("case", "message"), REFUSALS)
def test_each_refusal_is_one_line_and_changes_nothing(
    tmp_path: Path, system: str, case: str, message: str
) -> None:
    host = installed(tmp_path, system)
    assert host.install_root is not None
    options: list[str] = []
    uid, as_system = "0", True
    if case == "not-root":
        uid = "1000"
    elif case == "root-without-account":
        uid, as_system = "1000", False
        options = ["--install-root", str(host.install_root)]
    elif case == "home":
        options = ["--install-root", str(tmp_path / "elsewhere")]
    elif case == "missing":
        host.account = "inky-bird-rehearsal"
    elif case == "user-units":
        if system == "Darwin":
            pytest.skip("a LaunchDaemon is never the user installer's")
        for unit in host.units.iterdir():
            unit.write_text(unit.read_text().replace("# Inky Bird system service\n", ""))
    elif case == "unreadable-config":
        # The fake configuration loader of the installed version rejects it.
        (tag_file,) = host.app.rglob("runtime-tag")
        host.fail(f"{tag_file.read_text().strip()}:load_config")
    before = (jobs(host, "inky-bird"), code(host), host.state_of("launchd"))
    systemd = host.state_of("systemd")
    if case == "busy":
        from test_installer_behaviour import running_generation

        with running_generation(host):
            result = uninstall(host, *options, uid=uid, system=as_system)
    else:
        result = uninstall(host, *options, uid=uid, system=as_system)
    output = result.stdout + result.stderr
    assert result.returncode in (1, 3), output
    # The lock helper says what it waited for; the refusal is the last line.
    lines = output.splitlines()
    single = case not in ("busy", "unreadable-config")
    assert message in lines[-1] and (not single or len(lines) == 1), output
    assert output.rstrip().endswith("No changes made."), output
    assert (
        jobs(host, "inky-bird"),
        code(host),
        host.state_of("launchd"),
    ) == before
    assert host.state_of("systemd") == systemd


@pytest.mark.parametrize("system", SYSTEMS)
def test_a_job_that_will_not_stop_keeps_the_code_and_a_rerun_finishes(
    tmp_path: Path, system: str
) -> None:
    host = installed(tmp_path, system)
    if system == "Linux":
        host.fail("systemctl:disable:inky-bird-controller.service")
    else:
        host.fail("bootout:com.inky-bird.serve")
    result = uninstall(host)
    assert result.returncode == 1
    assert "Uninstall did not finish. Could not stop or remove:" in result.stderr
    assert host.app.is_dir()
    host.fail()
    result = uninstall(host)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not jobs(host, "inky-bird") and not host.app.exists()


def test_printed_removal_commands_quote_an_install_root_with_a_space_and_a_quote(
    tmp_path: Path,
) -> None:
    import shlex

    for system in SYSTEMS:
        host = installed(tmp_path / system, system)
        # Move the installation to a root with a space and a quote in it: the
        # fakes' web service cannot start from one, but an uninstall can run there.
        assert host.install_root is not None
        old = str(host.install_root)
        new = host.install_root.parent / "it's here" / "inky-bird"
        new.parent.mkdir()
        host.install_root.rename(new)
        accounts = host.state_of("accounts")
        accounts["inky-bird"]["home"] = str(new / "home")
        host.save_state("accounts", accounts)
        owners = host.state_of("owners")
        host.save_state("owners", {key.replace(old, str(new)): v for key, v in owners.items()})
        host.install_root, host.app = new, new / "app"
        result = uninstall(host)
        assert result.returncode == 0, result.stdout + result.stderr
        (line,) = [line for line in result.stdout.splitlines() if "sudo rm -R" in line]
        command = line.split("remove them with: ", 1)[1]
        words = shlex.split(command)
        assert words[:3] == ["sudo", "rm", "-R"] and words[3] == str(host.install_root)
        refused = uninstall(host, uid="1000")
        script = shlex.split(refused.stderr.split("as root: ", 1)[1])[1]
        assert script == str(host.script(UNINSTALLERS[system]))


def test_the_group_is_named_for_deletion_only_when_it_is_the_account_s_own(
    tmp_path: Path,
) -> None:
    host = installed(tmp_path, "Darwin")
    accounts = host.state_of("accounts")
    shared = dict(accounts["inky-bird"], home=str(tmp_path / "other"))
    accounts["inky-bird-other"] = shared
    host.save_state("accounts", accounts)
    result = uninstall(host)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "/Groups/" not in result.stdout
    assert "The group inky-bird is not the account's own, so it is left alone." in result.stdout
    del accounts["inky-bird-other"]
    host.save_state("accounts", accounts)
    again = uninstall(host)
    assert "sudo dscl . -delete /Groups/inky-bird" in again.stdout


def test_the_macos_uninstaller_run_as_root_without_an_account_refuses(tmp_path: Path) -> None:
    """As root, only a system service can be removed: it names the account of
    one that is installed, and otherwise says to run as the owning user."""
    host = installed(tmp_path, "Darwin")
    before = (jobs(host, "inky-bird"), code(host), host.state_of("launchd"))
    result = uninstall(host, system=False)
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "com.inky-bird.serve.plist is a system service of inky-bird. Run the uninstaller "
        "again with --service-account inky-bird. No changes made."
    )
    for daemon in host.agents.glob("com.inky-bird.*.plist"):
        daemon.unlink()
    result = uninstall(host, system=False)
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "Run the uninstaller as the account that owns the controller, not as root. No changes made."
    )
    assert code(host) == before[1] and host.state_of("launchd") == before[2]


@pytest.mark.parametrize("uid", ["0", "1000"])
def test_the_linux_user_uninstaller_leaves_a_system_service_of_the_same_name(
    tmp_path: Path, uid: str
) -> None:
    host = installed(tmp_path, "Linux")
    before = (jobs(host, "inky-bird"), code(host), host.state_of("systemd"))
    result = uninstall(host, uid=uid, system=False)
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "inky-bird-controller.service is a system service of inky-bird. Run the uninstaller "
        "as root with --service-account inky-bird. No changes made."
    )
    assert (jobs(host, "inky-bird"), code(host), host.state_of("systemd")) == before
