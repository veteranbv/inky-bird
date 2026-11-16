from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.mark.parametrize("label", ["serve", "scheduler", "generate", "controller-cycle"])
@pytest.mark.parametrize("family", ["inky-bird", "inky-bird-frame"])
def test_macos_user_installer_refuses_system_jobs_before_changing_files(
    tmp_path: Path, label: str, family: str
) -> None:
    scripts = tmp_path / "bin"
    scripts.mkdir()
    for name, content in {
        "uname": "#!/bin/sh\nprintf '%s\\n' Darwin\n",
        "launchctl": f'#!/bin/sh\n[ "$2" = system/com.{family}.{label} ]\n',
    }.items():
        executable = scripts / name
        executable.write_text(content)
        executable.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    installer = Path(__file__).resolve().parents[1] / "deploy/install-controller.sh"
    result = subprocess.run(
        ["/bin/bash", str(installer)],
        env={"HOME": str(home), "PATH": f"{scripts}{os.pathsep}{os.defpath}"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "operator-managed system services" in result.stderr
    assert list(home.iterdir()) == []


def test_system_job_guard_precedes_every_user_installer_mutation() -> None:
    installer = (Path(__file__).resolve().parents[1] / "deploy/install-controller.sh").read_text()
    guard = installer.index(
        "for label in serve refresh generate catalog-publish "
        "notifications scheduler controller-cycle;"
    )
    assert guard < installer.index('chmod 600 "${config_path}"')
    assert guard < installer.index('mkdir -p "${app_dir}/catalog"')
    assert guard < installer.index('"${uv_bin}" sync')
    assert '[ -e "${system_plist}" ] || [ -L "${system_plist}" ]' in installer


def test_serving_container_receives_provider_credentials_but_sso_stays_separate() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yaml").read_text()
    serving = compose.split("  controller:\n", 1)[1].split("  scheduler:\n", 1)[0]
    scheduler = compose.split("  scheduler:\n", 1)[1].split("\nvolumes:", 1)[0]
    assert "${INKY_BIRD_ENV:-./controller.env}" in serving
    assert "${INKY_ACCESS_ENV:-./access.env}" in serving
    assert "${INKY_BIRD_ENV:-./controller.env}" in scheduler
    assert "INKY_ACCESS_ENV" not in scheduler
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text()
    assert '.services.controller.environment.SECRET_BOUNDARY_SENTINEL == "present"' in workflow
    assert '.services.controller.environment.ACCESS_BOUNDARY_SENTINEL == "present"' in workflow
    for role in ("scheduler", "bootstrap"):
        assert f".services.{role}.environment.ACCESS_BOUNDARY_SENTINEL == null" in workflow
    assert '.services["config-sync"].environment.ACCESS_BOUNDARY_SENTINEL == null' in workflow


@pytest.mark.parametrize("ready", [True, False])
def test_docker_health_probe_uses_configured_transport_and_validates_readiness(ready: bool) -> None:
    root = Path(__file__).resolve().parents[1]
    health = (root / "compose.yaml").read_text().split("    healthcheck:\n", 1)[1]
    health = health.split("      interval:", 1)[0]
    assert "- CMD\n" in health and "- python\n" in health and "- -c\n" in health
    script = " ".join(line.strip() for line in health.split("- >-\n", 1)[1].splitlines())
    compile(script, "compose-health", "exec")
    with (
        patch("inky_bird_frame.config.load_config") as load,
        patch(
            "inky_bird_frame.installation.health_url",
            return_value="https://controller.test:8793/health",
        ) as url,
        patch("inky_bird_frame.http.get_json", return_value={"ok": ready}) as request,
    ):
        if ready:
            exec(script, {})
        else:
            with pytest.raises(AssertionError):
                exec(script, {})
        load.assert_called_once_with(Path("/data/config.toml"), load_secrets=False)
        url.assert_called_once()
        request.assert_called_once_with("https://controller.test:8793/health", timeout_seconds=5)


def test_docker_config_sync_keeps_the_host_file_authoritative_and_private() -> None:
    compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text()

    config_sync = compose.split("  config-sync:", maxsplit=1)[1].split("  bootstrap:", maxsplit=1)[
        0
    ]
    bootstrap = compose.split("  bootstrap:", maxsplit=1)[1].split("  controller:", maxsplit=1)[0]
    assert "${INKY_BIRD_CONFIG:-./config.toml}" in config_sync
    assert "target: /run/inky/config.toml" in config_sync
    assert "read_only: true" in config_sync
    assert "create_host_path: false" in config_sync
    assert "selinux: Z" in config_sync
    assert "network_mode: none" in config_sync
    assert 'user: "0:0"' in config_sync
    assert "DAC_OVERRIDE" in config_sync
    assert "CHOWN" in config_sync
    assert "no-new-privileges:true" in config_sync
    assert "INKY_BIRD_ENV" in config_sync
    assert "required: false" in config_sync
    assert "config-sync cannot read the host config" in config_sync
    assert "config install --destination /data/config.toml" in config_sync
    assert "< /run/inky/config.toml" in config_sync
    assert "chown 10001:10001 /data/config.toml" in config_sync
    assert "config-sync:" in bootstrap
    assert "condition: service_completed_successfully" in bootstrap


def _controller_installers() -> list[str]:
    root = Path(__file__).resolve().parents[1] / "deploy"
    return [
        (root / "install-controller.sh").read_text(),
        (root / "install-controller-systemd.sh").read_text(),
    ]


def test_native_installers_finish_without_exposing_or_blocking_owner_registration() -> None:
    for script in _controller_installers():
        setup = script.index('access setup --if-needed --config "${config_path}"')
        assert script.index("installation_complete=true") < setup
        assert "owner setup is incomplete" in script[setup:]
        assert "access prepare" not in script
    root = Path(__file__).resolve().parents[1]
    assert "access prepare" not in (root / "compose.yaml").read_text()
    assert "/data/var/controller" not in (root / "Dockerfile").read_text()
    workflow = (root / ".github/workflows/ci.yml").read_text()
    assert "access prepare --new-installation --config /data/config.toml" in workflow
    assert "set(p.name for p in path.parent.iterdir()) == {'config.toml'}" in workflow


def test_docker_bootstrap_applies_only_reviewed_catalog_migrations() -> None:
    compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text()

    bootstrap = compose.split("  controller:", maxsplit=1)[0]
    assert 'INKY_CATALOG_SYNC_APPLY_REVIEWED_MIGRATIONS: "1"' in bootstrap
    assert 'INKY_CATALOG_SYNC_RETAIN_INDEPENDENT_APPROVALS: "1"' in bootstrap
    assert "--apply-reviewed-migrations" not in bootstrap
    assert "--retain-independent-approvals" not in bootstrap


def test_controller_installers_use_provider_lists_for_managed_credentials() -> None:
    for script in _controller_installers():
        assert 'environment_credentials.append("geoapify_api_key_env")' in script
        assert "DiscoveryProvider.EBIRD in config.discovery.sources" in script
        assert "DiscoveryProvider.BIRDWEATHER in config.discovery.sources" in script
        assert "config.discovery.source." not in script


def test_controller_installers_sync_catalogs_with_reviewed_replacements() -> None:
    # Failure and restore order is exercised in tests/test_installer_behaviour.py.
    for script in _controller_installers():
        # The new runtime is built beside the live one, never over it.
        assert 'stage_runtime_source "${new_runtime}"' in script
        assert 'rsync -a "${root}/catalog/" "${app_dir}/catalog/"' not in script
        assert "retain_independent_approvals=True" in script
        assert "allow_replacements=True" in script
        assert "managed_catalog.resolve() != config.controller.catalog_dir.resolve()" in script
        assert "cannot use {names}" in script
        assert 'environment_credentials.append("ebird_api_key_env")' in script
        assert 'environment_credentials.append("birdweather_token_env")' in script
        assert "config.controller.workspace_dir.mkdir(parents=True, exist_ok=True)" in script
        assert "config.controller.catalog_dir.parent.mkdir(parents=True, exist_ok=True)" in script
        assert "rebuild_catalog_index" not in script
    macos = (Path(__file__).resolve().parents[1] / "deploy" / "install-controller.sh").read_text()
    assert "/usr/bin/plutil -replace RunAtLoad -bool false" in macos


def test_macos_controller_installer_limits_log_retention_and_permissions() -> None:
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install-controller.sh").read_text()
    assert 'chmod 700 "${support_dir}" "${log_dir}"' in script
    assert "find \"${log_dir}\" -maxdepth 1 -type f -name '*.log*' -exec chmod 600 {} +" in script
    assert '"Umask": 0o077' in script
    for name in ("serve", "refresh", "generate", "catalog-publish", "notifications"):
        assert f'**managed_agent("{name}")' in script
    assert '"INKY_BIRD_MANAGED_LOG_PATH": str(log_dir / f"{name}.log")' in script
    assert "from inky_bird_frame.installation import controller_executable_path" in script
    assert '"PATH": controller_executable_path(' in script
    assert "generation_enabled=config.controller.generation_enabled" in script


def test_systemd_controller_installer_restarts_boot_persistent_services() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "install-controller-systemd.sh"
    ).read_text()

    assert "controller_systemd_units" in script
    assert 'systemctl enable "${service_prefix}-controller.service"' in script
    assert 'systemctl restart "${service_prefix}-controller.service"' in script
    assert 'systemctl restart "${service_prefix}-refresh.timer"' in script
    assert 'systemctl restart "${service_prefix}-generate.timer"' in script
    initial_refresh = script.index(
        '"${new_runtime}/.venv/bin/inky-bird" refresh --config "${config_path}"'
    )
    quiesce = script.index('sudo systemctl stop "${timer}"')
    runtime_build = script.index('stage_runtime_source "${new_runtime}"')
    validation = script.index('"${new_runtime}/.venv/bin/inky-bird" catalog-publish')
    activation = script.index("\nactivate_runtime\n")
    unit_install = script.index('sudo install -m 0644 "${unit}"')
    assert runtime_build < quiesce < initial_refresh < validation < activation < unit_install
    assert "deadline=$((SECONDS + quiesce_timeout_seconds))" in script
    assert 'systemctl show --property=ActiveState --value "${service}"' in script
    assert "active | activating | deactivating | reloading" in script
    assert 'if [ "${installation_complete}" != true ]; then' in script
    # Restart reporting on failure is exercised in tests/test_installer_behaviour.py.
    assert 'systemctl is-enabled --quiet "${service_prefix}-catalog-publish.timer"' in script
    assert 'systemctl is-active --quiet "${service_prefix}-notifications.timer"' in script
    assert 'rsync -a "${root}/catalog/" "${app_dir}/catalog/"' not in script
    assert "retain_independent_approvals=True" in script
    assert "allow_replacements=True" in script
    assert "managed_catalog.resolve() != config.controller.catalog_dir.resolve()" in script
    assert "cannot use {names}" in script
    assert 'environment_credentials.append("ebird_api_key_env")' in script
    assert 'environment_credentials.append("birdweather_token_env")' in script
    assert "config.controller.workspace_dir.mkdir(parents=True, exist_ok=True)" in script
    assert "config.controller.catalog_dir.parent.mkdir(parents=True, exist_ok=True)" in script
    assert "rebuild_catalog_index" not in script


def test_repository_has_no_actions_deployment_path() -> None:
    # Production deploys run from the controller itself; no workflow reaches it.
    root = Path(__file__).resolve().parents[1]
    assert not (root / ".github" / "workflows" / "deploy.yml").exists()
    assert not (root / "deploy" / "install-controller-via-launchd.sh").exists()
    actionlint = (root / ".github" / "actionlint.yaml").read_text()
    assert "self-hosted-runner" not in actionlint


def test_local_display_installer_uses_configured_schedule_and_verifies_timer() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "install-display-local.sh"
    ).read_text()

    assert "display_systemd_units" in script
    assert "run_initial_display=${INKY_BIRD_RUN_INITIAL_DISPLAY:-false}" in script
    assert 'systemctl enable "${service_prefix}-display.timer"' in script
    assert 'systemctl restart "${service_prefix}-display.timer"' in script
    assert 'systemctl is-enabled --quiet "${service_prefix}-display.timer"' in script
    assert 'systemctl is-active --quiet "${service_prefix}-display.timer"' in script
    assert "noninteractive_sudo=${INKY_BIRD_NONINTERACTIVE_SUDO:-false}" in script
    assert "sudo_command+=(-n)" in script
    assert '"${sudo_command[@]}" systemctl daemon-reload' in script


