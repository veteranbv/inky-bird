"""Fail or stop every native installer at every step and check what it leaves.

A recording run of each scenario numbers every external call the installer
makes: the synthetic service managers, sudo, uv, pip, rsync, ssh, the Python
helpers, the inky-bird command and the file commands that build, switch and
remove runtimes. Each case then starts from the same machine state, makes the
call with one number fail (for sudo also: every later sudo call), or stops the
installer with SIGTERM once that call completes, and checks the invariants in
``check_install`` and ``check_uninstall``. Status queries are numbered but
never failed: a failed query is indistinguishable from a different answer.

The default run executes every case for the macOS and both Linux controller
upgrades from the single-directory layout and for the display upgrade on a Pi
that also runs the controller, plus every eighth step of every other scenario
and start state. Run every case with::

    INKY_INSTALLER_MATRIX=full uv run pytest tests/test_installer_matrix.py -q

Cases run one at a time, each group in a fixed directory, so the absolute
paths an installation records stay valid when its starting state is copied
back before every case. Every run has its own process group and a time limit;
nothing it starts may outlive it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from test_installer_behaviour import (
    MACOS_BASH,
    ROOT,
    Host,
    fake_tools,
    run_contained,
    running_generation,
)

FULL = os.environ.get("INKY_INSTALLER_MATRIX") == "full"
# Every eighth step of every group, for a quick pass across all of them.
SAMPLE = os.environ.get("INKY_INSTALLER_MATRIX") == "sample"
# Narrow a run to some groups while working on one, e.g. "install:macos:single".
ONLY = set(filter(None, os.environ.get("INKY_INSTALLER_MATRIX_ONLY", "").split(",")))
SINGLE_DIRECTORY = ROOT / "tests/fake_bin/single_directory"
TIMER_UNITS = "controller.service refresh.service refresh.timer generate.service generate.timer"
APP_UNITS = "controller.service scheduler.service"
AGENTS = "serve refresh catalog-publish"
SECRET = "synthetic-matrix-secret"
DEFAULT_STRIDE = 8
KILL_STRIDE = 3


@dataclass(frozen=True)
class Scenario:
    name: str
    script: str
    role: str
    system: str
    environment: tuple[tuple[str, str], ...]
    states: tuple[str, ...]
    uninstall: str = ""

    @property
    def shell(self) -> str:
        return MACOS_BASH if self.system == "Darwin" else "bash"


DISPLAY_ENVIRONMENT = (("INKY_BIRD_NONINTERACTIVE_SUDO", "true"),)
SCENARIOS = {
    scenario.name: scenario
    for scenario in (
        Scenario(
            "macos",
            "install-controller.sh",
            "controller",
            "Darwin",
            (("FAKE_AGENTS", AGENTS),),
            ("fresh", "single", "staged", "disabled"),
            "uninstall-controller.sh",
        ),
        Scenario(
            "linux-timers",
            "install-controller-systemd.sh",
            "controller",
            "Linux",
            (("FAKE_UNITS", TIMER_UNITS),),
            ("fresh", "single", "staged", "disabled", "shared-single", "shared-staged"),
            "uninstall-controller-systemd.sh",
        ),
        Scenario(
            "linux-app",
            "install-controller-systemd.sh",
            "controller",
            "Linux",
            (("FAKE_UNITS", APP_UNITS),),
            ("fresh", "single", "staged", "shared-single"),
        ),
        Scenario(
            "display",
            "install-display-local.sh",
            "display",
            "Linux",
            DISPLAY_ENVIRONMENT,
            (
                "fresh",
                "single",
                "staged",
                "disabled",
                "renamed",
                "shared-single",
                "shared-staged",
            ),
            "uninstall-display-local.sh",
        ),
        Scenario(
            "remote-display",
            "install-display-node.sh",
            "display",
            "Linux",
            (),
            ("fresh", "single", "staged"),
        ),
    )
}
# Every step of these runs in the default subset.
DEFAULT_FULL = {
    ("install", "macos", "single"),
    ("install", "linux-timers", "single"),
    ("install", "linux-app", "single"),
    ("install", "display", "shared-single"),
}
UNINSTALL_STATES = ("single", "staged", "shared")
# States in which this role is already installed, so an upgrade checks for a plate.
UPGRADE_STATES = ("single", "staged", "disabled", "shared-single")


# Machine state -----------------------------------------------------------------


def digest(path: Path) -> str:
    """Hash a tree's names, kinds, link targets and bytes; skip bytecode caches."""
    hasher = hashlib.sha256()
    if not path.exists() and not path.is_symlink():
        return "absent"
    paths = [path] if not path.is_dir() else sorted(path.rglob("*"))
    for item in paths:
        if "__pycache__" in item.parts:
            continue
        name = str(item.relative_to(path)) if item != path else "."
        if item.is_symlink():
            hasher.update(f"L {name} {os.readlink(item)}\n".encode())
        elif item.is_file():
            hasher.update(f"F {name}\n".encode() + item.read_bytes())
        else:
            hasher.update(f"D {name}\n".encode())
    return hasher.hexdigest()


