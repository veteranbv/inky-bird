"""Fail or stop every native installer at every step and check what it leaves.

A recording run of each scenario numbers every external call the installer
makes: the synthetic service managers, sudo, uv, pip, rsync, ssh, the Python
helpers, the inky-bird command and the file commands that build, switch and
remove runtimes. Each case then starts from the same machine state, makes the
call with one number fail (for sudo also: every later sudo call), or stops the
installer once that call completes (SIGTERM or SIGHUP to the script, or SIGINT
to its whole process group as Ctrl-C does), and checks the invariants in
``check_install`` and ``check_uninstall``. Status queries are numbered but
never failed: a failed query is indistinguishable from a different answer.

A second pass fails an upgrade twice: first right after the switch to the new
version, or at its last service start, then at each step of the cleanup that
follows, or stops it there. Rollback commands appear only in these runs.

With one fault every rollback command works, so every unit must be exactly as
it was. A unit may differ only when its own rollback failed (sudo expired, or
the second fault was one of that unit's calls), and only in the way the output
says it does. Every definition records the version that rendered it, so one
left from the new version differs from the one it replaced. Claims that the
previous or the new version runs must hold for the active link, every service
and every enabled or running unit; an installer's own success line is such a
claim. The previous version is hashed whole: its runtime directory, or the
source, installer and project files of a single-directory install.

The recording runs are tests too, ``<scenario>/<state>/recording`` (and
``-after<step>`` for a cleanup): the run without a fault must succeed and pass
every check, and a run with its first fault must fail.

The default run fails the first and the middle call of every scenario and
start state, stops the installer at that middle call, and fails every cleanup
step that moves, links or removes a runtime after a failed upgrade from the
single-directory layout on both controllers and on a shared Pi's display;
about 270 tests. Run every case, and the second pass of every upgrade, with::

    INKY_INSTALLER_MATRIX=full uv run pytest tests/test_installer_matrix.py -q

Case ids read ``<scenario>/<state>/<step>-<tool>-<mode>`` (``-after<step>``
for the second pass; ``<scenario>-uninstall`` for an uninstaller), so ``-k``
selects them, e.g. ``-k "linux-timers/single/ and rm-fail"``. Also:

- ``INKY_MATRIX_TOUCHING=rm,I6`` runs only the cases whose failed call is one
  of those tools, or whose last recorded result had something to check for one
  of those invariants (results are kept in ``build/installer-matrix``).
- ``INKY_MATRIX_SHARD=2/6`` runs the second of six equal parts, as CI does.
- ``INKY_MATRIX_WORKERS`` sets how many cases run at once (default 6).
- ``INKY_INSTALLER_MATRIX_ONLY=install:macos:single`` records only some groups.

Each starting state is built once and cloned for every run (a filesystem clone
where there is one), with the absolute paths it records pointed at the clone.
Every run has its own process group, a time limit and a process budget, and
starts under an account-wide process ceiling; nothing it starts may outlive it.
A run removes the machines of any earlier run that was killed. Tests of the
matrix itself, rather than its cases, run in the first shard only.
"""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import plistlib
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
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

# The single-directory installers as of 5ded22f, the last commit before the
# staged layout; they build the starting state every upgrade is tested from.
SINGLE_DIRECTORY = ROOT / "tests/fake_bin/single_directory"
SINGLE_DIRECTORY_SHA256 = {
    "install-controller-systemd.sh": (
        "2dfdd0552c2fd28595997ed0652c22f04a1a42cb6f7dcd49972e9af15862fc6a"
    ),
    "install-controller.sh": "c2075bfa1440a8722a66cd62fd41bd634b777e8bf91c726c871ce722869e1580",
    "install-display-local.sh": (
        "36ea22ef7d35f0f5b8bcc137d02551054c58a919397e747f7d35c0d56ee5b0da"
    ),
}
TIMER_UNITS = "controller.service refresh.service refresh.timer generate.service generate.timer"
APP_UNITS = "controller.service scheduler.service"
AGENTS = "serve refresh catalog-publish"
SECRET = "synthetic-matrix-secret"
KILL_STRIDE = 3
STOP_MODES = ("kill", "interrupt", "hangup")


@dataclass(frozen=True)
class Scenario:
    name: str
    script: str
    role: str
    system: str
    environment: tuple[tuple[str, str], ...]
    states: tuple[str, ...]
    uninstall: str = ""
    # The scenario whose installer built the starting state, when it differs:
    # switching between timers and the app-managed scheduler.
    start: str = ""

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
            ("fresh", "single", "staged", "disabled", "rolled-back"),
            "uninstall-controller.sh",
        ),
        Scenario(
            "linux-timers",
            "install-controller-systemd.sh",
            "controller",
            "Linux",
            (("FAKE_UNITS", TIMER_UNITS),),
            (
                "fresh",
                "single",
                "staged",
                "disabled",
                "rolled-back",
                "shared-single",
                "shared-staged",
                "shared-both",
            ),
            "uninstall-controller-systemd.sh",
        ),
        Scenario(
            "linux-app",
            "install-controller-systemd.sh",
            "controller",
            "Linux",
            (("FAKE_UNITS", APP_UNITS),),
            (
                "fresh",
                "single",
                "staged",
                "disabled",
                "rolled-back",
                "shared-single",
                "shared-staged",
            ),
            "uninstall-controller-systemd.sh",
        ),
        Scenario(
            "linux-timers-to-app",
            "install-controller-systemd.sh",
            "controller",
            "Linux",
            (("FAKE_UNITS", APP_UNITS),),
            ("single", "staged"),
            start="linux-timers",
        ),
        Scenario(
            "linux-app-to-timers",
            "install-controller-systemd.sh",
            "controller",
            "Linux",
            (("FAKE_UNITS", TIMER_UNITS),),
            ("staged",),
            start="linux-app",
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
                "rolled-back",
                "shared-single",
                "shared-staged",
                "shared-both",
            ),
            "uninstall-display-local.sh",
        ),
        Scenario(
            "remote-display",
            "install-display-node.sh",
            "display",
            "Linux",
            (),
            ("fresh", "single", "staged", "disabled", "shared-staged"),
        ),
    )
}


# Machine state -----------------------------------------------------------------


def digest(path: Path, *, inside: str = "") -> str:
    """Hash a tree's names, kinds, link targets and bytes; skip bytecode caches.

    With ``inside``, hash it as the only entry of a directory, under that name.
    """
    hasher = hashlib.sha256()
    if not path.exists() and not path.is_symlink():
        return "absent"
    items = [(".", path)] if not path.is_dir() or path.is_symlink() else []
    if path.is_dir() and not path.is_symlink():
        items = [(str(item.relative_to(path)), item) for item in sorted(path.rglob("*"))]
    if inside:
        items = [(inside, path)] + [(f"{inside}/{name}", item) for name, item in items]
    for name, item in items:
        if "__pycache__" in item.parts:
            continue
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

    def __init__(self, root: Path, scenario: Scenario, *, create: bool = True) -> None:
        super().__init__(root, scenario.system, number_file_commands=True, create=create)
        self.scenario = scenario
        self.key = root / "synthetic-key"
        if create:
            self.config.write_text(f"synthetic private config {SECRET}\n")
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
        """Run under the account-wide process ceiling and this run's own budget."""
        caps = limits()
        command = [scenario.shell, "-c", 'ulimit -u "$0" && exec "$@"', str(caps.ceiling)]
        return run_contained(
            [*command, scenario.shell, str(script)],
            self.environment_for(scenario, **extra),
            watch=caps.watch,
        )

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
        scenario = SCENARIOS[scenario.start] if scenario.start else scenario
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
            elif scenario.name == "linux-app":
                self.set_unit(f"{self.family}-scheduler.service", active=False, enabled=False)
            elif scenario.role == "controller":
                self.set_unit(f"{self.family}-generate.timer", active=False, enabled=False)
            else:
                self.set_unit(f"{self.family}-display.timer", active=False, enabled=False)
        elif state == "renamed":
            self.install_renamed_display()
        elif state == "rolled-back":
            self.install_rolled_back(scenario)
        elif state == "shared-single":
            self.install(other, "single", "other")
            self.install(scenario, "single", "old")
        elif state == "shared-staged":
            self.install(other, "staged", "other")
        elif state in ("shared-both", "shared"):
            self.install(other, "staged", "other")
            self.install(scenario, "staged", "old")
        else:
            raise AssertionError(state)
        self.check_built(scenario, state)

    def install_rolled_back(self, scenario: Scenario) -> None:
        """An upgrade the owner undid by hand by switching back to the kept version.

        The controller comes from the single-directory layout, so the version
        it switches back to is the environment that upgrade moved into a
        runtime of its own; the display switches back to a kept runtime.
        """
        if scenario.role == "controller":
            self.install(scenario, "single", "old")
            self.install(scenario, "staged", "mid")
            runtimes = self.app / ".runtimes"
            active = Path(os.path.realpath(self.app / ".venv")).parent
            (kept,) = (path for path in runtimes.iterdir() if path.resolve() != active.resolve())
            link = self.app / ".venv"
            link.unlink()
            link.symlink_to(kept / ".venv")
        else:
            self.install(scenario, "staged", "old")
            first = display_runtime(self).source
            assert first is not None
            self.install(scenario, "staged", "mid")
            # pip install -e <kept runtime>: the venv imports its source again.
            site = self.venv / "lib/python3/site-packages"
            (site / "_inky_bird.pth").write_text(f"{first}\n")
            self.save_state("pip", {"inky-bird": {"source": str(first.parent)}})
        self.restart_running()

    def restart_running(self) -> None:
        """Restart every running service so it runs what its executable now resolves to."""
        processes = self.state_of("processes")
        for unit in list(processes):
            executable = executable_of(self, unit)
            assert executable is not None, unit
            processes[unit] = fake_tools.resolve_runtime(str(executable))
        self.save_state("processes", processes)

    def check_built(self, scenario: Scenario, state: str) -> None:
        """A starting state is what it claims to be, whichever installer built it."""
        role = view(self, scenario.role)
        expected = {
            "fresh": None,
            "shared-staged": None,
            "renamed": "regular",
        }.get(state, "old")
        assert role.tag == expected, (state, role.tag)
        for name, process in role.processes.items():
            assert process["runtime"] == expected, (state, name, process)
        if state in ("staged", "shared-both", "shared"):
            active = active_runtime(self, scenario.role)
            assert active is not None and (Path(active) / "src").is_dir(), state
            assert (Path(active) / "deploy" / installed_script(self, scenario).name).is_file()
        if state.startswith("shared"):
            other = view(self, "display" if scenario.role == "controller" else "controller")
            # In the single-directory layout both roles share one source copy.
            tags = ("old", "other") if state == "shared-single" else ("other",)
            assert other.tag in tags, (state, other.tag)

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
    real = Path(os.path.realpath(venv))
    # A controller venv runs from where it is; a deleted or moved one is missing.
    if (real / "tag").exists():
        return (real / "tag").read_text().strip(), real
    # A regular install in site-packages is imported before any editable one.
    if (site / "inky_bird_frame").is_dir():
        return "regular", site
    if pth.exists():
        source = Path(pth.read_text().split()[0])
        tag = source / "runtime-tag"
        return (tag.read_text().strip() if tag.exists() else "missing"), source.parent
    tag = Path(os.path.realpath(venv)) / "tag"
    return (tag.read_text().strip() if tag.exists() else "missing"), Path(os.path.realpath(venv))


