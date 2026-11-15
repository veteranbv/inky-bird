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
            # A stray setting must not redirect the managed environment.
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
    assert not uv_calls(host)
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
    assert not uv_calls(host)
    assert host.unit_state("generate.timer")["active"] is True


def test_linux_asks_for_sudo_again_after_waiting_for_jobs(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.fail("sudo-v-expires-after:2")
    result = run_linux(host)
    assert result.returncode == 1
    # Nothing in the managed directory changed; the stopped timers are named.
    assert not uv_calls(host)
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


def test_linux_failure_does_not_restart_a_timer_it_removed_on_purpose(tmp_path: Path) -> None:
    host = linux_host(tmp_path)
    host.fail(f"systemctl:restart:{host.family}-refresh.timer")
    result = run_linux(host, "controller.service refresh.service refresh.timer")
    assert result.returncode != 0
    assert not (host.units / f"{host.family}-generate.timer").exists()
    starts = [call for call in host.calls() if call[:2] == ["systemctl", "start"]]
    assert starts == [["systemctl", "start", f"{host.family}-refresh.timer"]]
    assert host.unit_state("refresh.timer")["active"] is True
    assert "are stopped" not in result.stderr


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
    assert "still being made" in result.stderr and "No changes made" in result.stderr
    assert host.unit_state("scheduler.service")["active"] is True
    assert not any(call[:2] == ["systemctl", "stop"] for call in host.calls())
    assert not uv_calls(host)
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


def run_macos(host: Host, **extra: str) -> subprocess.CompletedProcess[str]:
    return host.run("install-controller.sh", shell=MACOS_BASH, **extra)


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
    result = run_macos(host, FAKE_AGENTS="serve refresh catalog-publish")
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
    with running_generation(host, 60):
        result = run_macos(host)
    assert result.returncode == 3
    assert "still being made" in result.stderr and "No changes made" in result.stderr
    assert not bootouts(host)
    assert not uv_calls(host)
    assert host.installed_version() == "old"
    assert (host.app / "src/runtime-tag").read_text() == "old\n"
    assert host.plist_generation("serve") == "old"


def test_macos_upgrade_waits_for_a_plate_to_finish(tmp_path: Path) -> None:
    host = macos_host(tmp_path, "serve", "refresh")
    started = time.monotonic()
    with running_generation(host, 1.5):
        result = run_macos(host)
    assert result.returncode == 0, result.stderr
    assert "Waiting for the plate being made to finish" in result.stderr
    assert time.monotonic() - started >= 1.5
    assert [call[3] for call in bootouts(host)] == ["lock-held", "lock-held"]


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
