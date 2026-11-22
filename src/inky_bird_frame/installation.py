"""Role setup orchestration and read-only installation diagnostics."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shutil
import ssl
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from xml.parsers.expat import ExpatError

from .birdbuddy import birdbuddy_status
from .config import AppConfig, DiscoveryProvider, load_config, parse_config_bytes
from .errors import ConfigurationError, InkyBirdFrameError, InstallationError
from .http import get_json, write_bytes_atomic
from .images import SUPPORTED_HARDWARE_SIZES
from .native_names import LEGACY_NAME, resolve_native_layout, service_family
from .notifications import notification_state_path, notification_status
from .system_service import valid_account_name

SUDO_AUTHORIZATION_TIMEOUT_SECONDS = 300
# CodexRunner's default per-call limit; a test keeps the two equal.
CODEX_CALL_TIMEOUT_SECONDS = 1200
# Discovery refresh, reference downloads and stopping the agent cleanly.
GENERATION_UNIT_MARGIN_SECONDS = 900
INSTALLER_TIMEOUT_SECONDS = 3600
# One native sign-in credential, bounded below normal OS environment limits.
MAX_NATIVE_ACCESS_BYTES = 64 * 1024
# One JSON-quoted path; the separate PEM bound leaves room for combined root sets.
MAX_NATIVE_TLS_BYTES = 16 * 1024
MAX_NATIVE_CA_BYTES = 4 * 1024 * 1024
# Names a private KEY=VALUE file that every command loads before reading
# configuration. The path itself is not secret, so service definitions may carry it.
PRIVATE_ENVIRONMENT_FILE_ENV = "INKY_BIRD_ENV_FILE"
MAX_PRIVATE_ENVIRONMENT_BYTES = 64 * 1024
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MANAGED_CONTROLLER_DEFAULT_EXECUTABLE_PATH = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)
# Sandboxing for units that never run Codex. The Codex-driven oneshot units
# (refresh, generate, catalog-publish, notifications) build their own
# bubblewrap user-namespace sandbox and must stay unconstrained. Services live
# in the user's home and the display node needs SPI/GPIO devices, so
# ProtectSystem/ProtectHome and device restrictions are intentionally omitted.
SYSTEMD_SERVICE_HARDENING = (
    "NoNewPrivileges=true\n"
    "PrivateTmp=true\n"
    "ProtectKernelTunables=true\n"
    "ProtectKernelModules=true\n"
    "ProtectControlGroups=true\n"
    "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n"
)
# The first line of every unit a system-service installation renders, so the
# user installer can tell those units from its own.
SYSTEM_SERVICE_MARKER = "# Inky Bird system service"
SYSTEMD_UNIT_DIRECTORY = Path("/etc/systemd/system")
LAUNCH_DAEMON_DIRECTORY = Path("/Library/LaunchDaemons")
MAX_CONFIG_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SystemService:
    """A root-installed controller that runs as its own account.

    Its units carry no secret: they name the private environment file, which
    the application loads itself. The web service, which starts no other
    program, may write only below ``writable_paths``.
    """

    group: str
    environment_file: Path
    writable_paths: tuple[Path, ...]


class InstallationRole(StrEnum):
    CONTROLLER = "controller"
    DISPLAY = "display"


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class DiagnosticCheck:
    check_id: str
    status: CheckStatus
    summary: str
    detail: str | None = None
    remediation: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.check_id,
            "status": self.status.value,
            "summary": self.summary,
        }
        if self.detail is not None:
            result["detail"] = self.detail
        if self.remediation is not None:
            result["remediation"] = self.remediation
        return result


@dataclass(frozen=True)
class DoctorReport:
    role: InstallationRole
    checks: tuple[DiagnosticCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.status is not CheckStatus.FAIL for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "ready": self.ready,
            "checks": [check.as_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _run(
    command: Sequence[str],
    *,
    timeout_seconds: int = 15,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            env=environment,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(1, "", str(exc))
    return CommandResult(completed.returncode, completed.stdout.strip(), completed.stderr.strip())


def _systemd_literal(value: str) -> str:
    if any(character in value for character in ("\0", "\n", "\r")):
        raise InstallationError("systemd values cannot contain NUL or line breaks")
    return value.replace("%", "%%")


def _systemd_quote(value: str) -> str:
    escaped = _systemd_literal(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _systemd_argument(value: str) -> str:
    # ExecStart expands $NAME and ${NAME} in arguments after the executable;
    # the executable token and other directives used here are not expanded.
    return _systemd_quote(value).replace("$", "$$")


def controller_executable_path(codex_path: Path, *, generation_enabled: bool = True) -> str:
    """Return a deterministic PATH for managed controller services."""
    if not codex_path.is_absolute():
        if not generation_enabled:
            return ":".join(MANAGED_CONTROLLER_DEFAULT_EXECUTABLE_PATH)
        raise InstallationError(
            "Codex CLI must resolve to an absolute path for managed controller services"
        )
    executable_paths = dict.fromkeys(
        (str(codex_path.parent), *MANAGED_CONTROLLER_DEFAULT_EXECUTABLE_PATH)
    )
    return ":".join(executable_paths)


def _native_oidc_secret(config: AppConfig, config_path: Path) -> tuple[str, str, Path] | None:
    """Return the OIDC secret native serving would retain, without writing anything."""
    if config.controller.oidc is None:
        return None
    name = config.controller.oidc.client_secret_env
    path = config_path.resolve().parent / "native-access.env"
    cached: object = None
    try:
        if path.exists() or path.is_symlink():
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                    raise InstallationError("Native access cache must be private and regular")
                body = handle.read(MAX_NATIVE_ACCESS_BYTES + 1)
            if len(body) > MAX_NATIVE_ACCESS_BYTES:
                raise InstallationError("Native access cache exceeds its supported size")
            key, separator, value = body.decode().partition("=")
            if separator and key == name:
                cached = json.loads(value)
        secret = os.environ.get(name, cached)
        if not isinstance(secret, str) or not secret or not secret.isprintable():
            raise InstallationError(
                "Supply the configured OIDC client secret when running native setup; "
                "use one nonempty printable value"
            )
    except (OSError, UnicodeError, ValueError):
        raise InstallationError("Native access cache is invalid or cannot be read") from None
    return name, secret, path


def native_oidc_environment(config: AppConfig, config_path: Path) -> dict[str, str]:
    """Persist only the configured OIDC secret for restart-safe native serving."""
    resolved = _native_oidc_secret(config, config_path)
    if resolved is None:
        return {}
    name, secret, path = resolved
    try:
        # JSON's printable UTF-8 string form is also a systemd EnvironmentFile
        # double-quoted value. No shell is used and no expansion is performed.
        content = f"{name}={json.dumps(secret, ensure_ascii=False)}\n".encode()
        if len(content) > MAX_NATIVE_ACCESS_BYTES:
            raise InstallationError("Native OIDC client secret exceeds its supported size")
        write_bytes_atomic(path, content, mode=0o600)
    except (OSError, UnicodeError, ValueError):
        raise InstallationError("Native access cache is invalid or cannot be written") from None
    return {name: secret}


class _OversizedFileError(ValueError):
    """A protected file is larger than its reader accepts."""


def _read_native_trust_file(path: Path, maximum: int, *, private: bool = False) -> bytes:
    # Trust files may be readable by others; a private file is owner-only.
    forbidden_mode = 0o077 if private else 0o022
    directory = path.parent.resolve(strict=True)
    for parent in (directory, *directory.parents):
        info = parent.stat()
        root_sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if info.st_uid not in (0, os.getuid()) or (
            stat.S_IMODE(info.st_mode) & 0o022 and not root_sticky
        ):
            raise ValueError("Native trust parents must be protected")
    descriptor = os.open(directory / path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid not in (0, os.getuid())
            or stat.S_IMODE(info.st_mode) & forbidden_mode
        ):
            raise ValueError("Unsafe native trust file")
        body = handle.read(maximum + 1)
    if len(body) > maximum:
        raise _OversizedFileError("Native trust file exceeds its supported size")
    return body


def native_tls_environment(config_path: Path) -> dict[str, str]:
    """Read only explicit operator-owned native CA trust, never ambient secrets."""
    path = config_path.resolve().parent / "native-tls.env"
    try:
        path.lstat()
    except FileNotFoundError:
        return {}
    except OSError:
        raise InstallationError("Native TLS trust file cannot be inspected") from None
    try:
        body = _read_native_trust_file(path, MAX_NATIVE_TLS_BYTES)
    except (OSError, ValueError):
        raise InstallationError("Native TLS trust file is unsafe or cannot be read") from None
    try:
        name, separator, encoded = body.decode().strip().partition("=")
        if name != "SSL_CERT_FILE" or not separator:
            raise ValueError("Only SSL_CERT_FILE is supported")
        value = json.loads(encoded)
        if not isinstance(value, str) or not Path(value).is_absolute() or not value.isprintable():
            raise ValueError("Native CA bundle requires an absolute path")
        # Consumers reopen the returned name. Keep it independent of any mutable
        # directory alias, while still refusing a symlink at the final component.
        bundle = Path(value).parent.resolve(strict=True) / Path(value).name
        certificates = _read_native_trust_file(bundle, MAX_NATIVE_CA_BYTES)
        if not certificates.strip():
            raise ValueError("Native CA bundle must contain certificates")
        ssl.create_default_context(cadata=certificates.decode("ascii"))
    except (OSError, UnicodeError, ValueError):
        raise InstallationError(
            "Native TLS trust requires only SSL_CERT_FILE with a protected, readable PEM bundle"
        ) from None
    return {"SSL_CERT_FILE": str(bundle)}


def private_environment_path() -> Path | None:
    """Return the private environment file named by INKY_BIRD_ENV_FILE, if any."""
    value = os.environ.get(PRIVATE_ENVIRONMENT_FILE_ENV, "")
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ConfigurationError(
            f"{PRIVATE_ENVIRONMENT_FILE_ENV} must be the absolute path of the private "
            "environment file"
        )
    return path


def _private_environment_value(value: str) -> str | None:
    """Return one KEY=VALUE value without shell expansion, or None if malformed."""
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            return None
        try:
            decoded: object = json.loads(value)
        except ValueError:
            return None
        return decoded if isinstance(decoded, str) else None
    if value.startswith("'"):
        inner = value[1:-1]
        if len(value) < 2 or not value.endswith("'") or "'" in inner:
            return None
        return inner
    return None if '"' in value or "'" in value else value


def read_private_environment(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines from an owner-only file; errors never contain values."""
    try:
        body = _read_native_trust_file(path, MAX_PRIVATE_ENVIRONMENT_BYTES, private=True)
    except FileNotFoundError:
        raise ConfigurationError(
            f"The private environment file {path} does not exist; create it or unset "
            f"{PRIVATE_ENVIRONMENT_FILE_ENV}"
        ) from None
    except _OversizedFileError:
        raise ConfigurationError(
            f"The private environment file {path} is larger than 64 KiB; keep only the "
            "settings this service needs in it"
        ) from None
    except (OSError, ValueError):
        raise ConfigurationError(
            f"The private environment file {path} must be a regular file, not a link, owned "
            "by this account or root, readable only by its owner (mode 0600), in folders "
            "that only their owner or root can change"
        ) from None
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        raise ConfigurationError(f"The private environment file {path} is not UTF-8 text") from None
    environment: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, raw_value = line.partition("=")
        value = _private_environment_value(raw_value.strip()) if separator else None
        if (
            value is None
            or _ENVIRONMENT_NAME.fullmatch(name) is None
            or name in environment
            or not value.isprintable()
        ):
            raise ConfigurationError(
                f"Line {number} of the private environment file {path} is not a valid "
                "setting; write each one once as NAME=value, NAME=\"value\" or NAME='value'"
            )
        environment[name] = value
    return environment


