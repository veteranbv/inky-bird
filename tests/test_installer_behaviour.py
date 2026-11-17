"""Run the native installers against synthetic service managers in a temp HOME.

Nothing here touches launchd, systemd, sudo, a package index or a real host:
``tests/fake_bin/fake_tools.py`` stands in for every external command, and
each script runs from a copy whose few fixed system paths point into the
test directory instead.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tests/fake_bin/fake_tools.py"
UPGRADE_GATES = ROOT / "src/inky_bird_frame/upgrade_gates.py"
_spec = importlib.util.spec_from_file_location("fake_tools", TOOLS)
assert _spec is not None and _spec.loader is not None
fake_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_tools)
# macOS runs these scripts with its bundled Bash 3.2; exercise that when present.
MACOS_BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"
# The shell a display's ssh session runs, by absolute path.
REMOTE_BASH = shutil.which("bash") or "/bin/bash"
FAKE_CONFIG_MODULE = """import os
from pathlib import Path
from types import SimpleNamespace

TAG = (Path(__file__).resolve().parents[1] / "runtime-tag").read_text().strip()


def load_config(path, load_secrets=True):
    rules = Path(os.environ["FAKE"], "fail")
    if rules.exists() and f"{TAG}:load_config" in rules.read_text().split():
        raise ValueError("synthetic rejection")
    state_dir = Path(os.environ["FAKE_STATE_DIR"])
    return SimpleNamespace(controller=SimpleNamespace(state_dir=state_dir))
"""
FAKE_INSTALLATION_MODULE = """from pathlib import Path

TAG = (Path(__file__).resolve().parents[1] / "runtime-tag").read_text().strip()


def display_systemd_units(config, *, executable, app_dir, config_path, user, service_prefix):
    service = f"# new\\n# rendered by {TAG}\\n[Service]\\nUser={user}\\nExecStart={executable}\\n"
    timer = f"# new\\n# rendered by {TAG}\\n"
    return {f"{service_prefix}-display.service": service, f"{service_prefix}-display.timer": timer}
"""
# 0.9.x rendered display units without a service prefix argument.
OLD_INSTALLATION_MODULE = """
def display_systemd_units(config, *, executable, app_dir, config_path, user):
    names = ("inky-bird-frame-display.service", "inky-bird-frame-display.timer")
    return {name: "# old\\n" for name in names}
"""
# The file commands installers use to build, switch and remove runtimes.
FILE_COMMANDS = ("mv", "ln", "rm", "cp", "install", "mkdir", "mktemp", "rmdir")
SYSTEM_PATHS = {
    "install-controller.sh": ("/Library/LaunchDaemons", "/usr/bin/plutil"),
    "uninstall-controller.sh": (),
    "install-controller-systemd.sh": ("/etc/systemd/system",),
    "uninstall-controller-systemd.sh": ("/etc/systemd/system",),
    "install-display-local.sh": ("/etc/systemd/system",),
    "uninstall-display-local.sh": ("/etc/systemd/system",),
    "install-display-node.sh": (),
}


# Every installer run gets this long, then its whole process group is killed.
RUN_TIMEOUT_SECONDS = 120


def group_members(group: int) -> list[str]:
    """Live processes in one process group, as ``pid command`` lines."""
    listing = subprocess.run(
        ["/bin/ps", "-ww", "-ax", "-o", "pid=", "-o", "pgid=", "-o", "stat=", "-o", "command="],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    members = []
    for line in listing.splitlines():
        pid, pgid, stat, *command = line.split(None, 3)
        if int(pgid) == group and not stat.startswith("Z"):
            members.append(f"{pid} {' '.join(command)}")
    return members


def end_group(group: int) -> list[str]:
    """Kill whatever is left of a run's process group; return what had survived it."""
    survivors = group_members(group)
    if survivors:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(group, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while group_members(group) and time.monotonic() < deadline:
            time.sleep(0.05)
    return survivors


def run_contained(
    command: list[str],
    environment: dict[str, str],
    timeout: int = RUN_TIMEOUT_SECONDS,
    *,
    watch: Callable[[int, Path], contextlib.AbstractContextManager[None]] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an installer in its own process group; nothing it starts outlives it.

    A run that times out, or leaves a process behind, fails the test after the
    whole group is killed and reaped. ``watch`` is entered with the group's ID
    and the file listing the groups of the commands the fake ssh ran, for as
    long as the run lasts.
    """
    assert timeout <= RUN_TIMEOUT_SECONDS
    process = subprocess.Popen(
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    # Commands the fake ssh ran on the "display", each in a group of its own.
    remote_groups = Path(environment.get("FAKE", "/nonexistent")) / "groups"

    def end_all() -> list[str]:
        survivors = end_group(process.pid)
        if remote_groups.exists():
            for group in remote_groups.read_text().split():
                # A group that has ended may have its number reused by another
                # one; only a group still running this machine's commands is ours.
                if any(
                    str(remote_groups.parent.parent) in member
                    for member in group_members(int(group))
                ):
                    survivors += end_group(int(group))
            remote_groups.unlink()
        return survivors

    with watch(process.pid, remote_groups) if watch is not None else contextlib.nullcontext():
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            end_all()
            process.communicate()
            raise AssertionError(f"{command[-1]} did not finish within {timeout}s") from None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        survivors = end_all()
    assert not survivors, f"{command[-1]} left processes running: {survivors}"
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def system_tool(name: str) -> str:
    """The real command, by absolute path, never a fake of the same name."""
    found = shutil.which(name, path=f"/bin{os.pathsep}/usr/bin")
    assert found is not None, name
    return found


def tool_wrapper(tool: str, *, own_path: bool = False) -> str:
    """A shell stand-in that hands one fake tool its arguments."""
    return str(fake_tools.wrapper(tool, own_path=own_path))


def coreutil_wrapper(name: str, real: str) -> str:
    """A shell stand-in that numbers a file command as a step before running it."""
    mkdir, rmdir, wc, tr, rm = (
        shlex.quote(system_tool(tool)) for tool in ("mkdir", "rmdir", "wc", "tr", "rm")
    )
    return f"""#!/bin/sh
{fake_tools.DEPTH_GUARD}lock="$FAKE/steps.lock"
# A stop signal in here would leave the lock and hang every later step: hold
# it until the lock is released, then die of it as the real command would.
stopped=""
trap 'stopped=HUP' HUP
trap 'stopped=INT' INT
trap 'stopped=TERM' TERM
# A Ctrl-C reaches the whole process group, the commands below too: they run
# in a subshell that ignores it, which they inherit, so none of them dies
# between taking the lock and releasing it (a mkdir that died after making the
# lock would leave this loop waiting for itself).
index=$(
  trap '' HUP INT TERM
  while ! {mkdir} "$lock" 2>/dev/null; do :; done
  printf '%s {name}\n' "$$" > "$lock.owner"
  index=0
  if [ -f "$FAKE/steps" ]; then
    index=$({wc} -l < "$FAKE/steps" | {tr} -d ' ')
  fi
  {{
    printf '%s\taction\t{name}' "$index"
    printf '\t%s' "$@" | {tr} '\n' ' '
    printf '\n'
  }} >> "$FAKE/steps"
  {rm} -f "$lock.owner"
  {rmdir} "$lock"
  printf '%s' "$index"
)
trap - HUP INT TERM
if [ -n "$stopped" ]; then
  kill -s "$stopped" $$
fi
mode=""
if [ -f "$FAKE/inject" ]; then
  while read -r at injected; do
    if [ "$at" = "$index" ]; then
      mode=$injected
    fi
  done < "$FAKE/inject"
fi
case "$mode" in
  fail | expire)
    echo "{name}: synthetic failure" >&2
    exit 1
    ;;
  kill | interrupt | hangup)
    status=0
    {shlex.quote(real)} "$@" || status=$?
    # The interrupt goes to the whole process group; this call has completed.
    trap '' INT
    {shlex.quote(sys.executable)} {shlex.quote(str(TOOLS))} kill-installer "$mode"
    exit "$status"
    ;;