@dataclass(frozen=True)
class Code:
    tag: str | None
    venv: Path | None = None
    source: Path | None = None


def controller_runtime(machine: Machine) -> Code:
    venv = machine.app / ".venv"
    if not venv.exists():
        return Code(None)
    real = Path(os.path.realpath(venv))
    tag = real / "tag"
    return Code(tag.read_text().strip() if tag.exists() else "missing", real, imported_source(real))


def imported_source(venv: Path) -> Path:
    """The source tree a controller venv imports, as its editable install names it."""
    pth = venv / "lib/python3/site-packages/_inky_bird.pth"
    return Path(pth.read_text().split()[0]) if pth.exists() else venv.parent / "src"


def display_runtime(machine: Machine) -> Code:
    site = machine.venv / "lib/python3/site-packages"
    found = runtime_of(machine.venv / "bin/inky-bird")
    if found is None or not site.exists() or not any(site.iterdir()):
        return Code(None)
    tag, directory = found
    return Code(tag, site, directory / "src" if tag != "regular" else None)


def controller_code(machine: Machine) -> tuple[str | None, str]:
    code = controller_runtime(machine)
    return code.tag, code_digest(code)


def display_code(machine: Machine) -> tuple[str | None, str]:
    code = display_runtime(machine)
    return code.tag, code_digest(code)


def code_digest(code: Code) -> str:
    if code.tag is None:
        return "absent"
    return (digest(code.venv) if code.venv else "") + (digest(code.source) if code.source else "")


def same_path(first: Path, second: Path) -> bool:
    return os.path.realpath(first) == os.path.realpath(second)


def runtime_tree(machine: Machine, role: str) -> dict[str, Path]:
    """Everything the active version runs and reinstalls from, by part.

    A runtime of its own is hashed whole. A single-directory install keeps
    its source, installer and project files in the managed directory, beside
    catalogs and other state that are not part of it. A controller venv moved
    out of that directory is a runtime that holds only the venv. The display's
    environment is shared by every version and is not part of one.
    """
    code = controller_runtime(machine) if role == "controller" else display_runtime(machine)
    if code.tag in (None, "regular", "missing") or code.source is None:
        return {}
    home = code.source.parent
    parts: dict[str, Path] = {}
    if same_path(home, machine.app):
        for name in ("src", "deploy", "pyproject.toml", "uv.lock"):
            parts[name] = home / name
    else:
        parts["runtime"] = home
    if role == "controller" and code.venv is not None and not same_path(code.venv.parent, home):
        parts["venv runtime"] = code.venv.parent
    elif role == "controller" and code.venv is not None and same_path(home, machine.app):
        parts[".venv"] = code.venv
    return parts


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
    active = controller_runtime(machine) if role == "controller" else display_runtime(machine)
    return RoleView(active.tag, code_digest(active), units, files, processes, runtimes)


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
    # The display's pre-1.0 regular install, which a shared environment holds.
    regular: str = "absent"
    # Each part of the version installed before the run: where it is, its hash.
    previous: dict[str, tuple[Path, str]] = field(default_factory=dict)
    # A single-directory venv hashed as the only entry of a runtime, as an
    # upgrade moves it.
    venv_alone: str = "absent"


def snapshot(machine: Machine, scenario: Scenario) -> Snapshot:
    other_role = "display" if scenario.role == "controller" else "controller"
    other = view(machine, other_role)
    tree = runtime_tree(machine, scenario.role)
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
        digest(machine.venv / "lib/python3/site-packages/inky_bird_frame"),
        {name: (path, digest(path)) for name, path in tree.items()},
        digest(tree[".venv"], inside=".venv") if ".venv" in tree else "absent",
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
    arguments: tuple[str, ...] = ()
    # A first failure whose cleanup this case fails or stops at ``step``.
    first: int | None = None

    @property
    def id(self) -> str:
        """``<scenario>/<state>/<step>-<tool>-<mode>``: stable across runs, selectable with -k."""
        scenario = self.scenario if self.kind == "install" else f"{self.scenario}-uninstall"
        after = "" if self.first is None else f"-after{self.first:03d}"
        return f"{scenario}/{self.state}/{self.step:03d}-{self.tool}-{self.mode}{after}"


@dataclass
class Outcome:
    returncode: int
    output: str
    violations: list[str] = field(default_factory=list)
    # The invariants whose checks had something to check in this case.
    covered: set[str] = field(default_factory=set)


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
    covered = outcome.covered
    covered.add("I9")
    # I1: every recorded process still finds the directory it started from,
    # and every enabled or running unit would start from an existing runtime.
    if machine.state_of("processes"):
        covered.add("I1")
    for name, process in machine.state_of("processes").items():
        directory = Path(str(process["directory"]))
        if not directory.exists() or directory.stat().st_ino != process["inode"]:
            report.append(f"I1: {name} runs from {directory}, which no longer exists as it was")
    for role in ("controller", "display"):
        current = view(machine, role)
        for unit, (enabled, active) in current.units.items():
            if not (enabled or active):
                continue
            covered.add("I1")
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
        covered.add("I5")
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
        if active:
            covered.add("I7")
        if active and current.files[unit] == "absent":
            report.append(f"I7: {unit} is still running but its definition was deleted")
    # I9: nothing private is printed.
    if SECRET in output or "synthetic private config" in output or "synthetic key" in output:
        report.append("I9: private configuration or key content was printed")
    roots = allowed_output_roots(machine)
    for match in PATH_PATTERN.findall(output):
        if match.startswith(str(machine.root)) and not match.startswith(roots):
            report.append(f"I9: a private path was printed: {match}")


# Each installer's own last word on success, as its final lines begin. A run
# that prints one says the new version is installed, whatever else it says.
SUCCESS_LINES = (
    "Controller installed from ",
    "Display node installed from ",
    "Display node deployed to ",
)


def says_installed(output: str) -> bool:
    return any(line.startswith(SUCCESS_LINES) for line in output.splitlines())


def claims_new(outcome: Outcome) -> bool:
    return (
        outcome.returncode == 0
        or says_installed(outcome.output)
        or any(
            phrase in outcome.output
            for phrase in (
                "new version is installed",
                "uses the new version",
                "the new one stays active",
                "the new one is kept in",
            )
        )
    )


