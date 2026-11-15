"""Run the native installers against synthetic service managers in a temp HOME.

Nothing here touches launchd, systemd, sudo, a package index or a real host:
``tests/fake_bin/fake_tools.py`` stands in for every external command, and
each script runs from a copy whose few fixed system paths point into the
test directory instead.
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tests/fake_bin/fake_tools.py"
# macOS runs these scripts with its bundled Bash 3.2; exercise that when present.
MACOS_BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"
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
    timer = "# new\\n"
    return {f"{service_prefix}-display.service": service, f"{service_prefix}-display.timer": timer}
"""
# 0.9.x rendered display units without a service prefix argument.
OLD_INSTALLATION_MODULE = """
def display_systemd_units(config, *, executable, app_dir, config_path, user):
    names = ("inky-bird-frame-display.service", "inky-bird-frame-display.timer")
    return {name: "# old\\n" for name in names}
"""
SYSTEM_PATHS = {
    "install-controller.sh": ("/Library/LaunchDaemons", "/usr/bin/plutil"),
    "uninstall-controller.sh": (),
    "install-controller-systemd.sh": ("/etc/systemd/system",),
    "uninstall-controller-systemd.sh": ("/etc/systemd/system",),
    "install-display-local.sh": ("/etc/systemd/system",),
    "uninstall-display-local.sh": ("/etc/systemd/system",),
    "install-display-node.sh": (),
}