def load_private_environment() -> list[str]:
    """Add the private environment file's settings that the process does not already have.

    Returns the names that were added, so a caller can remove them again.
    """
    path = private_environment_path()
    if path is None:
        return []
    added: list[str] = []
    for name, value in read_private_environment(path).items():
        if name not in os.environ:
            os.environ[name] = value
            added.append(name)
    return added


def system_service_problem(
    config: AppConfig, install_root: Path, others: Sequence[tuple[str, AppConfig]]
) -> str | None:
    """Why this installation would share data or a port with another, if it would.

    A system service's workspace, catalog and state stay under its own install
    root, so a second installation on the machine (a rehearsal beside
    production) has its own locks; and no two serve on one port.
    """
    root = install_root.resolve()
    controller = config.controller
    for name, path in (
        ("workspace_dir", controller.workspace_dir),
        ("catalog_dir", controller.catalog_dir),
        ("state_dir", controller.state_dir),
    ):
        if not path.resolve().is_relative_to(root):
            return f"{name} must be inside {root}, so no other installation shares it"
    for account, other in others:
        if other.controller.port == controller.port:
            return (
                f"Port {controller.port} is already the port of the installation of "
                f"{account}; choose another port"
            )
    return None


def read_account_config(path: Path, owner: int) -> AppConfig:
    """Load the configuration an account owns, as root may: the file itself,
    never a link, owned by that account; settings only, no secrets."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
                raise ConfigurationError(
                    f"The configuration {path} must be a regular file the service account owns"
                )
            content = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ConfigurationError(
            f"The configuration {path} must be a regular file, not a link"
        ) from None
    if len(content) > MAX_CONFIG_BYTES:
        raise ConfigurationError(f"The configuration {path} is larger than 1 MiB")
    return parse_config_bytes(content, path=path, load_secrets=False)


def generation_species_timeout_seconds(config: AppConfig) -> int:
    """The longest one bird can take: its agent calls at their timeout."""
    # A profile, one profile refresh, and per attempt one generation and one review.
    calls = 2 + 2 * config.controller.max_generation_attempts
    return calls * CODEX_CALL_TIMEOUT_SECONDS


def generation_cycle_timeout_seconds(config: AppConfig) -> int:
    """Bound one generation cycle by the agent calls its configuration allows."""
    # Deferred species still count toward the cap.
    birds = config.controller.max_species_attempts_per_cycle
    return birds * generation_species_timeout_seconds(config) + GENERATION_UNIT_MARGIN_SECONDS


def controller_systemd_units(
    config: AppConfig,
    *,
    executable: Path,
    app_dir: Path,
    config_path: Path,
    home: Path,
    user: str,
    service_prefix: str = LEGACY_NAME,
    system: SystemService | None = None,
) -> dict[str, str]:
    """Render the complete systemd controller unit set for one installation."""
    if system is None:
        service_prefix = service_family(service_prefix)
    elif not valid_account_name(service_prefix) or service_prefix != user:
        raise InstallationError("A system service is named after the account it runs as")
    service_path = controller_executable_path(
        config.controller.codex_path, generation_enabled=config.controller.generation_enabled
    )
    tls_environment = (
        "".join(
            f"Environment={_systemd_quote(f'{name}={value}')}\n"
            for name, value in native_tls_environment(config_path).items()
        )
        if system is None
        else ""
    )
    common = (
        f"User={user}\n"
        + ("" if system is None else f"Group={_systemd_literal(system.group)}\n")
        + f"WorkingDirectory={_systemd_literal(str(app_dir))}\n"
        f"Environment={_systemd_quote(f'HOME={home}')}\n"
        f"Environment={_systemd_quote(f'PATH={service_path}')}\n"
        "Environment=PYTHONUNBUFFERED=1\n" + tls_environment
    )
    access_path = config_path.resolve().parent / "native-access.env"
    owner_environment = (
        f"EnvironmentFile={_systemd_quote(str(access_path))}\n"
        if config.controller.oidc is not None and system is None
        else ""
    )
    serving_hardening = SYSTEMD_SERVICE_HARDENING
    if system is not None:
        environment_file = f"{PRIVATE_ENVIRONMENT_FILE_ENV}={system.environment_file}"
        common += f"Environment={_systemd_quote(environment_file)}\n"
        # A missing path (prefix "-") must not stop the service from starting.
        writable = " ".join(
            _systemd_quote(f"-{path}") for path in dict.fromkeys(system.writable_paths)
        )
        serving_hardening += f"ProtectSystem=strict\nReadWritePaths={writable}\n"

    def command(*arguments: str) -> str:
        return " ".join(
            (_systemd_quote(str(executable)), *(_systemd_argument(item) for item in arguments))
        )

    serving = common + owner_environment + serving_hardening
    units = _controller_units(config, service_prefix, common, serving, command, config_path)
    if system is not None:
        units = {name: f"{SYSTEM_SERVICE_MARKER}\n{text}" for name, text in units.items()}
    return units


def _controller_units(
    config: AppConfig,
    service_prefix: str,
    common: str,
    serving: str,
    command: Callable[..., str],
    config_path: Path,
) -> dict[str, str]:
    units = {
        f"{service_prefix}-controller.service": f"""[Unit]