def completed(outcome: Outcome) -> bool:
    """The run finished installing the new version; only a step after that failed."""
    return (
        outcome.returncode == 0
        or says_installed(outcome.output)
        or any(
            phrase in outcome.output
            for phrase in (
                "is installed and running, but a final step failed",
                "new version is installed, but a final step failed",
                "Stopped while finishing",
            )
        )
    )


def claims_previous(outcome: Outcome) -> bool:
    return any(
        phrase in outcome.output
        for phrase in (
            "previous version is running again",
            "started again on the previous version",
            "previous version is unchanged",
            "still runs the previous version",
        )
    )


def single_fault(case: Case) -> bool:
    """Only the injected call failed, so every rollback command works and the
    run must put everything back; naming what it left is no excuse."""
    return case.mode != "expire" and case.first is None


def unrestorable(case: Case, unit: str) -> bool:
    """The rollback could not put ``unit`` back: sudo had expired, or the
    failed cleanup call was one of this unit's own (for a timer, also its
    service's)."""
    if case.mode == "expire":
        return True
    if case.first is None or case.mode != "fail":
        return False
    names = {unit}
    if unit.endswith(".timer"):
        names.add(unit.replace(".timer", ".service"))
    if unit.startswith("com."):
        # An agent's restore also works on copies named by its label alone.
        names.add(unit.rsplit(".", 1)[1])
    return any(name in argument for name in names for argument in case.arguments)


# What a failed run says about a unit it did not put back, as each statement reads.
UNIT_STATEMENTS = (
    ("stuck", r"could not be stopped or disabled and may still run the new version, kept in \S+"),
    ("running", r"(?:services|agents) could not be stopped"),
    ("stopped", r"these services are stopped"),
    ("unrestored", r"these agents are not running"),
    ("changed", r"services are not set up as they were before the upgrade"),
    ("differs", r"display services are not as they were before the upgrade"),
    ("restarted", r"services were restarted on the new version"),
)
UNIT_STATEMENT = re.compile(
    "(?:"
    + "|".join(f"(?P<{name}>{pattern})" for name, pattern in UNIT_STATEMENTS)
    + r"): (?P<units>.+?)\.$",
    re.MULTILINE,
)


def named_units(output: str) -> dict[str, set[str]]:
    """Each unit the output names, with what it says about that unit."""
    found: dict[str, set[str]] = {}
    for match in UNIT_STATEMENT.finditer(output):
        statement = next(name for name, _ in UNIT_STATEMENTS if match.group(name))
        for unit in match.group("units").split():
            found.setdefault(unit, set()).add(statement)
    return found


def explained(
    statements: set[str],
    before: tuple[bool, bool],
    now: tuple[bool, bool],
    file_changed: bool,
    *,
    agent: bool,
) -> bool:
    """Whether what the output says about a unit accounts for every way it differs."""
    # An agent's two fields are both "loaded"; the second is checked below.
    if before[0] != now[0] and not agent:
        if now[0] and not statements & {"changed", "differs", "stuck"}:
            return False
        if not now[0] and not statements & {"changed", "differs", "unrestored", "stopped"}:
            return False
    if before[1] != now[1]:
        if now[1] and not statements & {"running", "stuck", "differs"}:
            return False
        if not now[1] and not statements & {"stopped", "unrestored", "differs"}:
            return False
    # A unit that could not be stopped keeps its file; one not put back may
    # have lost its saved definition.
    return not file_changed or bool(
        statements & {"changed", "differs", "stuck", "running", "unrestored"}
    )


def excused(case: Case, unit: str, named: dict[str, set[str]], statements: set[str]) -> bool:
    """A unit may differ only when its rollback could not be done and the
    output says exactly how it differs (``statements`` that would account for it)."""
    return unrestorable(case, unit) and bool(named.get(unit, set()) & statements)


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
    covered = outcome.covered
    covered.update(("I2", "I8", "I10") if after.processes else ("I8", "I10"))
    if before.role.tag is not None:
        covered.add("I6")
    if failed or after.units != before.role.units:
        covered.add("I4")
    # I8: an injected failure never reports success, and a kill is never silent.
    stopped = case.mode in STOP_MODES
    if outcome.returncode == 0 and case.mode != "none":
        # The new version runs; only owner setup, or a leftover the output names, remains.
        tolerated = (case.tool == "inky-bird" and "owner setup is incomplete" in output) or (
            case.tool in ("rm", "ssh")
            and ("by hand: " in output or "after reconnecting: " in output)
        )
        if stopped or not tolerated:
            report.append("I8: the run reported success although it was failed or stopped")
    named = named_units(output)
    # I2: what is said to run is what runs, in both directions: every service,
    # every enabled or running unit and the active link. Only a unit whose
    # rollback could not be done, and that the output says may run the other
    # version, may differ.
    previous = claims_previous(outcome)
    if previous and before.role.tag is None:
        report.append("I2: the output says the previous version runs, but there was none")
    if previous and claims_new(outcome):
        report.append("I2: the output says both the previous and the new version run")
    expected = "new" if claims_new(outcome) else before.role.tag
    other_version = {"stuck", "running", "restarted", "differs"}
    if (previous or claims_new(outcome)) and after.tag != expected:
        report.append(f"I2: the output implies {expected} is active, but {after.tag} is")
    for unit, process in after.processes.items():
        if process["runtime"] != expected and not excused(case, unit, named, other_version):
            report.append(
                f"I2: {unit} runs {process['runtime']}, the output implies {expected}"
                + ("" if unit in named else ", and it is not named")
            )
    for unit, (enabled, active) in after.units.items():
        if not (enabled or active) or not (
            unit.endswith(".timer") or previous or claims_new(outcome)
        ):
            continue
        executable = executable_of(machine, unit)
        found = runtime_of(executable) if executable is not None else None
        if (
            found is not None
            and found[0] != expected
            and not excused(case, unit, named, other_version)
        ):
            report.append(f"I2: {unit} would run {found[0]}, not {expected}")
    if claims_new(outcome):
        check_previous_kept(machine, scenario, before, report, output)
    if not failed:
        check_success(machine, scenario, case, before, after, report, output)
        return
    if completed(outcome):
        # A late step failed after the new version was installed and started.
        check_final_units(machine, scenario, after, report, case, named)
    # I4: every unit is back as it was. Only a unit whose rollback could not be
    # done may differ, and only in the way the output says it does. That holds
    # even when the new version could not be taken back out.
    agent = machine.system == "Darwin"
    for unit, state in before.role.units.items() if not completed(outcome) else ():
        now = after.units[unit]
        if (
            unit.endswith(".service")
            and not long_running(unit)
            and state[0] == now[0]
            and not now[1]
        ):
            continue
        file_changed = after.files[unit] != before.role.files[unit]
        if now == state and not file_changed:
            continue
        statements = named.get(unit, set())
        if unrestorable(case, unit) and explained(
            statements, state, now, file_changed, agent=agent
        ):
            continue
        report.append(
            f"I4: {unit} was {state} and is now {now}"
            + (", its definition changed" if file_changed else "")
            + (f"; the output says {sorted(statements)}" if statements else ", and it is not named")
        )
    # I6: until the output says the new version is in use, the previous one is
    # intact: what runs now, and every part of the version that ran before.
    if (
        before.role.tag is not None
        and not claims_new(outcome)
        and not shadowed_leftover(machine, case, before, outcome)
    ):
        if after.code != before.role.code:
            report.append("I6: the previous version's runtime, environment or source changed")
        for name, (part, code) in before.previous.items():
            if digest(part) != code:
                report.append(f"I6: the previous version's {name} changed")
    # I10: nothing staged is left behind unless the output names it, or it is
    # the runtime the output says is now in use.
    in_use = active_runtime(machine, scenario.role) if claims_new(outcome) else None
    for path in sorted(after.runtimes - before.role.runtimes):
        if path not in output and path != in_use:
            report.append(f"I10: runtime {path} was left behind without being named")
    # Once the new version is in use, versions older than the one it replaced go.
    for path in sorted(before.role.runtimes - after.runtimes) if not completed(outcome) else ():
        report.append(f"I10: runtime {path} existed before and was removed by a failed run")
    for path in sorted(stray_paths(machine) - before.stray):
        if path not in output and not removal_failed(case, path, machine.root):
            report.append(f"I10: {path} was left behind")


def shadowed_leftover(machine: Machine, case: Case, before: Snapshot, outcome: Outcome) -> bool:
    """The one change I6 accepts: a second failure kept the new display
    package from being taken out of the shared environment, the output says
    so, and the renamed install Python still imports first is unchanged."""
    return (
        case.first is not None
        and case.mode == "fail"
        and case.tool == "venv-python"
        and before.role.tag == "regular"
        and "the new version could not be taken out" in outcome.output
        and digest(machine.venv / "lib/python3/site-packages/inky_bird_frame") == before.regular
    )


