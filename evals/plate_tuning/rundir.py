"""The run directory: its seed, its file layout and small durable-write helpers."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shutil
import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypeVar

from inky_bird_frame.http import write_bytes_atomic, write_json_atomic

from .config import BudgetExhaustedError, HarnessError

RUN_FILE: Final = "run.json"
LOCK_FILE: Final = ".lock"
# Top-level names the harness owns; an arm can never be called one of these.
RESERVED_NAMES: Final = frozenset(
    {
        "arms",
        "blind",
        "calibration",
        "compare",
        "gepa",
        "keys",
        "references",
        "report",
        "review-labels",
        "reviews",
        "smoke",
        "tune",
        "tune-review",
        "unblinding",
        "verdicts",
    }
)
MAX_JSON_BYTES: Final = 16 * 1024 * 1024
T = TypeVar("T")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> object:
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_JSON_BYTES + 1)
    except OSError as exc:
        raise HarnessError(f"Could not read {path.name}") from exc
    if len(content) > MAX_JSON_BYTES:
        raise HarnessError(f"{path.name} is too large")
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"{path.name} is not valid JSON") from exc


def read_object(path: Path) -> dict[str, object]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise HarnessError(f"{path.name} must contain a JSON object")
    return value


def write_json(path: Path, value: object) -> None:
    write_json_atomic(path, value)


def write_text(path: Path, text: str) -> None:
    write_bytes_atomic(path, text.encode("utf-8"))


def run_relative(run: Run, path: Path) -> str:
    """A path inside the run as run-relative text, so records hold no home folder."""
    resolved = path.resolve()
    return str(resolved.relative_to(run.root)) if resolved.is_relative_to(run.root) else str(path)


def resolve_recorded(root: Path, recorded: str) -> Path:
    path = Path(recorded)
    return path if path.is_absolute() else root / path


def replace_directory(staging: Path, final: Path) -> None:
    """Publish a fully written staging directory under its final name in one rename."""
    if final.exists():
        shutil.rmtree(staging, ignore_errors=True)
        return
    staging.replace(final)


def staging_directory(final: Path) -> Path:
    staging = final.parent / f".{final.name}.partial"
    # A partial directory is what a crash before the rename leaves behind.
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    return staging


@contextmanager
def locked(root: Path) -> Iterator[None]:
    """Hold a run directory for one command, so two never pay for the same work.

    The kernel releases the lock when the process exits, however it exits.
    """
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HarnessError(
                "Another command is using this run directory; wait for it to finish"
            ) from None
        yield
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class Run:
    root: Path
    seed: int

    @classmethod
    def open(cls, root: Path, *, seed: int | None = None) -> Run:
        root = root.resolve()
        path = root / RUN_FILE
        if path.exists():
            recorded = read_object(path).get("seed")
            if not isinstance(recorded, int):
                raise HarnessError("run.json has no integer seed")
            if seed is not None and seed != recorded:
                raise HarnessError("This run directory was created with a different seed")
            return cls(root, recorded)
        root.mkdir(parents=True, exist_ok=True)
        chosen = secrets.randbits(32) if seed is None else seed
        write_json(path, {"seed": chosen, "created_at": now()})
        return cls(root, chosen)

    def derived(self, identity: str) -> int:
        """A stable per-item random seed, so a resumed run makes the same choices."""
        return int(sha256_text(f"{self.seed}|{identity}")[:16], 16)

    def opaque_id(self, identity: str) -> str:
        return sha256_text(f"pair|{self.seed}|{identity}")[:20]

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)


class Ledger:
    """Append-only record of every paid call this run started, finished or lost."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, event: str, **details: object) -> None:
        line = json.dumps({"at": now(), "event": event, **details}, sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+b") as handle:
                # A crash can leave a partial last line; end it so this record
                # is not joined to it and lost.
                size = handle.seek(0, 2)
                if size:
                    handle.seek(size - 1)
                    if handle.read(1) != b"\n":
                        handle.write(b"\n")
                handle.write((line + "\n").encode("utf-8"))
                handle.flush()

    def entries(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        found: list[dict[str, object]] = []
        with self._lock, self.path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # A crash can cut the last line short; it recorded nothing usable.
                    continue
                if isinstance(entry, dict):
                    found.append(entry)
        return found

    def count(self, event: str) -> int:
        return sum(1 for entry in self.entries() if entry.get("event") == event)

    def latest(self, *keys: str) -> dict[tuple[object, ...], str]:
        """The last event recorded for each item, identified by the values of keys."""
        last: dict[tuple[object, ...], str] = {}
        for entry in self.entries():
            event = entry.get("event")
            if isinstance(event, str) and all(key in entry for key in keys):
                last[tuple(entry[key] for key in keys)] = event
        return last


class Budget:
    """A hard cap on paid calls; used counts calls already started, in this run or before."""

    def __init__(self, limit: int, *, what: str = "generation", used: int = 0) -> None:
        if limit < 0:
            raise HarnessError(f"The {what} cap must not be negative")
        self.limit = limit
        self.what = what
        self._used = used
        # Calls set aside for later work, which reserve() may not spend.
        self._held = 0
        self._lock = threading.Lock()

    @classmethod
    def for_run(cls, limit: int, ledger: Ledger, event: str, *, what: str) -> Budget:
        """A cap on the whole run: every call its ledger shows started counts against it."""
        return cls(limit, what=what, used=ledger.count(event))

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(self.limit - self._used, 0)

    @property
    def available(self) -> int:
        """What reserve() may still spend: the remainder less anything held back."""
        with self._lock:
            return max(self.limit - self._used - self._held, 0)

    def reserve(self, count: int = 1) -> None:
        """Take count calls at once, or none of them."""
        with self._lock:
            if self._used + count > self.limit - self._held:
                raise BudgetExhaustedError(f"The {self.what} cap of {self.limit} was reached")
            self._used += count

    @contextmanager
    def holding(self, count: int) -> Iterator[None]:
        """Set count calls aside for later work; reserve() cannot spend them meanwhile."""
        with self._lock:
            if self._used + self._held + count > self.limit:
                raise BudgetExhaustedError(
                    f"The {self.what} cap of {self.limit} cannot set aside {count} more calls"
                )
            self._held += count
        try:
            yield
        finally:
            with self._lock:
                self._held -= count


def run_tasks(tasks: Sequence[Callable[[], T]], parallel: int) -> list[T]:
    """Run tasks, in order on this thread when parallel is 1.

    On the main thread the application's runner turns SIGTERM and SIGHUP into
    a clean stop of the agent's process group; worker threads cannot receive
    signals, so only a parallel run can leave agents to finish on their own.
    """
    if parallel <= 1:
        return [task() for task in tasks]
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = [pool.submit(task) for task in tasks]
        return [future.result() for future in futures]
