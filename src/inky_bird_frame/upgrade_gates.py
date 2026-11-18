"""What the native installers check around an upgrade's switch to a new runtime.

``hold-writer-locks`` waits until it holds every lock a writer takes, then
holds them until the installer that started it ends it or exits. The
installer runs it with the *installed* version's interpreter, by path, so the
installed code finds the state directory from the configuration: only the
standard library is imported here at module level.

``check-serve`` runs with the new version's interpreter after the switch and
asks the local web service for ``/health``: it must answer ``ok`` from this
same runtime. A system service passes its private environment file third.

``state-dir`` prints the configured state directory.

``remove-stale-work`` removes the work folders an installer left in a folder
before the current boot, when a power loss cut it short; nothing can use
them after a reboot.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

# Run by path without -P (an interpreter before 3.11), this directory would
# come first on the import path and its modules would shadow standard ones.
if sys.path and sys.path[0] and Path(sys.path[0]).resolve() == Path(__file__).resolve().parent:
    del sys.path[0]

# The locks writers take, in the state directory, with what holds each.
# Keep a name here after the application stops using it: an older version's
# writers may still be running when a newer installer waits for them.
WRITER_LOCKS = {
    "controller.lock": "the plate being made",  # controller.py exclusive_cycle_lock
    "refresh.lock": "the observation refresh",  # controller.py exclusive_refresh_lock
    "catalog-publish.lock": "the catalog publication",  # publisher.py exclusive_publish_lock
    "notifications-dispatch.lock": "notification delivery",  # notifications.py
    "notifications.lock": "a notification update",  # notifications.py _notification_lock
    "catalog-state.lock": "a catalog change",  # catalog.py catalog_state_lock
}
# Held by a running scheduler for its whole life to say it listens for retry
# wake-ups; it is not a writer lock, and waiting for it would wait for the
# scheduler the installer is about to stop.
PRESENCE_LOCKS = frozenset({"retry-wake-listener.lock"})
EXIT_PLATE_BUSY = 3
EXIT_OTHER_BUSY = 4
RETRY_SECONDS = 2.0
HEALTH_REQUEST_SECONDS = 5.0
HEALTH_RETRY_SECONDS = 1.0
MAX_HEALTH_BYTES = 64 * 1024
PRIVATE_ENVIRONMENT_FILE = "INKY_BIRD_ENV_FILE"
# The work folders an installer makes beside the configuration.
WORK_DIR_PATTERNS = ("install.*", "catalog-publish-recovery.*")


def lock_paths(state_dir: Path) -> list[Path]:
    """Every writer lock: the known ones, and any other lock file already in
    the state directory (a writer creates its lock on first use)."""
    found = [state_dir / name for name in WRITER_LOCKS]
    known = set(found)
    for directory, subdirectories, names in os.walk(state_dir, followlinks=False):
        subdirectories.sort()
        for name in sorted(names):
            path = Path(directory) / name
            if name.endswith(".lock") and name not in PRESENCE_LOCKS and path not in known:
                found.append(path)
    return found


def describe(path: Path) -> str:
    return WRITER_LOCKS.get(path.name, path.name)


def _open_lock(path: Path, *, create: bool) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError(f"{path.name} is not a regular file")
    return descriptor


def try_lock_all(paths: list[Path]) -> tuple[list[int], list[Path]]:
    """Take every lock at once, or none: holding some while waiting for the
    rest would deadlock with a writer that holds one and waits for another.
    Returns the held descriptors, or none and the locks that were busy."""
    held: list[int] = []
    busy: list[Path] = []
    try:
        for path in paths:
            try:
                descriptor = _open_lock(path, create=path.name in WRITER_LOCKS)
            except FileNotFoundError:
                # Removed since it was found: no writer holds it.
                continue
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                busy.append(path)
                continue
            held.append(descriptor)
    except BaseException:
        for descriptor in held:
            os.close(descriptor)
        raise
    if busy:
        for descriptor in held:
            os.close(descriptor)
        return [], busy
    return held, []


def wait_for_writers(state_dir: Path, timeout: float) -> list[int]:
    """Hold every writer lock within ``timeout`` seconds; exit 3 if a plate is
    still being made then, 4 if other work is."""
    state_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    announced: set[str] = set()
    while True:
        held, busy = try_lock_all(lock_paths(state_dir))
        if not busy:
            return held
        for path in busy:
            if describe(path) not in announced:
                announced.add(describe(path))
                print(f"Waiting for {describe(path)} to finish.", file=sys.stderr, flush=True)
        if time.monotonic() >= deadline:
            names = ", ".join(sorted({describe(path) for path in busy}))
            print(f"Still running after the wait: {names}.", file=sys.stderr, flush=True)
            plate = any(path.name == "controller.lock" for path in busy)
            raise SystemExit(EXIT_PLATE_BUSY if plate else EXIT_OTHER_BUSY)
        time.sleep(min(RETRY_SECONDS, max(0.0, deadline - time.monotonic())))


def hold_writer_locks(config_path: Path, ready: Path, timeout: float) -> int:
    try:
        from inky_bird_frame.config import load_config

        try:
            config = load_config(config_path, load_secrets=False)
        except TypeError:
            # Versions before load_secrets read every secret; still only the path is used.
            config = load_config(config_path)
        state_dir = Path(config.controller.state_dir)
    except Exception:
        print(
            "The installed version cannot be loaded with this configuration, so the "
            "upgrade cannot check for a plate being made.",
            file=sys.stderr,
        )
        return 0
    try:
        held = wait_for_writers(state_dir, timeout)
    except OSError as exc:
        print(f"A lock in the state directory cannot be used: {exc}", file=sys.stderr)
        return 1
    ready.touch()
    # Hold them until the installer ends this process or itself ends.
    parent = os.getppid()
    while os.getppid() == parent:
        time.sleep(1)
    for descriptor in held:
        os.close(descriptor)
    return 0


def runtime_id() -> str:
    """An opaque name for the code this process runs, the same in every
    process that imports this package from the same directory. It is the
    directory's device and inode, hashed: it names no path."""
    info = Path(__file__).resolve().parent.stat()
    return hashlib.sha256(f"{info.st_dev}:{info.st_ino}".encode()).hexdigest()[:16]