KEPT = re.compile(
    r"(?:Previous version kept for rollback: |The previous version is kept in )(\S+?)\.?$",
    re.MULTILINE,
)


def check_previous_kept(
    machine: Machine, scenario: Scenario, before: Snapshot, report: list[str], output: str
) -> None:
    """I6 once the new version is in use: the version it replaced is still
    byte-for-byte what ran before, wherever its environment now lives."""
    kept = KEPT.search(output)
    for name, (path, code) in before.previous.items():
        if name == "deploy":
            # The managed directory's installer is replaced: it reinstalls the
            # active version, and the kept one is found from the venv link.
            continue
        if name == ".venv" and not (path.is_dir() and not path.is_symlink()):
            # A single-directory environment moves into a runtime of its own,
            # which holds nothing else.
            if kept is None:
                report.append("I6: the previous version's environment moved and is not named")
            elif digest(Path(kept.group(1))) != before.venv_alone:
                report.append("I6: the runtime the previous environment moved into differs")
            continue
        if digest(path) != code:
            report.append(f"I6: the previous version's {name} changed after the upgrade")


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


def removal_failed(case: Case, path: str, root: Path) -> bool:
    """A failed ``rm`` of exactly that path is the one thing allowed to leave it."""
    # Either spelling of a path through a symlinked directory names the same path.
    targets = {os.path.realpath(on_machine(argument, root)) for argument in case.arguments}
    return case.tool == "rm" and case.mode == "fail" and os.path.realpath(path) in targets


def check_success(
    machine: Machine,
    scenario: Scenario,
    case: Case,
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
    # An older runtime may stay only when its removal was the injected failure
    # and the output names it to remove by hand.
    by_hand = [line for line in output.splitlines() if "by hand: " in line]
    named = {
        path
        for path in after.runtimes
        if removal_failed(case, path, machine.root) and any(path in line for line in by_hand)
    }
    if len(after.runtimes - named) != expected:
        report.append(f"I10: after success {len(after.runtimes)} runtimes remain, not {expected}")
    for path in sorted(stray_paths(machine) - before.stray):
        if path not in output:
            report.append(f"I10: {path} was left behind after success")


def check_final_units(
    machine: Machine,
    scenario: Scenario,
    after: RoleView,
    report: list[str],
    case: Case | None = None,
    named: dict[str, set[str]] | None = None,
) -> None:
    """I4 once the new version is installed: every unit as the new version wants it,
    except a unit whose setup could not be done and that a failed run names."""
    found: list[str] = []
    check_intended_units(machine, scenario, after, found)
    everything = {name for name, _ in UNIT_STATEMENTS}
    report.extend(
        item
        for item in found
        if case is None or not excused(case, item.split()[3], named or {}, everything)
    )


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
    outcome.covered.update(("U1", "U2", "U3"))
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
    outcome.covered.add("I3")
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
        # setup picks the checkout of the source the installed command imports.
        venv = machine.app / ".venv"
        if venv.exists():
            return imported_source(Path(os.path.realpath(venv))).parent / "deploy" / name
    else:
        found = runtime_of(machine.venv / "bin/inky-bird")
        if found is not None and found[0] not in ("regular", "missing"):
            return found[1] / "deploy" / name
    return machine.script(name)


# Process limits ---------------------------------------------------------------

WORKERS = max(1, int(os.environ.get("INKY_MATRIX_WORKERS", "6")))
# How many processes one run may have at once before its whole group is killed.
RUN_PROCESS_BUDGET = 100
WATCH_INTERVAL_SECONDS = 0.25


def account_tasks() -> int:
    """What this account runs now, as the process limit counts it: processes
    on macOS, threads on Linux."""
    uid = str(os.getuid())
    if platform.system() == "Linux":
        command = ["/bin/ps", "-L", "-U", uid, "-o", "lwp="]
    else:
        command = ["/bin/ps", "-U", uid, "-o", "pid="]
    listing = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    return len(listing.split())


class Limits:
    """Keep every run's processes bounded.

    ``ulimit -u`` counts every process of the account, not those of one
    shell, so it cannot give each worker a share: every run starts under one
    ceiling a little above what the account already runs plus every worker's
    budget, which keeps a runaway far from the machine's limit, and a watchdog
    kills any one run's process group that grows past its own budget.
    """

    def __init__(self, budget: int = RUN_PROCESS_BUDGET) -> None:
        self.budget = budget
        configured = os.environ.get("INKY_MATRIX_PROCESS_CEILING")
        self.ceiling = (
            int(configured) if configured else account_tasks() + 64 + WORKERS * RUN_PROCESS_BUDGET
        )
        soft, _ = resource.getrlimit(resource.RLIMIT_NPROC)
        if soft != resource.RLIM_INFINITY and self.ceiling > soft - 64:
            raise RuntimeError(
                f"the matrix would allow {self.ceiling} processes, too close to this "
                f"account's limit of {soft}; use fewer INKY_MATRIX_WORKERS"
            )
        self.lock = threading.Lock()
        # Each watched process group, and the most processes it was seen with.
        self.groups: dict[int, int] = {}
        self.thread: threading.Thread | None = None

    @contextlib.contextmanager
    def watch(self, group: int) -> Iterator[None]:
        with self.lock:
            self.groups[group] = 0
            if self.thread is None:
                self.thread = threading.Thread(target=self.loop, daemon=True)
                self.thread.start()
        try:
            yield
        finally:
            with self.lock:
                peak = self.groups.pop(group)
            if peak > self.budget:
                raise AssertionError(
                    f"the run had {peak} processes at once, over its budget of "
                    f"{self.budget}, and was killed"
                )

    def loop(self) -> None:
        while True:
            time.sleep(WATCH_INTERVAL_SECONDS)
            with self.lock:
                if not self.groups:
                    self.thread = None
                    return
                watched = set(self.groups)
            listing = subprocess.run(
                ["/bin/ps", "-ax", "-o", "pgid="], capture_output=True, text=True, check=False
            ).stdout
            counts = Counter(int(value) for value in listing.split())
            for group in watched:
                if counts[group] <= self.budget:
                    continue
                with self.lock:
                    # Only a run still being watched; its ID may be reused once it ends.
                    if group not in self.groups:
                        continue
                    self.groups[group] = max(self.groups[group], counts[group])
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(group, signal.SIGKILL)


LIMITS: Limits | None = None
LIMITS_LOCK = threading.Lock()


def limits() -> Limits:
    global LIMITS
    with LIMITS_LOCK:
        if LIMITS is None:
            LIMITS = Limits()
        return LIMITS


# Cloning starting states ------------------------------------------------------


@dataclass(frozen=True)
class Template:
    """A machine built once; ``rewrites`` names the files and links that
    record its own absolute path, which every clone points at itself."""

    root: Path
    rewrites: tuple[str, ...]


def entries(root: Path) -> Iterator[Path]:
    """Every file, link and directory below ``root``, without following links."""
    for directory, names, files in os.walk(root):
        for name in sorted(names + files):
            yield Path(directory) / name


def freeze(root: Path) -> Template:
    # Compiled bytecode records its source's path in a length-prefixed string that
    # a rewrite would corrupt; Python compiles it again in each clone instead.
    for cache in [path for path in entries(root) if path.name == "__pycache__"]:
        shutil.rmtree(cache)
    needle = str(root)
    rewrites = []
    for path in entries(root):
        if path.is_symlink():
            if needle in os.readlink(path):
                rewrites.append(str(path.relative_to(root)))
        elif path.is_file() and needle.encode() in path.read_bytes():
            rewrites.append(str(path.relative_to(root)))
    return Template(root, tuple(rewrites))


def clone_tree(source: Path, target: Path) -> None:
    """Copy a tree with a filesystem clone where the filesystem offers one."""
    option = "-c" if platform.system() == "Darwin" else "--reflink=auto"
    result = subprocess.run(
        ["/bin/cp", option, "-pR", str(source), str(target)], capture_output=True, check=False
    )
    if result.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target, symlinks=True)


def thaw(template: Template, target: Path) -> None:
    """Make ``target`` a copy of the template, recording ``target`` as its path."""
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    clone_tree(template.root, target)
    # The realpath form (/private/var for /var on macOS) contains the plain one.
    old, new = str(template.root), str(target)
    for name in template.rewrites:
        path = target / name
        if path.is_symlink():
            destination = os.readlink(path).replace(old, new)
            path.unlink()
            path.symlink_to(destination)
        else:
            path.write_bytes(path.read_bytes().replace(old.encode(), new.encode()))


# Running cases ----------------------------------------------------------------

MACHINE_TOKEN = "{machine}"


def off_machine(text: str, root: Path) -> str:
    """A recorded argument with the machine's own path, in either form, as a token."""
    for form in (os.path.realpath(root), str(root)):
        text = text.replace(form, MACHINE_TOKEN)
    return text


