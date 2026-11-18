"""A stop request ends every application lock wait promptly, without the lock.

Each case holds a real lock in this process, starts a process (or thread) that
waits for it through the application's own lock helper, stops it the way a
service manager or a terminal would, and checks that it ended within
``PROMPT_SECONDS`` and that the holder still holds its lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_web import _config

from inky_bird_frame import lock_wait, notifications
from inky_bird_frame.web import create_app

ROOT = Path(__file__).resolve().parents[1]
PROMPT_SECONDS = 2.0
# Long enough for a waiter to be inside its wait, short beside PROMPT_SECONDS.
SETTLE_SECONDS = 0.3

# Each wait site: the lock file it waits on, relative to the state folder, and
# the program that enters it with that folder as ``state``.
SITES = {
    "catalog-state": (
        "catalog-state.lock",
        "from inky_bird_frame.catalog import catalog_state_lock as lock\ncontext = lock(state)",
    ),
    "catalog-state-shared": (
        "catalog-state.lock",
        "from inky_bird_frame.catalog import catalog_state_lock as lock\n"
        "context = lock(state, shared=True)",
    ),
    "catalog-state-timeout": (
        "catalog-state.lock",
        "from inky_bird_frame.catalog import catalog_state_lock as lock\n"
        "context = lock(state, timeout=600)",
    ),
    "runtime-state": (
        "activity.lock",
        "from inky_bird_frame.runtime_state import state_lock\n"
        "context = state_lock(state / 'activity.lock')",
    ),
    "notifications": (
        "notifications.lock",
        "from inky_bird_frame.notifications import _notification_lock\n"
        "context = _notification_lock(state)",
    ),
    "access": (
        "web-access/access.lock",
        "from inky_bird_frame.access import AccessStore\ncontext = AccessStore(state)._locked()",
    ),
    "taxonomy-crosswalk": (
        "taxonomy.json.lock",
        "from inky_bird_frame.birds import _taxonomy_crosswalk_lock\n"
        "context = _taxonomy_crosswalk_lock(state / 'taxonomy.json')",
    ),
    "birdnet-history": (
        "birdnet-analyzer.lock",
        "from inky_bird_frame.birdnet_analyzer import _history_lock\n"
        "context = _history_lock(state)",
    ),
    "birdbuddy-auth": (
        "birdbuddy-auth.lock",
        "from inky_bird_frame.birdbuddy import _auth_lock\ncontext = _auth_lock(state)",
    ),
    "birdbuddy-history": (
        "birdbuddy-detections.lock",
        "from inky_bird_frame.birdbuddy import _history_lock\ncontext = _history_lock(state)",
    ),
    "ebird-history": (
        "ebird-archive.lock",
        "from inky_bird_frame.ebird_archive import _history_lock\ncontext = _history_lock(state)",
    ),
}

# Exit statuses of the waiting program.
ACQUIRED = 0
STOPPED = 10

WAITER = """
import signal, sys
from pathlib import Path
from inky_bird_frame import lock_wait

handler, state = sys.argv[1], Path(sys.argv[2])
if handler == "records":
    # As the scheduler and the web server do: note the stop, return.
    signal.signal(signal.SIGTERM, lambda signum, frame: lock_wait.request_stop(signum))
{site}
print("waiting", flush=True)
try:
    with context:
        sys.exit({acquired})
except lock_wait.StopRequestedError:
    sys.exit({stopped})
"""


@contextmanager
def held(path: Path) -> Iterator[int]:
    """Hold an exclusive lock on ``path`` in this process, as a writer would."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        os.close(descriptor)


def still_held(path: Path) -> bool:
    """Whether another holder still has ``path`` locked."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False


def start_waiter(program: str, handler: str, state: Path) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-c", program, handler, str(state)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=ROOT,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == "waiting\n", process.stderr
    time.sleep(SETTLE_SECONDS)
    assert process.poll() is None, process.communicate()
    return process


def stop_and_time(process: subprocess.Popen[str], signum: int) -> tuple[int, float]:
    started = time.monotonic()
    process.send_signal(signum)
    try:
        process.wait(timeout=PROMPT_SECONDS * 5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    return process.returncode, time.monotonic() - started


@pytest.mark.parametrize("site", sorted(SITES))
@pytest.mark.parametrize(
    ("handler", "signum", "expected"),
    [
        # A recording handler, as long-running services have: the wait ends.
        ("records", signal.SIGTERM, STOPPED),
        # No handler, as one-shot commands have: the process ends as signalled.
        ("default", signal.SIGTERM, -signal.SIGTERM),
        # Ctrl-C in a terminal.
        ("default", signal.SIGINT, -signal.SIGINT),
    ],
    ids=["sigterm-recorded", "sigterm-default", "sigint-default"],
)
def test_a_stop_ends_a_contended_lock_wait_promptly(
    tmp_path: Path, site: str, handler: str, signum: int, expected: int
) -> None:
    relative, enter = SITES[site]
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    lock = state / relative
    program = WAITER.format(site=enter, acquired=ACQUIRED, stopped=STOPPED)
    with held(lock):
        waiter = start_waiter(program, handler, state)
        returncode, elapsed = stop_and_time(waiter, signum)
        assert still_held(lock)
    assert returncode == expected, waiter.stderr.read() if waiter.stderr else ""
    assert elapsed < PROMPT_SECONDS


def test_a_blocking_flock_outlasts_a_recorded_stop(tmp_path: Path) -> None:
    """The failure these waits avoid: a plain blocking flock is restarted after
    a handler that only records the stop, so it waits for the holder."""
    lock = tmp_path / "plain.lock"
    program = (
        "import fcntl\n"
        f"handle = open({str(lock)!r}, 'a+')\n"
        "class Plain:\n"
        "    def __enter__(self): fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
        "    def __exit__(self, *_): return False\n"
        "context = Plain()"
    )
    with held(lock):
        waiter = start_waiter(
            WAITER.format(site=program, acquired=ACQUIRED, stopped=STOPPED),
            "records",
            tmp_path,
        )
        waiter.send_signal(signal.SIGTERM)
        with pytest.raises(subprocess.TimeoutExpired):
            waiter.wait(timeout=PROMPT_SECONDS)
        waiter.kill()
        waiter.wait()


def test_a_stop_ends_a_wait_in_another_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal reaches only the main thread; a worker thread's wait ends too."""
    monkeypatch.setattr(lock_wait, "_stop_signal", None)
    outcome: list[BaseException | None] = []

    def wait() -> None:
        try:
            with notifications._notification_lock(tmp_path):
                outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    with held(tmp_path / "notifications.lock"):
        worker = threading.Thread(target=wait)
        worker.start()
        time.sleep(SETTLE_SECONDS)
        assert worker.is_alive()
        started = time.monotonic()
        lock_wait.request_stop(signal.SIGTERM)
        worker.join(PROMPT_SECONDS)
        assert time.monotonic() - started < PROMPT_SECONDS
        assert still_held(tmp_path / "notifications.lock")
    assert len(outcome) == 1 and isinstance(outcome[0], lock_wait.StopRequestedError)
    assert outcome[0].signum == signal.SIGTERM