class Host:
    """A temp HOME, a source checkout and synthetic system tools."""

    def __init__(self, root: Path, system: str, family: str = "inky-bird") -> None:
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
        for directory in (self.bin, self.home, self.support, self.state, self.units):
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
            path.write_text(
                f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(TOOLS))} "
                f'{tool} "$@"\n'
            )
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
        (directory / "src/runtime-tag").write_text(f"{tag}\n")
        (directory / "catalog").mkdir(exist_ok=True)
        (directory / "deploy").mkdir(exist_ok=True)
        (directory / "docs").mkdir(exist_ok=True)
        for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "docs/browser-access.md"):
            (directory / name).write_text(f"synthetic {tag} {name}\n")

    def script(self, name: str, quiesce_timeout: int = 3) -> Path:
        text = (ROOT / "deploy" / name).read_text()
        replacements = {
            "/Library/LaunchDaemons": str(self.fake / "LaunchDaemons"),
            "/usr/bin/plutil": str(self.bin / "plutil"),
            "/etc/systemd/system": str(self.units),
        }
        for path in SYSTEM_PATHS[name]:
            assert path in text, f"{name} no longer names {path}"
            text = text.replace(path, replacements[path])
        text = text.replace(
            "quiesce_timeout_seconds=1800", f"quiesce_timeout_seconds={quiesce_timeout}"
        )
        target = self.source / "deploy" / name
        target.write_text(text)
        target.chmod(0o755)
        return target

    def environment(self, **extra: str) -> dict[str, str]:
        environment = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin{os.pathsep}/usr/sbin",
            "HOME": str(self.home),
            "FAKE": str(self.fake),
            "FAKE_UNAME": self.system,
            "FAKE_STATE_DIR": str(self.state),
            "FAKE_UNIT_DIR": str(self.units),
            "FAKE_LAYOUT_SERVICE_PREFIX": self.family,
            "FAKE_LAYOUT_APP_DIR": str(self.app),
            "FAKE_LAYOUT_SUPPORT_DIR": str(self.support),
            "FAKE_LAYOUT_CONFIG_PATH": str(self.config),
            "INKY_BIRD_DISPLAY_VENV": str(self.venv),
            # A stray setting must not redirect the new runtime's environment.
            "UV_PROJECT_ENVIRONMENT": str(self.root / "elsewhere"),
        }
        environment.update(extra)
        return environment

    def run(
        self, name: str, *, shell: str = "bash", quiesce_timeout: int = 3, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [shell, str(self.script(name, quiesce_timeout))],
            env=self.environment(**extra),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def run_installed(
        self, name: str, *, shell: str = "bash", **extra: str
    ) -> subprocess.CompletedProcess[str]:
        """Run the installer copy that ``setup --yes`` finds in the active runtime."""
        runtime = Path(os.path.realpath(self.app / "src")).parent
        return subprocess.run(
            [shell, str(runtime / "deploy" / name)],
            env=self.environment(**extra),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
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

    def busy_polls_left(self, unit: str) -> object:
        return json.loads((self.fake / "busy.json").read_text())[f"{self.family}-{unit}"]

    def build_venv(self, project: Path) -> None:
        subprocess.run([sys.executable, str(TOOLS), "build-venv", str(project)], check=True)

    def install_old_runtime(self, layout: str) -> Path:
        """Install the running version in the pre-1.0 or the runtime-directory layout."""
        runtime = self.app if layout == "legacy" else self.app / ".runtimes/runtime.previous"
        runtime.mkdir(parents=True)
        self.write_source(runtime, "old")
        self.build_venv(runtime)
        if layout != "legacy":
            (self.app / ".venv").symlink_to(runtime / ".venv")
            (self.app / "src").symlink_to(runtime / "src")
        return runtime

    def active_runtime(self) -> str:
        tag = Path(os.path.realpath(self.app / ".venv")) / "tag"
        return tag.read_text().strip()

    def runtime_dirs(self, name: str = ".runtimes") -> list[Path]:
        runtimes = self.app / name
        return sorted(runtimes.iterdir()) if runtimes.exists() else []

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


@contextmanager
def running_generation(host: Host, seconds: float) -> Iterator[None]:
    """Hold the controller cycle lock the way a running generation does."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time\n"
            "handle = open(sys.argv[1], 'a+')\n"
            "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "time.sleep(float(sys.argv[2]))\n",
            str(host.state / "controller.lock"),
            str(seconds),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        yield
    finally:
        holder.kill()
        holder.wait()


# macOS LaunchAgent installer -------------------------------------------------


@pytest.mark.parametrize("layout", ["legacy", "runtimes"])
def test_macos_upgrade_switches_runtime_and_keeps_one_previous(tmp_path: Path, layout: str) -> None:
    host = Host(tmp_path, "Darwin")
    previous = host.install_old_runtime(layout)
    host.load_agents("serve", "refresh")

    result = host.run("install-controller.sh", shell=MACOS_BASH)

    assert result.returncode == 0, result.stderr
    assert host.active_runtime() == "new"
    assert (host.app / ".venv").is_symlink() and (host.app / "src").is_symlink()
    assert (host.app / "src/runtime-tag").read_text() == "new\n"
    assert host.loaded_agents() == {
        "serve": {"generation": "new", "runtime": "new", "run_at_load": True},
        "refresh": {"generation": "new", "runtime": "new", "run_at_load": True},
    }
    # Agents stopped only while no plate was being made.
    assert [call[3] for call in bootouts(host)] == ["lock-held", "lock-held"]
    uv_calls = [call for call in host.calls() if call[0] == "uv"]
    assert len(uv_calls) == 1 and uv_calls[0][-1] == "False"
    runtimes = host.runtime_dirs()
    assert len(runtimes) == 2
    kept = next(path for path in runtimes if (path / ".venv/tag").read_text() == "old\n")
    assert (kept / "src/runtime-tag").read_text() == "old\n"
    if layout == "runtimes":
        assert kept == previous.resolve()
    assert (host.app / "deploy/install-controller.sh").exists()

    # The next upgrade keeps exactly one previous runtime: the one just replaced.
    first = Path(os.path.realpath(host.app / ".venv")).parent
    Host.write_source(host.source, "newer")
    result = host.run("install-controller.sh", shell=MACOS_BASH)
    assert result.returncode == 0, result.stderr
    assert host.active_runtime() == "newer"
    assert sorted(host.runtime_dirs()) == sorted(
        [first, Path(os.path.realpath(host.app / ".venv")).parent]
    )


def test_macos_fresh_install_starts_new_agents(tmp_path: Path) -> None:
    host = Host(tmp_path, "Darwin")
    result = host.run("install-controller.sh", shell=MACOS_BASH)
    assert result.returncode == 0, result.stderr
    assert host.active_runtime() == "new"
    assert set(host.loaded_agents()) == {"serve", "refresh"}
    assert len(host.runtime_dirs()) == 1


@pytest.mark.parametrize("failure", ["uv", "new:config", "new:heredoc"])
def test_macos_failed_build_or_validation_leaves_controller_untouched(
    tmp_path: Path, failure: str
) -> None:
    host = Host(tmp_path, "Darwin")
    host.install_old_runtime("legacy")
    host.load_agents("serve", "refresh")
    host.fail(failure)

    result = host.run("install-controller.sh", shell=MACOS_BASH)

    assert result.returncode == 1, result.stderr
    assert not bootouts(host)
    assert not any(call[:2] == ["launchctl", "bootstrap"] for call in host.calls())
    assert host.loaded_agents() == {
        "serve": {"generation": "old", "runtime": "old"},
        "refresh": {"generation": "old", "runtime": "old"},
    }
    assert not (host.app / ".venv").is_symlink()
    assert host.active_runtime() == "old"
    assert (host.app / "src/runtime-tag").read_text() == "old\n"
    assert host.plist_generation("serve") == "old"
    assert host.runtime_dirs() == []
    if failure == "new:config":
        assert "running controller was not changed" in result.stderr
        # The CLI reports the reason on stdout; the operator must still see it.
        assert "synthetic config failure" in result.stderr
        assert '"ok": false' not in result.stderr


def test_macos_failure_before_switch_restores_only_what_it_stopped(tmp_path: Path) -> None:
    host = Host(tmp_path, "Darwin")
    host.install_old_runtime("legacy")
    host.load_agents("serve", "refresh", "catalog-publish")
    host.fail("new:catalog-publish")

    result = host.run(
        "install-controller.sh", shell=MACOS_BASH, FAKE_AGENTS="serve refresh catalog-publish"
    )

    assert result.returncode == 1
    # Only the publisher was stopped (to validate it); it returns without RunAtLoad.
    assert [call[2] for call in bootouts(host)] == [f"com.{host.family}.catalog-publish"]
    agents = host.loaded_agents()
    assert agents["serve"] == {"generation": "old", "runtime": "old"}
    assert agents["refresh"] == {"generation": "old", "runtime": "old"}
    assert agents["catalog-publish"] == {
        "generation": "old",
        "runtime": "old",
        "run_at_load": False,
    }
    for label in ("serve", "refresh", "catalog-publish"):
        assert host.plist_generation(label) == "old"
    assert host.active_runtime() == "old"
    assert host.runtime_dirs() == []


@pytest.mark.parametrize("layout", ["legacy", "runtimes"])
def test_macos_failure_after_switch_restarts_old_agents_on_old_runtime(
    tmp_path: Path, layout: str
) -> None:
    host = Host(tmp_path, "Darwin")
    host.install_old_runtime(layout)
    host.load_agents("serve", "refresh")
    host.fail("bootstrap-new:refresh")

    result = host.run("install-controller.sh", shell=MACOS_BASH)

    assert result.returncode != 0
    assert host.active_runtime() == "old"
    assert (host.app / "src/runtime-tag").read_text() == "old\n"
    assert host.loaded_agents() == {
        "serve": {"generation": "old", "runtime": "old", "run_at_load": True},
        "refresh": {"generation": "old", "runtime": "old", "run_at_load": True},
    }
    assert host.runtime_dirs()
    assert all((path / ".venv/tag").read_text() == "old\n" for path in host.runtime_dirs())
    assert "previous version is running again" in result.stderr


def test_macos_rollback_leaves_an_agent_that_was_not_loaded_unloaded(tmp_path: Path) -> None:
    host = Host(tmp_path, "Darwin")
    host.install_old_runtime("legacy")
    host.load_agents("serve", "refresh")
    loaded = host.state_of("launchd")
    del loaded[f"com.{host.family}.refresh"]
    host.save_state("launchd", loaded)
    host.fail("bootstrap-new:notifications")

    result = host.run(
        "install-controller.sh", shell=MACOS_BASH, FAKE_AGENTS="serve refresh notifications"
    )

    assert result.returncode != 0
    assert host.active_runtime() == "old"
    assert set(host.loaded_agents()) == {"serve"}
    assert host.loaded_agents()["serve"]["runtime"] == "old"
    assert host.plist_generation("refresh") == "old"
    assert "previous version is running again" in result.stderr


def test_macos_failed_upgrade_reports_agents_it_could_not_restart(tmp_path: Path) -> None:
    host = Host(tmp_path, "Darwin")
    host.install_old_runtime("legacy")
    host.load_agents("serve", "refresh")
    host.fail("bootstrap-new:serve", "bootstrap-old:serve")

    result = host.run("install-controller.sh", shell=MACOS_BASH)

    assert result.returncode != 0
    assert f"com.{host.family}.serve" in result.stderr
    assert "launchctl bootstrap" in result.stderr
    assert host.loaded_agents()["refresh"]["runtime"] == "old"


def test_macos_setup_reruns_from_the_installed_runtime(tmp_path: Path) -> None:
    host = Host(tmp_path, "Darwin")
    assert host.run("install-controller.sh", shell=MACOS_BASH).returncode == 0
    first = Path(os.path.realpath(host.app / ".venv")).parent

    result = host.run_installed("install-controller.sh", shell=MACOS_BASH)

    assert result.returncode == 0, result.stderr
    assert f"installed from {host.app} into" in result.stdout
    assert host.active_runtime() == "new"
    assert Path(os.path.realpath(host.app / ".venv")).parent != first
    assert first in host.runtime_dirs()
    assert set(host.loaded_agents()) == {"serve", "refresh"}


def test_macos_rollback_keeps_the_runtime_of_an_agent_it_could_not_stop(tmp_path: Path) -> None:
    host = Host(tmp_path, "Darwin")
    host.fail("bootstrap-new:refresh", f"bootout:com.{host.family}.serve")

    result = host.run("install-controller.sh", shell=MACOS_BASH)

    assert result.returncode != 0
    serve = host.loaded_agents()["serve"]
    assert serve["runtime"] == "new"
    runtimes = host.runtime_dirs()
    assert len(runtimes) == 1
    assert (runtimes[0] / ".venv/tag").read_text() == "new\n"
    assert (runtimes[0] / "src/runtime-tag").read_text() == "new\n"
    assert f"com.{host.family}.serve" in result.stderr
    assert "could not be stopped" in result.stderr
    assert "already running uses the new version" in result.stderr
    assert "previous version" not in result.stderr
    assert "may still be running" not in result.stderr
    assert "are not running" not in result.stderr


def test_macos_rollback_says_a_stuck_agent_restarts_on_the_previous_version(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path, "Darwin")
    host.install_old_runtime("legacy")
    host.load_agents("refresh")
    host.fail("bootstrap-new:refresh", f"bootout:com.{host.family}.serve")

    result = host.run("install-controller.sh", shell=MACOS_BASH)

    assert result.returncode != 0
    assert host.active_runtime() == "old"
    assert host.loaded_agents()["serve"]["runtime"] == "new"
    assert "already running uses the new version" in result.stderr
    assert "when it starts again it uses the previous version" in result.stderr
    assert "may still be running" not in result.stderr


def test_macos_upgrade_refuses_while_a_plate_is_being_made(tmp_path: Path) -> None:
    host = Host(tmp_path, "Darwin")
    host.install_old_runtime("legacy")
    host.load_agents("serve", "refresh")

    with running_generation(host, 60):
        result = host.run("install-controller.sh", shell=MACOS_BASH)

    assert result.returncode == 3
    assert "still being made" in result.stderr and "No changes made" in result.stderr
    assert not bootouts(host)
    assert host.active_runtime() == "old"
    assert host.plist_generation("serve") == "old"
    assert host.runtime_dirs() == []


def test_macos_upgrade_waits_for_a_plate_to_finish(tmp_path: Path) -> None:
    host = Host(tmp_path, "Darwin")
    host.install_old_runtime("legacy")
    host.load_agents("serve", "refresh")
    started = time.monotonic()

    with running_generation(host, 1.5):
        result = host.run("install-controller.sh", shell=MACOS_BASH)

    assert result.returncode == 0, result.stderr
    assert "Waiting for the plate being made to finish" in result.stderr
    assert time.monotonic() - started >= 1.5
    assert [call[3] for call in bootouts(host)] == ["lock-held", "lock-held"]


def test_macos_failure_after_install_says_the_new_version_is_running(tmp_path: Path) -> None:
    host = Host(tmp_path, "Darwin")
    host.install_old_runtime("legacy")
    host.load_agents("serve", "refresh", "controller-cycle")
    host.fail(f"bootout:com.{host.family}.controller-cycle")

    result = host.run("install-controller.sh", shell=MACOS_BASH)

    assert result.returncode != 0
    assert host.active_runtime() == "new"
    assert host.loaded_agents()["serve"]["runtime"] == "new"
    assert "new version is installed and running" in result.stderr
    assert "previous version is running again" not in result.stderr


# Linux systemd controller installer ------------------------------------------


def linux_host(tmp_path: Path, mode: str = "timers") -> Host:
    host = Host(tmp_path, "Linux")
    host.install_old_runtime("legacy")
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


def test_linux_upgrade_switches_runtime_and_restarts_services(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    result = run_linux(host)
    assert result.returncode == 0, result.stderr
    assert host.active_runtime() == "new"
    assert "# new" in host.unit_text("controller.service")
    for unit in ("controller.service", "refresh.timer", "generate.timer"):
        assert host.unit_state(unit) == {"active": True, "enabled": True}
    assert len(host.runtime_dirs()) == 2


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


@pytest.mark.parametrize("failure", ["uv", "new:config", "new:heredoc", "new:catalog-publish"])
def test_linux_failure_before_switch_keeps_old_runtime_and_restarts_stopped_timers(
    tmp_path: Path, failure: str
) -> None:
    host = linux_host(tmp_path)
    host.fail(failure)
    result = run_linux(host, f"{TIMER_UNITS} catalog-publish.service catalog-publish.timer")
    assert result.returncode == 1
    assert host.active_runtime() == "old"
    assert not (host.app / ".venv").is_symlink()
    assert "# old" in host.unit_text("controller.service")
    for unit in ("controller.service", "refresh.timer", "generate.timer"):
        assert host.unit_state(unit)["active"] is True
    assert host.runtime_dirs() == []
    if failure in ("uv", "new:config"):
        # The build happens before anything is stopped.
        assert not any(call[:2] == ["systemctl", "stop"] for call in host.calls())
    if failure == "new:config":
        assert "running controller was not changed" in result.stderr
        assert "synthetic config failure" in result.stderr
        assert '"ok": false' not in result.stderr


def test_linux_failure_after_switch_restores_units_runtime_and_services(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.fail(f"systemctl:restart:{host.family}-generate.timer")
    result = run_linux(host)
    assert result.returncode != 0
    assert host.active_runtime() == "old"
    assert "# old" in host.unit_text("controller.service")
    assert "# old" in host.unit_text("generate.timer")
    for unit in ("controller.service", "refresh.timer"):
        assert host.unit_state(unit)["active"] is True
    restarts = [
        call
        for call in host.calls()
        if call[:2] == ["systemctl", "restart"] and call[2] == f"{host.family}-controller.service"
    ]
    # Once onto the new runtime, once back onto the old one.
    assert len(restarts) == 2
    assert all((path / ".venv/tag").read_text() == "old\n" for path in host.runtime_dirs())


def test_linux_expired_sudo_restarts_stopped_timers(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.fail("sudo:install")
    result = run_linux(host)
    assert result.returncode != 0
    assert host.active_runtime() == "old"
    for unit in ("controller.service", "refresh.timer", "generate.timer"):
        assert host.unit_state(unit)["active"] is True
    assert "running again" in result.stderr


def test_linux_reports_timers_it_could_not_restart(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.fail("sudo-expires-at-install")
    result = run_linux(host)
    assert result.returncode != 0
    assert host.unit_state("refresh.timer")["active"] is False
    assert f"{host.family}-refresh.timer" in result.stderr
    assert f"{host.family}-generate.timer" in result.stderr
    assert "sudo systemctl start" in result.stderr


def test_linux_expired_sudo_after_controller_restart_keeps_its_runtime(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    controller = f"{host.family}-controller.service"
    host.fail(f"sudo-expires-at:systemctl:restart:{host.family}-refresh.timer")

    result = run_linux(host)

    assert result.returncode != 0
    assert host.unit_state("controller.service")["active"] is True
    process = host.state_of("processes")[controller]
    assert process["runtime"] == "new"
    assert (Path(str(process["directory"])) / ".venv/tag").read_text() == "new\n"
    assert controller in result.stderr and "may still" in result.stderr
    stopped = [line for line in result.stderr.splitlines() if "are stopped" in line]
    assert stopped and all(controller not in line for line in stopped)
    assert "running again" not in result.stderr


def test_linux_failed_rollback_lists_only_services_that_are_really_stopped(
    tmp_path: Path,
) -> None:
    host = linux_host(tmp_path)
    host.install_units(
        controller_service="active",
        scheduler_service="active",
        refresh_service="inactive",
        refresh_timer="active",
        generate_service="inactive",
        generate_timer="active",
    )
    host.fail(f"sudo-expires-at:systemctl:restart:{host.family}-generate.timer")

    result = run_linux(host, f"{TIMER_UNITS} scheduler.service")

    assert result.returncode != 0
    assert host.unit_state("scheduler.service")["active"] is True
    assert host.unit_state("generate.timer")["active"] is False
    stopped = [line for line in result.stderr.splitlines() if "are stopped" in line]
    assert len(stopped) == 1
    assert f"{host.family}-generate.timer" in stopped[0]
    assert f"{host.family}-scheduler.service" not in stopped[0]
    assert f"{host.family}-refresh.timer" not in stopped[0]


def test_linux_unreachable_bird_source_does_not_block_install(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.fail("new:refresh:data_source")
    result = run_linux(host)
    assert result.returncode == 0, result.stderr
    assert "will try again" in result.stderr
    assert host.active_runtime() == "new"
    assert host.unit_state("refresh.timer") == {"active": True, "enabled": True}


@pytest.mark.parametrize(
    "failure", ["new:refresh", "new:refresh:storage", "new:refresh:unreadable"]
)
def test_linux_other_refresh_failures_abort_and_keep_the_old_runtime(
    tmp_path: Path, failure: str
) -> None:
    host = linux_host(tmp_path)
    host.fail(failure)
    result = run_linux(host)
    assert result.returncode == 1
    assert "will try again" not in result.stderr
    assert host.active_runtime() == "old"
    assert "# old" in host.unit_text("controller.service")
    for unit in ("controller.service", "refresh.timer", "generate.timer"):
        assert host.unit_state(unit)["active"] is True
    assert host.runtime_dirs() == []


@pytest.mark.parametrize("extra_failures", [(), ("systemctl:disable:inky-bird-refresh.timer",)])
def test_linux_rollback_leaves_previously_disabled_units_disabled_and_stopped(
    tmp_path: Path, extra_failures: tuple[str, ...]
) -> None:
    host = Host(tmp_path, "Linux")
    host.install_old_runtime("legacy")
    host.install_units(
        controller_service="active",
        scheduler_service="inactive",
        refresh_service="inactive",
        refresh_timer="inactive",
        generate_service="inactive",
        generate_timer="active",
    )
    state = host.state_of("systemd")
    for name in ("scheduler.service", "refresh.timer"):
        state[f"{host.family}-{name}"] = {"active": False, "enabled": False}
    host.save_state("systemd", state)
    host.fail(f"systemctl:restart:{host.family}-generate.timer", *extra_failures)

    result = run_linux(host, f"{TIMER_UNITS} scheduler.service")

    assert result.returncode != 0
    assert host.active_runtime() == "old"
    if extra_failures:
        assert host.unit_state("refresh.timer")["enabled"] is True
        assert "Could not put back" in result.stderr
        assert "previous version is running again" not in result.stderr
        return
    for name in ("scheduler.service", "refresh.timer"):
        assert host.unit_state(name) == {"active": False, "enabled": False}
    assert host.unit_state("controller.service") == {"active": True, "enabled": True}
    assert host.unit_state("generate.timer")["active"] is True
    assert "previous version is running again" in result.stderr


def unit_install_calls(host: Host) -> list[int]:
    return [
        index
        for index, call in enumerate(host.calls())
        if call[:2] == ["sudo", "install"] and call[-1].startswith(str(host.units))
    ]


def test_linux_upgrade_waits_for_a_running_timer_job_before_switching(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.set_busy("generate.service", 2)

    result = run_linux(host, quiesce_timeout=60)

    assert result.returncode == 0, result.stderr
    calls = host.calls()
    polls = [index for index, call in enumerate(calls) if call[0] == "busy-poll"]
    assert len(polls) == 2
    assert host.busy_polls_left("generate.service") == 0
    timer_stop = calls.index(["sudo", "systemctl", "stop", f"{host.family}-generate.timer"])
    renders = [index for index, call in enumerate(calls) if call == ["heredoc", "new"]]
    assert timer_stop < polls[0]
    # Nothing is rendered, installed or switched until the job has finished.
    refresh = calls.index(["inky-bird", "new", "refresh", "--config", str(host.config)])
    assert polls[-1] < renders[0] < refresh < unit_install_calls(host)[0]
    assert host.active_runtime() == "new"


def test_linux_upgrade_gives_up_on_a_job_that_does_not_finish(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.set_busy("generate.service", 1000)

    result = run_linux(host)

    assert result.returncode == 1
    assert f"Timed out waiting for {host.family}-generate.service" in result.stderr
    assert not unit_install_calls(host)
    assert not any(call[0] == "heredoc" for call in host.calls())
    assert host.active_runtime() == "old"
    assert not (host.app / ".venv").is_symlink()
    assert "# old" in host.unit_text("controller.service")
    for unit in ("refresh.timer", "generate.timer"):
        assert host.unit_state(unit)["active"] is True
    assert host.runtime_dirs() == []
    assert "previous version is running again" in result.stderr


def test_linux_rollback_stops_only_what_this_run_started(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    systemd = host.state_of("systemd")
    systemd[f"{host.family}-controller.service"]["active"] = False
    systemd[f"{host.family}-refresh.timer"] = {"active": False, "enabled": False}
    host.save_state("systemd", systemd)
    host.fail(f"systemctl:restart:{host.family}-generate.timer")

    result = run_linux(host)

    assert result.returncode != 0
    assert host.active_runtime() == "old"
    assert "# old" in host.unit_text("refresh.timer")
    assert host.unit_state("controller.service") == {"active": False, "enabled": True}
    assert host.unit_state("refresh.timer") == {"active": False, "enabled": False}
    assert host.unit_state("generate.timer") == {"active": True, "enabled": True}


def test_linux_setup_reruns_from_the_installed_runtime(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    assert run_linux(host).returncode == 0
    first = Path(os.path.realpath(host.app / ".venv")).parent

    result = host.run_installed("install-controller-systemd.sh", FAKE_UNITS=TIMER_UNITS)

    assert result.returncode == 0, result.stderr
    assert f"installed from {host.app} into" in result.stdout
    assert host.active_runtime() == "new"
    assert Path(os.path.realpath(host.app / ".venv")).parent != first
    assert first in host.runtime_dirs()
    for unit in ("controller.service", "refresh.timer", "generate.timer"):
        assert host.unit_state(unit) == {"active": True, "enabled": True}


def test_linux_rollback_keeps_the_runtime_of_a_service_it_could_not_stop(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    scheduler = f"{host.family}-scheduler.service"
    host.fail(f"systemctl:restart:{host.family}-generate.timer", f"systemctl:disable:{scheduler}")

    result = run_linux(host, f"{TIMER_UNITS} scheduler.service")

    assert result.returncode != 0
    assert host.unit_state("scheduler.service")["active"] is True
    process = host.state_of("processes")[scheduler]
    assert process["runtime"] == "new"
    assert (Path(str(process["directory"])) / ".venv/tag").read_text() == "new\n"
    assert scheduler in result.stderr and "may still" in result.stderr
    assert "previous version is running again" not in result.stderr
    # Its unit file stays so a rerun or the uninstaller can still find and stop it.
    assert (host.units / scheduler).exists()


def test_linux_failure_after_install_says_the_new_version_is_running(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    # A directory where the installer copies itself makes the final tidy-up fail.
    (host.app / "deploy/install-controller-systemd.sh").mkdir(parents=True)

    result = run_linux(host)

    assert result.returncode != 0
    assert host.active_runtime() == "new"
    assert host.unit_state("controller.service")["active"] is True
    assert "new version is installed and running" in result.stderr
    assert "previous version is running again" not in result.stderr


def test_linux_app_owned_upgrade_stops_scheduler_only_between_plates(tmp_path: Path) -> None:
    host = linux_host(tmp_path, mode="app")
    result = run_linux(host, APP_UNITS)
    assert result.returncode == 0, result.stderr
    stops = [call for call in host.calls() if call[0] == "lock-at-stop"]
    assert stops == [["lock-at-stop", f"{host.family}-scheduler.service", "lock-held"]]
    assert host.unit_state("scheduler.service")["active"] is True


def test_linux_app_owned_upgrade_refuses_while_a_plate_is_being_made(tmp_path: Path) -> None:
    host = linux_host(tmp_path, mode="app")
    with running_generation(host, 60):
        result = run_linux(host, APP_UNITS)
    assert result.returncode == 3
    assert "still being made" in result.stderr
    assert host.unit_state("scheduler.service")["active"] is True
    assert not any(call[:2] == ["systemctl", "stop"] for call in host.calls())
    assert host.active_runtime() == "old"


# Display node installer -----------------------------------------------------


def add_display_venv(host: Host) -> None:
    python = host.venv / "bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(TOOLS))} "
        'venv-python "$0" "$@"\n'
    )
    python.chmod(0o755)


def display_host(tmp_path: Path, installed: str | None) -> Host:
    """A Pimoroni venv with the old distribution installed and its timer stopped."""
    host = Host(tmp_path, "Linux")
    add_display_venv(host)
    if installed is not None:
        Host.write_source(host.app, "old")
        host.save_state("pip", {installed: {"source": str(host.app)}})
        site = host.venv / "lib/python3/site-packages"
        site.mkdir(parents=True)
        if installed == "inky-bird-frame":
            # The renamed distribution was a regular, non-editable install.
            package = site / "inky_bird_frame"
            shutil.copytree(host.app / "src/inky_bird_frame", package)
            (package / "installation.py").write_text(OLD_INSTALLATION_MODULE)
            (site / "runtime-tag").write_text("old\n")
        else:
            (site / "_inky_bird.pth").write_text(f"{host.app}/src\n")
        host.install_units(display_service="inactive", display_timer="inactive")
    return host


def test_display_upgrade_installs_new_before_removing_old_distribution(tmp_path: Path) -> None:
    host = display_host(tmp_path, "inky-bird-frame")
    result = host.run("install-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")
    assert result.returncode == 0, result.stderr
    pip = [call for call in host.calls() if call[0] in ("pip", "remove-distribution")]
    install = next(i for i, call in enumerate(pip) if call[1] == "install")
    removal = next(i for i, call in enumerate(pip) if call[0] == "remove-distribution")
    assert install < removal
    assert pip[removal] == ["remove-distribution", "inky-bird-frame", "inky-bird"]
    assert not any(call[1] == "uninstall" for call in pip)
    installed = host.state_of("pip")
    assert set(installed) == {"inky-bird"}
    source = Path(str(installed["inky-bird"]["source"]))
    assert source.parent == (host.app / ".display-runtimes").resolve()
    assert (source / "src/runtime-tag").read_text() == "new\n"
    assert (host.app / "src").is_symlink()
    assert host.unit_state("display.timer") == {"active": True, "enabled": True}


def test_display_setup_reruns_from_the_installed_runtime(tmp_path: Path) -> None:
    host = display_host(tmp_path, "inky-bird")
    assert run_display(host).returncode == 0
    first = display_source(host)

    result = host.run_installed("install-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")

    assert result.returncode == 0, result.stderr
    assert f"installed from {host.app} into" in result.stdout
    assert display_source(host) != first
    assert (display_source(host) / "src/runtime-tag").read_text() == "new\n"
    assert host.unit_state("display.timer") == {"active": True, "enabled": True}


def test_display_failed_package_install_keeps_old_runtime_and_restarts_timer(
    tmp_path: Path,
) -> None:
    host = display_host(tmp_path, "inky-bird-frame")
    host.fail("pip-install")
    result = host.run("install-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")
    assert result.returncode == 1
    assert set(host.state_of("pip")) == {"inky-bird-frame"}
    assert not any(call[0] == "remove-distribution" for call in host.calls())
    assert (host.app / "src/runtime-tag").read_text() == "old\n"
    assert not (host.app / "src").is_symlink()
    assert host.unit_state("display.timer")["active"] is True
    assert "# old" in host.unit_text("display.timer")
    assert host.runtime_dirs(".display-runtimes") == []
    assert "previous version" in result.stderr


def test_display_failure_after_install_reinstalls_the_same_named_previous_version(
    tmp_path: Path,
) -> None:
    host = display_host(tmp_path, "inky-bird")
    # The Pimoroni venv carries no runtime tag, so its unit rendering is "missing:".
    host.fail("missing:heredoc")
    result = host.run("install-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")
    assert result.returncode == 1
    installs = [call for call in host.calls() if call[:2] == ["pip", "install"]]
    assert len(installs) == 2
    assert installs[-1][-1] == f"{host.app}[inky]"
    assert host.state_of("pip") == {"inky-bird": {"source": str(host.app)}}
    assert (host.app / "src/runtime-tag").read_text() == "old\n"
    assert host.unit_state("display.timer")["active"] is True
    assert host.runtime_dirs(".display-runtimes") == []
    assert "previous version is unchanged" in result.stderr


def test_display_failed_fresh_install_leaves_no_enabled_timer(tmp_path: Path) -> None:
    host = display_host(tmp_path, None)
    host.fail(f"systemctl:restart:{host.family}-display.timer")
    result = host.run("install-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")
    assert result.returncode == 1
    assert not (host.units / f"{host.family}-display.timer").exists()
    assert host.unit_state("display.timer") == {"active": False, "enabled": False}


def test_display_failed_fresh_install_keeps_a_timer_it_could_not_disable(tmp_path: Path) -> None:
    host = display_host(tmp_path, None)
    timer = f"{host.family}-display.timer"
    host.fail(f"systemctl:restart:{timer}", f"systemctl:disable:{timer}")
    result = host.run("install-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")
    assert result.returncode == 1
    assert host.unit_state("display.timer")["enabled"] is True
    assert (host.units / timer).exists()
    assert "Could not put back" in result.stderr


def test_display_rejected_configuration_changes_nothing(tmp_path: Path) -> None:
    host = display_host(tmp_path, "inky-bird")
    host.fail("new:load_config")
    result = host.run("install-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")
    assert result.returncode == 1
    assert not any(call[:2] == ["pip", "install"] for call in host.calls())
    assert host.state_of("pip") == {"inky-bird": {"source": str(host.app)}}
    assert host.runtime_dirs(".display-runtimes") == []


def test_display_name_migration_renders_units_with_the_new_package(tmp_path: Path) -> None:
    host = display_host(tmp_path, "inky-bird-frame")
    result = host.run("install-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")
    assert result.returncode == 0, result.stderr
    # The old package shadowed the new one until it was removed; units come from the new one.
    assert "# rendered by new" in host.unit_text("display.service")
    assert not (host.venv / "lib/python3/site-packages/inky_bird_frame").exists()


def disable_display_timer(host: Host) -> None:
    state = host.state_of("systemd")
    state[f"{host.family}-display.timer"] = {"active": False, "enabled": False}
    host.save_state("systemd", state)


def test_display_rollback_leaves_a_previously_disabled_timer_disabled(tmp_path: Path) -> None:
    host = display_host(tmp_path, "inky-bird")
    disable_display_timer(host)
    host.fail(f"systemctl:start:{host.family}-display.service")

    result = host.run(
        "install-display-local.sh",
        INKY_BIRD_NONINTERACTIVE_SUDO="true",
        INKY_BIRD_RUN_INITIAL_DISPLAY="true",
    )

    assert result.returncode == 1
    assert host.unit_state("display.timer") == {"active": False, "enabled": False}
    assert "# old" in host.unit_text("display.timer")
    assert "previous version is unchanged" in result.stderr


def test_display_rollback_reports_a_timer_it_could_not_put_back(tmp_path: Path) -> None:
    host = display_host(tmp_path, "inky-bird")
    disable_display_timer(host)
    timer = f"{host.family}-display.timer"
    host.fail(f"systemctl:start:{host.family}-display.service", f"systemctl:disable:{timer}")

    result = host.run(
        "install-display-local.sh",
        INKY_BIRD_NONINTERACTIVE_SUDO="true",
        INKY_BIRD_RUN_INITIAL_DISPLAY="true",
    )

    assert result.returncode == 1
    assert host.unit_state("display.timer")["enabled"] is True
    assert timer in result.stderr and "could not be put back" in result.stderr
    assert "previous version is unchanged" not in result.stderr


def test_display_failure_after_timer_restart_rearms_the_previous_timer(tmp_path: Path) -> None:
    host = display_host(tmp_path, "inky-bird")
    timer = f"{host.family}-display.timer"
    host.fail(f"systemctl:start:{host.family}-display.service")

    result = host.run(
        "install-display-local.sh",
        INKY_BIRD_NONINTERACTIVE_SUDO="true",
        INKY_BIRD_RUN_INITIAL_DISPLAY="true",
    )

    assert result.returncode == 1
    calls = host.calls()
    restored = max(
        index
        for index, call in enumerate(calls)
        if call[:2] == ["sudo", "install"] and call[-1] == str(host.units / timer)
    )
    # Started again after its previous definition is back, not left on the new one.
    assert ["systemctl", "restart", timer] in calls[restored:]
    assert "# old" in host.unit_text("display.timer")
    assert host.unit_state("display.timer")["active"] is True
    assert (host.app / "src/runtime-tag").read_text() == "old\n"
    assert (display_source(host) / "src/runtime-tag").read_text() == "old\n"
    assert "previous version is unchanged" in result.stderr


def test_app_owned_rollback_restarts_the_scheduler_on_the_restored_runtime(
    tmp_path: Path,
) -> None:
    host = linux_host(tmp_path, mode="app")
    host.fail(f"systemctl:restart:{host.family}-notifications.timer")

    result = run_linux(host, f"{APP_UNITS} notifications.service notifications.timer")

    assert result.returncode != 0
    assert host.active_runtime() == "old"
    assert host.unit_state("scheduler.service")["active"] is True
    scheduler = host.state_of("processes")[f"{host.family}-scheduler.service"]
    assert scheduler["runtime"] == "old"
    assert Path(str(scheduler["directory"])).is_dir()
    assert "previous version is running again" in result.stderr


# One Pi running both roles -----------------------------------------------------


def let_display_finish(host: Host) -> None:
    """Stop the display timer the way the operator does before the next upgrade."""
    systemd = host.state_of("systemd")
    for suffix in ("service", "timer"):
        systemd[f"{host.family}-display.{suffix}"] = {"active": False, "enabled": True}
    host.save_state("systemd", systemd)


def run_display(host: Host) -> subprocess.CompletedProcess[str]:
    result = host.run("install-display-local.sh", INKY_BIRD_NONINTERACTIVE_SUDO="true")
    let_display_finish(host)
    return result


def display_source(host: Host) -> Path:
    return Path(str(host.state_of("pip")["inky-bird"]["source"]))


def test_display_upgrades_keep_the_controller_runtime(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    result = run_linux(host)
    assert result.returncode == 0, result.stderr
    controller_runtimes = host.runtime_dirs()
    add_display_venv(host)

    for tag in ("new", "newer"):
        Host.write_source(host.source, tag)
        result = run_display(host)
        assert result.returncode == 0, result.stderr

    python = host.app / ".venv/bin/python"
    assert python.exists()
    check = subprocess.run(
        [str(python), "-I", "-c", "print('ok')"],
        env=host.environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.stdout == "ok\n", check.stderr
    assert host.active_runtime() == "new"
    # The controller's own rollback runtime is kept as well.
    assert all(path.is_dir() for path in controller_runtimes)
    assert (display_source(host) / "src/runtime-tag").read_text() == "newer\n"


def test_controller_upgrades_keep_the_display_source(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    add_display_venv(host)
    result = run_display(host)
    assert result.returncode == 0, result.stderr
    source = display_source(host)

    for tag in ("newer", "newest"):
        Host.write_source(host.source, tag)
        result = run_linux(host)
        assert result.returncode == 0, result.stderr

    assert host.active_runtime() == "newest"
    assert display_source(host) == source
    assert (source / "src/runtime-tag").read_text() == "new\n"


def test_controller_rollback_restores_the_display_source_link(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    add_display_venv(host)
    result = run_display(host)
    assert result.returncode == 0, result.stderr
    linked = os.path.realpath(host.app / "src")
    Host.write_source(host.source, "newer")
    host.fail(f"systemctl:restart:{host.family}-generate.timer")

    result = run_linux(host)

    assert result.returncode != 0
    assert host.active_runtime() == "old"
    assert os.path.realpath(host.app / "src") == linked
    assert (host.app / "src/runtime-tag").read_text() == "new\n"


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