def on_machine(text: str, root: Path) -> str:
    """A recorded argument as it reads on the machine at ``root``."""
    return text.replace(MACHINE_TOKEN, str(root))


def fix_inodes(machine: Machine) -> None:
    """A copy is the same machine; processes keep running from the same places."""
    processes = machine.state_of("processes")
    for process in processes.values():
        directory = Path(str(process["directory"]))
        process["inode"] = directory.stat().st_ino if directory.exists() else 0
    machine.save_state("processes", processes)


class Group:
    """Cases that share a starting state; every run starts from a fresh clone of it."""

    def __init__(self, kind: str, scenario: Scenario, state: str, template: Template) -> None:
        self.kind = kind
        self.scenario = scenario
        self.state = state
        self.template = template

    def machine(self, slot: Path) -> Machine:
        thaw(self.template, slot)
        machine = Machine(slot, self.scenario, create=False)
        fix_inodes(machine)
        return machine

    def script(self, machine: Machine) -> Path:
        name = self.scenario.script if self.kind == "install" else self.scenario.uninstall
        return machine.source / "deploy" / name

    def run(self, machine: Machine, injection: str | None) -> tuple[Outcome, list[list[str]]]:
        if injection is not None:
            (machine.fake / "inject").write_text(injection + "\n")
        try:
            result = machine.execute(self.script(machine), self.scenario)
            outcome = Outcome(result.returncode, result.stdout + result.stderr)
        except AssertionError as error:
            outcome = Outcome(-1, "", [f"the run was not contained: {error}"])
        steps = machine.steps()
        (machine.fake / "inject").unlink(missing_ok=True)
        return outcome, steps

    def record(
        self, slot: Path, injection: str | None = None
    ) -> tuple[list[list[str]], list[str], bool]:
        """The calls of one run, a clean one or the cleanup after a first
        failure; what was wrong with it; and whether it ended as it must for
        its calls to be failed in turn."""
        machine = self.machine(slot)
        before = snapshot(machine, self.scenario)
        outcome, steps = self.run(machine, injection)
        if injection is None and not outcome.violations:
            # The run without a fault is checked like any other.
            clean = Case(self.kind, self.scenario.name, self.state, -1, "none", "none")
            if self.kind == "install":
                check_install(machine, self.scenario, clean, before, outcome)
            else:
                check_uninstall(machine, self.scenario, before, outcome)
        problems = list(outcome.violations)
        usable = outcome.returncode != -1
        if injection is None and outcome.returncode != 0:
            usable = False
            problems.append(f"I8: the run without a fault exited {outcome.returncode}")
        if injection is not None and outcome.returncode == 0:
            usable = False
            step = injection.split()[0]
            problems.append(f"I8: the run whose call {step} failed exited 0")
        survivors = machine.survivors()
        if survivors:
            usable = False
            problems.append(f"processes outlived the run: {survivors}")
        if problems:
            problems.append(outcome.output[-3000:])
        # Recordings are made in any worker's directory and used in every other.
        recorded = [[off_machine(part, slot) for part in step] for step in steps]
        return recorded, problems, usable

    def rerun(self, machine: Machine, outcome: Outcome) -> None:
        try:
            if self.kind == "install":
                check_rerun(machine, self.scenario, outcome)
            else:
                machine.reset_run()
                rerun = machine.execute(self.script(machine), self.scenario)
                if rerun.returncode != 0:
                    outcome.violations.append(
                        "I3: a second uninstall failed: " + rerun.stderr[-800:]
                    )
        except AssertionError as error:
            outcome.violations.append(f"I3: the rerun was not contained: {error}")

    def evaluate(self, case: Case, recorded: list[list[str]], slot: Path) -> Outcome:
        machine = self.machine(slot)
        before = snapshot(machine, self.scenario)
        injection = f"{case.step} {case.mode}"
        if case.first is not None:
            injection = f"{case.first} fail\n{injection}"
        outcome, steps = self.run(machine, injection)
        survivors = machine.survivors()
        if survivors:
            outcome.violations.append(f"processes outlived the run: {survivors}")
        if len(steps) <= case.step or steps[case.step][2] != recorded[case.step][2]:
            outcome.violations.append(
                f"the run diverged from the recording before step {case.step}"
            )
            return outcome
        if self.kind == "install":
            check_install(machine, self.scenario, case, before, outcome)
        else:
            check_uninstall(machine, self.scenario, before, outcome)
        self.rerun(machine, outcome)
        survivors = machine.survivors()
        if survivors:
            outcome.violations.append(f"processes outlived the rerun: {survivors}")
        return outcome

    def busy(self, slot: Path) -> Outcome:
        """I8: a plate being made stops an upgrade with exit 3 and no change."""
        machine = self.machine(slot)
        before = snapshot(machine, self.scenario)
        with running_generation(machine):
            outcome, _ = self.run(machine, None)
        outcome.covered.add("I8")
        survivors = machine.survivors()
        if survivors:
            outcome.violations.append(f"processes outlived the run: {survivors}")
        if outcome.returncode != 3:
            outcome.violations.append(f"I8: busy run exited {outcome.returncode}, not 3")
        after = snapshot(machine, self.scenario)
        if after.tree != before.tree or after.stray != before.stray:
            outcome.violations.append("I8: a busy run changed the machine")
        return outcome


# Which cases run --------------------------------------------------------------

# INKY_INSTALLER_MATRIX: unset for the default subset, "sample" or "full".
MODE = os.environ.get("INKY_INSTALLER_MATRIX", "")
assert MODE in ("", "sample", "full"), f"INKY_INSTALLER_MATRIX={MODE!r} is not sample or full"
FULL = MODE == "full"
SAMPLE = MODE == "sample"
# Narrow a run to some groups while working on one, e.g. "install:macos:single".
ONLY = set(filter(None, os.environ.get("INKY_INSTALLER_MATRIX_ONLY", "").split(",")))
# "<i>/<n>": run the i-th of n equal parts of the selected cases, as CI does.
SHARD = os.environ.get("INKY_MATRIX_SHARD", "")
# Tools (sudo, rm, launchctl) or invariants (I6, U2): run only the cases whose
# failed or stopped call is one of these tools, or whose checks of one of these
# invariants had something to check in the last recorded result for that case.
TOUCHING = set(filter(None, os.environ.get("INKY_MATRIX_TOUCHING", "").split(",")))
INVARIANT = re.compile(r"[IU]\d+")
STATE_DIRECTORY = Path(
    os.environ.get("INKY_MATRIX_STATE_DIR") or str(ROOT / "build/installer-matrix")
)
# The default subset fails the first and the middle call of every scenario and
# start state, stops the installer at that middle call, and fails each cleanup
# step that moves, links or removes a runtime after a failed upgrade from the
# single-directory layout on both controllers and on a shared Pi's display.
# "sample" fails every eighth call of every group instead.
SAMPLE_STRIDE = 8
DEFAULT_SECOND_PASS = {
    ("install", "macos", "single"),
    ("install", "linux-timers", "single"),
    ("install", "display", "shared-single"),
}
UNINSTALL_STATES = ("single", "staged", "shared")
# States in which this role is already installed, so an upgrade checks for a plate.
UPGRADE_STATES = ("single", "staged", "disabled", "rolled-back", "shared-single", "shared-both")


def second_pass(kind: str, scenario: str, state: str) -> bool:
    """Groups whose cleanup is failed a second time: every upgrade when the
    whole matrix runs, otherwise the default subset's three."""
    if kind != "install" or SAMPLE:
        return False
    return FULL or (kind, scenario, state) in DEFAULT_SECOND_PASS


def first_failures(steps: list[list[str]]) -> list[int]:
    """Two failures with the most to undo: the call right after the switch to
    the new version, and the last service start before the run completes."""
    actions = [int(index) for index, kind_of_call, *_ in steps if kind_of_call == "action"]
    switch = [
        int(index)
        for index, kind_of_call, tool, *arguments in steps
        if kind_of_call == "action"
        and (
            (tool == "python3" and any(item.endswith(".venv.next") for item in arguments))
            or (tool == "venv-python" and arguments[:3] == ["-m", "pip", "install"])
        )
    ]
    starts = [
        int(index)
        for index, kind_of_call, tool, *arguments in steps
        if kind_of_call == "action" and ("restart" in arguments or "bootstrap" in arguments)
    ]
    found = set()
    if switch:
        after = [index for index in actions if index > switch[-1]]
        found.update(after[:1])
    found.update(starts[-1:])
    return sorted(found)


def switches_back(tool: str, arguments: list[str]) -> bool:
    """A cleanup file command that moves, links or removes a runtime: these
    always run in the second pass, where earlier rollback defects were found."""
    return tool in ("mv", "ln", "rm", "rmdir") and any(
        ".venv" in argument or "/runtime." in argument for argument in arguments
    )