def _loopback(host: str) -> str:
    """Where to reach a service bound to ``host`` from this machine."""
    host = host.strip("[]")
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _tls_name(origins: tuple[str, ...], healthcheck_origin: str | None, port: int) -> str | None:
    """The name the service's certificate is for: an origin served on its own port."""
    from urllib.parse import urlsplit

    for origin in (healthcheck_origin, *origins):
        if origin is None:
            continue
        parsed = urlsplit(origin)
        if parsed.scheme == "https" and parsed.hostname and (parsed.port or 443) == port:
            return parsed.hostname
    return None


def fetch_health(
    address: str, port: int, *, server_name: str | None, ca_file: str | None, timeout: float
) -> object:
    """``GET /health`` from ``address``; with ``server_name``, over TLS verified for that name."""
    import http.client
    import json
    import socket
    import ssl

    connection: http.client.HTTPConnection
    if server_name is None:
        connection = http.client.HTTPConnection(address, port, timeout=timeout)
    else:
        context = ssl.create_default_context(cafile=ca_file)

        class Loopback(http.client.HTTPSConnection):
            """Connects to the local address, verifies the certificate's name."""

            def connect(self) -> None:
                raw = socket.create_connection((address, port), self.timeout)
                self.sock = context.wrap_socket(raw, server_hostname=server_name)

        connection = Loopback(server_name, port, timeout=timeout, context=context)
    try:
        connection.request("GET", "/health", headers={"Accept": "application/json"})
        response = connection.getresponse()
        body = response.read(MAX_HEALTH_BYTES + 1)
        if response.status != 200:
            raise ValueError(f"it answered HTTP {response.status}")
        if len(body) > MAX_HEALTH_BYTES:
            raise ValueError("its answer is too large")
        return json.loads(body)
    finally:
        connection.close()