Description=Serve the Inky Bird approved catalog
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{serving}ExecStart={command("serve", "--config", str(config_path))}
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
"""
    }
    if config.controller.configuration_ownership == "app":
        units[f"{service_prefix}-scheduler.service"] = f"""[Unit]
Description=Schedule Inky Bird application-owned work
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{common}ExecStart={command("scheduler", "--config", str(config_path))}
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
"""
        return units
    jobs = (
        ("refresh", config.schedule.refresh_minutes, True),
        ("generate", config.schedule.generation_minutes, config.controller.generation_enabled),
        ("catalog-publish", config.schedule.catalog_publish_minutes, config.public_catalog.enabled),
        (
            "notifications",
            config.notifications.delivery_retry_minutes,
            config.notifications.enabled,
        ),
    )
    timeouts = {"generate": f"{generation_cycle_timeout_seconds(config)}s"}
    for name, minutes, enabled in jobs:
        if not enabled:
            continue
        arguments = (
            ("notifications", "dispatch", "--config", str(config_path))
            if name == "notifications"
            else (name, "--config", str(config_path))
        )
        unit_dependencies = (
            f"After=network-online.target {service_prefix}-refresh.service\n"
            "Wants=network-online.target\n"
            f"Requires={service_prefix}-refresh.service"
            if name == "generate"
            else "After=network-online.target\nWants=network-online.target"
        )
        units[f"{service_prefix}-{name}.service"] = f"""[Unit]