def busy_checked(scenario: str, state: str) -> bool:
    return SCENARIOS[scenario].role == "controller" and state in UPGRADE_STATES


GroupKey = tuple[str, str, str]
CleanupKey = tuple[str, str, str, int]


def plan_groups() -> list[GroupKey]:
    groups: list[GroupKey] = []
    for scenario in SCENARIOS.values():
        groups.extend(("install", scenario.name, state) for state in scenario.states)
        if scenario.uninstall:
            # Only a Pi runs both roles.
            states = UNINSTALL_STATES if scenario.system == "Linux" else UNINSTALL_STATES[:-1]
            groups.extend(("uninstall", scenario.name, state) for state in states)
    if ONLY:
        groups = [group for group in groups if ":".join(group) in ONLY]
    return groups


def in_first_shard() -> bool:
    """Tests of the matrix itself, rather than its cases, run in one shard only."""
    return not SHARD or SHARD.startswith("1/")


def first_shard_only() -> None:
    if not in_first_shard():
        pytest.skip(f"runs in shard 1, not {SHARD}")


def shard(items: list[str]) -> set[str]:
    """The ids of this shard: every n-th of the sorted ids, from the i-th."""
    if not SHARD:
        return set(items)
    index, count = (int(part) for part in SHARD.split("/"))
    assert 1 <= index <= count, SHARD
    return {item for number, item in enumerate(sorted(items)) if number % count == index - 1}


# Recordings and results -------------------------------------------------------


def fingerprint() -> str:
    """Everything a recording depends on; a change records every group again."""
    hasher = hashlib.sha256()
    sources = [
        *sorted((ROOT / "deploy").glob("*.sh")),
        *sorted((ROOT / "tests/fake_bin").rglob("*.py")),
        *sorted(SINGLE_DIRECTORY.glob("*.sh")),
        ROOT / "tests/test_installer_matrix.py",
        ROOT / "tests/test_installer_behaviour.py",
    ]
    for path in sources:
        hasher.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes())
    hasher.update(f"{platform.system()} {MACOS_BASH} {sys.version_info[:2]}".encode())
    return hasher.hexdigest()


def signature(steps: list[list[str]]) -> list[tuple[str, str, str]]:
    """What a case relies on in a recording: each call's number, kind and tool."""
    return [(index, kind_of_call, tool) for index, kind_of_call, tool, *_ in steps]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def group_name(key: GroupKey) -> str:
    return ":".join(key)


def cleanup_name(key: CleanupKey) -> str:
    return ":".join((*key[:3], str(key[3])))


def group_key(name: str) -> GroupKey:
    kind, scenario, state = name.split(":")
    return (kind, scenario, state)


def cleanup_key(name: str) -> CleanupKey:
    kind, scenario, state, first = name.split(":")
    return (kind, scenario, state, int(first))


ROOT_PREFIX = "installer-matrix-"
OWNER_LOCK = "owner.lock"
# A directory without a lock is from before runs took one, or is being set up.
UNLOCKED_ROOT_AGE_SECONDS = 3600


def remove_stale_roots() -> None:
    """Remove this account's matrix directories that no running matrix owns,
    as a run that was killed leaves them."""
    for path in Path(tempfile.gettempdir()).glob(f"{ROOT_PREFIX}*"):
        try:
            if not path.is_dir() or path.is_symlink() or path.stat().st_uid != os.getuid():
                continue
            lock = path / OWNER_LOCK
            if not lock.exists():
                if time.time() - path.stat().st_mtime > UNLOCKED_ROOT_AGE_SECONDS:
                    shutil.rmtree(path, ignore_errors=True)
                continue
            with lock.open("a") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