def test_remote_display_installer_selects_noninteractive_sudo() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "install-display-node.sh"
    ).read_text()

    assert "INKY_BIRD_NONINTERACTIVE_SUDO=true" in script


def test_macos_controller_uninstaller_removes_agents_and_preserves_data() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "uninstall-controller.sh"
    ).read_text()

    assert "set -euo pipefail" in script
    assert (
        "for label in serve refresh generate catalog-publish notifications "
        "scheduler controller-cycle; do" in script
    )
    assert 'launchctl bootout "gui/${uid}/${agent}"' in script
    assert 'rm -f "${plist}"' in script
    assert "Intentionally left in place:" in script
    assert "native_names.py" in script
    assert "rm -rf" not in script
    assert 'rm -f "${config_path}"' not in script


def test_systemd_controller_uninstaller_removes_units_and_preserves_data() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "uninstall-controller-systemd.sh"
    ).read_text()

    assert "set -euo pipefail" in script
    assert "for name in refresh generate catalog-publish notifications; do" in script
    assert 'disable_unit "${service_prefix}-controller.service"' in script
    assert 'disable_unit "${service_prefix}-scheduler.service"' in script
    assert (
        "for name in controller scheduler refresh generate catalog-publish notifications; do"
        in script
    )
    assert 'sudo rm -f "/etc/systemd/system/${unit}"' in script
    assert "sudo systemctl daemon-reload" in script
    assert "Intentionally left in place:" in script
    assert "rm -rf" not in script
    assert 'rm -f "${config_path}"' not in script


