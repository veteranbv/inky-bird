"""The native installers' upgrade helper: writer locks and the web service health check."""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from test_server import _serving

from inky_bird_frame import upgrade_gates
from inky_bird_frame.upgrade_gates import (
    EXIT_OTHER_BUSY,
    EXIT_PLATE_BUSY,
    WRITER_LOCKS,
    check_serve,
    lock_paths,
    runtime_id,
    try_lock_all,
    wait_for_writers,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/inky_bird_frame"


def test_every_lock_the_application_takes_is_a_writer_lock_or_the_scheduler_presence() -> None:
    """A new lock file name in the code must be classified here, so an
    upgrade cannot stop its writer mid-run without waiting for it."""
    found: dict[str, str] = {}
    for path in sorted(SOURCE.glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for name in re.findall(r'"([\w-]+\.lock)"', line):
                found.setdefault(name, f"{path.name}:{number}")
    # The display node's own cycle, and owner setup's lock beside the config,
    # which the installer itself takes only once the upgrade is done.
    elsewhere = {"display-cycle.lock", "owner-init.lock"}
    classified = set(WRITER_LOCKS) | upgrade_gates.PRESENCE_LOCKS | elsewhere
    # Held when the file exists: a writer creates its lock on first use, so
    # one that does not exist yet has no writer holding it. The taxonomy
    # crosswalk caches' locks are named from their cache files.
    held_when_present = {
        "access.lock",
        "owner-operation.lock",
        "activity.lock",
        "retry-requests.lock",
        "birdnet-analyzer.lock",
        "ebird-archive.lock",
        "birdbuddy-auth.lock",
        "birdbuddy-detections.lock",
        "birdbuddy-sync.lock",
    }
    unclassified = {
        name: where for name, where in found.items() if name not in classified | held_when_present
    }
    assert not unclassified, unclassified


def test_lock_paths_names_every_writer_lock_and_any_other_existing_lock(tmp_path: Path) -> None:
    (tmp_path / "activity.lock").write_text("")
    (tmp_path / "retry-wake-listener.lock").write_text("")
    (tmp_path / "web-access").mkdir()
    (tmp_path / "web-access/access.lock").write_text("")
    (tmp_path / "observations.json").write_text("{}")
    paths = lock_paths(tmp_path)
    assert paths[: len(WRITER_LOCKS)] == [tmp_path / name for name in WRITER_LOCKS]
    assert set(paths[len(WRITER_LOCKS) :]) == {
        tmp_path / "activity.lock",
        tmp_path / "web-access/access.lock",
    }


@contextmanager
def held(path: Path) -> Iterator[None]:
    """Hold ``path`` the way a writer does, from another open file."""
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def test_locks_are_taken_all_at_once_or_not_at_all(tmp_path: Path) -> None:
    """Holding the free ones while waiting for a busy one would deadlock with
    a writer that holds that one and waits for a free one."""
    paths = lock_paths(tmp_path)
    with held(tmp_path / "catalog-publish.lock"):
        descriptors, busy = try_lock_all(paths)
        assert descriptors == [] and busy == [tmp_path / "catalog-publish.lock"]
        # Nothing was kept: a writer can still take any other lock.
        with held(tmp_path / "catalog-state.lock"):
            pass
    descriptors, busy = try_lock_all(paths)
    assert busy == [] and len(descriptors) == len(WRITER_LOCKS)
    try:
        with (tmp_path / "controller.lock").open("a+") as handle, pytest.raises(BlockingIOError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def test_a_lock_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").write_text("")
    (tmp_path / "refresh.lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        try_lock_all(lock_paths(tmp_path))


@pytest.mark.parametrize(
    ("name", "status", "described"),
    [
        ("controller.lock", EXIT_PLATE_BUSY, "the plate being made"),
        ("notifications.lock", EXIT_OTHER_BUSY, "a notification update"),
        ("activity.lock", EXIT_OTHER_BUSY, "activity.lock"),
    ],
)
def test_a_writer_still_running_at_the_deadline_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, status: int, described: str
) -> None:
    (tmp_path / name).write_text("")
    with held(tmp_path / name), pytest.raises(SystemExit) as stopped:
        wait_for_writers(tmp_path, 0.2)
    assert stopped.value.code == status
    error = capsys.readouterr().err
    assert f"Waiting for {described} to finish." in error
    assert f"Still running after the wait: {described}." in error


def test_the_wait_ends_once_the_writer_finishes(tmp_path: Path) -> None:
    lock = tmp_path / "refresh.lock"
    lock.write_text("")
    finished = threading.Event()

    def writer() -> None:
        with held(lock):
            finished.wait(10)

    thread = threading.Thread(target=writer)
    thread.start()
    time.sleep(0.1)
    threading.Timer(0.3, finished.set).start()
    started = time.monotonic()
    descriptors = wait_for_writers(tmp_path, 30)
    thread.join()
    assert time.monotonic() - started < 10
    assert len(descriptors) == len(WRITER_LOCKS)
    for descriptor in descriptors:
        os.close(descriptor)


def test_the_helper_runs_by_path_on_an_installed_interpreter_and_holds_until_its_parent_ends(
    tmp_path: Path,
) -> None:
    """The installer runs it with -I by path: it imports the installed
    package's configuration, not anything beside itself."""
    installed = tmp_path / "installed/inky_bird_frame"
    installed.mkdir(parents=True)
    (installed / "__init__.py").write_text("")
    state = tmp_path / "state"
    (installed / "config.py").write_text(
        "from pathlib import Path\nfrom types import SimpleNamespace\n"
        "def load_config(path, load_secrets=True):\n"
        f"    return SimpleNamespace(controller=SimpleNamespace(state_dir=Path({str(state)!r})))\n"
    )
    ready = tmp_path / "ready"
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys; subprocess.Popen(sys.argv[1:]); sys.stdin.read()",
            sys.executable,
            "-S",
            "-P",
            str(SOURCE / "upgrade_gates.py"),
            "hold-writer-locks",
            str(tmp_path / "config.toml"),
            str(ready),
            "5",
        ],
        stdin=subprocess.PIPE,
        env={"PYTHONPATH": str(installed.parent), "PATH": "/usr/bin:/bin"},
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert time.monotonic() < deadline, "the helper never held the locks"
            time.sleep(0.05)
        with (state / "controller.lock").open("a+") as handle, pytest.raises(BlockingIOError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        assert parent.stdin is not None
        parent.stdin.close()
        parent.wait(timeout=30)
    # Its parent gone, the helper lets go within a few seconds.
    deadline = time.monotonic() + 10
    while True:
        descriptors, busy = try_lock_all(lock_paths(state))
        if not busy:
            break
        assert time.monotonic() < deadline, f"still held: {busy}"
        time.sleep(0.1)
    for descriptor in descriptors:
        os.close(descriptor)


def test_an_installed_version_that_cannot_read_the_config_holds_nothing_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("inky_bird_frame.config.load_config", side_effect=ValueError("bad")):
        assert upgrade_gates.hold_writer_locks(tmp_path / "c", tmp_path / "ready", 1) == 0
    assert not (tmp_path / "ready").exists()
    assert "cannot check for a plate being made" in capsys.readouterr().err


# The health check -------------------------------------------------------------


def controller(port: int, **extra: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "bind_host": "127.0.0.1",
        "port": port,
        "tls_certificate": None,
        "owner_allowed_origins": (),
        "healthcheck_origin": None,
    }
    fields.update(extra)
    return SimpleNamespace(controller=SimpleNamespace(**fields))


@contextmanager
def answering(answer: object, *, delay: float = 0.0) -> Iterator[int]:
    """A loopback service whose /health answers ``answer`` after ``delay`` seconds."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the standard handler's name
            time.sleep(delay)
            body = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *arguments: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def run_check(port: int, deadline: float, **extra: object) -> int:
    with patch("inky_bird_frame.config.load_config", return_value=controller(port, **extra)):
        return check_serve(Path("/nonexistent/config.toml"), deadline)


def test_the_check_passes_only_for_this_runtime() -> None:
    with answering({"ok": True, "runtime": runtime_id()}) as port:
        assert run_check(port, 5) == 0


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": True, "runtime": "0" * 16},
        {"ok": True},
        {"ok": False, "runtime": "same"},
        ["not", "an", "object"],
    ],
)
def test_another_runtime_or_an_unhealthy_answer_fails(
    answer: object, capsys: pytest.CaptureFixture[str]
) -> None:
    if isinstance(answer, dict) and answer.get("runtime") == "same":
        answer["runtime"] = runtime_id()
    with answering(answer) as port:
        assert run_check(port, 0.5) == 1
    assert "health check failed" in capsys.readouterr().err


def test_a_service_that_never_answers_fails_within_the_deadline() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        started = time.monotonic()
        assert run_check(listener.getsockname()[1], 1) == 1
    assert time.monotonic() - started < 10


def test_a_service_that_starts_late_passes() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    class Late(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the standard handler's name
            body = json.dumps({"ok": True, "runtime": runtime_id()}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *arguments: object) -> None:
            pass

    servers: list[HTTPServer] = []

    def start() -> None:
        server = HTTPServer(("127.0.0.1", port), Late)
        servers.append(server)
        server.serve_forever()

    timer = threading.Timer(1.0, start)
    timer.daemon = True
    timer.start()
    try:
        assert run_check(port, 10) == 0
    finally:
        timer.cancel()
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize(
    ("bind", "address"), [("0.0.0.0", "127.0.0.1"), ("::", "::1"), ("[::1]", "::1")]
)
def test_a_wildcard_bind_is_checked_on_loopback(bind: str, address: str) -> None:
    assert upgrade_gates._loopback(bind) == address


def test_the_real_service_reports_this_runtime(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    with _serving(tmp_path / "catalog", state / "active-catalog.json", state) as port:
        assert run_check(port, 10) == 0


def certificate_for(name: str, directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path


@pytest.mark.parametrize("trusted", [True, False])
def test_tls_is_checked_on_loopback_for_the_certificate_name(tmp_path: Path, trusted: bool) -> None:
    """The service's certificate names its public origin; the check connects
    to this machine and verifies that name against the configured trust."""
    state = tmp_path / "state"
    state.mkdir()
    certificate, key = certificate_for("inky.example.test", tmp_path)
    with _serving(
        tmp_path / "catalog",
        state / "active-catalog.json",
        state,
        certificate=certificate,
        private_key=key,
    ) as port:
        trust = {"SSL_CERT_FILE": str(certificate)} if trusted else {}
        with patch("inky_bird_frame.installation.native_tls_environment", return_value=trust):
            status = run_check(
                port,
                2 if trusted else 0.5,
                tls_certificate=certificate,
                owner_allowed_origins=(f"https://inky.example.test:{port}",),
            )
    assert status == (0 if trusted else 1)


def test_the_runtime_id_names_the_imported_package_not_the_helper_file(tmp_path: Path) -> None:
    """A regular install imports its own copy of the package from the venv,
    while the runtime's source tree keeps another: two processes importing the
    same copy agree, and a helper run by path from the source does not."""
    for root in ("site", "src"):
        package = tmp_path / root / "inky_bird_frame"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "upgrade_gates.py").write_text((SOURCE / "upgrade_gates.py").read_text())
    imported = "from inky_bird_frame.upgrade_gates import runtime_id; print(runtime_id())"

    def run(*arguments: str) -> str:
        return subprocess.run(
            [sys.executable, "-S", "-P", *arguments],
            env={"PYTHONPATH": str(tmp_path / "site"), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    serving = run("-c", imported)
    by_module = run(
        "-c",
        "import runpy; runpy.run_module('inky_bird_frame.upgrade_gates', run_name='x')\n"
        + imported,
    )
    by_path = run(
        "-c",
        "import runpy, sys; m = runpy.run_path(sys.argv[1]); print(m['runtime_id']())",
        str(tmp_path / "src/inky_bird_frame/upgrade_gates.py"),
    )
    assert serving == by_module
    assert by_path != serving
