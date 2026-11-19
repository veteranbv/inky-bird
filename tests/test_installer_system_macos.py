"""The macOS controller installer's system-service mode, against the fakes.

Run as root with ``--service-account``, the installer keeps the code root's,
writes LaunchDaemons that run as the account it verified, and runs every
application step as that account through ``system_service.py run-as``. The
fakes model the account database, file owners, the system launchd domain and
the account's access to each program by real file modes, and record who ran
each call. The installer matrix fails and stops every step of these runs.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest
from test_installer_behaviour import MACOS_BASH, Host, run_contained
from test_installer_system import assert_outside_intact, identities, machine_state

SCRIPT = "install-controller.sh"
AGENTS = "serve refresh catalog-publish"


def system_host(tmp_path: Path, account: str = "inky-bird", uid: int = 2001) -> Host:
    host = Host(tmp_path, "Darwin", account)
    host.make_system(account, uid)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/keep").write_text("outside\n")
    return host


def run_system(
    host: Host, *options: str, uid: str = "0", system: bool = True, **extra: str
) -> subprocess.CompletedProcess[str]:
    script = host.script(SCRIPT)
    environment = host.environment(FAKE_AGENTS=AGENTS, FAKE_UID=uid, **extra)
    chosen = host.system_options() if system else ()
    return run_contained([MACOS_BASH, str(script), *chosen, *options], environment)


def daemons(host: Host, account: str) -> dict[str, dict[str, object]]:
    found = {}
    for path in sorted(host.agents.glob(f"com.{account}.*.plist")):
        found[path.name] = plistlib.loads(path.read_bytes())
    return found


def test_a_fresh_install_writes_launch_daemons_that_run_as_the_account(tmp_path: Path) -> None:
    host = system_host(tmp_path)
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    ran = identities(host)
    assert ran["upgrade-gate remove-stale-work"] == {"root"}
    for what in ("inky-bird", "upgrade-gate", "heredoc sync"):
        assert ran[what] == {"account"}, what
    for what in ("uv", "launchctl", "rsync", "chown", "heredoc render"):
        assert ran[what] == {"root"}, what
    jobs = daemons(host, "inky-bird")
    assert set(jobs) == {f"com.inky-bird.{label}.plist" for label in AGENTS.split()}
    for job in jobs.values():
        assert job["UserName"] == "inky-bird" and job["GroupName"] == "inky-bird"
    loaded = host.state_of("launchd")
    assert all(entry.get("domain") == "system" for entry in loaded.values())
    assert not list((host.home / "Library/LaunchAgents").glob("*.plist"))
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
    ("login", "can log in; give it the shell /usr/bin/false"),
    ("home", "must be"),
    ("root-writable", "can be changed by someone other than root"),
    ("source-writable", "can change the source checkout"),
    ("code-link", "must be a folder, not a link"),
    ("unreadable-code", "cannot read or search"),
    ("not-root", "Install system services as root"),
    ("root-without-account", "--install-root needs --service-account"),
    ("launch-agent", "is a LaunchAgent of"),
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
    extra: dict[str, str] = {}
    changes: dict[str, dict[str, object]] = {
        "root-uid": {"uid": 0},
        "administrator": {"groups": ["admin"]},
        "login": {"shell": "/bin/zsh"},
        "home": {"home": str(tmp_path / "elsewhere")},
    }
    if case == "name":
        host.account = "birds"
    elif case == "missing":
        host.account = "inky-bird-rehearsal"
    elif case in changes:
        accounts = host.state_of("accounts")
        for name, value in changes[case].items():
            accounts["inky-bird"][name] = value
        host.save_state("accounts", accounts)
    elif case == "root-writable":
        host.give(host.install_root.parent, "inky-bird")
    elif case == "source-writable":
        host.give(host.source / "src/inky_bird_frame", "inky-bird")
    elif case == "code-link":
        host.app.symlink_to(tmp_path / "outside")
    elif case == "unreadable-code":
        (host.app / ".runtimes").mkdir(parents=True)
        (host.app / ".runtimes").chmod(0o700)
    elif case == "not-root":
        uid = "1000"
    elif case == "root-without-account":
        uid = "1000"
        system = False
        options = ["--install-root", str(host.install_root)]
    elif case == "launch-agent":
        # The user who ran sudo has this installation's agents loaded.
        host.save_state("launchd", {"com.inky-bird.serve": {"generation": "old"}})
        extra = {"SUDO_UID": "501", "SUDO_USER": "owner"}
    before = machine_state(host)
    result = run_system(host, *options, uid=uid, system=system, **extra)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert len(output.splitlines()) == 1 and message in output, output
    assert output.rstrip().endswith("No changes made."), output
    assert machine_state(host) == before
    assert not [call for call in host.calls() if call[0] in ("uv", "as-account")]
    assert_outside_intact(host)


def test_root_without_the_option_names_the_account_of_installed_launch_daemons(
    tmp_path: Path,
) -> None:
    host = system_host(tmp_path)
    assert run_system(host).returncode == 0
    result = run_system(host, system=False)
    assert result.returncode == 1
    assert result.stderr == (
        "com.inky-bird.serve.plist is a system service of inky-bird. Run the installer "
        "again with --service-account inky-bird. No changes made.\n"
    )
    for plist in host.agents.glob("*.plist"):
        plist.unlink()
    result = run_system(host, system=False)
    assert result.stderr == (
        "Run the installer as the account that will own the controller, not as root. "
        "No changes made.\n"
    )


def test_the_user_installer_points_at_the_system_service(tmp_path: Path) -> None:
    host = system_host(tmp_path)
    assert run_system(host).returncode == 0
    result = run_contained(
        [MACOS_BASH, str(host.script(SCRIPT))], host.environment(FAKE_AGENTS=AGENTS)
    )
    assert result.returncode == 1
    assert result.stderr.startswith(
        "com.inky-bird.serve is a system service of inky-bird. Upgrade it as root: sudo "
    )
    assert result.stderr.endswith("--service-account inky-bird. No changes made.\n")


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
    assert "the agents it stopped were started again on the previous version" in result.stderr
    assert not [call for call in host.calls() if call[0] == "stopped-hook"]
    assert "com.inky-bird.serve" in host.state_of("launchd")


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
    host.give(runtime / "src", "inky-bird")
    result = run_system(host)
    assert result.returncode == 1
    assert f"inky-bird could change {runtime}/src; the upgrade did not switch " in result.stderr
    assert os.path.realpath(host.app / ".venv") == active


def assert_jobs_start_real_commands(host: Host, account: str) -> None:
    """Every program a LaunchDaemon starts exists, and its account can run it."""
    jobs = daemons(host, account)
    assert jobs
    for name, job in jobs.items():
        arguments = job["ProgramArguments"]
        assert isinstance(arguments, list)
        program = Path(arguments[0])
        assert program.is_file() and os.access(program, os.X_OK), (name, program)
        assert job["UserName"] == account


@pytest.mark.parametrize("account", ["inky-bird", "inky-bird-rehearsal"])
def test_every_job_starts_a_command_the_installed_runtime_has(tmp_path: Path, account: str) -> None:
    host = system_host(tmp_path, account)
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    assert_jobs_start_real_commands(host, account)
    runtime = Path(os.path.realpath(host.app / ".venv")).parent
    assert runtime.stat().st_mode & 0o777 == 0o755


def test_a_checkout_only_root_can_read_still_gives_the_account_a_readable_runtime(
    tmp_path: Path,
) -> None:
    host = system_host(tmp_path)
    (host.source / "src/inky_bird_frame/config.py").chmod(0o600)
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    runtime = Path(os.path.realpath(host.app / ".venv")).parent
    assert (runtime / "src/inky_bird_frame/config.py").stat().st_mode & 0o777 == 0o644


def test_a_program_the_account_cannot_reach_is_refused_by_run_as(tmp_path: Path) -> None:
    host = system_host(tmp_path)
    hidden = tmp_path / "hidden"
    (hidden / "bin").mkdir(parents=True)
    program = hidden / "bin/tool"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    hidden.chmod(0o700)
    helper = host.source / "src/inky_bird_frame/system_service.py"
    command = [str(host.bin / "python3"), "-I", str(helper), "run-as", "inky-bird", "--"]
    command += ["env", "-i", "PATH=/usr/bin:/bin", str(program)]
    environment = host.environment(FAKE_UID="0")
    denied = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert denied.returncode == 126 and "Permission denied" in denied.stderr
    hidden.chmod(0o755)
    allowed = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert allowed.returncode == 0, allowed.stderr


def test_two_installations_under_different_accounts_do_not_touch_each_other(
    tmp_path: Path,
) -> None:
    host = system_host(tmp_path)
    assert run_system(host).returncode == 0
    first_root = host.install_root
    assert first_root is not None
    first_jobs = {path.name: path.read_bytes() for path in host.agents.glob("*.plist")}
    first_app = sorted(str(path) for path in (first_root / "app").rglob("*"))
    # A rehearsal beside it, under its own account and root.
    host.make_system("inky-bird-rehearsal", 2002)
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    added = sorted({path.name for path in host.agents.glob("*.plist")} - set(first_jobs))
    assert added and all(name.startswith("com.inky-bird-rehearsal.") for name in added)
    result = run_system(host, "--stopped-hook", str(host.stopped_hook()))
    assert result.returncode == 0, result.stdout + result.stderr
    assert {name: (host.agents / name).read_bytes() for name in first_jobs} == first_jobs
    assert sorted(str(path) for path in (first_root / "app").rglob("*")) == first_app
    assert "com.inky-bird.serve" in host.state_of("launchd")
    assert_outside_intact(host)


def installer_program() -> str:
    """The Python program the macOS installer runs to write its jobs."""
    text = (Path(__file__).resolve().parents[1] / "deploy" / SCRIPT).read_text()
    start = text.index("<<'PY'\n", text.index("controller_program() {")) + len("<<'PY'\n")
    return text[start : text.index("\nPY\n", start) + 1]


@pytest.mark.parametrize("account", ["inky-bird", "inky-bird-rehearsal"])
def test_the_real_program_renders_launch_daemons_that_lint_and_run_the_command(
    tmp_path: Path, account: str
) -> None:
    """Rendering only: the installer's own program, as root would run it, writes
    LaunchDaemons for the account that plutil accepts, that run the installed
    command as the account, and that hold no secret."""
    from test_installation import write_config

    codex = tmp_path / "codex"
    codex.touch(mode=0o700)
    install_root = tmp_path / account
    (install_root / "config").mkdir(parents=True)
    config = write_config(install_root / "config", codex)
    jobs = tmp_path / "LaunchDaemons"
    jobs.mkdir()
    paths = [
        jobs / f"com.{account}.{label}.plist"
        for label in (
            "serve",
            "refresh",
            "generate",
            "catalog-publish",
            "notifications",
            "scheduler",
        )
    ]
    app = install_root / "app"
    # The installer names launchd's folder, where other installations' jobs are.
    environment = {
        **os.environ,
        "INKY_BIRD_SERVICE_PREFIX": account,
        "INKY_BIRD_JOBS_DIR": str(jobs),
    }
    arguments = [*map(str, paths), str(app), str(app), str(config), str(install_root / "logs")]
    arguments += ["true", "render", account, account, str(install_root / "config/private.env")]
    arguments += [str(install_root / "home"), str(os.getuid())]
    result = subprocess.run(
        [sys.executable, "-", *arguments],
        input=installer_program(),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    written = [path for path in paths if path.exists()]
    assert written
    for path in written:
        if Path("/usr/bin/plutil").exists():
            lint = subprocess.run(["/usr/bin/plutil", "-lint", str(path)], capture_output=True)
            assert lint.returncode == 0, lint.stdout
        job = plistlib.loads(path.read_bytes())
        assert path.stat().st_mode & 0o777 == 0o644
        assert job["Label"] == path.name.removesuffix(".plist")
        assert job["UserName"] == account and job["GroupName"] == account
        assert job["InitGroups"] is True
        assert job["ProgramArguments"][0] == str(app / ".venv/bin/inky-bird")
        variables = job["EnvironmentVariables"]
        assert variables["INKY_BIRD_ENV_FILE"] == str(install_root / "config/private.env")
        assert variables["HOME"] == str(install_root / "home")
        assert not {"SSL_CERT_FILE"} & set(variables)
        assert str(install_root / "logs") in job["StandardOutPath"]
    # Its data stays under its own root, so a second installation has its own locks.
    shared = f'state_dir = "{tmp_path}/shared"'
    config.write_text(config.read_text().replace('state_dir = "controller-state"', shared))
    refused = subprocess.run(
        [sys.executable, "-", *arguments],
        input=installer_program(),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0 and "state_dir must be inside" in refused.stderr


def test_the_interpreter_uv_downloads_is_code_the_account_cannot_change(tmp_path: Path) -> None:
    """uv leaves a lock file anyone can write beside a Python it downloads into
    the install root; the code check refused that, as a real host showed."""
    host = system_host(tmp_path)
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    assert host.install_root is not None
    python_dir = host.install_root / "app" / ".python"
    assert (python_dir / ".lock").is_file()
    for path in [python_dir, *python_dir.rglob("*")]:
        assert not path.lstat().st_mode & 0o022, path
    # An upgrade, with the interpreter already there, also succeeds.
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr


def test_root_s_work_folders_from_before_the_boot_are_removed_from_the_install_root(
    tmp_path: Path,
) -> None:
    host = system_host(tmp_path)
    assert host.install_root is not None
    assert run_system(host).returncode == 0
    stale = host.install_root / "install.stale"
    stale.mkdir()
    os.utime(stale, (0, 0))
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not stale.exists()
    assert not list(host.install_root.glob("install.*"))


def test_a_repair_of_a_config_the_installed_version_cannot_read_waits_for_writers(
    tmp_path: Path,
) -> None:
    from test_installer_behaviour import running_generation
    from test_installer_system import installed_as_old, system_lock_holders

    host = system_host(tmp_path)
    assert run_system(host).returncode == 0
    installed_as_old(host)
    host.fail("old:load_config")
    (host.fake / "log").write_text("")
    jobs = daemons(host, "inky-bird")
    launchd = host.state_of("launchd")
    with running_generation(host):
        result = run_system(host)
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stderr.rstrip().endswith("No changes made.")
    assert "A plate is still being made" in result.stderr
    assert system_lock_holders(host) == ["old", "new"]
    assert daemons(host, "inky-bird") == jobs
    assert host.state_of("launchd") == launchd
    assert host.installed_version() == "old"

    (host.fake / "log").write_text("")
    result = run_system(host)
    assert result.returncode == 0, result.stdout + result.stderr
    assert host.installed_version() == "new"
    # The publisher's check holds them a second time, again with the new version.
    assert system_lock_holders(host) == ["old", "new", "new"]
    assert identities(host)["upgrade-gate"] == {"account"}


def test_a_stop_at_the_health_check_puts_the_previous_daemon_back_while_launchd_stops_it(
    tmp_path: Path,
) -> None:
    from test_installer_behaviour import step_of
    from test_installer_system import installed_as_old

    probe = system_host(tmp_path / "probe")
    assert run_system(probe).returncode == 0
    (probe.fake / "steps").unlink()
    probe.fail("bootout-async")
    assert run_system(probe).returncode == 0
    at = step_of(probe, "venv-python", "check-serve")
    host = system_host(tmp_path / "run")
    assert run_system(host).returncode == 0
    installed_as_old(host)
    (host.fake / "steps").unlink()
    host.fail("bootout-async")
    (host.fake / "inject").write_text(f"{at} kill\n")
    result = run_system(host)
    assert result.returncode == 143, result.stdout + result.stderr
    assert "not running" not in result.stderr, result.stderr
    serve = host.state_of("launchd")["com.inky-bird.serve"]
    assert serve["runtime"] == "old" and "shutting_down" not in serve, serve
    assert host.installed_version() == "old"