def test_local_display_uninstaller_removes_units_and_preserves_data() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "uninstall-display-local.sh"
    ).read_text()

    assert "set -euo pipefail" in script
    assert "noninteractive_sudo=${INKY_BIRD_NONINTERACTIVE_SUDO:-false}" in script
    assert "sudo_command+=(-n)" in script
    assert (
        '"${sudo_command[@]}" systemctl disable --now "${service_prefix}-display.timer"' in script
    )
    assert '"${sudo_command[@]}" systemctl stop "${service_prefix}-display.service"' in script
    assert '"${sudo_command[@]}" rm -f "/etc/systemd/system/${unit}"' in script
    assert '"${sudo_command[@]}" systemctl daemon-reload' in script
    assert "Intentionally left in place:" in script
    assert "Pimoroni Python environment" in script
    assert "rm -rf" not in script
    assert 'rm -f "${config_path}"' not in script


# The display installer's distribution programs, run for real in a real venv.

DISPLAY_INSTALLER = Path(__file__).resolve().parents[1] / "deploy/install-display-local.sh"


def display_program(name: str) -> str:
    """A Python program the display installer keeps in a shell variable, as it runs it."""
    found = re.search(
        rf"^{name}='(.*?)^'$", DISPLAY_INSTALLER.read_text(), re.MULTILINE | re.DOTALL
    )
    assert found is not None, name
    return found.group(1)