def check_serve(
    config_path: Path, deadline_seconds: float, environment_file: Path | None = None
) -> int:
    """Ask the local web service for ``/health`` until it answers ``ok`` from
    this runtime, or the deadline passes. A system service's private
    environment file may name the trust (``SSL_CERT_FILE``) to verify it with."""
    from inky_bird_frame.config import load_config
    from inky_bird_frame.installation import load_private_environment, native_tls_environment

    if environment_file is not None:
        os.environ[PRIVATE_ENVIRONMENT_FILE] = str(environment_file)
        load_private_environment()
    config = load_config(config_path, load_secrets=False)
    controller = config.controller
    address = _loopback(controller.bind_host)
    server_name = ca_file = None
    if controller.tls_certificate is not None:
        server_name = (
            _tls_name(
                controller.owner_allowed_origins, controller.healthcheck_origin, controller.port
            )
            or address
        )
        ca_file = native_tls_environment(config_path).get("SSL_CERT_FILE")
    expected = runtime_id()
    deadline = time.monotonic() + deadline_seconds
    problem = "it did not answer"
    while True:
        remaining = deadline - time.monotonic()
        try:
            payload = fetch_health(
                address,
                controller.port,
                server_name=server_name,
                ca_file=ca_file,
                timeout=max(0.1, min(HEALTH_REQUEST_SECONDS, remaining)),
            )
        except (OSError, ValueError) as exc:
            problem = str(exc) or type(exc).__name__
        else:
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                problem = "it did not report ok"
            elif payload.get("runtime") != expected:
                problem = "the version answering is not the one just installed"
            else:
                return 0
        if time.monotonic() >= deadline:
            print(f"The web service health check failed: {problem}.", file=sys.stderr)
            return 1
        time.sleep(min(HEALTH_RETRY_SECONDS, max(0.0, deadline - time.monotonic())))


def print_state_dir(config_path: Path) -> int:
    from inky_bird_frame.config import load_config

    print(load_config(config_path, load_secrets=False).controller.state_dir)
    return 0


def boot_time() -> float:
    """When this machine last booted, in seconds since the epoch."""
    if sys.platform == "darwin":
        reply = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.boottime"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        found = re.search(r"\bsec = (\d+)", reply)
        if found is None:
            raise ValueError(f"unreadable boot time: {reply!r}")
        return float(found.group(1))
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    raise ValueError("no boot time in /proc/stat")


def remove_stale_work(folder: Path, booted: float) -> list[Path]:
    """Remove the work folders directly in ``folder`` last changed before
    ``booted``, never following a link, and name each one removed."""
    removed = []
    for pattern in WORK_DIR_PATTERNS:
        for path in sorted(folder.glob(pattern)):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mtime >= booted:
                continue
            try:
                shutil.rmtree(path)
            except OSError as error:
                print(f"Could not remove {path}: {error.strerror}", file=sys.stderr)
                continue
            print(f"Removed work files an interrupted install left: {path}", file=sys.stderr)
            removed.append(path)
    return removed


def main(arguments: list[str]) -> int:
    command, *rest = arguments
    if command == "hold-writer-locks":
        config_path, ready, timeout = rest
        return hold_writer_locks(Path(config_path), Path(ready), float(timeout))
    if command == "check-serve":
        config_path, deadline, *environment_file = rest
        return check_serve(
            Path(config_path), float(deadline), *(Path(path) for path in environment_file)
        )
    if command == "state-dir":
        (config_path,) = rest
        return print_state_dir(Path(config_path))
    if command == "remove-stale-work":
        (folder,) = rest
        remove_stale_work(Path(folder), boot_time())
        return 0
    print(f"Unknown command: {command}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