Description=Run the Inky Bird {name} cycle
{unit_dependencies}

[Service]
Type=oneshot
{common}ExecStart={command(*arguments)}
TimeoutStartSec={timeouts.get(name, "30min")}
"""
        units[f"{service_prefix}-{name}.timer"] = f"""[Unit]
Description=Schedule the Inky Bird {name} cycle

[Timer]
OnActiveSec=2min
OnUnitActiveSec={minutes}min
Unit={service_prefix}-{name}.service

[Install]
WantedBy=timers.target
"""
    return units


def display_systemd_units(
    config: AppConfig,
    *,
    executable: Path,
    app_dir: Path,
    config_path: Path,
    user: str,
    service_prefix: str = LEGACY_NAME,
) -> dict[str, str]:
    """Render the display service and timer from validated configuration."""
    service_prefix = service_family(service_prefix)
    exec_start = (
        f"{_systemd_quote(str(executable))} display-cycle "
        f"--config {_systemd_argument(str(config_path))}"
    )
    tls_environment = "".join(
        f"Environment={_systemd_quote(f'{name}={value}')}\n"
        for name, value in native_tls_environment(config_path).items()
    )
    service = f"""[Unit]
Description=Rotate the next approved Inky Bird plate
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User={user}
WorkingDirectory={_systemd_literal(str(app_dir))}
Environment=PYTHONUNBUFFERED=1
{tls_environment}{SYSTEMD_SERVICE_HARDENING}ExecStart={exec_start}
TimeoutStartSec=15min
"""
    timer = f"""[Unit]
Description=Rotate the Inky Bird on its configured schedule

[Timer]
OnActiveSec={config.schedule.display_startup_delay_seconds}s
OnUnitActiveSec={config.schedule.rotation_minutes * 60}s
RandomizedDelaySec={config.schedule.rotation_jitter_seconds}s
Unit={service_prefix}-display.service