class Matrix:
    """Build each starting state once and run cases from clones of it, in parallel.

    Recordings are cached under ``STATE_DIRECTORY`` by ``fingerprint()`` so
    cases can be listed without running anything; a group's recording is
    still made again before its first case runs, and a difference fails it.
    """

    def __init__(self) -> None:
        # A shell started with SIGINT ignored, as a background job is, cannot trap
        # it, so every interrupt case would pass through; Ctrl-C never finds that.
        if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
            signal.signal(signal.SIGINT, signal.default_int_handler)
        remove_stale_roots()
        self.root = Path(tempfile.mkdtemp(prefix=ROOT_PREFIX))
        # Held until this process ends, however it ends: a later run that can
        # take it knows this directory is no one's.
        self.owner = (self.root / OWNER_LOCK).open("w")
        fcntl.flock(self.owner.fileno(), fcntl.LOCK_EX)
        # Collecting alone records every group; remove them even if no case runs.
        atexit.register(shutil.rmtree, self.root, True)
        self.pool = ThreadPoolExecutor(WORKERS, thread_name_prefix="matrix")
        atexit.register(self.pool.shutdown, wait=False, cancel_futures=True)
        self.lock = threading.Lock()
        self.slots = threading.local()
        self.slot_count = 0
        self.locks: dict[object, threading.Lock] = {}
        self.bases: dict[tuple[str, str], Template] = {}
        self.groups: dict[GroupKey, Group] = {}
        self.verified: dict[object, str] = {}
        # This session's own recording of each run, once it has been made.
        self.fresh: dict[object, list[list[str]]] = {}
        self.futures: dict[str, Future[Outcome]] = {}
        self.cases_by_id: dict[str, Case] = {}
        # Every recording run made, by id, with what was wrong with it.
        self.recorded: dict[str, list[str]] = {}
        self.verifying: dict[str, Future[str]] = {}
        self.fingerprint = fingerprint()
        self.cache_path = STATE_DIRECTORY / "recordings.json"
        self.results_path = STATE_DIRECTORY / "results.json"
        cache = read_json(self.cache_path)
        if cache.get("fingerprint") != self.fingerprint:
            cache = {}
        groups = cache.get("groups", {})
        cleanups = cache.get("cleanups", {})
        assert isinstance(groups, dict) and isinstance(cleanups, dict)
        self.recordings: dict[GroupKey, list[list[str]]] = {}
        self.cleanups: dict[CleanupKey, list[list[str]]] = {}
        planned = plan_groups()
        missing = [key for key in planned if group_name(key) not in groups]
        for key, steps in zip(missing, self.pool.map(self.record, missing), strict=True):
            groups[group_name(key)] = steps
        for key in planned:
            self.recordings[key] = groups[group_name(key)]
        wanted = [
            (*key, first)
            for key in planned
            if second_pass(*key)
            for first in first_failures(self.recordings[key])
        ]
        missing_cleanups = [key for key in wanted if cleanup_name(key) not in cleanups]
        found = self.pool.map(self.record_cleanup, missing_cleanups)
        for cleanup, steps in zip(missing_cleanups, found, strict=True):
            cleanups[cleanup_name(cleanup)] = steps
        for cleanup in wanted:
            self.cleanups[cleanup] = cleanups[cleanup_name(cleanup)]
        if missing or missing_cleanups:
            # A run that went wrong is recorded, and checked, again next time.
            broken = {name for name, problems in self.recorded.items() if problems}
            cache = {
                "fingerprint": self.fingerprint,
                "groups": {
                    name: steps
                    for name, steps in groups.items()
                    if steps and recording_id(group_key(name)) not in broken
                },
                "cleanups": {
                    name: steps
                    for name, steps in cleanups.items()
                    if steps and recording_id(cleanup_key(name)) not in broken
                },
            }
            write_json(self.cache_path, cache)
        for key in planned:
            self.recorded.setdefault(recording_id(key), [])
        for cleanup in wanted:
            self.recorded.setdefault(recording_id(cleanup), [])

    # Machines.
    def slot(self) -> Path:
        """This worker's own directory, where each of its runs builds its machine."""
        slot: Path | None = getattr(self.slots, "path", None)
        if slot is None:
            with self.lock:
                self.slot_count += 1
                number = self.slot_count
            slot = self.root / "slots" / f"w{number:02d}" / "machine"
            self.slots.path = slot
        return slot

    def once(self, key: object) -> threading.Lock:
        with self.lock:
            return self.locks.setdefault(key, threading.Lock())

    def base(self, scenario: Scenario, state: str) -> Template:
        """The starting state as its installers left it, built once."""
        built = SCENARIOS[scenario.start] if scenario.start else scenario
        key = (built.name, state)
        with self.once(("base", key)):
            if key not in self.bases:
                directory = self.root / "bases" / f"{built.name}-{state}" / "machine"
                machine = Machine(directory, built)
                machine.build(built, state)
                assert not machine.survivors()
                self.bases[key] = freeze(directory)
            return self.bases[key]

    def group(self, key: GroupKey) -> Group:
        with self.once(("group", key)):
            if key not in self.groups:
                kind, name, state = key
                scenario = SCENARIOS[name]
                directory = self.root / "templates" / "-".join(key) / "machine"
                thaw(self.base(scenario, state), directory)
                machine = Machine(directory, scenario, create=False)
                if kind == "install":
                    machine.script(scenario.script)
                    if scenario.name == "remote-display":
                        machine.script("install-display-local.sh")
                else:
                    machine.script(scenario.uninstall)
                machine.reset_run()
                self.groups[key] = Group(kind, scenario, state, freeze(directory))
            return self.groups[key]

    # Recordings.
    def record(self, key: GroupKey) -> list[list[str]]:
        return self.keep(key, *self.group(key).record(self.slot()))

    def record_cleanup(self, key: CleanupKey) -> list[list[str]]:
        return self.keep(key, *self.group(key[:3]).record(self.slot(), f"{key[3]} fail"))

    def keep(
        self,
        key: GroupKey | CleanupKey,
        steps: list[list[str]],
        problems: list[str],
        usable: bool,
    ) -> list[list[str]]:
        """A recording's calls, unless the run did not end as it must; then
        the group has no cases, only its recording's test."""
        self.recorded[recording_id(key)] = problems
        if not usable:
            return []
        self.verified[key] = ""
        self.fresh[key] = steps
        return steps

    def verify(self, key: GroupKey | CleanupKey, cached: list[list[str]]) -> str:
        """Record again what a cached recording says; a difference is the reason."""
        with self.once(("verify", key)):
            if key not in self.verified:
                injection = None if len(key) == 3 else f"{key[3]} fail"
                fresh, problems, usable = self.group(key[:3]).record(self.slot(), injection)
                self.fresh[key] = fresh
                self.recorded[recording_id(key)] = problems
                if not usable:
                    self.verified[key] = f"the recording run of {key} went wrong: " + "\n".join(
                        problems
                    )
                elif signature(fresh) != signature(cached):
                    self.verified[key] = (
                        f"the recording of {key} changed since it was cached; run again"
                    )
                else:
                    self.verified[key] = ""
            return self.verified[key]

    # Cases.
    def cases(self) -> list[Case]:
        found: list[Case] = []
        for number, ((kind, scenario, state), steps) in enumerate(self.recordings.items()):
            actions = [
                (index, tool, tuple(arguments))
                for index, (_, kind_of_call, tool, *arguments) in enumerate(steps)
                if kind_of_call == "action"
            ]
            middle = len(actions) // 2
            for position, (index, tool, called) in enumerate(actions):
                # Alternate a stop of the installer, an interrupt of its whole
                # process group (Ctrl-C) and a hangup.
                if FULL or SAMPLE:
                    chosen = position % (1 if FULL else SAMPLE_STRIDE) == 0
                    stop = position % KILL_STRIDE == 0
                    signal_mode = STOP_MODES[(position // KILL_STRIDE) % len(STOP_MODES)]
                else:
                    chosen = position in (0, middle)
                    stop = position == middle
                    signal_mode = STOP_MODES[number % len(STOP_MODES)]
                if not chosen:
                    continue
                call = (kind, scenario, state, index)
                found.append(Case(*call, "fail", tool, called))
                if tool == "sudo":
                    found.append(Case(*call, "expire", tool))
                if stop:
                    found.append(Case(*call, signal_mode, tool))
        for (kind, scenario, state, first), steps in self.cleanups.items():
            position = 0
            for index, (_, kind_of_call, tool, *arguments) in enumerate(steps):
                if index <= first or kind_of_call != "action":
                    continue
                if FULL or switches_back(tool, arguments):
                    call = (kind, scenario, state, index)
                    found.append(Case(*call, "fail", tool, tuple(arguments), first))
                    if position % KILL_STRIDE == 0:
                        # A stop during cleanup must not cut it short.
                        signal_mode = STOP_MODES[(position // KILL_STRIDE) % len(STOP_MODES)]
                        found.append(Case(*call, signal_mode, tool, (), first))
                position += 1
        kept = shard([case.id for case in found])
        found = [case for case in found if case.id in kept and self.touches(case.id, case.tool)]
        self.cases_by_id.update((case.id, case) for case in found)
        return found

    def busy_groups(self) -> list[tuple[str, str]]:
        names = [
            (scenario, state)
            for kind, scenario, state in plan_groups()
            if kind == "install" and busy_checked(scenario, state)
        ]
        kept = shard([busy_id(*name) for name in names])
        return [name for name in names if busy_id(*name) in kept and self.touches(busy_id(*name))]

    def touches(self, case_id: str, tool: str = "busy") -> bool:
        if not TOUCHING:
            return True
        if tool in TOUCHING:
            return True
        wanted = {item for item in TOUCHING if INVARIANT.fullmatch(item)}
        if not wanted:
            return False
        results = read_json(self.results_path).get("cases")
        if not isinstance(results, dict) or not results:
            raise RuntimeError(
                "selecting by invariant needs an earlier run's results; run the matrix first"
            )
        entry = results.get(case_id)
        if not isinstance(entry, dict):
            return False
        checked = set(entry.get("covered", []))
        checked |= {str(item).split(":", 1)[0] for item in entry.get("violations", [])}
        return bool(checked & wanted)

    # Running.
    def start(self, wanted: list[str]) -> None:
        """Queue the selected cases in the order pytest will report them, after
        recording again every run they rely on, so no worker waits for another."""
        checks: dict[GroupKey | CleanupKey, list[list[str]]] = {}
        for name in wanted:
            case = self.cases_by_id.get(name)
            if case is None:
                continue
            key = (case.kind, case.scenario, case.state)
            if case.first is None:
                checks[key] = self.recordings[key]
            else:
                checks[(*key, case.first)] = self.cleanups[(*key, case.first)]
        for check, recorded in checks.items():
            if check not in self.verified:
                self.verifying[recording_id(check)] = self.pool.submit(self.verify, check, recorded)
        for name in wanted:
            if name in self.futures:
                continue
            if name in self.cases_by_id:
                self.futures[name] = self.pool.submit(self.evaluate, self.cases_by_id[name])
            elif name.startswith("clone:"):
                kind, scenario, state = name.removeprefix("clone:").split(":")
                self.futures[name] = self.pool.submit(compare_clone, self, (kind, scenario, state))
            else:
                scenario, state, _ = name.split("/")
                self.futures[name] = self.pool.submit(self.busy, scenario, state)

    def outcome(self, name: str) -> Outcome:
        return self.futures[name].result()

    def recording_problems(self, name: str) -> list[str]:
        """What was wrong with a recording run, once this session's last one is made."""
        if name in self.verifying:
            self.verifying[name].result()
        return self.recorded[name]

    def evaluate(self, case: Case) -> Outcome:
        key = (case.kind, case.scenario, case.state)
        check: GroupKey | CleanupKey = key
        recorded = self.recordings[key]
        if case.first is not None:
            check = (*key, case.first)
            recorded = self.cleanups[check]
        changed = self.verify(check, recorded)
        if changed:
            return Outcome(-1, "", [changed])
        # Paths a run creates, such as a runtime's random name, are those of this
        # session's starting state, which only this session's recording names.
        recorded = self.fresh[check]
        if case.arguments:
            case = replace(case, arguments=tuple(recorded[case.step][3:]))
        return self.group(key).evaluate(case, recorded, self.slot())

    def busy(self, scenario: str, state: str) -> Outcome:
        return self.group(("install", scenario, state)).busy(self.slot())

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)
        results = read_json(self.results_path).get("cases")
        stored = results if isinstance(results, dict) else {}
        for name, future in self.futures.items():
            if not future.done() or future.cancelled() or future.exception() is not None:
                continue
            outcome = future.result()
            case = self.cases_by_id.get(name)
            stored[name] = {
                "passed": not outcome.violations,
                "exit": outcome.returncode,
                "tool": case.tool if case else "busy",
                "covered": sorted(outcome.covered),
                "violations": outcome.violations,
            }
        if self.futures:
            write_json(self.results_path, {"fingerprint": self.fingerprint, "cases": stored})
        shutil.rmtree(self.root, ignore_errors=True)
        self.owner.close()


def busy_id(scenario: str, state: str) -> str:
    return f"{scenario}/{state}/busy"


def recording_id(key: GroupKey | CleanupKey) -> str:
    """``<scenario>/<state>/recording``, ``-after<step>`` for a failed run's cleanup."""
    kind, scenario, state = key[:3]
    name = scenario if kind == "install" else f"{scenario}-uninstall"
    after = f"-after{key[3]:03d}" if len(key) == 4 else ""
    return f"{name}/{state}/recording{after}"


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
        names = matrix().busy_groups()
        metafunc.parametrize("busy_group", names, ids=[busy_id(*name) for name in names])
    if "recording" in metafunc.fixturenames:
        recorded = sorted(shard(list(matrix().recorded)))
        metafunc.parametrize("recording", recorded, ids=recorded)


@pytest.fixture(scope="module")
def executed(request: pytest.FixtureRequest) -> Iterator[Matrix]:
    runner = matrix()
    wanted = []
    for item in request.session.items:
        callspec = getattr(item, "callspec", None)
        if callspec is None or getattr(item, "module", None) is not sys.modules[__name__]:
            continue
        # An empty parametrization leaves one skipped item without a value.
        value = next(iter(callspec.params.values()), None)
        if isinstance(value, Case):
            wanted.append(value.id)
        elif "busy_group" in callspec.params and isinstance(value, tuple):
            wanted.append(busy_id(*value))
        elif (
            "key" in callspec.params
            and isinstance(value, tuple)
            and not (TOUCHING or ONLY)
            and in_first_shard()
        ):
            wanted.append(clone_id(value))
    runner.start(wanted)
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
    outcome = executed.outcome(case.id)
    report(case.id, outcome)
    assert not outcome.violations, (
        "\n".join(outcome.violations) + f"\nexit {outcome.returncode}\n" + outcome.output[-4000:]
    )


def test_recording_run(recording: str, executed: Matrix) -> None:
    """The run every case of a group is made from: without a fault it
    succeeds and leaves what every invariant asks, and with its first fault
    it fails."""
    problems = executed.recording_problems(recording)
    assert not problems, "\n".join(problems)


def test_busy_plate_changes_nothing(busy_group: tuple[str, str], executed: Matrix) -> None:
    name = busy_id(*busy_group)
    outcome = executed.outcome(name)
    report(name, outcome)
    assert not outcome.violations, "\n".join(outcome.violations) + "\n" + outcome.output[-2000:]


def test_matrix_covers_every_action_step(executed: Matrix) -> None:
    """The recording runs saw the layout's own steps, so the cases exercise them."""
    first_shard_only()
    if ONLY:
        pytest.skip("only some groups were recorded")
    tools: set[str] = set()
    for steps in executed.recordings.values():
        tools |= {tool for _, kind_of_call, tool, *_ in steps if kind_of_call == "action"}
    assert {"mv", "ln", "rm", "mktemp", "sudo", "uv", "rsync", "ssh", "inky-bird"} <= tools
    assert {"launchctl", "systemctl", "venv-python", "python3"} <= tools


# A run of each kind of scenario, compared on a fresh build and on a clone.
CLONE_CHECKS = (
    ("install", "macos", "single"),
    ("install", "linux-timers", "shared-single"),
    ("install", "linux-app", "staged"),
    ("install", "display", "shared-both"),
    ("install", "remote-display", "single"),
    ("uninstall", "linux-timers", "shared"),
)
RANDOM_NAME = re.compile(
    r"((?:runtime|install|tmp|catalog-publish-recovery|inky-bird-stage)\.)[A-Za-z0-9]{6,12}"
)


def normalized(text: str, root: Path) -> str:
    """Text with a machine's own path and mktemp's random names replaced."""
    for form in (os.path.realpath(root), str(root)):
        text = text.replace(form, "<machine>")
    return RANDOM_NAME.sub(r"\1<random>", text)


def machine_tree(root: Path) -> list[tuple[str, str]]:
    """Every entry's kind and content, as a machine at any path would have it."""
    found = []
    for path in entries(root):
        if "__pycache__" in path.parts:
            continue
        name = normalized(str(path.relative_to(root)), root)
        if path.is_symlink():
            found.append((name, "link " + normalized(os.readlink(path), root)))
        elif path.is_dir():
            found.append((name, "directory"))
        elif path.name == "processes.json":
            # Inodes differ between copies; each must match its own directory.
            processes = json.loads(path.read_text())
            for process in processes.values():
                directory = Path(str(process["directory"]))
                current = directory.stat().st_ino if directory.exists() else 0
                process["inode"] = process["inode"] == current
            found.append((name, normalized(json.dumps(processes, sort_keys=True), root)))
        else:
            data = path.read_bytes()
            text = data.decode("utf-8", "backslashreplace")
            found.append((name, f"mode {path.stat().st_mode:o} " + normalized(text, root)))
    return sorted(found)


@pytest.mark.parametrize("key", CLONE_CHECKS, ids=[group_name(key) for key in CLONE_CHECKS])
def test_a_cloned_machine_is_the_machine_built_fresh(key: GroupKey, executed: Matrix) -> None:
    """A clone has the same tree as a fresh build and behaves the same: a clean
    run and a failed one leave the same calls, output and machine."""
    first_shard_only()
    if TOUCHING or ONLY:
        pytest.skip("a targeted run")
    executed.outcome(clone_id(key))


def clone_id(key: GroupKey) -> str:
    return f"clone:{group_name(key)}"


def compare_clone(executed: Matrix, key: GroupKey) -> Outcome:
    kind, name, state = key
    scenario = SCENARIOS[name]
    # Beside the matrix's own machines, so both paths take the same form.
    directory = executed.root / "equivalence" / "-".join(key)
    fresh = Machine(directory / "fresh", scenario)
    fresh.build(SCENARIOS[scenario.start] if scenario.start else scenario, state)
    if kind == "install":
        fresh.script(scenario.script)
        if scenario.name == "remote-display":
            fresh.script("install-display-local.sh")
    else:
        fresh.script(scenario.uninstall)
    fresh.reset_run()
    group = executed.group(key)
    template = directory / "template"
    shutil.copytree(fresh.root, template, symlinks=True)
    clone = group.machine(directory / "clone")
    assert machine_tree(clone.root) == machine_tree(fresh.root)
    steps = executed.recordings[key]
    middle = [index for index, kind_of_call, *_ in steps if kind_of_call == "action"]
    for injection in (None, f"{middle[len(middle) // 2]} fail"):
        results = []
        for machine in (fresh, clone):
            outcome, recorded = group.run(machine, injection)
            assert not machine.survivors()
            results.append(
                (
                    outcome.returncode,
                    normalized(outcome.output, machine.root),
                    [normalized("\t".join(step), machine.root) for step in recorded],
                    machine_tree(machine.root),
                )
            )
        assert results[0] == results[1], injection
        # Both runs start again from the same state.
        shutil.rmtree(fresh.root)
        shutil.copytree(template, fresh.root, symlinks=True)
        fix_inodes(fresh)
        clone = group.machine(directory / "clone")
    return Outcome(0, "")


def test_a_run_over_its_process_budget_is_killed() -> None:
    """The watchdog ends a run with more processes than its budget, and all of them."""
    first_shard_only()
    caps = Limits(budget=3)
    command = "for n in 1 2 3 4 5; do sleep 60 & done; wait"
    started = time.monotonic()
    with pytest.raises(AssertionError, match="over its budget of 3"):
        run_contained(["/bin/sh", "-c", command], {"PATH": "/bin:/usr/bin"}, watch=caps.watch)
    assert time.monotonic() - started < 30


def test_runs_start_under_the_process_ceiling(tmp_path: Path) -> None:
    first_shard_only()
    machine = Machine(tmp_path / "machine", SCENARIOS["linux-timers"])
    script = tmp_path / "limit.sh"
    script.write_text("ulimit -u\n")
    result = machine.execute(script, SCENARIOS["linux-timers"])
    assert result.returncode == 0 and result.stdout.strip() == str(limits().ceiling)


# The last line of each installer that runs only once the new version is in
# place: the switch to it is done and every service it starts has been checked.
COMPLETION_MARKERS = {
    "install-controller.sh": "installation_complete=true",
    "install-controller-systemd.sh": "installation_complete=true",
    "install-display-local.sh": "installation_complete=true",
    # The display's own installer has finished on the remote machine.
    "install-display-node.sh": "REMOTE",
}


@pytest.mark.parametrize("name", sorted(COMPLETION_MARKERS))
def test_an_installer_says_it_succeeded_only_once_it_has(name: str) -> None:
    """A success line before the switch could sit beside a rollback message;
    the matrix reads it as a claim that the new version is installed."""
    first_shard_only()
    lines = (ROOT / "deploy" / name).read_text().splitlines()
    success = [
        number
        for number, line in enumerate(lines)
        if line.startswith('echo "') and line.removeprefix('echo "').startswith(SUCCESS_LINES)
    ]
    assert len(success) == 1, f"{name} has {len(success)} success lines"
    (number,) = success
    done = max(index for index, line in enumerate(lines) if line == COMPLETION_MARKERS[name])
    assert number > done, f"{name} says it succeeded before the new version is in place"
    printed = re.sub(r"\$\{[^}]+\}", "/x", lines[number].removeprefix('echo "').rstrip('"'))
    assert claims_new(Outcome(1, f"{printed}\nThe upgrade failed.\n"))


def test_single_directory_installers_are_the_pinned_copies() -> None:
    """Upgrades are tested from exactly these installers; a change must be deliberate."""
    first_shard_only()
    found = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(SINGLE_DIRECTORY.glob("*.sh"))
    }
    assert found == SINGLE_DIRECTORY_SHA256