def test_a_free_lock_is_taken_without_waiting(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path / "free.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        started = time.monotonic()
        assert lock_wait.flock(descriptor, fcntl.LOCK_EX)
        assert time.monotonic() - started < lock_wait.FIRST_POLL_SECONDS
        assert still_held(tmp_path / "free.lock")
    finally:
        os.close(descriptor)


def test_a_deadline_ends_the_wait_without_the_lock(tmp_path: Path) -> None:
    lock = tmp_path / "busy.lock"
    with held(lock):
        descriptor = os.open(lock, os.O_RDWR)
        try:
            started = time.monotonic()
            assert not lock_wait.flock(descriptor, fcntl.LOCK_EX, deadline=started + 0.2)
            assert 0.2 <= time.monotonic() - started < PROMPT_SECONDS
        finally:
            os.close(descriptor)
        assert still_held(lock)


def test_an_owner_request_waiting_for_a_lock_ends_with_503_once_stopping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lock_wait, "_stop_signal", signal.SIGTERM)
    config = _config(tmp_path)
    lock = config.state_dir / "web-access" / "access.lock"
    app = create_app(config)
    with held(lock), TestClient(app, base_url="https://inky.example.test") as client:
        response = client.get("/v1/access")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_stopping"


SERVER = """
import sys
from pathlib import Path
from inky_bird_frame.config import ControllerConfig
from inky_bird_frame.server import serve_catalog

root, port = Path(sys.argv[1]), int(sys.argv[2])
serve_catalog(
    ControllerConfig(
        workspace_dir=root / "work",
        catalog_dir=root / "catalog",
        state_dir=root / "state",
        codex_path=Path("codex"),
        codex_model=None,
        bind_host="127.0.0.1",
        port=port,
        references_per_species=3,
        generations_per_cycle=1,
        max_generation_attempts=3,
        owner_allowed_origins=(f"http://127.0.0.1:{port}",),
    )
)
"""


def test_the_web_server_stops_promptly_with_a_request_waiting_for_a_lock(
    tmp_path: Path,
) -> None:
    """uvicorn waits for every request before it exits; one waiting for a
    lock must end, so the service stops before its manager's kill timeout."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, "-c", SERVER, str(tmp_path), str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=ROOT,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                connection = HTTPConnection("127.0.0.1", port, timeout=1)
                connection.request("GET", "/health")
                connection.getresponse().read()
                break
            except OSError:
                assert server.poll() is None and time.monotonic() < deadline
                time.sleep(0.1)
        responses: list[tuple[int, dict[str, object]]] = []

        def request() -> None:
            connection = HTTPConnection("127.0.0.1", port, timeout=PROMPT_SECONDS * 5)
            connection.request("GET", "/v1/access")
            response = connection.getresponse()
            responses.append((response.status, json.loads(response.read())))

        lock = tmp_path / "state" / "web-access" / "access.lock"
        with held(lock):
            client = threading.Thread(target=request)
            client.start()
            time.sleep(SETTLE_SECONDS)
            assert not responses
            returncode, elapsed = stop_and_time(server, signal.SIGTERM)
            client.join(PROMPT_SECONDS)
            assert still_held(lock)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()
    output = server.stdout.read() if server.stdout else ""
    assert elapsed < PROMPT_SECONDS, output
    assert returncode == -signal.SIGTERM, output
    assert len(responses) == 1, output
    status, body = responses[0]
    error = body["error"]
    assert status == 503 and isinstance(error, dict), body
    assert error["code"] == "service_stopping"


def test_a_free_lock_is_still_taken_after_a_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopping process can still record how its work ended."""
    monkeypatch.setattr(lock_wait, "_stop_signal", signal.SIGTERM)
    with notifications._notification_lock(tmp_path):
        assert still_held(tmp_path / "notifications.lock")