esac
# GNU install and cp refuse to copy a file onto itself; the BSD tools on macOS
# accept it, so the fakes refuse it the GNU way everywhere.
if [ "{name}" = install ] || [ "{name}" = cp ]; then
  source=""
  target=""
  for argument in "$@"; do
    source=$target
    target=$argument
  done
  if [ -d "$target" ] && [ -n "$source" ]; then
    target="$target/${{source##*/}}"
  fi
  if [ -n "$source" ] && [ -e "$source" ] && [ "$source" -ef "$target" ]; then
    echo "{name}: '$source' and '$target' are the same file" >&2
    exit 1
  fi
fi
# macOS mktemp ignores TMPDIR without a template; keep temporary files in the test root.
if [ "{name}" = mktemp ] && [ "$*" = "-d" ]; then
  exec {shlex.quote(real)} -d "$TMPDIR/tmp.XXXXXXXXXX"
fi
exec {shlex.quote(real)} "$@"
"""


class Host:
    """A temp HOME, a source checkout and synthetic system tools."""

    def __init__(
        self,
        root: Path,
        system: str,
        family: str = "inky-bird",
        *,
        number_file_commands: bool = False,
        create: bool = True,
    ) -> None:
        """``create=False`` names the paths of a host already on disk, such as a copy."""
        self.root = root
        self.system = system
        self.family = family
        self.fake = root / "fake"
        self.bin = self.fake / "bin"
        self.home = root / "home"
        self.source = root / "source"
        self.app = self.home / "Services" / family
        self.support = self.home / "support"
        self.config = self.support / "config.toml"
        self.state = root / "state"
        self.units = self.fake / "systemd-system"
        self.agents = self.home / "Library/LaunchAgents"
        self.venv = self.home / ".virtualenvs/pimoroni"
        self.tmp = root / "tmp"
        if not create:
            return
        for directory in (self.bin, self.home, self.support, self.state, self.units, self.tmp):
            directory.mkdir(parents=True, exist_ok=True)
        (self.fake / "LaunchDaemons").mkdir()
        (self.fake / "log").write_text("")
        self.config.write_text("synthetic private config\n")
        for tool in (
            "uname",
            "id",
            "python3",
            "launchctl",
            "plutil",
            "uv",
            "systemctl",
            "sudo",
            "rsync",
            "ssh",
        ):
            path = self.bin / tool
            path.write_text(tool_wrapper(tool))
            path.chmod(0o755)
        if number_file_commands:
            # The matrix numbers and injects file changes as steps, too.
            for command in FILE_COMMANDS:
                path = self.bin / command
                path.write_text(coreutil_wrapper(command, system_tool(command)))
                path.chmod(0o755)
        self.write_source(self.source, "new")

    @staticmethod
    def write_source(directory: Path, tag: str) -> None:
        package = directory / "src/inky_bird_frame"
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("")
        (package / "native_names.py").write_text("# replaced by the synthetic python3\n")
        (package / "config.py").write_text(FAKE_CONFIG_MODULE)
        (package / "installation.py").write_text(FAKE_INSTALLATION_MODULE)
        # The installers' upgrade helper runs for real against the synthetic config.
        (package / "upgrade_gates.py").write_text(UPGRADE_GATES.read_text())
        (directory / "src/runtime-tag").write_text(f"{tag}\n")
        (directory / "catalog").mkdir(exist_ok=True)
        (directory / "deploy").mkdir(exist_ok=True)
        (directory / "docs").mkdir(exist_ok=True)
        for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "docs/browser-access.md"):
            (directory / name).write_text(f"synthetic {tag} {name}\n")

    def script(
        self,
        name: str,
        quiesce_timeout: int = 3,
        *,
        health_timeout: int = 5,
        source: Path | None = None,
        text: str | None = None,
        system_paths: tuple[str, ...] | None = None,
    ) -> Path:
        """Copy an installer into a checkout with its system paths in this host;
        ``system_paths`` names them for a ``text`` that is not today's script."""
        text = (ROOT / "deploy" / name).read_text() if text is None else text
        replacements = {
            "/Library/LaunchDaemons": str(self.fake / "LaunchDaemons"),
            "/usr/bin/plutil": str(self.bin / "plutil"),
            "/etc/systemd/system": str(self.units),
        }
        for path in SYSTEM_PATHS[name] if system_paths is None else system_paths:
            assert path in text, f"{name} no longer names {path}"
            text = text.replace(path, replacements[path])
        text = text.replace(
            "quiesce_timeout_seconds=1800", f"quiesce_timeout_seconds={quiesce_timeout}"
        )
        text = text.replace("health_timeout_seconds=90", f"health_timeout_seconds={health_timeout}")
        # Polling for the cycle lock helper is only a wait; a short one keeps runs quick.
        text = text.replace("poll_seconds=1\n", "poll_seconds=0.05\n")
        # So is the helper's own watch for its installer to end, and its wait for a
        # plate; a run's output only closes once the helper has gone.
        text = text.replace("\n    time.sleep(1)\n", "\n    time.sleep(0.05)\n")
        text = text.replace("\n        time.sleep(2)\n", "\n        time.sleep(0.05)\n")
        target = (self.source if source is None else source) / "deploy" / name
        target.write_text(text)
        target.chmod(0o755)
        return target

    def stopped_hook(self) -> Path:
        """An owner's --stopped-hook, which reaches the fake tools without the
        installer's environment: it runs with only the variables it is given."""
        path = self.support / "stopped-hook"
        exports = "".join(
            f"{name}={shlex.quote(value)}; export {name}\n"
            for name, value in (
                ("FAKE", str(self.fake)),
                ("FAKE_STATE_DIR", str(self.state)),
                ("FAKE_LAYOUT_APP_DIR", str(self.app)),
            )
        )
        shebang, body = tool_wrapper("stopped-hook").split("\n", 1)
        path.write_text(f"{shebang}\n{exports}{body}")
        path.chmod(0o755)
        return path

    def environment(self, **extra: str) -> dict[str, str]:
        environment = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin{os.pathsep}/usr/sbin",
            "HOME": str(self.home),
            "FAKE": str(self.fake),
            "FAKE_UNAME": self.system,
            "FAKE_BASH": REMOTE_BASH,
            "FAKE_STATE_DIR": str(self.state),
            "FAKE_UNIT_DIR": str(self.units),
            "FAKE_LAYOUT_SERVICE_PREFIX": self.family,
            "FAKE_LAYOUT_APP_DIR": str(self.app),
            "FAKE_LAYOUT_SUPPORT_DIR": str(self.support),
            "FAKE_LAYOUT_CONFIG_PATH": str(self.config),
            "TMPDIR": str(self.tmp),
            "INKY_BIRD_DISPLAY_VENV": str(self.venv),
            # A stray setting must not redirect the managed environment.
            "UV_PROJECT_ENVIRONMENT": str(self.root / "elsewhere"),
        }
        environment.update(extra)
        return environment

    def run(
        self, name: str, *, shell: str = "bash", quiesce_timeout: int = 3, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        return run_contained(
            [shell, str(self.script(name, quiesce_timeout))], self.environment(**extra)
        )

    def run_with(
        self,
        name: str,
        options: tuple[str, ...],
        *,
        shell: str = "bash",
        health_timeout: int = 5,
        **extra: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run a script with command-line options and its health check's time limit."""
        script = self.script(name, health_timeout=health_timeout)
        return run_contained([shell, str(script), *options], self.environment(**extra))

    def start(
        self,
        name: str,
        output: Path,
        *,
        shell: str = "bash",
        quiesce_timeout: int = 3,
        options: tuple[str, ...] = (),
        **extra: str,
    ) -> subprocess.Popen[str]:
        """Start a script in the background with stdout and stderr in ``output``."""
        with output.open("w") as handle:
            return subprocess.Popen(
                [shell, str(self.script(name, quiesce_timeout)), *options],
                env=self.environment(**extra),
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )

    def fail(self, *rules: str) -> None:
        (self.fake / "fail").write_text("\n".join(rules) + "\n")

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in (self.fake / "log").read_text().splitlines()]

    def state_of(self, name: str) -> dict[str, dict[str, object]]:
        path = self.fake / f"{name}.json"
        loaded: dict[str, dict[str, object]] = json.loads(path.read_text()) if path.exists() else {}
        return loaded

    def save_state(self, name: str, value: dict[str, dict[str, object]]) -> None:
        (self.fake / f"{name}.json").write_text(json.dumps(value))

    def set_busy(self, unit: str, polls: int) -> None:
        """Report ``unit`` as still running for the next ``polls`` state queries."""
        (self.fake / "busy.json").write_text(json.dumps({f"{self.family}-{unit}": polls}))

    def install_old_version(self) -> None:
        """Install the running version in place, as the installers lay it out."""
        self.app.mkdir(parents=True)
        self.write_source(self.app, "old")
        subprocess.run([sys.executable, str(TOOLS), "build-venv", str(self.app)], check=True)

    def installed_version(self) -> str:
        return (self.app / ".venv/tag").read_text().strip()

    # macOS
    def plist(self, label: str) -> Path:
        return self.agents / f"com.{self.family}.{label}.plist"

    def load_agents(self, *labels: str) -> None:
        self.agents.mkdir(parents=True, exist_ok=True)
        loaded: dict[str, dict[str, object]] = {}
        for label in labels:
            payload = {
                "Label": f"com.{self.family}.{label}",
                "ProgramArguments": [str(self.app / ".venv/bin/inky-bird"), label],
                "RunAtLoad": True,
                "Comment": "old",
            }
            self.plist(label).write_bytes(plistlib.dumps(payload))
            loaded[f"com.{self.family}.{label}"] = {"generation": "old", "runtime": "old"}
        self.save_state("launchd", loaded)

    def loaded_agents(self) -> dict[str, dict[str, object]]:
        return {
            label.rsplit(".", 1)[-1]: value for label, value in self.state_of("launchd").items()
        }

    def plist_generation(self, label: str) -> object:
        return plistlib.loads(self.plist(label).read_bytes())["Comment"]

    # systemd
    def install_units(self, **units: str) -> None:
        state: dict[str, dict[str, object]] = {}
        for name, status in units.items():
            stem, suffix = name.rsplit("_", 1)
            unit = f"{self.family}-{stem.replace('_', '-')}.{suffix}"
            content = "[Unit]\n# old\n"
            if unit.endswith("service"):
                content += f"[Service]\nUser=fixture\nExecStart={self.app}/.venv/bin/inky-bird\n"
            (self.units / unit).write_text(content)
            state[unit] = {"active": status == "active", "enabled": True}
        self.save_state("systemd", state)

    def unit_state(self, name: str) -> dict[str, object]:
        return self.state_of("systemd")[f"{self.family}-{name}"]

    def unit_text(self, name: str) -> str:
        return (self.units / f"{self.family}-{name}").read_text()


def bootouts(host: Host) -> list[list[str]]:
    return [call for call in host.calls() if call[:2] == ["launchctl", "bootout"]]


def bootstraps(host: Host) -> list[list[str]]:
    return [call for call in host.calls() if call[:2] == ["launchctl", "bootstrap"]]


@contextmanager
def running_generation(host: Host, lock: str = "controller.lock") -> Iterator[Callable[[], None]]:
    """Hold a writer lock the way a running writer does: by default the
    controller cycle lock, as a plate being made holds it.

    The lock is held until the yielded ``release`` is called or the block ends.
    """
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys\n"
            "handle = open(sys.argv[1], 'a+')\n"
            "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "sys.stdin.read()\n",
            str(host.state / lock),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    def release() -> None:
        assert holder.stdin is not None
        holder.stdin.close()
        assert holder.wait(timeout=30) == 0

    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        yield release
    finally:
        holder.kill()
        holder.wait()


def wait_until(condition: Callable[[], bool], what: str, timeout: float = 60.0) -> None:
    """Poll ``condition`` and fail the test loudly if it is not met in time."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(0.05)


@pytest.mark.parametrize(
    "stop", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP], ids=["INT", "TERM", "HUP"]
)
def test_a_stopped_step_never_leaves_the_step_lock(tmp_path: Path, stop: signal.Signals) -> None:
    """A stop signal that reaches a numbered file command while it holds the
    step lock is delivered once the lock is released; a lock left behind would
    hang every later step of the run and of its rerun."""
    host = Host(tmp_path, "Linux", number_file_commands=True)
    lock = host.fake / "steps.lock"
    lock.mkdir()
    step = subprocess.Popen(
        [str(host.bin / "mkdir"), str(tmp_path / "made")],
        env=host.environment(),
        start_new_session=True,
    )
    time.sleep(0.5)
    os.kill(step.pid, stop)
    time.sleep(0.2)
    assert step.poll() is None, "the step died while it waited for the lock"
    lock.rmdir()
    assert step.wait(timeout=30) == -stop
    assert not lock.exists()
    assert not (tmp_path / "made").exists()


# Uninstallers ----------------------------------------------------------------


def test_linux_uninstaller_reports_a_unit_it_could_not_remove(tmp_path: Path) -> None:
    host = Host(tmp_path, "Linux")
    host.install_units(
        controller_service="active", refresh_service="inactive", refresh_timer="active"
    )
    host.fail(f"systemctl:disable:{host.family}-refresh.timer")
    result = host.run("uninstall-controller-systemd.sh")
    assert result.returncode == 1
    assert f"{host.family}-refresh.timer" in result.stderr
    assert "Controller services uninstalled." not in result.stdout
    assert host.unit_state("controller.service")["active"] is False


def test_linux_uninstaller_keeps_a_unit_it_could_not_stop_until_a_rerun_stops_it(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path, "Linux")
    host.install_units(
        controller_service="active", refresh_service="inactive", refresh_timer="active"
    )
    timer = f"{host.family}-refresh.timer"
    host.fail(f"systemctl:disable:{timer}")
    for _ in range(2):
        result = host.run("uninstall-controller-systemd.sh")
        assert result.returncode == 1, result.stdout
        assert timer in result.stderr
        assert "Controller services uninstalled." not in result.stdout
        assert (host.units / timer).exists()
        assert host.unit_state("refresh.timer")["active"] is True
    assert not (host.units / f"{host.family}-controller.service").exists()

    host.fail()
    result = host.run("uninstall-controller-systemd.sh")
    assert result.returncode == 0, result.stderr
    assert not (host.units / timer).exists()
    assert host.unit_state("refresh.timer")["active"] is False


def test_display_uninstaller_keeps_a_unit_it_could_not_stop_until_a_rerun_stops_it(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path, "Linux")
    host.install_units(display_service="inactive", display_timer="active")
    timer = f"{host.family}-display.timer"
    host.fail(f"systemctl:disable:{timer}")
    for _ in range(2):
        result = host.run("uninstall-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")
        assert result.returncode == 1, result.stdout
        assert timer in result.stderr
        assert "Display node services uninstalled." not in result.stdout
        assert (host.units / timer).exists()
        assert host.unit_state("display.timer")["active"] is True

    host.fail()
    result = host.run("uninstall-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")
    assert result.returncode == 0, result.stderr
    assert not (host.units / timer).exists()


def test_linux_uninstaller_succeeds_when_units_are_absent(tmp_path: Path) -> None:
    host = Host(tmp_path, "Linux")
    host.install_units(controller_service="active")
    result = host.run("uninstall-controller-systemd.sh")
    assert result.returncode == 0, result.stderr
    assert "Controller services uninstalled." in result.stdout
    assert not (host.units / f"{host.family}-controller.service").exists()
    assert host.config.exists()


def test_macos_uninstaller_reports_an_agent_it_could_not_stop(tmp_path: Path) -> None:
    host = Host(tmp_path, "Darwin")
    host.load_agents("serve", "refresh")
    host.fail("bootout:com.inky-bird.serve")
    result = host.run("uninstall-controller.sh", shell=MACOS_BASH)
    assert result.returncode == 1
    assert "com.inky-bird.serve" in result.stderr
    assert "Controller LaunchAgents uninstalled." not in result.stdout
    assert "refresh" not in host.loaded_agents()
    # The plist stays so a rerun can still find and stop the agent.
    assert host.plist("serve").exists()


def test_display_uninstaller_reports_a_unit_it_could_not_remove(tmp_path: Path) -> None:
    host = Host(tmp_path, "Linux")
    host.install_units(display_service="inactive", display_timer="active")
    host.fail("sudo:rm")
    result = host.run("uninstall-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")
    assert result.returncode == 1
    assert f"{host.family}-display" in result.stderr
    assert "Display node services uninstalled." not in result.stdout


# Remote display deployment ---------------------------------------------------


@pytest.mark.parametrize("failure", ["ssh-stage-noise", "rsync"])
def test_remote_display_staging_is_removed_on_every_exit(tmp_path: Path, failure: str) -> None:
    host = Host(tmp_path, "Linux")
    key = tmp_path / "synthetic-key"
    key.write_text("")
    shutil.copyfile(
        ROOT / "deploy/install-display-local.sh", host.source / "deploy/install-display-local.sh"
    )
    host.fail(failure)
    result = host.run(
        "install-display-node.sh",
        INKY_BIRD_DISPLAY_HOST="display.example.test",
        INKY_BIRD_DISPLAY_USER="fixture",
        INKY_BIRD_DISPLAY_SSH_KEY=str(key),
        INKY_BIRD_DISPLAY_APP_DIR=str(host.app),
        INKY_BIRD_DISPLAY_CONFIG_PATH=str(host.config),
    )
    assert result.returncode != 0
    assert any(call[0] == "ssh" for call in host.calls())
    assert not list(host.app.parent.glob(".inky-bird-stage.*"))


def test_remote_display_refuses_an_existing_staging_path(tmp_path: Path) -> None:
    host = Host(tmp_path, "Linux")
    key = tmp_path / "synthetic-key"
    key.write_text("")
    od = host.bin / "od"
    od.write_text("#!/bin/sh\nprintf ' 0a 1b 2c 3d 4e 5f\\n'\n")
    od.chmod(0o755)
    host.app.parent.mkdir(parents=True, exist_ok=True)
    foreign = host.app.parent / ".inky-bird-stage.0a1b2c3d4e5f"
    foreign.mkdir()
    (foreign / "keep").write_text("someone else's file\n")
    result = host.run(
        "install-display-node.sh",
        INKY_BIRD_DISPLAY_HOST="display.example.test",
        INKY_BIRD_DISPLAY_USER="fixture",
        INKY_BIRD_DISPLAY_SSH_KEY=str(key),
        INKY_BIRD_DISPLAY_APP_DIR=str(host.app),
        INKY_BIRD_DISPLAY_CONFIG_PATH=str(host.config),
        INKY_BIRD_DISPLAY_VENV=str(host.venv),
    )
    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert (foreign / "keep").read_text() == "someone else's file\n"
    assert not any(call[0] == "rsync" for call in host.calls())


@pytest.mark.parametrize("script", sorted(path.name for path in (ROOT / "deploy").glob("*.sh")))
def test_deploy_scripts_parse_with_the_bundled_macos_shell(script: str) -> None:
    result = subprocess.run(
        [MACOS_BASH, "-n", str(ROOT / "deploy" / script)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# Linux systemd controller installer ------------------------------------------


def linux_host(tmp_path: Path, mode: str = "timers") -> Host:
    host = Host(tmp_path, "Linux")
    host.install_old_version()
    if mode == "timers":
        host.install_units(
            controller_service="active",
            refresh_service="inactive",
            refresh_timer="active",
            generate_service="inactive",
            generate_timer="active",
        )
    else:
        host.install_units(controller_service="active", scheduler_service="active")
    return host


TIMER_UNITS = "controller.service refresh.service refresh.timer generate.service generate.timer"
APP_UNITS = "controller.service scheduler.service"


def run_linux(
    host: Host, units: str = TIMER_UNITS, quiesce_timeout: int = 3, **extra: str
) -> subprocess.CompletedProcess[str]:
    return host.run(
        "install-controller-systemd.sh", quiesce_timeout=quiesce_timeout, FAKE_UNITS=units, **extra
    )


def uv_calls(host: Host) -> list[list[str]]:
    return [call for call in host.calls() if call[0] == "uv"]


def assert_previous_runtime_untouched(host: Host) -> None:
    """The new runtime is built beside the live one; a refused run leaves neither changed."""
    assert host.installed_version() == "old"
    assert (host.app / "src/runtime-tag").read_text() == "old\n"
    assert not (host.app / ".venv").is_symlink()
    runtimes = host.app / ".runtimes"
    assert not runtimes.exists() or not any(runtimes.iterdir())


def test_linux_upgrade_restarts_services_and_builds_the_managed_venv(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    result = run_linux(host)
    assert result.returncode == 0, result.stderr
    assert host.installed_version() == "new"
    assert "# new" in host.unit_text("controller.service")
    for unit in ("controller.service", "refresh.timer", "generate.timer"):
        assert host.unit_state(unit) == {"active": True, "enabled": True}
    # A stray UV_PROJECT_ENVIRONMENT never reaches uv, so the units' venv is the one built.
    calls = uv_calls(host)
    assert len(calls) == 1 and calls[0][-1] == "False"


def test_linux_installer_refuses_root(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    result = run_linux(host, FAKE_UID="0")
    assert result.returncode == 1
    assert "not as root" in result.stderr and "No changes made" in result.stderr
    assert not any(call[0] in ("sudo", "uv", "systemctl") for call in host.calls())


def test_linux_installer_refuses_units_owned_by_another_account(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    unit = host.units / f"{host.family}-controller.service"
    unit.write_text(unit.read_text().replace("User=fixture", "User=someone-else"))
    result = run_linux(host)
    assert result.returncode == 1
    assert "someone-else" in result.stderr and "No changes made" in result.stderr
    assert not any(call[0] in ("sudo", "uv", "systemctl") for call in host.calls())
    assert "User=someone-else" in unit.read_text()


def test_linux_failed_stop_aborts_before_the_runtime_changes(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.fail(f"systemctl:stop:{host.family}-generate.timer")
    result = run_linux(host)
    assert result.returncode == 1
    assert_previous_runtime_untouched(host)
    assert host.installed_version() == "old"
    assert host.unit_state("refresh.timer")["active"] is True
    assert "started again" in result.stderr


def test_linux_expired_sudo_restarts_stopped_timers_and_says_so(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.fail("sudo:install")
    result = run_linux(host)
    assert result.returncode != 0
    for unit in ("controller.service", "refresh.timer", "generate.timer"):
        assert host.unit_state(unit)["active"] is True
    assert "the services it stopped were started again" in result.stderr


def test_linux_reports_timers_it_could_not_restart(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.fail("sudo-expires-at-install")
    result = run_linux(host)
    assert result.returncode != 0
    assert host.unit_state("refresh.timer")["active"] is False
    assert f"{host.family}-refresh.timer" in result.stderr
    assert f"{host.family}-generate.timer" in result.stderr
    assert "sudo systemctl start" in result.stderr


def test_linux_asks_for_sudo_again_before_stopping_anything(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.fail("sudo-v-expires-after:1")
    result = run_linux(host)
    assert result.returncode == 1
    assert "No changes made" in result.stderr
    assert not any(call[:3] == ["sudo", "systemctl", "stop"] for call in host.calls())
    assert_previous_runtime_untouched(host)
    assert host.unit_state("generate.timer")["active"] is True


def test_linux_asks_for_sudo_again_after_waiting_for_jobs(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.fail("sudo-v-expires-after:2")
    result = run_linux(host)
    assert result.returncode == 1
    # Nothing in the managed directory changed; the stopped timers are named.
    assert_previous_runtime_untouched(host)
    assert host.installed_version() == "old"
    assert f"{host.family}-generate.timer" in result.stderr
    assert "sudo systemctl start" in result.stderr


def test_linux_unreachable_bird_source_does_not_block_install(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.fail("new:refresh:data_source")
    result = run_linux(host)
    assert result.returncode == 0, result.stderr
    assert "will try again" in result.stderr
    assert host.unit_state("refresh.timer") == {"active": True, "enabled": True}
    assert "# new" in host.unit_text("refresh.timer")


@pytest.mark.parametrize(
    "failure", ["new:refresh", "new:refresh:storage", "new:refresh:unreadable"]
)
def test_linux_other_refresh_failures_abort_the_install(tmp_path: Path, failure: str) -> None:
    host = linux_host(tmp_path)
    host.fail(failure)
    result = run_linux(host)
    assert result.returncode == 1
    assert "will try again" not in result.stderr
    assert "# old" in host.unit_text("controller.service")
    for unit in ("controller.service", "refresh.timer", "generate.timer"):
        assert host.unit_state(unit)["active"] is True


def test_linux_keeps_an_optional_timer_it_could_not_disable(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    timer = f"{host.family}-generate.timer"
    host.fail(f"systemctl:disable:{timer}")
    result = run_linux(host, "controller.service refresh.service refresh.timer")
    assert result.returncode != 0
    assert (host.units / timer).exists()


def test_linux_failure_puts_back_a_timer_the_new_version_removes(tmp_path: Path) -> None:
    # A failed upgrade rolls back to the previous version, which still runs the timer.
    host = linux_host(tmp_path)
    host.fail(f"systemctl:restart:{host.family}-refresh.timer")
    result = run_linux(host, "controller.service refresh.service refresh.timer")
    assert result.returncode != 0
    assert "# old" in host.unit_text("generate.timer")
    for unit in ("controller.service", "refresh.timer", "generate.timer"):
        assert host.unit_state(unit) == {"active": True, "enabled": True}
    assert host.installed_version() == "old"
    assert "are stopped" not in result.stderr
    assert "started again on the previous version" in result.stderr


def test_linux_app_owned_upgrade_stops_scheduler_only_between_plates(tmp_path: Path) -> None:
    host = linux_host(tmp_path, mode="app")
    result = run_linux(host, APP_UNITS)
    assert result.returncode == 0, result.stderr
    stops = [call for call in host.calls() if call[0] == "lock-at-stop"]
    assert stops == [["lock-at-stop", f"{host.family}-scheduler.service", "lock-held"]]
    assert host.unit_state("scheduler.service")["active"] is True


def test_linux_app_owned_upgrade_refuses_while_a_plate_is_being_made(tmp_path: Path) -> None:
    host = linux_host(tmp_path, mode="app")
    with running_generation(host):
        result = run_linux(host, APP_UNITS)
    assert result.returncode == 3
    assert "still being made" in result.stderr and "No changes made" in result.stderr
    assert host.unit_state("scheduler.service")["active"] is True
    assert not any(call[:2] == ["systemctl", "stop"] for call in host.calls())
    assert_previous_runtime_untouched(host)
    assert host.installed_version() == "old"


def test_linux_upgrade_proceeds_when_the_installed_version_cannot_be_loaded(
    tmp_path: Path,
) -> None:
    host = linux_host(tmp_path, mode="app")
    (host.app / "src/inky_bird_frame/config.py").write_text("raise ImportError('broken')\n")
    result = run_linux(host, APP_UNITS)
    assert result.returncode == 0, result.stderr
    assert "cannot check for a plate being made" in result.stderr
    assert host.installed_version() == "new"


# macOS LaunchAgent installer -------------------------------------------------


def macos_host(tmp_path: Path, *agents: str) -> Host:
    host = Host(tmp_path, "Darwin")
    host.install_old_version()
    host.load_agents(*agents)
    return host


def run_macos(host: Host, agents: str = "serve refresh") -> subprocess.CompletedProcess[str]:
    return host.run("install-controller.sh", shell=MACOS_BASH, FAKE_AGENTS=agents)


def test_macos_upgrade_stops_agents_only_between_plates(tmp_path: Path) -> None:
    host = macos_host(tmp_path, "serve", "refresh")
    result = run_macos(host)
    assert result.returncode == 0, result.stderr
    assert host.installed_version() == "new"
    assert host.loaded_agents() == {
        "serve": {"generation": "new", "runtime": "new", "run_at_load": True},
        "refresh": {"generation": "new", "runtime": "new", "run_at_load": True},
    }
    assert [call[3] for call in bootouts(host)] == ["lock-held", "lock-held"]
    calls = uv_calls(host)
    assert len(calls) == 1 and calls[0][-1] == "False"


def test_macos_fresh_install_starts_new_agents(tmp_path: Path) -> None:
    host = Host(tmp_path, "Darwin")
    result = run_macos(host)
    assert result.returncode == 0, result.stderr
    assert set(host.loaded_agents()) == {"serve", "refresh"}


def test_macos_rejected_configuration_leaves_running_agents_alone(tmp_path: Path) -> None:
    host = macos_host(tmp_path, "serve", "refresh")
    host.fail("new:heredoc")
    result = run_macos(host)
    assert result.returncode == 1
    assert not bootouts(host)
    assert not bootstraps(host)
    assert host.loaded_agents() == {
        "serve": {"generation": "old", "runtime": "old"},
        "refresh": {"generation": "old", "runtime": "old"},
    }
    assert host.plist_generation("serve") == "old"
    assert host.plist_generation("refresh") == "old"


def test_macos_failed_publisher_check_restores_only_the_publisher(tmp_path: Path) -> None:
    host = macos_host(tmp_path, "serve", "refresh", "catalog-publish")
    host.fail("new:catalog-publish")
    result = run_macos(host, "serve refresh catalog-publish")
    assert result.returncode == 1
    assert [call[2] for call in bootouts(host)] == [f"com.{host.family}.catalog-publish"]
    agents = host.loaded_agents()
    assert agents["serve"] == {"generation": "old", "runtime": "old"}
    assert agents["refresh"] == {"generation": "old", "runtime": "old"}
    # The publisher returns with its own schedule, without running at load.
    assert agents["catalog-publish"]["generation"] == "old"
    assert agents["catalog-publish"]["run_at_load"] is False
    for label in ("serve", "refresh", "catalog-publish"):
        assert host.plist_generation(label) == "old"


def test_macos_failed_start_puts_back_the_agents_it_stopped(tmp_path: Path) -> None:
    host = macos_host(tmp_path, "serve", "refresh")
    host.fail("bootstrap-new:refresh")
    result = run_macos(host)
    assert result.returncode != 0
    agents = host.loaded_agents()
    assert agents["serve"]["generation"] == "old"
    assert agents["refresh"]["generation"] == "old"
    assert host.plist_generation("serve") == "old"
    assert "the agents it stopped were started again" in result.stderr


def test_macos_failed_upgrade_reports_agents_it_could_not_restart(tmp_path: Path) -> None:
    host = macos_host(tmp_path, "serve", "refresh")
    host.fail("bootstrap-new:serve", "bootstrap-old:serve")
    result = run_macos(host)
    assert result.returncode != 0
    assert f"com.{host.family}.serve" in result.stderr
    assert "launchctl bootstrap" in result.stderr
    assert host.loaded_agents()["refresh"]["generation"] == "old"
    assert "started again" not in result.stderr


def test_macos_failed_stop_is_not_ignored(tmp_path: Path) -> None:
    host = macos_host(tmp_path, "serve", "refresh")
    host.fail(f"bootout:com.{host.family}.refresh")
    result = run_macos(host)
    assert result.returncode != 0
    assert not any(call[3] == "new" for call in bootstraps(host))
    agents = host.loaded_agents()
    assert agents["refresh"] == {"generation": "old", "runtime": "old"}
    assert agents["serve"]["generation"] == "old"
    assert host.plist_generation("refresh") == "old"


def test_macos_upgrade_refuses_while_a_plate_is_being_made(tmp_path: Path) -> None:
    host = macos_host(tmp_path, "serve", "refresh")
    with running_generation(host):
        result = run_macos(host)
    assert result.returncode == 3
    assert "still being made" in result.stderr and "No changes made" in result.stderr
    assert not bootouts(host)
    assert_previous_runtime_untouched(host)
    assert host.installed_version() == "old"
    assert (host.app / "src/runtime-tag").read_text() == "old\n"
    assert host.plist_generation("serve") == "old"


def test_macos_upgrade_waits_for_a_plate_to_finish(tmp_path: Path) -> None:
    host = macos_host(tmp_path, "serve", "refresh")
    output = tmp_path / "installer.out"
    waiting = "Waiting for the plate being made to finish"
    with running_generation(host) as release:
        installer = host.start(
            "install-controller.sh",
            output,
            shell=MACOS_BASH,
            quiesce_timeout=120,
            FAKE_AGENTS="serve refresh",
        )
        try:
            wait_until(
                lambda: waiting in output.read_text() or installer.poll() is not None,
                "the installer to try the cycle lock",
            )
            # Still blocked on the plate: nothing stopped yet.
            assert installer.poll() is None, output.read_text()
            assert not bootouts(host)
            release()
            returncode = installer.wait(timeout=120)
        finally:
            if installer.poll() is None:
                installer.kill()
                installer.wait()
            assert not end_group(installer.pid)
    assert returncode == 0, output.read_text()
    assert [call[3] for call in bootouts(host)] == ["lock-held", "lock-held"]
    assert host.installed_version() == "new"


def test_macos_upgrade_proceeds_when_the_installed_version_cannot_be_loaded(
    tmp_path: Path,
) -> None:
    host = macos_host(tmp_path, "serve", "refresh")
    (host.app / "src/inky_bird_frame/config.py").write_text("raise ImportError('broken')\n")
    result = run_macos(host)
    assert result.returncode == 0, result.stderr
    assert "cannot check for a plate being made" in result.stderr
    assert host.installed_version() == "new"


def test_macos_failure_after_install_says_the_new_version_is_running(tmp_path: Path) -> None:
    host = macos_host(tmp_path, "serve", "refresh", "controller-cycle")
    host.fail(f"bootout:com.{host.family}.controller-cycle")
    result = run_macos(host)
    assert result.returncode != 0
    assert host.loaded_agents()["serve"]["generation"] == "new"
    assert "new version is installed and running" in result.stderr
    assert "started again" not in result.stderr


# Writer locks, the health check and the stopped-boundary hook ---------------


def controller_host(tmp_path: Path, system: str) -> Host:
    if system == "Darwin":
        return macos_host(tmp_path, "serve", "refresh")
    return linux_host(tmp_path)


def run_controller(
    host: Host, *, options: tuple[str, ...] = (), health_timeout: int = 5
) -> subprocess.CompletedProcess[str]:
    if host.system == "Darwin":
        return host.run_with(
            "install-controller.sh",
            options,
            shell=MACOS_BASH,
            health_timeout=health_timeout,
            FAKE_AGENTS="serve refresh",
        )
    return host.run_with(
        "install-controller-systemd.sh",
        options,
        health_timeout=health_timeout,
        FAKE_UNITS=TIMER_UNITS,
    )


def assert_previous_running(host: Host) -> None:
    assert host.installed_version() == "old"
    if host.system == "Darwin":
        assert {label: value["generation"] for label, value in host.loaded_agents().items()} == {
            "serve": "old",
            "refresh": "old",
        }
    else:
        assert "# old" in host.unit_text("controller.service")
        for unit in ("controller.service", "refresh.timer", "generate.timer"):
            assert host.unit_state(unit)["active"] is True


def stops(host: Host) -> list[list[str]]:
    return [call for call in host.calls() if call[0] == "stopped"]


SYSTEMS = ["Darwin", "Linux"]


@pytest.mark.parametrize("lock", ["refresh.lock", "notifications.lock", "catalog-state.lock"])
@pytest.mark.parametrize("system", SYSTEMS)
def test_an_upgrade_waits_for_every_writer_and_changes_nothing_if_one_runs_on(
    tmp_path: Path, system: str, lock: str
) -> None:
    host = controller_host(tmp_path, system)
    with running_generation(host, lock):
        result = run_controller(host)
    assert result.returncode == 3, result.stderr
    assert "Waiting for" in result.stderr
    assert "Other work is still running" in result.stderr and "No changes made" in result.stderr
    assert not stops(host)
    assert_previous_running(host)


def test_linux_upgrade_stops_the_scheduler_only_once_a_writer_finishes(tmp_path: Path) -> None:
    host = linux_host(tmp_path, mode="app")
    output = tmp_path / "installer.out"
    with running_generation(host, "notifications-dispatch.lock") as release:
        installer = host.start(
            "install-controller-systemd.sh", output, quiesce_timeout=120, FAKE_UNITS=APP_UNITS
        )
        try:
            wait_until(
                lambda: (
                    "Waiting for notification delivery to finish" in output.read_text()
                    or installer.poll() is not None
                ),
                "the installer to wait for notification delivery",
            )
            assert installer.poll() is None, output.read_text()
            assert not stops(host)
            release()
            returncode = installer.wait(timeout=120)
        finally:
            if installer.poll() is None:
                installer.kill()
                installer.wait()
            assert not end_group(installer.pid)
    assert returncode == 0, output.read_text()
    assert [call[2] for call in stops(host)] == ["lock-held"]
    assert host.installed_version() == "new"


def test_macos_publisher_check_runs_between_two_holds_of_the_writer_locks(tmp_path: Path) -> None:
    """The check takes the publisher's own locks; every agent stops while the
    installer holds them all."""
    host = macos_host(tmp_path, "serve", "refresh", "catalog-publish")
    result = run_macos(host, "serve refresh catalog-publish")
    assert result.returncode == 0, result.stderr
    held = [call for call in host.calls() if call[0] == "upgrade-gate"]
    assert [call[1] for call in held] == ["hold-writer-locks", "hold-writer-locks", "check-serve"]
    assert {label for _, label, *_ in stops(host)} == {
        f"com.{host.family}.{label}" for label in ("serve", "refresh", "catalog-publish")
    }
    assert {call[2] for call in stops(host)} == {"lock-held"}


@pytest.mark.parametrize("system", SYSTEMS)
def test_the_health_check_accepts_a_regular_install(tmp_path: Path, system: str) -> None:
    """With UV_NO_EDITABLE the service imports its own copy of the package
    from the venv; the check must name the runtime by that same copy."""
    host = controller_host(tmp_path, system)
    if system == "Darwin":
        result = host.run_with(
            "install-controller.sh",
            (),
            shell=MACOS_BASH,
            FAKE_AGENTS="serve refresh",
            UV_NO_EDITABLE="1",
        )
    else:
        result = host.run_with(
            "install-controller-systemd.sh", (), FAKE_UNITS=TIMER_UNITS, UV_NO_EDITABLE="1"
        )
    assert result.returncode == 0, result.stderr
    new = Path(os.path.realpath(host.app / ".venv"))
    assert (new / "lib/python3/site-packages/inky_bird_frame").is_dir()
    assert host.installed_version() == "new"


@pytest.mark.parametrize("system", SYSTEMS)
def test_a_failed_health_check_switches_back(tmp_path: Path, system: str) -> None:
    host = controller_host(tmp_path, system)
    host.fail("health-fail")
    result = run_controller(host)
    assert result.returncode == 1
    assert "web service failed its health check" in result.stderr
    assert "started again on the previous version" in result.stderr
    assert_previous_running(host)


@pytest.mark.parametrize("system", SYSTEMS)
def test_a_health_check_that_never_answers_is_stopped(tmp_path: Path, system: str) -> None:
    host = controller_host(tmp_path, system)
    host.fail("health-hang")
    started = time.monotonic()
    # run_contained also fails if the check outlives the run.
    result = run_controller(host, health_timeout=1)
    assert time.monotonic() - started < 60
    assert result.returncode == 1
    assert "web service failed its health check" in result.stderr
    assert_previous_running(host)


@pytest.mark.parametrize("system", SYSTEMS)
def test_a_lock_holder_that_ignores_a_stop_is_forced_and_the_run_still_ends(
    tmp_path: Path, system: str
) -> None:
    """Cleanup never waits without a bound for a helper it started."""
    host = controller_host(tmp_path, system)
    host.fail("holder-ignores-term", "new:heredoc" if system == "Darwin" else "new:refresh")
    started = time.monotonic()
    result = run_controller(host)
    assert time.monotonic() - started < 90
    assert result.returncode == 1
    assert_previous_running(host)
    helpers = subprocess.run(
        ["/bin/ps", "-ww", "-ax", "-o", "command="], capture_output=True, text=True, check=True
    ).stdout
    assert str(tmp_path) not in helpers


def stop_timeouts(host: Host) -> list[list[str]]:
    return [call for call in host.calls() if call[0] in ("stop-timeout", "exit-timeout")]


@pytest.mark.parametrize("hook", [False, True])
def test_linux_services_whose_shutdown_needs_a_lock_stop_without_a_timeout(
    tmp_path: Path, hook: bool
) -> None:
    """The fake scheduler and web service do not exit while the installer
    holds the writer locks, as a scheduler recording the end of a job does
    not: they are asked to stop under the locks and waited for without them."""
    host = linux_host(tmp_path, mode="app")
    options = ("--stopped-hook", str(host.stopped_hook())) if hook else ()
    result = host.run_with("install-controller-systemd.sh", options, FAKE_UNITS=APP_UNITS)
    assert result.returncode == 0, result.stderr
    assert not stop_timeouts(host)
    stopped = {call[1]: call[2] for call in stops(host)}
    expected = {f"{host.family}-scheduler.service": "lock-held"}
    if hook:
        expected[f"{host.family}-controller.service"] = "lock-held"
        ((_, held, _, running, *_),) = hook_calls(host)
        assert held == "lock-held" and not json.loads(running)
    assert stopped == expected
    assert host.unit_state("scheduler.service") == {"active": True, "enabled": True}


def test_linux_upgrade_waits_for_a_timer_job_blocked_on_a_lock_without_holding_it(
    tmp_path: Path,
) -> None:
    host = linux_host(tmp_path)
    # A refresh a timer started, waiting for a writer lock.
    host.set_busy("refresh.service", 2)
    result = run_linux(host, quiesce_timeout=30)
    assert result.returncode == 0, result.stderr
    assert "Timed out" not in result.stderr


def test_macos_agent_whose_shutdown_needs_a_lock_is_killed_only_after_launchds_timeout(
    tmp_path: Path,
) -> None:
    """launchd stops an agent in bootout and has no stop whose signal can be
    seen before it returns, so the installer boots agents out holding the
    locks. One that waits for a lock is killed after launchd's exit timeout:
    bounded, and while it holds no lock, so no write is cut short."""
    host = macos_host(tmp_path, "serve", "refresh")
    host.fail("agents-wait-for-locks")
    started = time.monotonic()
    result = run_macos(host)
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 60
    assert stop_timeouts(host) == [["exit-timeout", f"com.{host.family}.serve"]]
    assert host.installed_version() == "new"


def hook_calls(host: Host) -> list[list[str]]:
    return [call for call in host.calls() if call[0] == "stopped-hook"]


@pytest.mark.parametrize("system", SYSTEMS)
def test_the_stopped_hook_runs_once_with_everything_stopped_and_locked(
    tmp_path: Path, system: str
) -> None:
    host = controller_host(tmp_path, system)
    hook = host.stopped_hook()
    result = run_controller(host, options=("--stopped-hook", str(hook)))
    assert result.returncode == 0, result.stderr
    assert "synthetic hook output" in result.stdout
    ((_, held, active, running, given, extra),) = hook_calls(host)
    assert (held, active, extra) == ("lock-held", "old", "[]")
    family = {f"com.{host.family}.serve", f"com.{host.family}.refresh"}
    family |= {f"{host.family}-{name}" for name in TIMER_UNITS.split()}
    assert not set(json.loads(running)) & family
    variables = json.loads(given)
    assert variables["INKY_BIRD_HOOK_CONFIG"] == str(host.config)
    assert variables["INKY_BIRD_HOOK_STATE_DIR"] == str(host.state)
    assert Path(variables["INKY_BIRD_HOOK_PREVIOUS_RUNTIME"]).resolve() == host.app.resolve()
    assert (
        Path(variables["INKY_BIRD_HOOK_NEW_RUNTIME"]).parent.resolve()
        == (host.app / ".runtimes").resolve()
    )
    assert host.installed_version() == "new"
    if system == "Linux":
        # The web service stopped for the hook runs again, on the new version.
        assert host.unit_state("controller.service") == {"active": True, "enabled": True}
        assert "lock-held" in [call[2] for call in stops(host)]


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ("hook-fail", "failed with exit status 7"),
        ("hook-killed", "failed with exit status 137"),
        ("health-fail", "web service failed its health check"),
    ],
)
@pytest.mark.parametrize("system", SYSTEMS)
def test_a_failed_hook_or_a_later_failure_switches_back(
    tmp_path: Path, system: str, rule: str, message: str
) -> None:
    host = controller_host(tmp_path, system)
    hook = host.stopped_hook()
    host.fail(rule)
    result = run_controller(host, options=("--stopped-hook", str(hook)))
    assert result.returncode == 1
    assert message in result.stderr
    assert "started again on the previous version" in result.stderr
    assert len(hook_calls(host)) == 1
    assert_previous_running(host)


@pytest.mark.parametrize("system", SYSTEMS)
def test_a_hook_past_its_time_limit_is_stopped(tmp_path: Path, system: str) -> None:
    host = controller_host(tmp_path, system)
    hook = host.stopped_hook()
    host.fail("hook-hang")
    started = time.monotonic()
    result = run_controller(
        host, options=("--stopped-hook", str(hook), "--stopped-hook-timeout", "1")
    )
    assert time.monotonic() - started < 60
    assert result.returncode == 1
    assert "did not finish within 1 seconds and was stopped" in result.stderr
    assert_previous_running(host)


@pytest.mark.parametrize("system", SYSTEMS)
def test_a_stopped_installer_stops_its_hook_before_starting_services_again(
    tmp_path: Path, system: str
) -> None:
    host = controller_host(tmp_path, system)
    hook = host.stopped_hook()
    host.fail("hook-hang")
    output = tmp_path / "installer.out"
    if system == "Darwin":
        installer = host.start(
            "install-controller.sh",
            output,
            shell=MACOS_BASH,
            options=("--stopped-hook", str(hook)),
            FAKE_AGENTS="serve refresh",
        )
    else:
        installer = host.start(
            "install-controller-systemd.sh",
            output,
            options=("--stopped-hook", str(hook)),
            FAKE_UNITS=TIMER_UNITS,
        )
    try:
        wait_until(lambda: bool(hook_calls(host)), "the hook to start")
        installer.send_signal(signal.SIGTERM)
        returncode = installer.wait(timeout=60)
    finally:
        if installer.poll() is None:
            installer.kill()
            installer.wait()
        survivors = end_group(installer.pid)
    assert not survivors, survivors
    assert returncode == 143, output.read_text()
    assert "started again on the previous version" in output.read_text()
    assert_previous_running(host)


def unsafe_hooks(host: Host, tmp_path: Path) -> dict[str, str]:
    """Hooks the installers refuse before they change anything."""
    good = host.stopped_hook()
    writable = tmp_path / "writable-hook"
    writable.write_text(good.read_text())
    writable.chmod(0o775)
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    in_shared = shared / "hook"
    in_shared.write_text(good.read_text())
    in_shared.chmod(0o755)
    link = tmp_path / "linked-hook"
    link.symlink_to(good)
    plain = tmp_path / "plain-hook"
    plain.write_text(good.read_text())
    return {
        "relative": "stopped-hook",
        "missing": str(tmp_path / "missing"),
        "group-writable": str(writable),
        "in a shared directory": str(in_shared),
        "a link": str(link),
        "not executable": str(plain),
        "a directory": str(tmp_path),
    }


@pytest.mark.parametrize("system", SYSTEMS)
def test_unsafe_hooks_and_bad_options_are_refused_before_any_change(
    tmp_path: Path, system: str
) -> None:
    host = controller_host(tmp_path / "machine", system)
    refusals: list[tuple[tuple[str, ...], str]] = [
        (("--stopped-hook", hook), "--stopped-hook must name")
        for hook in unsafe_hooks(host, tmp_path).values()
    ]
    refusals += [
        (("--stopped-hook",), "--stopped-hook needs a value"),
        (("--stopped-hook-timeout", "soon"), "must be a whole number of seconds"),
        (("--stopped-hook-timeout", "5"), "--stopped-hook-timeout needs --stopped-hook"),
        (("--later",), "Unknown option: --later."),
    ]
    for options, message in refusals:
        (host.fake / "log").write_text("")
        result = run_controller(host, options=options)
        assert result.returncode == 1, options
        assert message in result.stderr and "No changes made" in result.stderr, options
        assert not uv_calls(host) and not hook_calls(host), options
        assert_previous_running(host)


# Display node installer -----------------------------------------------------


def display_host(tmp_path: Path) -> Host:
    """A Pimoroni venv with the renamed pre-1.0 distribution and its timer stopped."""
    host = Host(tmp_path, "Linux")
    python = host.venv / "bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(tool_wrapper("venv-python", own_path=True))
    python.chmod(0o755)
    Host.write_source(host.app, "old")
    host.save_state("pip", {"inky-bird-frame": {"source": str(host.app)}})
    # The renamed distribution was a regular, non-editable install.
    package = host.venv / "lib/python3/site-packages/inky_bird_frame"
    shutil.copytree(host.app / "src/inky_bird_frame", package)
    (package / "installation.py").write_text(OLD_INSTALLATION_MODULE)
    host.install_units(display_service="inactive", display_timer="inactive")
    return host


def run_display(host: Host) -> subprocess.CompletedProcess[str]:
    return host.run("install-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")


def test_display_upgrade_installs_new_before_removing_the_renamed_distribution(
    tmp_path: Path,
) -> None:
    host = display_host(tmp_path)
    result = run_display(host)
    assert result.returncode == 0, result.stderr
    pip = [call for call in host.calls() if call[0] in ("pip", "remove-distribution")]
    install = next(i for i, call in enumerate(pip) if call[1] == "install")
    removal = next(i for i, call in enumerate(pip) if call[0] == "remove-distribution")
    assert install < removal
    assert pip[removal] == ["remove-distribution", "inky-bird-frame", "inky-bird"]
    assert not any(call[1] == "uninstall" for call in pip)
    assert set(host.state_of("pip")) == {"inky-bird"}
    # Units are rendered by the new package, not the old one it replaced.
    assert "# rendered by new" in host.unit_text("display.service")
    assert host.unit_state("display.timer") == {"active": True, "enabled": True}


def test_display_failed_package_install_keeps_the_old_distribution(tmp_path: Path) -> None:
    host = display_host(tmp_path)
    host.fail("pip-install")
    result = run_display(host)
    assert result.returncode == 1
    assert set(host.state_of("pip")) == {"inky-bird-frame"}
    assert (host.venv / "lib/python3/site-packages/inky_bird_frame/installation.py").exists()
    # Only the new distribution may be taken back out; the renamed one stays.
    removals = [call for call in host.calls() if call[0] == "remove-distribution"]
    assert removals in ([], [["remove-distribution", "inky-bird", "inky-bird-frame"]])
    assert "# old" in host.unit_text("display.timer")