def display_venv(host: Host) -> None:
    """A Pimoroni environment whose python the fake tools stand in for."""
    python = host.venv / "bin/python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(fake_tools.wrapper("venv-python"))
    python.chmod(0o755)
    inky = host.venv / "bin/inky-bird"
    inky.write_text(fake_tools.wrapper("inky-bird"))
    inky.chmod(0o755)


class Machine(Host):
    """One synthetic computer for a group of cases, in a fixed directory."""

    def __init__(self, root: Path, scenario: Scenario) -> None:
        super().__init__(root, scenario.system, number_file_commands=True)
        self.scenario = scenario
        self.config.write_text(f"synthetic private config {SECRET}\n")
        self.key = root / "synthetic-key"
        self.key.write_text("synthetic key\n")
        display_venv(self)

    # Building starting states.
    def checkout(self, tag: str) -> Path:
        directory = self.root / f"source-{tag}"
        Host.write_source(directory, tag)
        return directory

    def environment_for(self, scenario: Scenario, **extra: str) -> dict[str, str]:
        environment = self.environment(**dict(scenario.environment))
        if scenario.name == "remote-display":
            environment.update(
                INKY_BIRD_DISPLAY_HOST="display.example.test",
                INKY_BIRD_DISPLAY_USER="fixture",
                INKY_BIRD_DISPLAY_SSH_KEY=str(self.key),
                INKY_BIRD_DISPLAY_APP_DIR=str(self.app),
                INKY_BIRD_DISPLAY_CONFIG_PATH=str(self.config),
            )
        environment.update(extra)
        return environment

    def execute(
        self, script: Path, scenario: Scenario, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        return run_contained([scenario.shell, str(script)], self.environment_for(scenario, **extra))

    def survivors(self) -> list[str]:
        """Kill and name any process still running from this machine's directory."""
        listing = subprocess.run(
            ["/bin/ps", "-ax", "-o", "pid=", "-o", "stat=", "-o", "command="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        found = []
        for line in listing.splitlines():
            pid, stat, *command = line.split(None, 2)
            if command and str(self.root) in command[0] and not stat.startswith("Z"):
                found.append(f"{pid} {command[0][:200]}")
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid), signal.SIGKILL)
        return found

    def install(self, scenario: Scenario, layout: str, tag: str) -> None:
        """Install ``scenario``'s role from a ``tag`` checkout in one layout."""
        source = self.checkout(tag)
        name = scenario.script
        if scenario.name == "remote-display":
            name = "install-display-local.sh"
            scenario = SCENARIOS["display"]
        text = (SINGLE_DIRECTORY / name).read_text() if layout == "single" else None
        script = self.script(name, source=source, text=text)
        result = self.execute(script, scenario)
        assert result.returncode == 0, result.stdout + result.stderr
        if scenario.role == "display":
            # An upgrade needs the timer stopped first.
            self.set_unit(f"{self.family}-display.timer", active=False)

    def set_unit(self, unit: str, *, active: bool, enabled: bool | None = None) -> None:
        units = self.state_of("systemd")
        record = units.setdefault(unit, {"active": False, "enabled": False})
        record["active"] = active
        if enabled is not None:
            record["enabled"] = enabled
        self.save_state("systemd", units)
        if not active:
            processes = self.state_of("processes")
            processes.pop(unit, None)
            self.save_state("processes", processes)

    def install_renamed_display(self) -> None:
        """The pre-1.0 renamed distribution as a regular install, timer stopped."""
        Host.write_source(self.app, "old")
        self.save_state("pip", {"inky-bird-frame": {"source": str(self.app)}})
        package = self.venv / "lib/python3/site-packages/inky_bird_frame"
        shutil.copytree(self.app / "src/inky_bird_frame", package)
        service = f"{self.family}-display.service"
        (self.units / service).write_text(
            f"[Service]\nUser=fixture\nExecStart={self.venv}/bin/inky-bird\n"
        )
        (self.units / f"{self.family}-display.timer").write_text("[Timer]\n# old\n")
        self.set_unit(service, active=False, enabled=False)
        self.set_unit(f"{self.family}-display.timer", active=False, enabled=True)

    def build(self, scenario: Scenario, state: str) -> None:
        other = SCENARIOS["display"] if scenario.role == "controller" else SCENARIOS["linux-timers"]
        if state == "fresh":
            pass
        elif state in ("single", "staged"):
            self.install(scenario, state, "old")
        elif state == "disabled":
            self.install(scenario, "single", "old")
            if scenario.system == "Darwin":
                loaded = self.state_of("launchd")
                loaded.pop(f"com.{self.family}.refresh", None)
                self.save_state("launchd", loaded)
                processes = self.state_of("processes")
                processes.pop(f"com.{self.family}.refresh", None)
                self.save_state("processes", processes)
            elif scenario.role == "controller":
                self.set_unit(f"{self.family}-generate.timer", active=False, enabled=False)
            else:
                self.set_unit(f"{self.family}-display.timer", active=False, enabled=False)
        elif state == "renamed":
            self.install_renamed_display()
        elif state == "shared-single":
            self.install(other, "single", "other")
            self.install(scenario, "single", "old")
        elif state == "shared-staged":
            self.install(other, "staged", "other")
        elif state == "shared":
            self.install(other, "staged", "other")
            self.install(scenario, "staged", "old")
        else:
            raise AssertionError(state)

    def reset_run(self) -> None:
        for name in ("steps", "inject", "sudo-expired", "sudo-validations", "fail"):
            (self.fake / name).unlink(missing_ok=True)
        (self.fake / "log").write_text("")

    def steps(self) -> list[list[str]]:
        path = self.fake / "steps"
        if not path.exists():
            return []
        return [line.split("\t") for line in path.read_text().splitlines()]


# Observing a machine ----------------------------------------------------------


@dataclass
class RoleView:
    """What one role has installed and running, as the fake machine records it."""

    tag: str | None
    code: str
    units: dict[str, tuple[bool, bool]]
    files: dict[str, str]
    processes: dict[str, dict[str, object]]
    runtimes: frozenset[str]


def runtime_of(executable: Path) -> tuple[str, Path] | None:
    """The tag and directory a command started from ``executable`` would run."""
    if not executable.exists():
        return None
    venv = executable.parent.parent
    site = venv / "lib/python3/site-packages"
    pth = site / "_inky_bird.pth"
    # A regular install in site-packages is imported before any editable one.
    if (site / "inky_bird_frame").is_dir():
        return "regular", site
    if pth.exists():
        source = Path(pth.read_text().split()[0])
        tag = source / "runtime-tag"
        return (tag.read_text().strip() if tag.exists() else "missing"), source.parent
    tag = Path(os.path.realpath(venv)) / "tag"
    return (tag.read_text().strip() if tag.exists() else "missing"), Path(os.path.realpath(venv))


def controller_code(machine: Machine) -> tuple[str | None, str]:
    venv = machine.app / ".venv"
    if not venv.exists():
        return None, "absent"
    real = Path(os.path.realpath(venv))
    source = real.parent / "src" if (real.parent / "src").is_dir() else machine.app / "src"
    tag = real / "tag"
    return (tag.read_text().strip() if tag.exists() else "missing"), (digest(real) + digest(source))


def display_code(machine: Machine) -> tuple[str | None, str]:
    site = machine.venv / "lib/python3/site-packages"
    found = runtime_of(machine.venv / "bin/inky-bird")
    if found is None or not site.exists() or not any(site.iterdir()):
        return None, "absent"
    tag, directory = found
    code = digest(site)
    if tag != "regular":
        code += digest(directory / "src")
    return tag, code


def unit_names(machine: Machine, role: str) -> list[str]:
    if machine.system == "Darwin":
        return [
            f"com.{machine.family}.{label}"
            for label in (
                "serve",
                "refresh",
                "generate",
                "catalog-publish",
                "notifications",
                "scheduler",
            )
        ]
    if role == "display":
        return [f"{machine.family}-display.service", f"{machine.family}-display.timer"]
    names = ("controller", "scheduler", "refresh", "generate", "catalog-publish", "notifications")
    return [
        f"{machine.family}-{name}.{suffix}" for name in names for suffix in ("service", "timer")
    ]


def unit_file(machine: Machine, unit: str) -> Path:
    if machine.system == "Darwin":
        return machine.agents / f"{unit}.plist"
    return machine.units / unit


def view(machine: Machine, role: str) -> RoleView:
    units: dict[str, tuple[bool, bool]] = {}
    files: dict[str, str] = {}
    systemd = machine.state_of("systemd")
    loaded = machine.state_of("launchd")
    for unit in unit_names(machine, role):
        if machine.system == "Darwin":
            units[unit] = (unit in loaded, unit in loaded)
        else:
            record = systemd.get(unit, {})
            units[unit] = (bool(record.get("enabled")), bool(record.get("active")))
        path = unit_file(machine, unit)
        files[unit] = path.read_text("utf-8", "replace") if path.exists() else "absent"
    processes = {
        name: value for name, value in machine.state_of("processes").items() if name in units
    }
    directory = machine.app / (".runtimes" if role == "controller" else ".display-runtimes")
    runtimes = (
        frozenset(str(path) for path in directory.iterdir()) if directory.is_dir() else frozenset()
    )
    tag, code = controller_code(machine) if role == "controller" else display_code(machine)
    return RoleView(tag, code, units, files, processes, runtimes)


def long_running(unit: str) -> bool:
    return unit.endswith(
        (".timer", "-controller.service", "-scheduler.service")
    ) or unit.startswith("com.")


def executable_of(machine: Machine, unit: str) -> Path | None:
    path = unit_file(machine, unit)
    if machine.system == "Darwin":
        if not path.exists():
            return None
        return Path(plistlib.loads(path.read_bytes())["ProgramArguments"][0])
    if unit.endswith(".timer"):
        path = machine.units / unit.replace(".timer", ".service")
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if line.startswith("ExecStart="):
            return Path(line.partition("=")[2].split()[0])
    return None


def stray_paths(machine: Machine) -> set[str]:
    """Staging left where installers stage: temp dirs, link swaps, remote stages."""
    found = {str(path) for path in machine.tmp.iterdir()}
    found |= {str(path) for path in machine.support.glob("install.*")}
    found |= {str(path) for path in machine.support.glob("catalog-publish-recovery.*")}
    if machine.app.parent.is_dir():
        found |= {str(path) for path in machine.app.parent.glob(".inky-bird-stage.*")}
    if (machine.app / ".venv.next").is_symlink():
        found.add(str(machine.app / ".venv.next"))
    return found


@dataclass
class Snapshot:
    role: RoleView
    other: RoleView
    other_code: str
    stray: set[str]
    tree: str


def snapshot(machine: Machine, scenario: Scenario) -> Snapshot:
    other_role = "display" if scenario.role == "controller" else "controller"
    other = view(machine, other_role)
    return Snapshot(
        view(machine, scenario.role),
        other,
        other.code,
        stray_paths(machine),
        digest(machine.app)
        + digest(machine.units)
        + json.dumps(
            [machine.state_of(name) for name in ("systemd", "launchd", "pip")], sort_keys=True
        ),
    )


# Invariants -------------------------------------------------------------------


@dataclass
class Case:
    kind: str
    scenario: str
    state: str
    step: int
    mode: str
    tool: str

    @property
    def id(self) -> str:
        return f"{self.kind}-{self.scenario}-{self.state}-{self.mode}-{self.step:03d}-{self.tool}"


@dataclass
class Outcome:
    returncode: int
    output: str
    violations: list[str] = field(default_factory=list)


PATH_PATTERN = re.compile(r"/[^\s'\"<>:;,]+")


def allowed_output_roots(machine: Machine) -> tuple[str, ...]:
    return (
        str(machine.app),
        str(machine.app.parent / ".inky-bird-stage."),
        str(machine.support),
        str(machine.agents),
        str(machine.units),
        str(machine.venv),
        str(machine.source),
        str(machine.key),
        str(machine.tmp),
    )


def check_common(machine: Machine, scenario: Scenario, before: Snapshot, outcome: Outcome) -> None:
    """I1, I5, I7 and I9: hold after every run, installs and uninstalls alike."""
    report = outcome.violations
    output = outcome.output
    # I1: every recorded process still finds the directory it started from,
    # and every enabled or running unit would start from an existing runtime.
    for name, process in machine.state_of("processes").items():
        directory = Path(str(process["directory"]))
        if not directory.exists() or directory.stat().st_ino != process["inode"]:
            report.append(f"I1: {name} runs from {directory}, which no longer exists as it was")
    for role in ("controller", "display"):
        current = view(machine, role)
        for unit, (enabled, active) in current.units.items():
            if not (enabled or active):
                continue
            executable = executable_of(machine, unit)
            if executable is None:
                if active:
                    report.append(f"I1: {unit} is running without its definition")
                continue
            found = runtime_of(executable)
            if found is None or found[0] == "missing" or not found[1].exists():
                report.append(f"I1: {unit} would start from a runtime that does not exist")
    # I5: the other role on a shared machine is untouched.
    if scenario.role == "controller":
        other = view(machine, "display")
    else:
        other = view(machine, "controller")
    if before.other.tag is not None:
        if other.code != before.other_code:
            report.append("I5: the other role's runtime changed")
        if other.units != before.other.units or other.files != before.other.files:
            report.append("I5: the other role's units changed")
        if other.runtimes != before.other.runtimes:
            report.append("I5: the other role's runtime directories changed")
        if other.processes.keys() != before.other.processes.keys():
            report.append("I5: the other role's running services changed")
    # I7: no definition was deleted for a unit that is still running.
    current = view(machine, scenario.role)
    for unit, (_, active) in current.units.items():
        if active and current.files[unit] == "absent":
            report.append(f"I7: {unit} is still running but its definition was deleted")
    # I9: nothing private is printed.
    if SECRET in output or "synthetic private config" in output or "synthetic key" in output:
        report.append("I9: private configuration or key content was printed")
    roots = allowed_output_roots(machine)
    for match in PATH_PATTERN.findall(output):
        if match.startswith(str(machine.root)) and not match.startswith(roots):
            report.append(f"I9: a private path was printed: {match}")


def claims_new(outcome: Outcome) -> bool:
    return outcome.returncode == 0 or any(
        phrase in outcome.output
        for phrase in (
            "new version is installed",
            "uses the new version",
            "the new one stays active",
        )
    )


def check_install(
    machine: Machine,
    scenario: Scenario,
    case: Case,
    before: Snapshot,
    outcome: Outcome,
) -> None:
    report = outcome.violations
    output = outcome.output
    check_common(machine, scenario, before, outcome)
    after = view(machine, scenario.role)
    failed = outcome.returncode != 0
    # I8: an injected failure never reports success, and a kill is never silent.
    if case.mode in ("fail", "expire", "kill") and outcome.returncode == 0:
        # The new version runs; only owner setup, or a leftover the output names, remains.
        tolerated = (case.tool == "inky-bird" and "owner setup is incomplete" in output) or (
            case.tool in ("rm", "ssh")
            and ("by hand: " in output or "after reconnecting: " in output)
        )
        if case.mode == "kill" or not tolerated:
            report.append("I8: the run reported success although it was failed or stopped")
    # I2: what is said to run is what runs; anything else is named.
    expected = "new" if claims_new(outcome) else before.role.tag
    for unit, process in after.processes.items():
        if process["runtime"] != expected and unit not in output:
            report.append(
                f"I2: {unit} runs {process['runtime']}, the output implies {expected}, "
                "and it is not named"
            )
    for unit, (_, active) in after.units.items():
        if active and unit.endswith(".timer"):
            executable = executable_of(machine, unit)
            found = runtime_of(executable) if executable is not None else None
            if found is not None and found[0] != expected and unit not in output:
                report.append(f"I2: {unit} would run {found[0]}, not {expected}, unnamed")
    if not failed:
        check_success(machine, scenario, before, after, report, output)
        return
    if claims_new(outcome):
        # A late step failed after the new version was installed and started.
        check_final_units(machine, scenario, after, report, output)
    # I4: every unit is back as it was, or named.
    for unit, state in before.role.units.items() if not claims_new(outcome) else ():
        now = after.units[unit]
        if (
            unit.endswith(".service")
            and not long_running(unit)
            and state[0] == now[0]
            and not now[1]
        ):
            continue
        if now != state and unit not in output:
            report.append(f"I4: {unit} was {state} and is now {now}, not named")
        if (
            after.files[unit] != before.role.files[unit]
            and unit not in output
            and not claims_new(outcome)
        ):
            report.append(f"I4: {unit} definition changed and it is not named")
    # I6: until the output says the new version is in use, the previous one is intact.
    if before.role.tag is not None and after.code != before.role.code and not claims_new(outcome):
        report.append("I6: the previous version's runtime, environment or source changed")
    # I10: nothing staged is left behind unless the output names it, or it is
    # the runtime the output says is now in use.
    in_use = active_runtime(machine, scenario.role) if claims_new(outcome) else None
    for path in sorted(after.runtimes - before.role.runtimes):
        if path not in output and path != in_use:
            report.append(f"I10: runtime {path} was left behind without being named")
    for path in sorted(before.role.runtimes - after.runtimes):
        report.append(f"I10: runtime {path} existed before and was removed by a failed run")
    for path in sorted(stray_paths(machine) - before.stray):
        if path not in output and not removal_failed(case, path):
            report.append(f"I10: {path} was left behind")


def active_runtime(machine: Machine, role: str) -> str | None:
    """The runtime directory the role's services start from now, as listed."""
    if role == "controller":
        venv = machine.app / ".venv"
        if not venv.is_symlink():
            return None
        directory = Path(os.path.realpath(venv)).parent
    else:
        found = runtime_of(machine.venv / "bin/inky-bird")
        if found is None or found[0] == "regular":
            return None
        directory = Path(os.path.realpath(found[1]))
    listing = machine.app / (".runtimes" if role == "controller" else ".display-runtimes")
    for path in listing.iterdir() if listing.is_dir() else ():
        if os.path.realpath(path) == str(directory):
            return str(path)
    return None


def removal_failed(case: Case, path: str) -> bool:
    """A failed ``rm`` of exactly that directory is the one thing allowed to leave it."""
    return case.tool == "rm" and case.mode == "fail"


def check_success(
    machine: Machine,
    scenario: Scenario,
    before: Snapshot,
    after: RoleView,
    report: list[str],
    output: str,
) -> None:
    """I4 and I8 for a run that exited 0: the intended final state, exactly."""
    if after.tag != "new":
        report.append(f"I8: success reported but the active version is {after.tag}")
    check_final_units(machine, scenario, after, report)
    for process in after.processes.values():
        if process["runtime"] != "new":
            report.append(f"I2: after success a service still runs {process['runtime']}")
    # The previous version stays for rollback; nothing older does.
    expected = 1 if before.role.tag is None or before.role.tag == "regular" else 2
    if scenario.role == "display" and before.role.tag is not None and not before.role.runtimes:
        expected = 1
    if len(after.runtimes) != expected:
        report.append(f"I10: after success {len(after.runtimes)} runtimes remain, not {expected}")
    for path in sorted(stray_paths(machine) - before.stray):
        if path not in output:
            report.append(f"I10: {path} was left behind after success")


def check_final_units(
    machine: Machine, scenario: Scenario, after: RoleView, report: list[str], named: str = ""
) -> None:
    """I4 once the new version is installed: every unit as the new version wants it,
    except a unit a failed run names as not set up."""
    found: list[str] = []
    check_intended_units(machine, scenario, after, found)
    report.extend(item for item in found if not named or item.split()[3] not in named)


def check_intended_units(
    machine: Machine, scenario: Scenario, after: RoleView, report: list[str]
) -> None:
    if machine.system == "Darwin":
        wanted = {f"com.{machine.family}.{label}" for label in AGENTS.split()}
        for unit, (loaded, _) in after.units.items():
            if loaded != (unit in wanted):
                report.append(f"I4: after success {unit} loaded={loaded}")
    else:
        if scenario.role == "display":
            wanted_files = {f"{machine.family}-display.service", f"{machine.family}-display.timer"}
            running = {f"{machine.family}-display.timer"}
        else:
            names = dict(scenario.environment)["FAKE_UNITS"].split()
            wanted_files = {f"{machine.family}-{name}" for name in names}
            running = {unit for unit in wanted_files if long_running(unit)}
        for unit, (enabled, active) in after.units.items():
            present = after.files[unit] != "absent"
            if present != (unit in wanted_files):
                report.append(f"I4: after success {unit} definition present={present}")
            if unit in running and not (enabled and active):
                report.append(f"I4: after success {unit} enabled={enabled} active={active}")


def check_uninstall(
    machine: Machine, scenario: Scenario, before: Snapshot, outcome: Outcome
) -> None:
    report = outcome.violations
    check_common(machine, scenario, before, outcome)
    after = view(machine, scenario.role)
    remaining = [
        unit
        for unit, (enabled, active) in after.units.items()
        if enabled or active or after.files[unit] != "absent"
    ]
    if outcome.returncode == 0 and remaining:
        report.append(f"U1: uninstall reported success but left {remaining}")
    if "No changes made" in outcome.output:
        if after.units != before.role.units or after.files != before.role.files:
            report.append("U2: the uninstaller said it made no changes, but it did")
        return
    for unit in remaining:
        if (
            outcome.returncode != 0
            and unit not in outcome.output
            and (after.units[unit] != before.role.units[unit] or after.units[unit][1])
        ):
            report.append(f"U2: {unit} was not removed and is not named")
    if after.code != before.role.code:
        report.append("U3: uninstall changed the installed runtime")


def check_rerun(machine: Machine, scenario: Scenario, outcome: Outcome) -> None:
    """I3: the installer the installed command would pick works once the fault is gone."""
    machine.reset_run()
    script = installed_script(machine, scenario)
    if scenario.role == "display":
        # setup asks the owner to stop the display timer before an upgrade.
        machine.set_unit(f"{machine.family}-display.timer", active=False)
        machine.set_unit(f"{machine.family}-display.service", active=False)
        rerun = SCENARIOS["display"]
    else:
        rerun = scenario
    result = machine.execute(script, rerun)
    if result.returncode != 0:
        outcome.violations.append(
            f"I3: rerunning {script.parent.parent.name}/deploy/{script.name} failed: "
            + (result.stdout + result.stderr)[-1500:]
        )
        return
    tag = (controller_code if scenario.role == "controller" else display_code)(machine)[0]
    for name, process in machine.state_of("processes").items():
        if name in unit_names(machine, scenario.role) and process["runtime"] != tag:
            outcome.violations.append(f"I3: after the rerun {name} runs {process['runtime']}")


def installed_script(machine: Machine, scenario: Scenario) -> Path:
    name = "install-display-local.sh" if scenario.role == "display" else scenario.script
    if scenario.role == "controller":
        venv = machine.app / ".venv"
        if venv.is_symlink():
            return Path(os.path.realpath(venv)).parent / "deploy" / name
        if venv.exists():
            return machine.app / "deploy" / name
    else:
        found = runtime_of(machine.venv / "bin/inky-bird")
        if found is not None and found[0] not in ("regular", "missing"):
            return found[1] / "deploy" / name
    return machine.script(name)


# Running cases ----------------------------------------------------------------


class Group:
    """Cases that share a starting state, run one after another in one directory."""

    def __init__(self, base: Path, kind: str, scenario: Scenario, state: str) -> None:
        self.kind = kind
        self.scenario = scenario
        self.state = state
        self.directory = base / "machine"
        self.template = base / "template"
        self.machine = Machine(self.directory, scenario)
        self.machine.build(scenario, state)
        if kind == "install":
            self.machine.script(scenario.script)
            if scenario.name == "remote-display":
                self.machine.script("install-display-local.sh")
        else:
            self.machine.script(scenario.uninstall)
        self.machine.reset_run()
        shutil.copytree(self.directory, self.template, symlinks=True)

    def restore(self) -> None:
        shutil.rmtree(self.directory)
        shutil.copytree(self.template, self.directory, symlinks=True)
        # A copy is the same machine; processes keep running from the same places.
        processes = self.machine.state_of("processes")
        for process in processes.values():
            directory = Path(str(process["directory"]))
            process["inode"] = directory.stat().st_ino if directory.exists() else 0
        self.machine.save_state("processes", processes)

    def script(self) -> Path:
        name = self.scenario.script if self.kind == "install" else self.scenario.uninstall
        return self.machine.source / "deploy" / name

    def run(self, injection: str | None, *, fresh: bool = True) -> tuple[Outcome, list[list[str]]]:
        if fresh:
            self.restore()
        machine = self.machine
        if injection is not None:
            (machine.fake / "inject").write_text(injection + "\n")
        try:
            result = machine.execute(self.script(), self.scenario)
            outcome = Outcome(result.returncode, result.stdout + result.stderr)
        except AssertionError as error:
            outcome = Outcome(-1, "", [f"the run was not contained: {error}"])
        steps = machine.steps()
        (machine.fake / "inject").unlink(missing_ok=True)
        return outcome, steps

    def record(self) -> list[list[str]]:
        outcome, steps = self.run(None)
        assert outcome.returncode == 0 and not outcome.violations, outcome
        assert not self.machine.survivors()
        return steps

    def rerun(self, outcome: Outcome) -> None:
        try:
            if self.kind == "install":
                check_rerun(self.machine, self.scenario, outcome)
            else:
                self.machine.reset_run()
                rerun = self.machine.execute(self.script(), self.scenario)
                if rerun.returncode != 0:
                    outcome.violations.append(
                        "I3: a second uninstall failed: " + rerun.stderr[-800:]
                    )
        except AssertionError as error:
            outcome.violations.append(f"I3: the rerun was not contained: {error}")

    def evaluate(self, case: Case, recorded: list[list[str]]) -> Outcome:
        self.restore()
        before = snapshot(self.machine, self.scenario)
        outcome, steps = self.run(f"{case.step} {case.mode}")
        survivors = self.machine.survivors()
        if survivors:
            outcome.violations.append(f"processes outlived the run: {survivors}")
        if len(steps) <= case.step or steps[case.step][2] != recorded[case.step][2]:
            outcome.violations.append(
                f"the run diverged from the recording before step {case.step}"
            )
            return outcome
        if self.kind == "install":
            check_install(self.machine, self.scenario, case, before, outcome)
        else:
            check_uninstall(self.machine, self.scenario, before, outcome)
        self.rerun(outcome)
        survivors = self.machine.survivors()
        if survivors:
            outcome.violations.append(f"processes outlived the rerun: {survivors}")
        return outcome

    def busy(self) -> Outcome:
        """I8: a plate being made stops an upgrade with exit 3 and no change."""
        self.restore()
        before = snapshot(self.machine, self.scenario)
        with running_generation(self.machine):
            # The lock is held on this copy's state; do not copy it again.
            outcome, _ = self.run(None, fresh=False)
        survivors = self.machine.survivors()
        if survivors:
            outcome.violations.append(f"processes outlived the run: {survivors}")
        if outcome.returncode != 3:
            outcome.violations.append(f"I8: busy run exited {outcome.returncode}, not 3")
        after = snapshot(self.machine, self.scenario)
        if after.tree != before.tree or after.stray != before.stray:
            outcome.violations.append("I8: a busy run changed the machine")
        return outcome


def selected(kind: str, scenario: str, state: str, position: int) -> bool:
    if SAMPLE:
        return position % DEFAULT_STRIDE == 0
    if FULL or (kind, scenario, state) in DEFAULT_FULL:
        return True
    return position % DEFAULT_STRIDE == 0


def busy_checked(scenario: str, state: str) -> bool:
    return SCENARIOS[scenario].role == "controller" and state in UPGRADE_STATES


def plan_groups() -> list[tuple[str, Scenario, str]]:
    groups: list[tuple[str, Scenario, str]] = []
    for scenario in SCENARIOS.values():
        groups.extend(("install", scenario, state) for state in scenario.states)
        if scenario.uninstall:
            # Only a Pi runs both roles.
            states = UNINSTALL_STATES if scenario.system == "Linux" else UNINSTALL_STATES[:-1]
            groups.extend(("uninstall", scenario, state) for state in states)
    if ONLY:
        groups = [group for group in groups if f"{group[0]}:{group[1].name}:{group[2]}" in ONLY]
    return groups


class Matrix:
    """Record every group once; cases then run one at a time, never in parallel."""

    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="installer-matrix-"))
        self.groups: dict[tuple[str, str, str], Group] = {}
        self.recordings: dict[tuple[str, str, str], list[list[str]]] = {}
        for number, (kind, scenario, state) in enumerate(plan_groups()):
            group = Group(self.base / f"g{number:02d}", kind, scenario, state)
            key = (kind, scenario.name, state)
            self.groups[key] = group
            self.recordings[key] = group.record()

    def cases(self) -> list[Case]:
        found: list[Case] = []
        for (kind, scenario, state), steps in self.recordings.items():
            position = 0
            for index, (_, kind_of_call, tool, *_rest) in enumerate(steps):
                if kind_of_call != "action":
                    continue
                if selected(kind, scenario, state, position):
                    found.append(Case(kind, scenario, state, index, "fail", tool))
                    if tool == "sudo":
                        found.append(Case(kind, scenario, state, index, "expire", tool))
                    if position % KILL_STRIDE == 0:
                        found.append(Case(kind, scenario, state, index, "kill", tool))
                position += 1
        return found

    def evaluate(self, case: Case) -> Outcome:
        key = (case.kind, case.scenario, case.state)
        return self.groups[key].evaluate(case, self.recordings[key])

    def busy(self, scenario: str, state: str) -> Outcome:
        return self.groups[("install", scenario, state)].busy()

    def close(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


MATRIX: Matrix | None = None


def matrix() -> Matrix:
    global MATRIX
    if MATRIX is None:
        MATRIX = Matrix()
    return MATRIX


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "case" in metafunc.fixturenames:
        cases = matrix().cases()
        metafunc.parametrize("case", cases, ids=[case.id for case in cases])
    if "busy_group" in metafunc.fixturenames:
        names = [
            (scenario.name, state)
            for kind, scenario, state in plan_groups()
            if kind == "install" and busy_checked(scenario.name, state)
        ]
        metafunc.parametrize("busy_group", names, ids=[f"{name}-{state}" for name, state in names])


@pytest.fixture(scope="module")
def executed() -> Iterator[Matrix]:
    runner = matrix()
    yield runner
    runner.close()


def report(name: str, outcome: Outcome) -> None:
    """Append a failing case to ``INKY_INSTALLER_MATRIX_LOG`` while a long run continues."""
    path = os.environ.get("INKY_INSTALLER_MATRIX_LOG")
    if path and outcome.violations:
        entry = {"case": name, "exit": outcome.returncode, "violations": outcome.violations}
        entry["output"] = outcome.output[-3000:]
        with open(path, "a") as handle:
            handle.write(json.dumps(entry) + "\n")


def test_installer_case(case: Case, executed: Matrix) -> None:
    outcome = executed.evaluate(case)
    report(case.id, outcome)
    assert not outcome.violations, (
        "\n".join(outcome.violations) + f"\nexit {outcome.returncode}\n" + outcome.output[-4000:]
    )


def test_busy_plate_changes_nothing(busy_group: tuple[str, str], executed: Matrix) -> None:
    outcome = executed.busy(*busy_group)
    report("busy-" + "-".join(busy_group), outcome)
    assert not outcome.violations, "\n".join(outcome.violations) + "\n" + outcome.output[-2000:]


def test_matrix_covers_every_action_step(executed: Matrix) -> None:
    """The recording runs saw the layout's own steps, so the cases exercise them."""
    if ONLY:
        pytest.skip("only some groups were recorded")
    tools: set[str] = set()
    for steps in executed.recordings.values():
        tools |= {tool for _, kind_of_call, tool, *_ in steps if kind_of_call == "action"}
    assert {"mv", "ln", "rm", "mktemp", "sudo", "uv", "rsync", "ssh", "inky-bird"} <= tools
    assert {"launchctl", "systemctl", "venv-python", "python3"} <= tools
