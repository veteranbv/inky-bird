"""Select one native installation identity before any installer mutation.

This file also runs directly with the host's Python 3.9+ before the managed
environment exists. Keep it standard-library-only and independent of the app.
"""

from __future__ import annotations

import argparse
import os
import platform
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

CANONICAL_NAME = "inky-bird"
LEGACY_NAME = "inky-bird-frame"
NATIVE_JOBS = (
    "serve",
    "controller",
    "scheduler",
    "refresh",
    "generate",
    "catalog-publish",
    "notifications",
    "controller-cycle",
    "display",
)


@dataclass(frozen=True)
class NativeLayout:
    family: str
    app_dir: Path
    support_dir: Path
    config_path: Path


def service_family(value: str) -> str:
    if value not in (CANONICAL_NAME, LEGACY_NAME):
        raise ValueError("Unrecognized native service family")
    return value


def _loaded(family: str, system: str) -> bool:
    if system == "Darwin":
        commands = [
            ["launchctl", "print", f"{domain}/com.{family}.{job}"]
            for domain in (f"gui/{os.getuid()}", "system")
            for job in NATIVE_JOBS
        ]
    else:
        commands = [
            ["systemctl", "show", "--property=LoadState", "--value", f"{family}-{job}.{suffix}"]
            for job in NATIVE_JOBS
            for suffix in ("service", "timer")
        ]
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        except FileNotFoundError:
            return False
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Cannot determine the installed native service identity") from exc
        if result.returncode == 0 and (
            system == "Darwin" or result.stdout.strip() not in ("", "not-found")
        ):
            return True
    return False


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.expanduser().resolve())
    except ValueError:
        return False
    return True


def _display(path: Path, home: Path) -> str:
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def _ambiguity_message(found: dict[str, list[str]]) -> str:
    listed = "; ".join(
        f"{family}: {', '.join(evidence[:6])}" for family, evidence in sorted(found.items())
    )
    return (
        "Both inky-bird and inky-bird-frame installations exist. No changes made. "
        f"Found {listed}. Keep the installation you use. For the other one, remove its "
        "services (see Uninstall in docs/installation.md) and move its folders aside, for "
        "example by renaming them, once you no longer need their data. Then run this again."
    )


def resolve_native_layout(
    *,
    home: Path | None = None,
    system: str | None = None,
    system_root: Path = Path("/"),
    check_services: bool = True,
    app_dir: Path | None = None,
    support_dir: Path | None = None,
    config_path: Path | None = None,
) -> NativeLayout:
    home = Path.home() if home is None else home
    system = platform.system() if system is None else system
    if system not in ("Darwin", "Linux"):
        raise ValueError("Native setup supports macOS and Linux only")
    defaults: dict[str, tuple[Path, Path]] = {}
    found: dict[str, list[str]] = {}
    installed: set[str] = set()
    owner: str | None = None
    selected_config = None if config_path is None else config_path.expanduser().resolve()
    for family in (CANONICAL_NAME, LEGACY_NAME):
        support = (
            home
            / "Library/Application Support"
            / ("Inky Bird" if family == CANONICAL_NAME else "Inky Bird Frame")
            if system == "Darwin"
            else home / ".config" / family
        )
        app = home / "Services" / family
        defaults[family] = app, support
        if selected_config is not None and _within(selected_config, support):
            owner = family
        # A source checkout alone is not an installed runtime. Retain a support
        # root even if its config was lost: it may still contain private state.
        # A dangling link also counts; never silently adopt around it.
        data_markers = [support, app / ".venv"]
        service_markers: list[Path] = []
        if system == "Darwin":
            for directory in (home / "Library/LaunchAgents", system_root / "Library/LaunchDaemons"):
                service_markers.extend(
                    directory / f"com.{family}.{job}.plist" for job in NATIVE_JOBS
                )
        else:
            for unit_directory in (
                "etc/systemd/system",
                "usr/lib/systemd/system",
                "lib/systemd/system",
            ):
                for job in NATIVE_JOBS:
                    service_markers.extend(
                        system_root / unit_directory / f"{family}-{job}.{suffix}"
                        for suffix in ("service", "timer")
                    )
        evidence = [
            _display(path, home)
            for path in (*data_markers, *service_markers)
            if path.exists() or path.is_symlink()
        ]
        has_services = any(path.exists() or path.is_symlink() for path in service_markers)
        if not has_services and check_services and _loaded(family, system):
            has_services = True
            evidence.append("loaded services")
        if evidence:
            found[family] = evidence
        if has_services:
            installed.add(family)
    present = set(found)
    if len(present) > 1:
        # Leftover data from an uninstalled family does not compete with the
        # family whose services exist, or with the family the chosen config is in.
        chosen = next(iter(installed)) if len(installed) == 1 else owner
        if len(installed) > 1 or chosen is None or (owner is not None and owner != chosen):
            raise ValueError(_ambiguity_message(found))
        present = {chosen}
    family = next(iter(present), CANONICAL_NAME)
    default_app, default_support = defaults[family]
    selected_support = support_dir or default_support
    return NativeLayout(
        family,
        app_dir or default_app,
        selected_support,
        config_path or selected_support / "config.toml",
    )


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    def optional_path(name: str) -> Path | None:
        value = os.environ.get(name)
        return Path(value).expanduser() if value else None

    try:
        layout = resolve_native_layout(
            app_dir=optional_path("INKY_BIRD_APP_DIR"),
            support_dir=optional_path("INKY_BIRD_SUPPORT_DIR"),
            config_path=optional_path("INKY_BIRD_CONFIG_PATH"),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    # Only fixed keys and shell-quoted paths cross the shell boundary.
    for name, value in (
        ("service_prefix", layout.family),
        ("app_dir", layout.app_dir),
        ("support_dir", layout.support_dir),
        ("config_path", layout.config_path),
    ):
        print(f"{name}={shlex.quote(str(value))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