@dataclass(frozen=True)
class Venv:
    root: Path
    python: Path
    site: Path
    bin: Path

    def run(
        self, program: str, *arguments: str, flags: tuple[str, ...] = ()
    ) -> subprocess.CompletedProcess[str]:
        """Run as the installer does: ``python -I -c <program> <arguments>``."""
        return subprocess.run(
            [str(self.python), "-I", *flags, "-c", program, *arguments],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": os.defpath},
        )

    def imported(self) -> tuple[str | None, str]:
        """The inky-bird-frame distribution's version, if any, and the version
        of the package Python imports."""
        probe = (
            "import importlib.metadata as m, inky_bird_frame as p\n"
            "try:\n    print(m.version('inky-bird-frame'))\n"
            "except m.PackageNotFoundError:\n    print('none')\n"
            "print(p.VERSION)\n"
        )
        # Without bytecode: a cache left in a package directory would keep it.
        result = self.run(probe, flags=("-B",))
        assert result.returncode == 0, result.stderr
        distribution, package = result.stdout.split()
        return (None if distribution == "none" else distribution), package


def make_venv(root: Path) -> Venv:
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], check=True)
    python = root / "bin/python"
    site = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Venv(root, python, Path(site), root / "bin")


def record_line(site: Path, path: Path) -> str:
    data = path.read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{os.path.relpath(path, site)},sha256={digest},{len(data)}"