[Install]
WantedBy=timers.target
"""
    return {
        f"{service_prefix}-display.service": service,
        f"{service_prefix}-display.timer": timer,
    }


def _pass(check_id: str, summary: str, detail: str | None = None) -> DiagnosticCheck:
    return DiagnosticCheck(check_id, CheckStatus.PASS, summary, detail)


def _warn(
    check_id: str,
    summary: str,
    *,
    detail: str | None = None,
    remediation: str | None = None,
) -> DiagnosticCheck:
    return DiagnosticCheck(check_id, CheckStatus.WARN, summary, detail, remediation)


def _fail(
    check_id: str,
    summary: str,
    *,
    detail: str | None = None,
    remediation: str | None = None,
) -> DiagnosticCheck:
    return DiagnosticCheck(check_id, CheckStatus.FAIL, summary, detail, remediation)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _supported_platform(role: InstallationRole) -> DiagnosticCheck:
    system = platform.system()
    if role is InstallationRole.CONTROLLER and system == "Darwin":
        return _pass("platform", "macOS launchd is available for controller services")
    if system == "Linux" and shutil.which("systemctl") is not None:
        return _pass("platform", f"systemd is available for {role.value} services")
    supported = "macOS or systemd Linux" if role is InstallationRole.CONTROLLER else "systemd Linux"
    return _fail(
        "platform",
        (
            "A native service manager is not available for "
            f"{role.value} setup on {system or 'Unknown'}"
        ),
        remediation=f"Use {supported}; see docs/installation.md for the support matrix.",
    )


def _python_check() -> DiagnosticCheck:
    version = platform.python_version()
    return _pass("python", f"Python {version} satisfies the 3.11+ requirement")


def _private_environment_check() -> DiagnosticCheck | None:
    try:
        path = private_environment_path()
        if path is None:
            return None
        read_private_environment(path)
    except ConfigurationError as exc:
        return _fail(
            "private_environment",
            "The private environment file cannot be used",
            detail=str(exc),
            remediation=f"Fix the file, or unset {PRIVATE_ENVIRONMENT_FILE_ENV} to stop using it.",
        )
    return _pass("private_environment", f"Private environment file is in use: {path}")


def _config_permissions(path: Path) -> DiagnosticCheck:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return _warn(
            "config_permissions",
            "The private configuration is readable by other local users",
            detail=f"Mode is {mode:04o}",
            remediation=f"Run: chmod 600 {path}",
        )
    return _pass("config_permissions", "Private configuration permissions are restricted")


def _unknown_keys_check(config: AppConfig) -> DiagnosticCheck:
    # Names only: an unknown key may still hold a private value.
    return _warn(
        "config_unknown_keys",
        "The configuration has settings this version does not use",
        detail=", ".join(config.unknown_keys),
        remediation="Check these names for typos, and remove settings you no longer need.",
    )


def _notification_dead_letter_check(config: AppConfig, config_path: Path) -> DiagnosticCheck:
    check_id = "notification_dead_letters"
    try:
        # stat, not is_file: a denied lookup must be reported, not read as absent.
        try:
            recorded = stat.S_ISREG(notification_state_path(config).stat().st_mode)
        except FileNotFoundError:
            recorded = False
        if not recorded:
            return _pass(check_id, "No notifications are waiting to be retried")
        status = notification_status(config)
    except (InkyBirdFrameError, OSError) as exc:
        return _warn(
            check_id,
            "Notification delivery state could not be read",
            detail=str(exc),
            remediation="Inspect the controller state directory permissions.",
        )
    count = status.get("dead_letters")
    if not isinstance(count, int) or count <= 0:
        return _pass(check_id, "No notifications are waiting to be retried")
    return _warn(
        check_id,
        f"{count} notification(s) stopped retrying and were not delivered",
        remediation=(
            f"Fix the destination, then run: inky-bird notifications retry --config {config_path}"
        ),
    )


def _birdbuddy_auth_check(config: AppConfig) -> DiagnosticCheck:
    try:
        status = birdbuddy_status(
            config.controller.state_dir,
            include_manual_sightings=(config.discovery.birdbuddy_include_manual_sightings),
        )
    except InkyBirdFrameError as exc:
        return _fail(
            "birdbuddy_auth",
            "Bird Buddy authentication state is invalid",
            detail=str(exc),
            remediation="Repair private state permissions or run birdbuddy login again.",
        )
    if status.get("authenticated") is not True:
        return _warn(
            "birdbuddy_auth",
            "Bird Buddy is selected but not authenticated",
            remediation="Obtain permission, then run birdbuddy login.",
        )
    feeder = status.get("feeder")
    detail = None
    if isinstance(feeder, dict):
        name = feeder.get("name")
        role = feeder.get("role")
        if isinstance(name, str) and isinstance(role, str):
            detail = f"Selected feeder: {name} ({role})"
    return _pass("birdbuddy_auth", "Bird Buddy authentication state is ready", detail)


def _writable_directory(check_id: str, path: Path) -> DiagnosticCheck:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK):
        return _pass(check_id, f"Runtime path is writable: {path}")
    return _fail(
        check_id,
        f"Runtime path is not writable: {path}",
        remediation=(
            "Choose a path owned by the service user or correct its ownership and permissions."
        ),
    )


def _existing_writable_directory(check_id: str, path: Path) -> DiagnosticCheck:
    if not path.is_dir():
        return _fail(
            check_id,
            f"Required runtime directory does not exist: {path}",
            remediation=f"Create the directory and grant the service user access: {path}",
        )
    return _writable_directory(check_id, path)


def _executable_check(path: Path) -> DiagnosticCheck:
    if path.is_file() and os.access(path, os.X_OK):
        return _pass("codex_executable", f"Codex CLI is executable: {path}")
    return _fail(
        "codex_executable",
        f"Codex CLI is not executable: {path}",
        remediation="Install Codex CLI and set controller.codex_path to its absolute path.",
    )


def _codex_auth_check(path: Path) -> DiagnosticCheck:
    environment = os.environ.copy()
    environment["PATH"] = controller_executable_path(path)
    result = _run([str(path), "login", "status"], environment=environment)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0:
        return _fail(
            "codex_auth",
            "Codex CLI is not authenticated",
            detail=output or "codex login status failed",
            remediation="Run codex login, or codex login --device-auth on a headless controller.",
        )
    if "ChatGPT" in output:
        return _pass("codex_auth", "Codex CLI is authenticated with ChatGPT")
    return _warn(
        "codex_auth",
        "Codex CLI is authenticated without a confirmed ChatGPT login",
        detail=output,
        remediation="Use codex login with ChatGPT to use subscription-backed generation.",
    )


def health_url(config: AppConfig, role: InstallationRole) -> str:
    """Select the configured service endpoint for doctor and container probes."""
    if role is InstallationRole.CONTROLLER:
        if config.controller.healthcheck_origin is not None:
            return f"{config.controller.healthcheck_origin}/health"
        if config.controller.tls_certificate:
            for origin in config.controller.owner_allowed_origins:
                parsed_origin = urlsplit(origin)
                if (
                    parsed_origin.scheme == "https"
                    and (parsed_origin.port or 443) == config.controller.port
                ):
                    return f"{origin}/health"
        host = config.controller.bind_host
        if host == "0.0.0.0":
            host = "127.0.0.1"
        elif host in {"::", "[::]"}:
            host = "::1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        scheme = "https" if config.controller.tls_certificate else "http"
        return f"{scheme}://{host}:{config.controller.port}/health"
    parsed = urlsplit(config.display_node.controller_url)
    health_path = f"{parsed.path.rstrip('/')}/health"
    return urlunsplit((parsed.scheme, parsed.netloc, health_path, "", ""))


def _controller_health_check(config: AppConfig, role: InstallationRole) -> DiagnosticCheck:
    url = health_url(config, role)
    try:
        payload = get_json(url, timeout_seconds=5)
    except (InkyBirdFrameError, OSError, ValueError) as exc:
        return _fail(
            "controller_health",
            "The controller health endpoint is not reachable",
            detail=str(exc),
            remediation=f"Confirm routing and open {url} from this machine.",
        )
    if isinstance(payload, dict) and payload.get("ok") is True:
        return _pass("controller_health", f"Controller health endpoint is ready: {url}")
    return _fail(
        "controller_health",
        "The controller returned an invalid health response",
        detail=f"URL: {url}",
        remediation="Inspect the controller service logs and configuration.",
    )


def _launchd_target(label: str, uid: int) -> tuple[str, str]:
    """Accept an administrator-managed system job only for the calling identity."""
    import pwd

    path = LAUNCH_DAEMON_DIRECTORY / f"{label}.plist"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return f"gui/{uid}/{label}", "LaunchAgent"
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError("The system plist must be a protected administrator-owned regular file")
    with path.open("rb") as handle:
        job = plistlib.load(handle)
    if not isinstance(job, dict) or job.get("Label") != label:
        raise ValueError("The system plist label does not match")
    username = job.get("UserName")
    if not isinstance(username, str) or pwd.getpwnam(username).pw_uid != uid:
        raise ValueError("Run doctor as the system service account")
    return f"system/{label}", "LaunchDaemon"


LAUNCHD_PERIODIC_JOBS = {
    "refresh": "observation refresh",
    "generate": "generation cycle",
    "catalog-publish": "catalog publication",
    "notifications": "notification dispatch",
}


def _launchd_last_run_check(job: str, printed: str) -> DiagnosticCheck:
    """Judge a periodic job by the exit launchd recorded for its most recent run."""
    check_id = f"job_{job.replace('-', '_')}"
    description = LAUNCHD_PERIODIC_JOBS[job]
    fields: dict[str, str] = {}
    for line in printed.splitlines():
        key, separator, value = line.strip().partition(" = ")
        if separator:
            fields.setdefault(key, value.strip())
    remediation = "Inspect the controller logs, then run doctor again after the next run."
    signal_name = fields.get("last terminating signal")
    if signal_name:
        return _fail(
            check_id,
            f"The most recent {description} was stopped before it finished",
            detail=f"Stopped by signal: {signal_name}",
            remediation=remediation,
        )
    exit_code = fields.get("last exit code")
    if exit_code is None or exit_code.startswith("(never exited)"):
        return _pass(check_id, f"The {description} has not finished a run yet")
    number = exit_code.split(":", 1)[0].strip()
    if number == "0":
        return _pass(check_id, f"The most recent {description} succeeded")
    if number.isdigit():
        return _fail(
            check_id,
            f"The most recent {description} failed",
            detail=f"Last run ended with exit code {exit_code}",
            remediation=remediation,
        )
    return _fail(
        check_id,
        f"The {description} result could not be determined",
        detail=f"launchctl reported: {exit_code}",
        remediation=remediation,
    )


def _native_family(config: AppConfig) -> str | DiagnosticCheck:
    """Return the installed service family, or the check explaining why it is unknown."""
    try:
        return resolve_native_layout(config_path=config.configuration_path).family
    except ValueError as exc:
        return _fail("native_identity", str(exc))
    except OSError as exc:
        return _fail(
            "native_identity",
            "The installed services could not be identified",
            detail=str(exc),
            remediation="Run doctor as the service account, or check its folder permissions.",
        )


def _launchd_checks(config: AppConfig) -> Iterable[DiagnosticCheck]:
    prefix: str | DiagnosticCheck | None = _system_service_prefix()
    if prefix is None:
        prefix = _native_family(config)
    if isinstance(prefix, DiagnosticCheck):
        yield prefix
        return
    labels = [
        "com.inky-bird-frame.serve",
        "com.inky-bird-frame.refresh",
    ]
    if config.controller.generation_enabled:
        labels.append("com.inky-bird-frame.generate")
    if config.public_catalog.enabled:
        labels.append("com.inky-bird-frame.catalog-publish")
    if config.notifications.enabled:
        labels.append("com.inky-bird-frame.notifications")
    if config.controller.configuration_ownership == "app":
        labels = ["com.inky-bird-frame.serve", "com.inky-bird-frame.scheduler"]
    uid = os.getuid()
    for label in labels:
        label = label.replace(LEGACY_NAME, prefix)
        try:
            target, kind = _launchd_target(label, uid)
        except (OSError, ValueError, KeyError, plistlib.InvalidFileException, ExpatError) as exc:
            yield _fail(
                f"service_{label.rsplit('.', 1)[-1]}",
                f"Cannot verify the operator-managed system job: {label}",
                detail=str(exc),
                remediation="Ask the service operator to verify its plist and service account.",
            )
            continue
        result = _run(["launchctl", "print", target])
        if result.returncode == 0:
            job = label.rsplit(".", 1)[-1]
            yield _pass(f"service_{job}", f"{kind} is loaded: {label}")
            if job in LAUNCHD_PERIODIC_JOBS:
                yield _launchd_last_run_check(job, result.stdout)
        else:
            yield _fail(
                f"service_{label.rsplit('.', 1)[-1]}",
                f"{kind} is not loaded: {label}",
                remediation=(
                    "Ask the service operator to load the system job."
                    if kind == "LaunchDaemon"
                    else "Run setup controller --yes in the logged-in service account."
                ),
            )


def _systemd_unit_check(unit: str) -> DiagnosticCheck:
    enabled = _run(["systemctl", "is-enabled", unit])
    running = _run(["systemctl", "is-active", unit])
    if enabled.returncode == 0 and running.returncode == 0:
        return _pass(f"service_{unit}", f"systemd unit is enabled and active: {unit}")
    states = (
        f"enabled={enabled.stdout or enabled.stderr}; active={running.stdout or running.stderr}"
    )
    return _fail(
        f"service_{unit}",
        f"systemd unit is not ready: {unit}",
        detail=states,
        remediation=f"Inspect: systemctl status {unit}",
    )


def _systemd_last_run_check(
    unit: str,
    *,
    check_id: str,
    description: str,
) -> DiagnosticCheck:
    result = _run(["systemctl", "is-failed", unit])
    if result.returncode == 0:
        return _fail(
            check_id,
            f"The most recent {description} failed",
            remediation=f"Inspect: journalctl -u {unit}",
        )
    if result.returncode == 1 and result.stdout:
        return _pass(check_id, f"The {description} service is not failed")
    detail = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return _fail(
        check_id,
        f"The {description} service state could not be determined",
        detail=detail or f"systemctl exited with status {result.returncode}",
        remediation=f"Inspect: systemctl status {unit}",
    )


def _system_service_prefix() -> str | None:
    """The account running doctor, when system services were installed for it.

    A system service is named after its account: its systemd units start with
    SYSTEM_SERVICE_MARKER, and its LaunchDaemons run as that account. Run as
    that account, doctor checks those units or jobs.
    """
    import pwd

    try:
        name = pwd.getpwuid(os.getuid()).pw_name
        if platform.system() == "Darwin":
            # A LaunchDaemon this installer made runs as the account it is named after.
            with (LAUNCH_DAEMON_DIRECTORY / f"com.{name}.serve.plist").open("rb") as handle:
                job = plistlib.load(handle)
            ours = isinstance(job, dict) and job.get("UserName") == name
        else:
            unit = SYSTEMD_UNIT_DIRECTORY / f"{name}-controller.service"
            with unit.open(encoding="utf-8", errors="replace") as handle:
                ours = handle.readline().rstrip("\n") == SYSTEM_SERVICE_MARKER
    except (KeyError, OSError, ValueError, plistlib.InvalidFileException, ExpatError):
        return None
    return name if ours and valid_account_name(name) else None


def _systemd_controller_checks(config: AppConfig) -> Iterable[DiagnosticCheck]:
    prefix: str | DiagnosticCheck | None = _system_service_prefix()
    if prefix is None:
        prefix = _native_family(config)
    if isinstance(prefix, DiagnosticCheck):
        yield prefix
        return
    yield _systemd_unit_check(f"{prefix}-controller.service")
    if config.controller.configuration_ownership == "app":
        yield _systemd_unit_check(f"{prefix}-scheduler.service")
        return
    jobs = [("refresh", "observation refresh")]
    if config.controller.generation_enabled:
        jobs.append(("generate", "generation cycle"))
    if config.public_catalog.enabled:
        jobs.append(("catalog-publish", "catalog publication"))
    if config.notifications.enabled:
        jobs.append(("notifications", "notification dispatch"))
    for name, description in jobs:
        yield _systemd_unit_check(f"{prefix}-{name}.timer")
        yield _systemd_last_run_check(
            f"{prefix}-{name}.service",
            check_id=f"job_{name.replace('-', '_')}",
            description=description,
        )


def _display_hardware_check() -> DiagnosticCheck:
    code = (
        "from inky.auto import auto; display = auto(); print(f'{display.width}x{display.height}')"
    )
    result = _run([sys.executable, "-c", code], timeout_seconds=30)
    output = result.stdout.strip()
    if result.returncode != 0:
        return _fail(
            "inky_hardware",
            "Pimoroni Inky auto-detection failed",
            detail=result.stderr or output,
            remediation="Check the 40-pin connection, SPI/I2C settings, and the Inky Python extra.",
        )
    supported_outputs = {f"{width}x{height}" for width, height in SUPPORTED_HARDWARE_SIZES}
    if output in supported_outputs:
        return _pass("inky_hardware", f"Detected the supported {output} Inky display")
    return _fail(
        "inky_hardware",
        f"Detected unsupported Inky geometry: {output or 'unknown'}",
        remediation=(
            "This release supports the 7.3-inch PIM773 800x480 and "
            "13.3-inch PIM774 1600x1200 panels."
        ),
    )


def _display_boot_config_check() -> DiagnosticCheck:
    paths = (Path("/boot/firmware/config.txt"), Path("/boot/config.txt"))
    path = next((candidate for candidate in paths if candidate.is_file()), None)
    if path is None:
        return _warn(
            "boot_config",
            "Raspberry Pi boot configuration was not found",
            remediation="Use Raspberry Pi OS Bookworm or later and follow Pimoroni's Inky setup.",
        )
    try:
        content = path.read_text(errors="replace")
    except OSError as exc:
        return _warn(
            "boot_config",
            f"Raspberry Pi boot configuration could not be read: {path}",
            detail=str(exc),
            remediation="Run doctor with an account that can read the Pi boot configuration.",
        )
    lines = {
        line.strip().lower()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = [
        setting for setting in ("dtparam=spi=on", "dtoverlay=spi0-0cs") if setting not in lines
    ]
    if not missing:
        return _pass("boot_config", f"SPI and Inky chip-select settings are present in {path}")
    return _warn(
        "boot_config",
        "Recommended Pimoroni boot settings are not both visible",
        detail=f"Missing from {path}: {', '.join(missing)}",
        remediation="Run the Pimoroni Inky installer or apply its documented manual settings.",
    )


def doctor(role: InstallationRole, config_path: Path) -> DoctorReport:
    checks: list[DiagnosticCheck] = [_supported_platform(role), _python_check()]
    private_environment = _private_environment_check()
    if private_environment is not None:
        checks.append(private_environment)
    try:
        config = load_config(config_path)
    except (ConfigurationError, OSError) as exc:
        checks.append(
            _fail(
                "config",
                "Application configuration is invalid",
                detail=str(exc),
                remediation="Start from config.example.toml, then run config validate.",
            )
        )
        return DoctorReport(role, tuple(checks))
    checks.append(_pass("config", f"Configuration is valid: {config_path}"))
    checks.append(_config_permissions(config_path))
    if config.unknown_keys:
        checks.append(_unknown_keys_check(config))

    if role is InstallationRole.CONTROLLER:
        checks.extend(
            (
                _existing_writable_directory("workspace", config.controller.workspace_dir),
                _writable_directory("catalog", config.controller.catalog_dir),
                _writable_directory("controller_state", config.controller.state_dir),
            )
        )
        if config.controller.generation_enabled:
            checks.append(_executable_check(config.controller.codex_path))
            if config.controller.codex_path.is_file():
                checks.append(_codex_auth_check(config.controller.codex_path))
        else:
            checks.append(_pass("generation", "Generation is off; Codex is not required"))
        if DiscoveryProvider.BIRDBUDDY in config.discovery.sources:
            checks.append(_birdbuddy_auth_check(config))
        if config.notifications.enabled:
            checks.append(_notification_dead_letter_check(config, config_path))
        if platform.system() == "Darwin":
            checks.extend(_launchd_checks(config))
        elif shutil.which("systemctl") is not None:
            checks.extend(_systemd_controller_checks(config))
        checks.append(_controller_health_check(config, role))
    else:
        checks.append(_writable_directory("display_state", config.display_node.state_dir))
        checks.append(_display_boot_config_check())
        checks.append(_display_hardware_check())
        if platform.system() == "Linux" and shutil.which("systemctl") is not None:
            prefix = _native_family(config)
            if isinstance(prefix, DiagnosticCheck):
                checks.append(prefix)
            else:
                checks.append(_systemd_unit_check(f"{prefix}-display.timer"))
                checks.append(
                    _systemd_last_run_check(
                        f"{prefix}-display.service",
                        check_id="display_last_run",
                        description="display refresh",
                    )
                )
        checks.append(_controller_health_check(config, role))
    return DoctorReport(role, tuple(checks))


def _setup_script(role: InstallationRole, source_dir: Path | None) -> Path:
    system = platform.system()
    root = source_dir.expanduser().resolve() if source_dir is not None else _repository_root()
    if role is InstallationRole.CONTROLLER and system == "Darwin":
        return root / "deploy/install-controller.sh"
    if role is InstallationRole.CONTROLLER and system == "Linux":
        return root / "deploy/install-controller-systemd.sh"
    if role is InstallationRole.DISPLAY and system == "Linux":
        return root / "deploy/install-display-local.sh"
    raise InstallationError(f"Setup does not support {role.value} on {system}")


def _authorize_linux_setup() -> None:
    """Acquire sudo credentials while the prompt can use the caller's terminal."""
    try:
        completed = subprocess.run(
            ["sudo", "-v"],
            check=False,
            timeout=SUDO_AUTHORIZATION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallationError(f"Administrator authorization could not run: {exc}") from exc
    if completed.returncode != 0:
        raise InstallationError("Administrator authorization failed; run sudo -v and try again")


def validate_native_controller_setup(
    config: AppConfig, config_path: Path, *, system_service: bool = False
) -> None:
    """Refuse, before any change, what the native controller installers would refuse.

    A system service names its private environment file in every unit and the
    application loads it, so settings read from environment variables work
    there; the two serving-only native files are replaced by that one file.
    """
    if system_service:
        for name in ("native-access.env", "native-tls.env"):
            path = config_path.parent / name
            if path.exists() or path.is_symlink():
                raise InstallationError(
                    f"A system service does not read {path}; move its setting into the "
                    f"private environment file, then remove {name}"
                )
        return
    environment_credentials: list[str] = []
    if config.discovery.geoapify_api_key_env is not None:
        environment_credentials.append("geoapify_api_key_env")
    if (
        DiscoveryProvider.EBIRD in config.discovery.sources
        and config.discovery.ebird_api_key_env is not None
    ):
        environment_credentials.append("ebird_api_key_env")
    if (
        DiscoveryProvider.BIRDWEATHER in config.discovery.sources
        and config.discovery.birdweather_token_env is not None
    ):
        environment_credentials.append("birdweather_token_env")
    if environment_credentials:
        names = ", ".join(environment_credentials)
        raise InstallationError(
            f"Managed controller services cannot use {names}; store the corresponding "
            "credential directly in the private mode-0600 configuration file"
        )
    environment_destinations = [
        destination.name
        for destination in config.notifications.destinations
        if config.notifications.enabled and destination.url_env is not None
    ]
    if environment_destinations:
        names = ", ".join(environment_destinations)
        raise InstallationError(
            "Managed controller services require direct notification URLs in the private "
            f"configuration file; replace url_env for: {names}"
        )
    _native_oidc_secret(config, config_path)
    native_tls_environment(config_path)


def setup(
    role: InstallationRole,
    config_path: Path,
    *,
    apply: bool,
    source_dir: Path | None = None,
    app_dir: Path | None = None,
    support_dir: Path | None = None,
    uv_bin: Path | None = None,
    python_version: str | None = None,
    venv: Path | None = None,
    service_account: str | None = None,
) -> dict[str, object]:
    config_path = config_path.expanduser().resolve()
    config = load_config(config_path)
    if role is InstallationRole.CONTROLLER:
        validate_native_controller_setup(
            config, config_path, system_service=service_account is not None
        )
    script = _setup_script(role, source_dir)
    if not script.is_file():
        raise InstallationError(f"Installer is missing: {script}")
    changes = [
        "copy the application into its managed runtime directory",
        "install or update the role's Python environment",
        "install and enable native service-manager definitions",
        "verify the installed services with the native service manager",
    ]
    if (
        role is InstallationRole.CONTROLLER
        and config.controller.oidc is not None
        and service_account is None
    ):
        changes.append("retain the named OIDC secret in a private serving-only native cache")
    result: dict[str, object] = {
        "role": role.value,
        "source": str(script.parent.parent),
        "config": str(config_path),
        "applied": False,
        "changes": changes,
    }
    if role is InstallationRole.CONTROLLER:
        result["generation_enabled"] = config.controller.generation_enabled
        if not config.controller.generation_enabled:
            result["generation"] = (
                "New artwork is off. To turn it on, set generation_enabled = true "
                "under [controller] in the configuration, then run setup again."
            )
    if service_account is not None:
        result["service_account"] = service_account
        command = f"sudo {script} --service-account {service_account}"
        if apply:
            raise InstallationError(f"Install a system service as root: {command}")
        result["confirmation"] = f"Install it as root: {command}"
        return result
    if not apply:
        result["confirmation"] = "Repeat the same command with --yes to apply these changes."
        return result

    if platform.system() == "Linux":
        _authorize_linux_setup()

    environment = os.environ.copy()
    for variable in (
        "INKY_BIRD_APP_DIR",
        "INKY_BIRD_SUPPORT_DIR",
        "UV_BIN",
        "INKY_BIRD_PYTHON_VERSION",
        "INKY_BIRD_DISPLAY_VENV",
        "INKY_BIRD_RUN_INITIAL_DISPLAY",
    ):
        environment.pop(variable, None)
    environment["INKY_BIRD_CONFIG_PATH"] = str(config_path)
    if app_dir is not None:
        environment["INKY_BIRD_APP_DIR"] = str(app_dir.expanduser().resolve())
    if support_dir is not None:
        environment["INKY_BIRD_SUPPORT_DIR"] = str(support_dir.expanduser().resolve())
    if uv_bin is not None:
        environment["UV_BIN"] = str(uv_bin.expanduser().resolve())
    if python_version is not None:
        environment["INKY_BIRD_PYTHON_VERSION"] = python_version
    if venv is not None:
        environment["INKY_BIRD_DISPLAY_VENV"] = str(venv.expanduser().resolve())
    try:
        completed = subprocess.run(
            [str(script)],
            check=False,
            stdout=sys.stderr,
            timeout=INSTALLER_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallationError(f"Installer could not run: {exc}") from exc
    if completed.returncode != 0:
        raise InstallationError(
            f"Installer exited with status {completed.returncode}; review the output above"
        )
    result["applied"] = True
    result["summary"] = "Installer completed successfully."
    return result