def write_distribution(
    venv: Venv, name: str, version: str, files: dict[str, str], scripts: tuple[str, ...]
) -> Path:
    """A distribution as pip leaves it: its files, console scripts and a RECORD of them."""
    written = []
    for relative, content in files.items():
        path = venv.site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written.append(path)
    for script in scripts:
        path = venv.bin / script
        path.write_text(f"#!{venv.python}\n# {name} {version}\nimport sys\n")
        path.chmod(0o755)
        written.append(path)
    info = venv.site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    (info / "INSTALLER").write_text("pip\n")
    entry_points = "".join(f"{script} = inky_bird_frame.entrypoint:main\n" for script in scripts)
    (info / "entry_points.txt").write_text(f"[console_scripts]\n{entry_points}")
    written += [info / "METADATA", info / "INSTALLER", info / "entry_points.txt"]
    lines = [record_line(venv.site, path) for path in written]
    (info / "RECORD").write_text("\n".join([*lines, f"{info.name}/RECORD,,"]) + "\n")
    return info


@dataclass(frozen=True)
class Upgrade:
    """A Pimoroni environment mid-upgrade: the renamed release installed
    regularly, the new one editable from a runtime outside it."""

    venv: Venv
    old: Path
    new: Path
    backup: Path


def make_upgrade(tmp_path: Path) -> Upgrade:
    venv = make_venv(tmp_path / "pimoroni")
    old = write_distribution(
        venv,
        "inky-bird-frame",
        "0.9.8",
        {
            "inky_bird_frame/__init__.py": "VERSION = '0.9.8'\n",
            "inky_bird_frame/cli.py": "def main():\n    pass\n",
        },
        ("inky-bird-frame",),
    )
    source = tmp_path / "runtime/src"
    (source / "inky_bird_frame").mkdir(parents=True)
    (source / "inky_bird_frame/__init__.py").write_text("VERSION = '1.0.0'\n")
    new = write_distribution(
        venv,
        "inky-bird",
        "1.0.0",
        {"_inky_bird.pth": f"{source}\n"},
        ("inky-bird", "inky-bird-frame"),
    )
    # The installer's own temporary directory, which holds the backup.
    (tmp_path / "work").mkdir()
    return Upgrade(venv, old, new, tmp_path / "work/previous-package")


def tree(root: Path) -> dict[str, str]:
    """Every entry below ``root``: kind, mode and content or link target."""
    found = {}
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        name = str(path.relative_to(root))
        if path.is_symlink():
            found[name] = f"link {os.readlink(path)}"
        elif path.is_dir():
            found[name] = "directory"
        else:
            content = hashlib.sha256(path.read_bytes()).hexdigest()
            found[name] = f"file {stat.S_IMODE(path.stat().st_mode):o} {content}"
    return found


def old_files(upgrade: Upgrade) -> set[str]:
    """What the old distribution's RECORD names that the new one does not, from the venv root."""

    def named(info: Path) -> set[str]:
        return {
            os.path.relpath(os.path.normpath(info.parent / line.split(",")[0]), upgrade.venv.root)
            for line in (info / "RECORD").read_text().splitlines()
        }

    return named(upgrade.old) - named(upgrade.new)


def moves(backup: Path) -> list[tuple[str, str]]:
    """Each file a removal set aside, as the plan lists it: where it was, where it is kept."""
    plan = json.loads((backup / "plan.json").read_text())
    return [(entry["original"], entry["saved"]) for entry in plan]


def identity(path: Path) -> str:
    """What the programs record a file as: a link's target or a file's hash."""
    if path.is_symlink():
        return f"link:{os.readlink(path)}"
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def write_plan(backup: Path, entries: list[tuple[Path, Path, str]]) -> None:
    plan = [
        {"original": str(original), "saved": str(saved), "identity": recorded}
        for original, saved, recorded in entries
    ]
    (backup / "plan.json").write_text(json.dumps(plan))


REMOVE = "remove_distribution_program"
RESTORE = "restore_distribution_program"


def test_display_removal_sets_the_old_distribution_aside_and_restore_puts_it_back(
    tmp_path: Path,
) -> None:
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    assert venv.imported() == ("0.9.8", "0.9.8")
    before = tree(venv.root)
    planned = old_files(upgrade)
    assert "bin/inky-bird-frame" not in planned

    result = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))
    assert result.returncode == 0, result.stderr

    plan = moves(upgrade.backup)
    moved = {os.path.relpath(os.path.normpath(original), venv.root) for original, _ in plan}
    assert moved == planned
    for original, saved in plan:
        assert not Path(original).exists() and Path(saved).is_file()
        assert Path(saved).parent == upgrade.backup
    after = tree(venv.root)
    emptied = {str(Path(name).parent) for name in planned}
    assert {name: value for name, value in before.items() if name not in planned | emptied} == after
    assert venv.imported() == (None, "1.0.0")

    result = venv.run(display_program(RESTORE), str(upgrade.backup))
    assert result.returncode == 0, result.stderr
    assert tree(venv.root) == before
    assert venv.imported() == ("0.9.8", "0.9.8")


def test_display_removal_without_a_backup_takes_out_the_new_distribution(
    tmp_path: Path,
) -> None:
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    before = tree(venv.root)
    gone = {
        f"lib/{venv.site.parent.name}/site-packages/_inky_bird.pth",
        "bin/inky-bird",
        *(str(path.relative_to(venv.root)) for path in [upgrade.new, *upgrade.new.iterdir()]),
    }

    result = venv.run(display_program(REMOVE), "inky-bird", "inky-bird-frame")
    assert result.returncode == 0, result.stderr

    assert {name: value for name, value in before.items() if name not in gone} == tree(venv.root)
    assert venv.imported() == ("0.9.8", "0.9.8")


def test_display_removal_skips_a_file_already_gone(tmp_path: Path) -> None:
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    (venv.site / "inky_bird_frame/cli.py").unlink()
    before = tree(venv.root)

    result = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))
    assert result.returncode == 0, result.stderr
    assert not any(original.endswith("cli.py") for original, _ in moves(upgrade.backup))

    assert venv.run(display_program(RESTORE), str(upgrade.backup)).returncode == 0
    assert tree(venv.root) == before


def test_display_removal_falls_back_to_copying_across_filesystems(tmp_path: Path) -> None:
    """A rename that fails as it does between filesystems makes every move a copy
    and a delete; the result must be the same. (No second filesystem is
    available to a test; this makes the environment's own rename fail.)"""
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    (venv.site / "sitecustomize.py").write_text(
        "import errno, os\n"
        "def rename(*arguments, **options):\n"
        "    raise OSError(errno.EXDEV, 'Invalid cross-device link')\n"
        "os.rename = rename\n"
    )
    before = tree(venv.root)

    result = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))
    assert result.returncode == 0, result.stderr
    assert venv.imported() == (None, "1.0.0")
    assert venv.run(display_program(RESTORE), str(upgrade.backup)).returncode == 0
    assert tree(venv.root) == before
    assert venv.imported() == ("0.9.8", "0.9.8")


@pytest.mark.parametrize("entry", ["escaping", "absolute", "linked-directory"])
def test_display_removal_moves_nothing_outside_the_environment(tmp_path: Path, entry: str) -> None:
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the environment's\n")
    if entry == "escaping":
        name = os.path.relpath(outside / "keep.txt", venv.site)
    elif entry == "absolute":
        name = str(outside / "keep.txt")
    else:
        (venv.site / "inky_bird_frame/linked").symlink_to(outside)
        name = "inky_bird_frame/linked/keep.txt"
    record = upgrade.old / "RECORD"
    record.write_text(record.read_text() + f"{name},,\n")
    before = tree(venv.root)

    result = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))

    assert (outside / "keep.txt").read_text() == "not the environment's\n"
    assert result.returncode != 0
    assert tree(venv.root) == before
    assert not (upgrade.backup / "plan.json").exists()


def test_display_removal_moves_a_link_not_what_it_points_to(tmp_path: Path) -> None:
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    outside = tmp_path / "outside.txt"
    outside.write_text("not the environment's\n")
    link = venv.site / "inky_bird_frame/data.txt"
    link.symlink_to(outside)
    record = upgrade.old / "RECORD"
    record.write_text(record.read_text() + "inky_bird_frame/data.txt,,\n")
    before = tree(venv.root)

    result = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))
    assert result.returncode == 0, result.stderr
    assert not link.is_symlink() and outside.read_text() == "not the environment's\n"
    assert venv.run(display_program(RESTORE), str(upgrade.backup)).returncode == 0
    assert tree(venv.root) == before


def test_display_removal_refuses_a_backup_that_already_holds_a_plan(tmp_path: Path) -> None:
    """A second removal into the same backup would replace the plan and the
    files the first one set aside; it changes nothing instead."""
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    first = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))
    assert first.returncode == 0, first.stderr
    plan = moves(upgrade.backup)
    # A partial run: the metadata is back, the package files are still aside.
    for original, saved in plan:
        if ".dist-info" in original:
            Path(original).parent.mkdir(exist_ok=True)
            shutil.move(saved, original)
    saved_before = tree(upgrade.backup)
    before = tree(venv.root)

    result = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))

    assert result.returncode != 0
    assert tree(upgrade.backup) == saved_before
    assert tree(venv.root) == before


def test_display_restore_without_a_plan_changes_nothing(tmp_path: Path) -> None:
    upgrade = make_upgrade(tmp_path)
    upgrade.backup.mkdir()
    before = tree(upgrade.venv.root)
    result = upgrade.venv.run(display_program(RESTORE), str(upgrade.backup))
    assert result.returncode == 0, result.stderr
    assert tree(upgrade.venv.root) == before


@pytest.mark.parametrize("field", ["original", "saved"])
def test_display_restore_refuses_a_plan_outside_its_places(tmp_path: Path, field: str) -> None:
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    upgrade.backup.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    saved = upgrade.backup / "0"
    saved.write_text("set aside\n")
    original = venv.site / "inky_bird_frame/extra.py"
    if field == "original":
        original = elsewhere / "planted.py"
    else:
        saved = elsewhere / "0"
        saved.write_text("not set aside by the installer\n")
    write_plan(upgrade.backup, [(original, saved, "sha256:" + hashlib.sha256(b"x").hexdigest())])
    before = tree(venv.root)

    result = venv.run(display_program(RESTORE), str(upgrade.backup))

    assert result.returncode != 0
    assert tree(venv.root) == before
    assert not (elsewhere / "planted.py").exists()
    assert saved.exists()


def test_display_restore_leaves_a_file_that_was_never_moved(tmp_path: Path) -> None:
    """A move that stopped while copying leaves the original and part of a copy;
    the original is the file to keep."""
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    upgrade.backup.mkdir()
    original = venv.site / "inky_bird_frame/cli.py"
    partial = upgrade.backup / "0"
    partial.write_text("def ma")
    write_plan(upgrade.backup, [(original, partial, identity(original))])
    before = tree(venv.root)

    result = venv.run(display_program(RESTORE), str(upgrade.backup))

    assert result.returncode == 0, result.stderr
    assert tree(venv.root) == before


def test_display_restore_runs_as_the_kept_file_the_installer_names(tmp_path: Path) -> None:
    """The command an operator is given after a failed restore: the program,
    saved beside the files as the installer saves it, run as a file."""
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    before = tree(venv.root)
    removal = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))
    assert removal.returncode == 0, removal.stderr
    (upgrade.backup / "restore.py").write_text(display_program(RESTORE))

    result = subprocess.run(
        [str(venv.python), "-I", str(upgrade.backup / "restore.py"), str(upgrade.backup)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.defpath},
    )

    assert result.returncode == 0, result.stderr
    assert tree(venv.root) == before
    assert venv.imported() == ("0.9.8", "0.9.8")


def test_display_restore_stopped_part_way_finishes_when_run_again(tmp_path: Path) -> None:
    """A restore that fails after putting some files back keeps the rest and its
    plan; once the cause is gone, running it again puts back the remainder."""
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    before = tree(venv.root)
    removal = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))
    assert removal.returncode == 0, removal.stderr
    # Something now stands where the metadata goes back, the last files listed.
    blocker = upgrade.old
    blocker.write_text("in the way\n")

    first = venv.run(display_program(RESTORE), str(upgrade.backup))
    assert first.returncode != 0
    assert (venv.site / "inky_bird_frame/__init__.py").is_file()
    assert (upgrade.backup / "plan.json").exists()
    assert any(path.name.isdigit() for path in upgrade.backup.iterdir())

    blocker.unlink()
    second = venv.run(display_program(RESTORE), str(upgrade.backup))
    assert second.returncode == 0, second.stderr
    assert tree(venv.root) == before
    assert venv.imported() == ("0.9.8", "0.9.8")


# Every rename fails as it does across filesystems, so each move is a copy; a
# copy can be made to stop half-way, as a full disk or a lost mount stops it.
CROSS_DEVICE = """import errno, os, shutil
from pathlib import Path
RULE = Path(__file__).with_name("break-copy")
def rename(*arguments, **options):
    raise OSError(errno.EXDEV, "Invalid cross-device link")
os.rename = rename
copy2 = shutil.copy2
def broken(source, destination, **options):
    rule = RULE.read_text().strip() if RULE.exists() else ""
    names = {os.path.basename(source), os.path.basename(destination)}
    if rule and rule in names:
        data = Path(source).read_bytes()
        Path(destination).write_bytes(data[: len(data) // 2])
        raise OSError(errno.ENOSPC, "No space left on device")
    return copy2(source, destination, **options)
shutil.copy2 = broken
"""


def cross_device(upgrade: Upgrade) -> Path:
    """Make the environment's renames fail across devices; return the file that
    names which copy stops half-way."""
    (upgrade.venv.site / "sitecustomize.py").write_text(CROSS_DEVICE)
    return upgrade.venv.site / "break-copy"


def test_display_set_aside_keeps_a_file_whose_copy_failed(tmp_path: Path) -> None:
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    rule = cross_device(upgrade)
    rule.write_text("cli.py\n")
    original = (venv.site / "inky_bird_frame/cli.py").read_bytes()
    before = tree(venv.root)

    removal = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))

    assert removal.returncode != 0
    assert (venv.site / "inky_bird_frame/cli.py").read_bytes() == original
    assert not [path for path in upgrade.backup.iterdir() if path.name.endswith(".partial")]
    rule.unlink()
    restore = venv.run(display_program(RESTORE), str(upgrade.backup))
    assert restore.returncode == 0, restore.stderr
    assert {name: value for name, value in tree(venv.root).items() if name != rule.name} == {
        name: value for name, value in before.items() if not name.endswith("break-copy")
    }
    assert venv.imported() == ("0.9.8", "0.9.8")


def test_display_restore_whose_copy_failed_fails_and_a_retry_repairs(tmp_path: Path) -> None:
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    rule = cross_device(upgrade)
    before = tree(venv.root)
    removal = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))
    assert removal.returncode == 0, removal.stderr
    rule.write_text(".cli.py.partial\n")

    failed = venv.run(display_program(RESTORE), str(upgrade.backup))

    assert failed.returncode != 0
    assert "could not be put back" in failed.stderr
    assert not (venv.site / "inky_bird_frame/cli.py").exists()
    assert not (venv.site / "inky_bird_frame/.cli.py.partial").exists()
    rule.unlink()
    retry = venv.run(display_program(RESTORE), str(upgrade.backup))
    assert retry.returncode == 0, retry.stderr
    assert tree(venv.root) == before
    assert venv.imported() == ("0.9.8", "0.9.8")


def test_display_restore_replaces_a_truncated_original(tmp_path: Path) -> None:
    """A copy back that stopped half-way, before restores were made whole, left
    a truncated file in place beside the complete saved one."""
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    before = tree(venv.root)
    removal = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))
    assert removal.returncode == 0, removal.stderr
    cli = venv.site / "inky_bird_frame/cli.py"
    cli.parent.mkdir(exist_ok=True)
    cli.write_text("def ma")

    result = venv.run(display_program(RESTORE), str(upgrade.backup))

    assert result.returncode == 0, result.stderr
    assert tree(venv.root) == before
    assert venv.imported() == ("0.9.8", "0.9.8")


def test_display_restore_fails_loudly_for_a_damaged_saved_copy(tmp_path: Path) -> None:
    upgrade = make_upgrade(tmp_path)
    venv = upgrade.venv
    removal = venv.run(display_program(REMOVE), "inky-bird-frame", "inky-bird", str(upgrade.backup))
    assert removal.returncode == 0, removal.stderr
    saved = next(
        Path(saved) for original, saved in moves(upgrade.backup) if original.endswith("cli.py")
    )
    saved.write_text("damaged\n")

    result = venv.run(display_program(RESTORE), str(upgrade.backup))

    assert result.returncode != 0
    assert "could not be put back" in result.stderr
    assert not (venv.site / "inky_bird_frame/cli.py").exists()
