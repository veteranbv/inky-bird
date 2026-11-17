"""Synthetic service managers and package tools for installer behaviour tests.

Every tool records its call in ``$FAKE/log`` and keeps its state under
``$FAKE``. Lines in ``$FAKE/fail`` name the calls that should fail. Nothing
here contacts a network, a real service manager or a real package index.

Every call is also numbered in ``$FAKE/steps``. Each line of ``$FAKE/inject``
(``<step> <mode>``) makes the call with that number fail, makes it and every later sudo
call fail (``expire``), or stops the installer once it completes: SIGTERM
(``kill``), SIGHUP (``hangup``) or SIGINT to its whole process group
(``interrupt``; a display installer run over ssh then gets SIGHUP at once, as
when the connection drops). Status queries are numbered but never injected.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path

# Every installer step starts a fresh interpreter; importing what only some
# tools need where they use it takes about a quarter off each call.

FAKE = Path(os.environ.get("FAKE", "/nonexistent"))
TOOLS = Path(__file__).resolve()


def _rules() -> set[str]:
    path = FAKE / "fail"
    return set(path.read_text().split()) if path.exists() else set()


# The stops an installer can be sent, by the matrix or by its own cleanup.
STOPS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)


@contextlib.contextmanager
def deferred_stops() -> Iterator[None]:
    """Run a critical section whole: a stop that arrives meanwhile is
    delivered once it ends, so shared state and logs are never left torn or
    missing. Every fake runs under it, except while it waits on something
    that must stay stoppable (``stops_allowed``)."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, STOPS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


@contextlib.contextmanager
def stops_allowed() -> Iterator[None]:
    previous = signal.pthread_sigmask(signal.SIG_UNBLOCK, STOPS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def stoppable_child() -> None:
    """``preexec_fn`` for a child: it gets stops as the real command would."""
    signal.pthread_sigmask(signal.SIG_UNBLOCK, STOPS)


def _append(path: Path, text: str) -> None:
    """Append in one write, so a line is never half there."""
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(descriptor, text.encode())
    finally:
        os.close(descriptor)


def _replace(path: Path, text: str) -> None:
    """Replace a file whole: a fake stopped part way must not leave it empty
    or torn for every later call to fail on."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(text)
    os.replace(temporary, path)


def _log(*entry: object) -> None:
    _append(FAKE / "log", json.dumps([str(item) for item in entry]) + "\n")


def _state(name: str) -> dict[str, object]:
    path = FAKE / f"{name}.json"
    loaded: dict[str, object] = json.loads(path.read_text()) if path.exists() else {}
    return loaded


def _save(name: str, value: dict[str, object]) -> None:
    _replace(FAKE / f"{name}.json", json.dumps(value, sort_keys=True))


def _identity() -> str:
    """Who this call runs as: the service account under the fake setpriv,
    root when the installer runs as root (``FAKE_UID=0``), otherwise the user."""
    return os.environ.get("FAKE_IDENTITY") or (
        "root" if os.environ.get("FAKE_UID") == "0" else "user"
    )


def _as(*what: object) -> None:
    """Record who ran a call that only one identity may run."""
    _log("as", _identity(), *what)


STEP_LOCK_TIMEOUT_SECONDS = 30.0
KILL = False
# Which stop ``KILL`` sends once the call completes.
STOP = "kill"


def _step_lock() -> Path:
    import time

    lock = FAKE / "steps.lock"
    deadline = time.monotonic() + STEP_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock.mkdir()
            # Who holds it, so a lock left behind names its holder.
            _replace(FAKE / "steps.lock.owner", f"{os.getpid()} {' '.join(sys.argv[1:3])}\n")
            return lock
        except FileExistsError:
            if time.monotonic() >= deadline:
                owner = FAKE / "steps.lock.owner"
                held = owner.read_text().strip() if owner.exists() else "unknown"
                raise RuntimeError(f"the step lock is still held by {held}") from None
            time.sleep(0.001)


def step(tool: str, arguments: list[str], *, action: bool) -> str:
    """Number this call and return what the matrix injects at it, if anything;
    an injected fault is logged, so a case can tell it was made."""
    steps = FAKE / "steps"
    if not FAKE.is_dir():
        return ""
    # A stop while the lock is held would leave it and hang every later step.
    with deferred_stops():
        lock = _step_lock()
        try:
            index = len(steps.read_text().splitlines()) if steps.exists() else 0
            kind = "action" if action else "query"
            line = "\t".join([str(index), kind, tool, *arguments]).replace("\n", " ")
            _append(steps, line + "\n")
        finally:
            (FAKE / "steps.lock.owner").unlink(missing_ok=True)
            lock.rmdir()
        inject = FAKE / "inject"
        if not action or not inject.exists():
            return ""
        # One line per injected fault; a second one lands in the first one's cleanup.
        for entry in inject.read_text().splitlines():
            at_text, mode = entry.split()
            at = int(at_text)
            if mode == "expire" and tool == "sudo" and index >= at:
                (FAKE / "sudo-expired").touch()
                _log("fault-injected", index, mode)
                return "fail"
            if index == at:
                _log("fault-injected", index, mode)
                return "fail" if mode == "expire" else mode
    return ""


def _installer_pid(*, outermost: bool = False) -> int | None:
    """The nearest installer script above this call, not one of its subshells,
    or with ``outermost`` the one the owner started (a remote display deploy
    runs the local installer on the other end of ssh)."""

    import subprocess

    def parent_and_command(pid: int) -> tuple[int, str]:
        output = subprocess.run(
            ["/bin/ps", "-ww", "-o", "ppid=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not output:
            return 0, ""
        parent, _, command = output.partition(" ")
        return int(parent), command.strip()

    pid = os.getppid()
    found = None
    while pid > 1:
        parent, command = parent_and_command(pid)
        if "/deploy/install-" in command or "/deploy/uninstall-" in command:
            while parent > 1 and parent_and_command(parent)[1] == command:
                pid = parent
                parent = parent_and_command(pid)[0]
            if not outermost:
                return pid
            found = pid
        pid = parent
    if found is not None:
        return found
    return None


STOP_SIGNALS = {"kill": "SIGTERM", "hangup": "SIGHUP", "interrupt": "SIGINT"}


def kill_installer(mode: str = "kill") -> None:
    """Stop the installer: TERM or HUP to the script itself, or INT to its whole
    process group as Ctrl-C does. This call ignores the interrupt it sends.
    It runs whole, sending and logging, under ``deferred_stops``."""
    pid = _installer_pid(outermost=mode == "interrupt")
    if pid is None:
        return
    if mode == "interrupt":
        nearest = _installer_pid()
        if nearest is not None and nearest != pid:
            # The call ran on the display: the connection drops while its
            # installer waits for this call, not once it has gone on.
            os.kill(nearest, signal.SIGHUP)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        os.killpg(os.getpgid(pid), signal.SIGINT)
    else:
        os.kill(pid, getattr(signal, STOP_SIGNALS[mode]))
    _log("stop-sent", mode)


def _exec(arguments: list[str], environment: dict[str, str] | None = None) -> int:
    """Replace this process, or run and wait when the installer is stopped after."""
    environment = dict(os.environ) if environment is None else environment
    if KILL:
        import subprocess

        return subprocess.run(
            arguments, env=environment, check=False, preexec_fn=stoppable_child
        ).returncode
    # The command gets stops as it would run alone; one already waiting ends
    # this process here, before it runs.
    signal.pthread_sigmask(signal.SIG_SETMASK, ())
    os.execvpe(arguments[0], arguments, environment)
    return 1


def _hold_then_stop(command: list[str], environment: dict[str, str], ready: Path) -> int:
    """The lock helper runs in the background until the installer ends it;
    stop the installer once the helper holds the locks, and end with it.

    The installer goes on meanwhile and may end this helper before the stop
    is sent; that stop is deferred until this one has gone out, so the case
    always stops the installer."""
    import subprocess
    import time

    global KILL
    KILL = False
    helper = subprocess.Popen(command, env=environment, preexec_fn=stoppable_child)

    def finish(signum: int, frame: object) -> None:
        # Only unwind here; waiting inside a handler can deadlock on Popen's lock.
        raise SystemExit(128 + signum)

    try:
        deadline = time.monotonic() + STEP_LOCK_TIMEOUT_SECONDS
        while not ready.exists() and helper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if "stop-sent-late" in _rules():
            # Let the installer get as far as ending this helper first.
            late = time.monotonic() + 30
            while not signal.sigpending() & {signal.SIGTERM, signal.SIGHUP}:
                if time.monotonic() >= late:
                    break
                time.sleep(0.01)
        kill_installer(STOP)
        signal.signal(signal.SIGTERM, finish)
        signal.signal(signal.SIGHUP, finish)
        with stops_allowed():
            return helper.wait()
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        if helper.poll() is None:
            helper.terminate()
        helper.wait()


# The application's own list of writer locks, which installers must hold.
UPGRADE_GATES = TOOLS.parents[2] / "src/inky_bird_frame/upgrade_gates.py"


def writer_locks() -> tuple[str, ...]:
    import ast

    tree = ast.parse(UPGRADE_GATES.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "WRITER_LOCKS":
            value = ast.literal_eval(node.value)
            assert isinstance(value, dict)
            return tuple(value)
    raise AssertionError("upgrade_gates.py names no WRITER_LOCKS")


def _lock_held(lock: Path) -> bool:
    import fcntl

    if not lock.exists():
        return False
    with lock.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def _cycle_lock_held() -> bool:
    """Whether something holds every writer lock, as an installer must
    before it stops anything that writes."""
    state = Path(os.environ["FAKE_STATE_DIR"])
    return all(_lock_held(state / name) for name in writer_locks())


def _process_runtime(name: str) -> str:
    """The runtime a recorded service process runs, or ``none``."""
    process = _state("processes").get(name)
    return str(process["runtime"]) if isinstance(process, dict) else "none"


def runtime_tag(executable: str) -> str:
    """Name the runtime an executable path resolves to right now."""
    tag = Path(os.path.realpath(executable)).parent.parent / "tag"
    return tag.read_text().strip() if tag.exists() else "missing"


def resolve_runtime(executable: str) -> dict[str, object]:
    """Which code a command started from ``executable`` runs, and from where.

    A controller venv carries its runtime's tag; the display's shared venv
    runs the source its editable install names, or a regular install's copy.
    """
    venv = Path(executable).parent.parent
    site = site_packages(venv)
    source = editable_source(site)
    # A controller venv carries its tag; it runs from the venv directory itself.
    if (Path(os.path.realpath(venv)) / "tag").exists():
        tag, directory = runtime_tag(executable), Path(os.path.realpath(venv))
    # A regular install in site-packages is imported before any editable one.
    elif (site / "inky_bird_frame").is_dir():
        tag, directory = "regular", site / "inky_bird_frame"
    elif source is not None:
        tag_file = source / "runtime-tag"
        tag = tag_file.read_text().strip() if tag_file.exists() else "missing"
        directory = Path(os.path.realpath(source))
    else:
        tag, directory = runtime_tag(executable), Path(os.path.realpath(venv))
    inode = directory.stat().st_ino if directory.exists() else 0
    return {"runtime": tag, "directory": str(directory), "inode": inode}


def editable_source(site: Path) -> Path | None:
    """The source an editable install makes Python import: of the ``.pth``
    entries, in the order Python reads them, the first that holds the
    package, else the first (whose source is gone)."""
    sources = [Path(pth.read_text().split()[0]) for pth in sorted(site.glob("*.pth"))]
    found = [source for source in sources if (source / "inky_bird_frame").is_dir()]
    return next(iter(found or sources), None)


def distribution_name(project: Path) -> str:
    """The distribution a project builds: the last release's, or today's."""
    import re

    pyproject = project / "pyproject.toml"
    text = pyproject.read_text() if pyproject.exists() else ""
    found = re.search(r'^name = "([^"]+)"', text, re.MULTILINE)
    return found.group(1) if found else "inky-bird"


# The commands each distribution installs: the release had only the old name.
SCRIPTS = {"inky-bird": ("inky-bird", "inky-bird-frame"), "inky-bird-frame": ("inky-bird-frame",)}


# Fakes reach each other through PATH (sudo runs systemctl, ssh runs an
# installer); one that found itself again would nest without end, so every
# wrapper counts how deep it is and refuses past the limit.
DEPTH_LIMIT = 3
DEPTH_GUARD = f"""depth=$(( ${{FAKE_DEPTH:-0}} + 1 ))
if [ "$depth" -gt {DEPTH_LIMIT} ]; then
  echo "fake tools nested more than {DEPTH_LIMIT} deep" >&2
  exit 70
fi
export FAKE_DEPTH=$depth
"""


def wrapper(tool: str, *, own_path: bool = True) -> str:
    """A stand-in executable for ``tool``; ``own_path`` passes its path so a
    venv's fake knows which runtime it belongs to."""
    import shlex

    # Imported, not run as a script, so its compiled form is cached; -I keeps the
    # caller's directory and PYTHON* settings out of the fake's own imports.
    program = f"import sys; sys.path.insert(0, {str(TOOLS.parent)!r}); import fake_tools; "
    program += "raise SystemExit(fake_tools.main())"
    command = f"{shlex.quote(sys.executable)} -I -S -c {shlex.quote(program)} {tool}"
    own = ' "$0"' if own_path else ""
    return f'#!/bin/sh\n{DEPTH_GUARD}exec {command}{own} "$@"\n'


def build_venv(project: Path) -> None:
    """Create the synthetic venv ``uv sync`` would build for ``project``."""
    bin_dir = project / ".venv/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    scripts = SCRIPTS[distribution_name(project)]
    for name, tool in (("python", "venv-python"), *((script, "inky-bird") for script in scripts)):
        path = bin_dir / name
        path.write_text(wrapper(tool))
        path.chmod(0o755)
    tag = project / "src/runtime-tag"
    (project / ".venv/tag").write_text(tag.read_text() if tag.exists() else "missing\n")
    site = site_packages(project / ".venv")
    site.mkdir(parents=True, exist_ok=True)
    if os.environ.get("UV_NO_EDITABLE", "").lower() in ("1", "true"):
        # A regular install: the venv imports its own copy of the package.
        import shutil

        shutil.copytree(project / "src/inky_bird_frame", site / "inky_bird_frame")
        return
    # uv installs the project editable: the venv imports this source tree by
    # absolute path, wherever the venv itself is later moved.
    (site / "_inky_bird.pth").write_text(f"{os.path.abspath(project)}/src\n")


def uname(arguments: list[str]) -> int:
    print(os.environ["FAKE_UNAME"])
    return 0


def identity(arguments: list[str]) -> int:
    # A fixed non-root account, whoever runs pytest; FAKE_UID overrides the UID.
    if arguments == ["-u"]:
        print(os.environ.get("FAKE_UID", "1000"))
        return 0
    if arguments == ["-un"]:
        print("fixture")
        return 0
    print(f"unexpected id call: {arguments}", file=sys.stderr)
    return 64


def system_python(arguments: list[str]) -> int:
    if arguments and arguments[0].endswith("native_names.py"):
        import shlex

        for name in ("service_prefix", "app_dir", "support_dir", "config_path"):
            print(f"{name}={shlex.quote(os.environ['FAKE_LAYOUT_' + name.upper()])}")
        return 0
    helper = [index for index, item in enumerate(arguments) if item.endswith("system_service.py")]
    if helper:
        # The real helper, against the synthetic account database and file owners.
        program = f"import sys; sys.path.insert(0, {str(TOOLS.parent)!r}); import fake_tools; "
        program += "fake_tools.run_with_accounts(sys.argv[1:])"
        return _exec([sys.executable, "-I", "-S", "-c", program, *arguments[helper[0] :]])
    if any(argument.endswith(".venv.next") for argument in arguments):
        # Points the active link at a runtime: the switch, or a switch back.
        _log("switch")
    return _exec([sys.executable, *arguments])


def launchctl(arguments: list[str]) -> int:
    import plistlib

    loaded = _state("launchd")
    verb = arguments[0]
    if verb == "print":
        target = arguments[1]
        return 0 if not target.startswith("system/") and target.split("/")[-1] in loaded else 1
    if verb == "bootout":
        label = arguments[1].split("/")[-1]
        if label not in loaded:
            return 3
        held = "lock-held" if _cycle_lock_held() else "lock-free"
        _log("launchctl", "bootout", label, held)
        if (
            "agents-wait-for-locks" in _rules()
            and held == "lock-held"
            and _waits_for_locks_to_stop(label)
            and not _until_locks_free(STOP_TIMEOUT_SECONDS)
        ):
            # launchd waits its exit timeout, then kills the agent.
            _log("exit-timeout", label)
        _log("stopped", label, held, _process_runtime(label))
        if f"bootout:{label}" in _rules():
            return 5
        del loaded[label]
        _save("launchd", loaded)
        processes = _state("processes")
        processes.pop(label, None)
        _save("processes", processes)
        return 0
    if verb == "bootstrap":
        payload = plistlib.loads(Path(arguments[2]).read_bytes())
        label = payload["Label"]
        generation = payload.get("Comment", "unknown")
        if label in loaded:
            return 17
        if f"bootstrap-{generation}:{label.split('.')[-1]}" in _rules():
            _log("launchctl", "bootstrap-failed", label, generation)
            return 5
        process = resolve_runtime(payload["ProgramArguments"][0])
        tag = str(process["runtime"])
        loaded[label] = {
            "generation": generation,
            "runtime": tag,
            "run_at_load": payload.get("RunAtLoad"),
        }
        _save("launchd", loaded)
        processes = _state("processes")
        processes[label] = process
        _save("processes", processes)
        _log("launchctl", "bootstrap", label, generation, tag)
        return 0
    print(f"unexpected launchctl call: {arguments}", file=sys.stderr)
    return 64


def plutil(arguments: list[str]) -> int:
    import plistlib

    assert arguments[:3] == ["-replace", "RunAtLoad", "-bool"], arguments
    path = Path(arguments[4])
    payload = plistlib.loads(path.read_bytes())
    payload["RunAtLoad"] = arguments[3] == "true"
    path.write_bytes(plistlib.dumps(payload))
    return 0


def uv(arguments: list[str]) -> int:
    project = Path(arguments[arguments.index("--project") + 1])
    _as("uv")
    _log("uv", *arguments, "UV_PROJECT_ENVIRONMENT" in os.environ)
    if "uv" in _rules():
        (project / ".venv").mkdir(exist_ok=True)
        print("synthetic dependency resolution failure", file=sys.stderr)
        return 1
    build_venv(project)
    return 0


def _units_dir() -> Path:
    return Path(os.environ["FAKE_UNIT_DIR"])


def _record_process(unit: str, running: bool) -> None:
    """Remember which runtime a started service's executable resolved to."""
    processes = _state("processes")
    path = _units_dir() / unit
    executable = ""
    if running and path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("ExecStart="):
                executable = line.partition("=")[2].split()[0]
    if executable:
        processes[unit] = resolve_runtime(executable)
    else:
        processes.pop(unit, None)
    _save("processes", processes)


# How many state queries a cycle a timer starts is seen as running for.
CYCLE_POLLS = 3


def _oneshot(unit: str) -> bool:
    """A service that runs to its end: Type=oneshot without RemainAfterExit,
    never "active", "activating" while it runs."""
    return unit.endswith(".service") and not unit.endswith(
        ("-controller.service", "-scheduler.service")
    )


def _end_cycle(unit: str) -> None:
    busy = _state("busy")
    busy.pop(unit, None)
    _save("busy", busy)
    _record_process(unit, running=False)


def _start_cycle(timer: str) -> None:
    """A timer that starts may fire at once: its service runs, "activating"."""
    service = timer.removesuffix(".timer") + ".service"
    if not (_units_dir() / service).exists():
        return
    busy = _state("busy")
    busy[service] = CYCLE_POLLS
    _save("busy", busy)
    _record_process(service, running=True)


def _record_run(unit: str) -> None:
    """A oneshot service started and waited for: which version that run was."""
    path = _units_dir() / unit
    executable = ""
    for line in path.read_text().splitlines() if path.exists() else ():
        if line.startswith("ExecStart="):
            executable = line.partition("=")[2].split()[0]
    runs = _state("runs")
    ran = runs.setdefault(unit, [])
    assert isinstance(ran, list)
    ran.append(str(resolve_runtime(executable)["runtime"]) if executable else "missing")
    _save("runs", runs)


# How long a service manager waits for a stopping service before it kills it.
STOP_TIMEOUT_SECONDS = 2.0


def _waits_for_locks_to_stop(unit: str) -> bool:
    """Services whose own shutdown can need a writer lock: the scheduler
    records the end of the job it stops, the web service finishes requests."""
    return unit.endswith(("-scheduler.service", "-controller.service", ".scheduler", ".serve"))


def _until_locks_free(seconds: float) -> bool:
    import time

    deadline = time.monotonic() + seconds
    while _cycle_lock_held():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def systemctl(arguments: list[str]) -> int:
    units = _state("systemd")
    verb = arguments[0]
    names = [argument for argument in arguments[1:] if not argument.startswith("--")]
    unit = names[-1] if names else ""
    record = units.setdefault(unit, {"active": False, "enabled": False}) if unit else {}
    assert isinstance(record, dict)
    rules = _rules()
    if verb == "show":
        busy = _state("busy")
        stopping = _state("stopping")
        if "--property=ActiveState" in arguments and unit in stopping:
            # Signalled to stop, it finishes only once it can take the lock it waits for.
            if _cycle_lock_held():
                print("deactivating")
                return 0
            stopping.pop(unit)
            _save("stopping", stopping)
            _record_process(unit, running=False)
            print("inactive")
            return 0
        if "--property=ActiveState" in arguments:
            remaining = int(str(busy.get(unit, 0)))
            if remaining > 0 and _cycle_lock_held():
                # A job waiting for a writer lock the installer holds.
                print("activating")
            elif remaining > 0:
                busy[unit] = remaining - 1
                _save("busy", busy)
                _log("busy-poll", unit, remaining - 1)
                if remaining == 1 and _oneshot(unit):
                    # The cycle ends after this answer.
                    _record_process(unit, running=False)
                print("activating")
            else:
                print("active" if record["active"] else "inactive")
        elif "--property=LoadState" in arguments:
            print("loaded" if (_units_dir() / unit).exists() else "not-found")
        return 0
    if verb == "is-active":
        return 0 if record["active"] else 3
    if verb == "is-enabled":
        return 0 if record["enabled"] else 1
    _log("systemctl", *arguments)
    _as("systemctl", verb)
    if f"systemctl:{verb}:{unit}" in rules or f"systemctl:{verb}" in rules:
        return 1
    if verb == "daemon-reload":
        return 0
    exists = (_units_dir() / unit).exists()
    if verb in ("start", "restart", "enable") and not exists:
        return 5
    if verb == "disable" and not exists:
        return 1
    if verb in ("start", "restart", "disable"):
        stopping = _state("stopping")
        if stopping.pop(unit, None) is not None:
            _save("stopping", stopping)
    if verb in ("start", "restart") and _oneshot(unit):
        _record_run(unit)
        record["active"] = False
    elif verb in ("start", "restart"):
        # start leaves an already running process, and its runtime, alone.
        if verb == "restart" or not record["active"]:
            _record_process(unit, running=True)
            if unit.endswith(".timer"):
                _start_cycle(unit)
        record["active"] = True
    elif verb == "stop":
        held = "lock-held" if _cycle_lock_held() else "lock-free"
        if unit.endswith("scheduler.service") or unit.endswith("generate.service"):
            _log("lock-at-stop", unit, held)
        if unit.endswith(".service") and (record["active"] or unit in _state("busy")):
            _log("stopped", unit, held, _process_runtime(unit))
        if record["active"] and held == "lock-held" and _waits_for_locks_to_stop(unit):
            # Its shutdown waits for a lock the caller holds (the scheduler
            # records each job's end); systemd waits for it to exit.
            if "--no-block" in arguments:
                record["active"] = False
                stopping = _state("stopping")
                stopping[unit] = True
                _save("stopping", stopping)
                _save("systemd", units)
                return 0
            if not _until_locks_free(STOP_TIMEOUT_SECONDS):
                # systemd's stop timeout: it kills the service.
                _log("stop-timeout", unit)
        record["active"] = False
        _record_process(unit, running=False)
        if _oneshot(unit):
            _end_cycle(unit)
    elif verb == "enable":
        record["enabled"] = True
    elif verb == "disable":
        record["enabled"] = False
        if "--now" in arguments:
            record["active"] = False
            _record_process(unit, running=False)
            if _oneshot(unit):
                _end_cycle(unit)
    _save("systemd", units)
    return 0


def sudo(arguments: list[str]) -> int:
    if arguments[:1] == ["-n"]:
        arguments = arguments[1:]
    _log("sudo", *arguments)
    rules = _rules()
    expired = FAKE / "sudo-expired"
    if arguments == ["-v"]:
        if expired.exists():
            return 1
        # ``sudo-v-expires-after:N`` lets the first N validations succeed.
        counter = FAKE / "sudo-validations"
        count = int(counter.read_text()) if counter.exists() else 0
        _replace(counter, str(count + 1))
        limits = [
            int(rule.rpartition(":")[2])
            for rule in rules
            if rule.startswith("sudo-v-expires-after:")
        ]
        if limits and count >= limits[0]:
            expired.touch()
            return 1
        return 1 if "sudo:-v" in rules else 0
    expires_at = ":".join(arguments) in {
        rule.removeprefix("sudo-expires-at:")
        for rule in rules
        if rule.startswith("sudo-expires-at:")
    }
    if (
        expires_at
        or ("sudo-expires-at-install" in rules and arguments[0] == "install")
        or expired.exists()
    ):
        expired.touch()
        print("sudo: a password is required", file=sys.stderr)
        return 1
    if f"sudo:{arguments[0]}" in rules:
        print("sudo: a password is required", file=sys.stderr)
        return 1
    return _exec(arguments)


def rsync(arguments: list[str]) -> int:
    import shutil

    paths: list[str] = []
    skip = False
    for argument in arguments:
        if skip:
            skip = False
        elif argument == "-e":
            skip = True
        elif not argument.startswith("-"):
            paths.append(argument)
    source_text, target_text = (path.split(":", 1)[-1] for path in paths[-2:])
    _log("rsync", source_text, target_text)
    _as("rsync")
    if "rsync" in _rules():
        return 23
    source, target = Path(source_text), Path(target_text)
    if source.is_dir():
        if "--delete" in arguments and target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        destination = (
            target / source.name if target_text.endswith("/") or target.is_dir() else target
        )
        shutil.copy2(source, destination)
    return 0


def _remote_installers(group: int) -> list[int]:
    """The installer scripts running in ``group``, not their subshells."""
    import subprocess

    listing = subprocess.run(
        ["/bin/ps", "-ww", "-ax", "-o", "pid=", "-o", "ppid=", "-o", "pgid=", "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    members: dict[int, tuple[int, str]] = {}
    for line in listing.splitlines():
        pid, parent, pgid, *command = line.split(None, 3)
        if int(pgid) == group and command:
            members[int(pid)] = (int(parent), command[0])
    return [
        pid
        for pid, (parent, command) in members.items()
        if "/deploy/install-" in command and members.get(parent, (0, ""))[1] != command
    ]


def ssh(arguments: list[str]) -> int:
    """Run the command as the display would: in its own process group, which
    a Ctrl-C on this side never reaches. Stopping this end drops the
    connection: the installer running on the other end is told with a hangup,
    finishes its own cleanup, and this end then fails as ssh does; any other
    command there runs to its end."""
    import subprocess

    command = arguments[-1]
    _log("ssh", command)
    group: list[int] = []
    dropped: list[int] = []

    def hang_up() -> None:
        for pid in [installer for leader in group for installer in _remote_installers(leader)]:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGHUP)

    def drop(signum: int, frame: object) -> None:
        dropped.append(signum)
        hang_up()

    # Caught signals return to their defaults in the command it starts.
    for stop in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(stop, drop)
    remote = subprocess.Popen(
        [os.environ["FAKE_BASH"], "-c", command],
        stdin=sys.stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        process_group=0,
        preexec_fn=stoppable_child,
    )
    group.append(remote.pid)
    # The test run kills this group too if anything in it outlives the run.
    _append(FAKE / "groups", f"{remote.pid}\n")
    if dropped:
        hang_up()
    with stops_allowed():
        stdout, stderr = remote.communicate()
    sys.stderr.write(stderr)
    if dropped:
        return 255
    if (
        "ssh-stage-noise" in _rules()
        and command.startswith("bash -s --")
        and remote.returncode == 0
    ):
        stdout = "Welcome to the synthetic display\n" + stdout
    sys.stdout.write(stdout)
    return remote.returncode


def site_packages(venv: Path) -> Path:
    return venv / "lib/python3/site-packages"


def _python_path(venv: Path, runtime: Path) -> str:
    """Order imports the way a venv does: site-packages, then .pth entries."""
    entries: list[str] = []
    site = site_packages(venv)
    if site.is_dir():
        entries.append(str(site))
        for pth in sorted(site.glob("*.pth")):
            entries.extend(line for line in pth.read_text().splitlines() if line)
    if (runtime / "src").is_dir():
        entries.append(str(runtime / "src"))
    return os.pathsep.join(entries)


def _package_paths(venv: Path, name: str) -> list[Path]:
    """What a distribution put in site-packages: the renamed one a regular
    package or the release's editable path, today's its editable path."""
    site = site_packages(venv)
    if name == "inky-bird-frame":
        return [site / "inky_bird_frame", site / "_inky_bird_frame.pth"]
    return [site / "_inky_bird.pth"]


def _remove(venv: Path, name: str) -> None:
    import shutil

    for path in _package_paths(venv, name):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def _remove_distribution(venv: Path, removed: str, kept: str, backup: Path | None) -> int:
    """Remove one distribution's files, except the commands the other one also
    installed; with ``backup``, move them there behind a plan to put them back."""
    import shutil

    _log("remove-distribution", removed, kept)
    dists = _state("pip")
    gone = dists.pop(removed, None)

    # An install made elsewhere names no commands of its own.
    def scripts(dist: object) -> set[str]:
        return set(dist.get("scripts", ())) if isinstance(dist, dict) else set()

    paths = _package_paths(venv, removed)
    paths += [venv / "bin" / script for script in sorted(scripts(gone) - scripts(dists.get(kept)))]
    paths = [path for path in paths if path.exists() or path.is_symlink()]
    if backup is not None:
        backup.mkdir(exist_ok=True)
        plan = {
            "distribution": [removed, gone],
            "files": [[str(path), str(backup / str(number))] for number, path in enumerate(paths)],
        }
        _replace(backup / "plan.json", json.dumps(plan))
    _save("pip", dists)
    for number, path in enumerate(paths):
        if backup is not None:
            shutil.move(str(path), str(backup / str(number)))
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    return 0


def _restore_distribution(venv: Path, backup: Path) -> int:
    """Put back what ``_remove_distribution`` moved aside, as its plan lists."""
    import shutil

    plan_path = backup / "plan.json"
    _log("restore-distribution", backup.name)
    if not plan_path.exists():
        return 0
    plan = json.loads(plan_path.read_text())
    for original, saved in plan["files"]:
        if Path(saved).exists() or Path(saved).is_symlink():
            Path(original).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(saved, original)
    name, record = plan["distribution"]
    if record is not None:
        dists = _state("pip")
        dists[name] = record
        _save("pip", dists)
    return 0


def _pip(venv: Path, arguments: list[str]) -> int:
    dists = _state("pip")
    _log("pip", *arguments)
    verb = arguments[0]
    if verb == "show":
        return 0 if arguments[-1] in dists else 1
    if verb == "install":
        if "pip-install" in _rules():
            print("synthetic index failure", file=sys.stderr)
            return 1
        source = arguments[-1].split("[", 1)[0]
        name = distribution_name(Path(source))
        dists[name] = {"source": source, "scripts": list(SCRIPTS[name])}
        _save("pip", dists)
        site = site_packages(venv)
        site.mkdir(parents=True, exist_ok=True)
        (site / f"_{name.replace('-', '_')}.pth").write_text(f"{source}/src\n")
        for script in SCRIPTS[name]:
            path = venv / "bin" / script
            path.write_text(wrapper("inky-bird"))
            path.chmod(0o755)
        return 0
    if verb == "uninstall":
        dists.pop(arguments[-1], None)
        _save("pip", dists)
        _remove(venv, arguments[-1])
        return 0
    return 0


def _executable(source: str, prefix: str, mode: str = "") -> str:
    """The command a controller program's services start, as it names it:
    a fixed name, the service prefix, or for a system service's units
    (``render``) the command its program picks."""
    import re

    named = re.search(r'executable = app_dir / "\.venv/bin/([\w-]+)"', source)
    if named is not None:
        return named.group(1)
    picked = 'command_name = "inky-bird" if mode == "render" else service_prefix'
    if picked in source and 'executable = app_dir / ".venv/bin" / command_name' in source:
        return "inky-bird" if mode == "render" else prefix
    assert 'executable = app_dir / ".venv/bin" / service_prefix' in source, "unknown executable"
    return prefix


def venv_python(executable: str, arguments: list[str]) -> int:
    venv = Path(executable).parent.parent
    runtime = Path(os.path.realpath(executable)).parent.parent.parent
    tag = runtime_tag(executable)
    rules = _rules()
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("PYTHON")
    }
    environment["PYTHONPATH"] = _python_path(venv, runtime)
    if arguments[:2] == ["-m", "pip"]:
        return _pip(venv, arguments[2:])
    if arguments[:1] == ["-c"]:
        # Inline programs must run isolated (-I) so the caller's directory and
        # PYTHON* variables cannot shadow the installed package.
        print("inline program run without -I", file=sys.stderr)
        return 64
    if arguments[:1] and arguments[0].endswith(".py"):
        print("helper script run without -I", file=sys.stderr)
        return 64
    if arguments[:1] == ["-I"]:
        arguments = arguments[1:]
    if arguments[:1] and arguments[0].endswith(".py"):
        return _upgrade_gate(tag, venv, arguments[0], arguments[1:], environment)
    if arguments[:2] == ["-m", "inky_bird_frame.upgrade_gates"]:
        return _upgrade_gate(tag, venv, None, arguments[2:], environment)
    if arguments[:1] == ["-c"]:
        program = arguments[1]
        if "distribution(" in program:
            removed, kept = arguments[2:4]
            backup = Path(arguments[4]) if len(arguments) > 4 else None
            return _remove_distribution(venv, removed, kept, backup)
        if "plan.json" in program:
            return _restore_distribution(venv, Path(arguments[2]))
        _log("python-c", tag)
        # -S keeps the test interpreter's own site-packages out of the import path.
        command = [sys.executable, "-S", "-c", program, *arguments[2:]]
        if KILL and "controller.lock" in program:
            return _hold_then_stop(command, environment, Path(arguments[3]))
        # Replacing this process keeps the PID the installer waits on or kills.
        return _exec(command, environment)
    if arguments[:1] == ["-"]:
        source = sys.stdin.read()
        _log("heredoc", tag)
        # A system service checks and syncs as its account, then renders as root.
        mode = arguments[7] if len(arguments) > 7 else ""
        _as("heredoc", mode or "both")
        # The real program takes the catalog lock unless told the installer holds it.
        if "catalog_state_lock" in source and _cycle_lock_held() and arguments[-1] != "true":
            print("waiting forever for the catalog lock the installer holds", file=sys.stderr)
            return 75
        if f"{tag}:heredoc" in rules:
            print("synthetic configuration rejection", file=sys.stderr)
            return 1
        # The release named its services before the name was chosen at install.
        prefix = os.environ.get("INKY_BIRD_SERVICE_PREFIX", "inky-bird-frame")
        # The macOS program takes one plist per agent, then the source and app.
        plists = len([argument for argument in arguments if argument.endswith(".plist")])
        if "sync_public_catalog" in source and mode != "render":
            # The real program syncs the catalog from the install source first.
            root = Path(arguments[plists + 1] if "plistlib" in source else arguments[2])
            if not (root / "catalog").is_dir():
                print("synthetic catalog source is missing", file=sys.stderr)
                return 1
        if mode == "sync":
            return 0
        if "plistlib" in source or "controller_systemd_units" in source:
            executable = _executable(source, prefix, mode)
        if "plistlib" in source:
            import plistlib

            paths = arguments[1 : plists + 1]
            app_dir = Path(arguments[plists + 2])
            order = (
                "serve",
                "refresh",
                "generate",
                "catalog-publish",
                "notifications",
                "scheduler",
            )
            wanted = os.environ.get("FAKE_AGENTS", "serve refresh").split()
            if plists == 5:
                # The release always rendered these three agents.
                wanted += ["serve", "refresh", "generate"]
            for label, path in zip(order[: len(paths)], paths, strict=True):
                if label in wanted:
                    payload = {
                        "Label": f"com.{prefix}.{label}",
                        "ProgramArguments": [
                            str(app_dir / ".venv/bin" / executable),
                            label,
                        ],
                        "RunAtLoad": True,
                        "Comment": "new",
                        # Which runtime rendered it, so a definition left
                        # from the new version differs from the one it replaced.
                        "RenderedBy": tag,
                    }
                    Path(path).write_bytes(plistlib.dumps(payload))
                else:
                    Path(path).unlink(missing_ok=True)
        elif "controller_systemd_units" in source:
            unit_dir, app_dir, user = Path(arguments[1]), Path(arguments[3]), arguments[6]
            for name in os.environ["FAKE_UNITS"].split():
                content = f"[Unit]\n# new\n# rendered by {tag}\n"
                if mode == "render":
                    content = f"{SYSTEM_MARKER}\n{content}"
                if name.endswith(".service"):
                    content += (
                        f"[Service]\nUser={user}\nExecStart={app_dir}/.venv/bin/{executable}\n"
                    )
                    if mode == "render":
                        group, environment_file = arguments[8:10]
                        content += (
                            f"Group={group}\nEnvironment=INKY_BIRD_ENV_FILE={environment_file}\n"
                        )
                (unit_dir / f"{prefix}-{name}").write_text(content)
        elif "display_systemd_units" in source:
            import subprocess

            # Render with whichever package the venv's import order selects.
            return subprocess.run(
                [sys.executable, "-S", "-", *arguments[1:]],
                input=source,
                env=environment,
                text=True,
                check=False,
                preexec_fn=stoppable_child,
            ).returncode
        return 0
    return 0


def _upgrade_gate(
    tag: str, venv: Path, script: str | None, arguments: list[str], environment: dict[str, str]
) -> int:
    """The application's upgrade helper, run by path or (``script`` None) as
    the module the venv imports: it runs for real against the synthetic
    configuration, except the web service health check, which compares the
    package the helper is from with the one the serving process imports, as
    the real one compares their runtime IDs."""
    command = arguments[0]
    _log("upgrade-gate", command, tag)
    _as("upgrade-gate", command)
    if command == "check-serve":
        helper = Path(script).resolve().parent if script else imported_package(venv)
        return _check_serve(helper)
    # -P keeps the helper's own directory, which shadows standard modules, off the path.
    module = [script] if script else ["-m", "inky_bird_frame.upgrade_gates"]
    real = [sys.executable, "-S", "-P", *module, *arguments]
    if KILL and command == "hold-writer-locks":
        return _hold_then_stop(real, environment, Path(arguments[2]))
    if command == "hold-writer-locks" and "holder-ignores-term" in _rules():
        import signal

        # Ignored across exec: the helper outlives an installer's polite stop.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    return _exec(real, environment)


def imported_package(venv: Path) -> Path:
    """The package directory a venv's Python imports ``inky_bird_frame`` from."""
    site = site_packages(venv)
    if (site / "inky_bird_frame").is_dir():
        return (site / "inky_bird_frame").resolve()
    source = editable_source(site)
    return (source / "inky_bird_frame").resolve() if source else Path("/nonexistent")


def _check_serve(helper: Path) -> int:
    """``health-fail`` fails the check and ``health-hang`` never answers."""
    import time

    rules = _rules()
    if "health-hang" in rules:
        with stops_allowed():
            time.sleep(3600)
    prefix = os.environ.get("INKY_BIRD_SERVICE_PREFIX")
    # The account's scrubbed environment names no prefix: any web service.
    names = (
        [name for name in _state("processes") if name.endswith("-controller.service")]
        if prefix is None
        else [f"com.{prefix}.serve", f"{prefix}-controller.service"]
    )
    serving = set()
    for name in names:
        process = _state("processes").get(name)
        if isinstance(process, dict):
            serving.add(imported_package(Path(str(process["directory"]))))
    if "health-fail" in rules or helper not in serving:
        print("The web service health check failed: synthetic failure.", file=sys.stderr)
        return 1
    return 0


# What an installer tells its --stopped-hook.
HOOK_VARIABLES = (
    "INKY_BIRD_HOOK_PREVIOUS_RUNTIME",
    "INKY_BIRD_HOOK_NEW_RUNTIME",
    "INKY_BIRD_HOOK_CONFIG",
    "INKY_BIRD_HOOK_STATE_DIR",
)


def stopped_hook(arguments: list[str]) -> int:
    """An owner's --stopped-hook: record what it finds, then succeed, or as
    the rules say fail (``hook-fail``), die (``hook-killed``) or never end
    (``hook-hang``)."""
    import time

    rules = _rules()
    _as("stopped-hook")
    held = "lock-held" if _cycle_lock_held() else "lock-free"
    venv = Path(os.environ["FAKE_LAYOUT_APP_DIR"]) / ".venv"
    tag = Path(os.path.realpath(venv)) / "tag"
    active = tag.read_text().strip() if tag.exists() else "none"
    systemd = _state("systemd")
    running = {
        name
        for name, record in systemd.items()
        if isinstance(record, dict) and record.get("active")
    }
    running |= set(_state("launchd")) | set(_state("busy"))
    given = {name: os.environ.get(name, "") for name in HOOK_VARIABLES}
    # The shell that runs this fake sets PWD, SHLVL and _, its wrapper exports
    # FAKE*, Python sets LC_CTYPE for a C locale, and macOS adds its own.
    shell = {"PATH", "HOME", "PWD", "OLDPWD", "SHLVL", "_", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    extra = sorted(
        name
        for name in set(os.environ) - set(HOOK_VARIABLES) - shell
        if not name.startswith("FAKE")
    )
    _log(
        "stopped-hook",
        held,
        active,
        json.dumps(sorted(running)),
        json.dumps(given),
        json.dumps(extra),
    )
    if "hook-killed" in rules:
        os.kill(os.getpid(), signal.SIGKILL)
    if "hook-hang" in rules:
        with stops_allowed():
            time.sleep(3600)
    if "hook-fail" in rules:
        print("synthetic hook failure", file=sys.stderr)
        return 7
    print("synthetic hook output")
    return 0


def inky_bird(executable: str, arguments: list[str]) -> int:
    tag = runtime_tag(executable)
    _log("inky-bird", tag, *arguments)
    _as("inky-bird", arguments[0])
    # These take writer locks themselves: run while an installer holds them,
    # they would wait for it forever.
    if arguments[:1] in (["refresh"], ["catalog-publish"]) and _cycle_lock_held():
        print("waiting forever for a writer lock the installer holds", file=sys.stderr)
        return 75
    rules = _rules()
    # ``tag:command`` fails as a configuration error; ``tag:command:type`` names the type.
    kinds = [rule.rpartition(":")[2] for rule in rules if rule.startswith(f"{tag}:{arguments[0]}:")]
    if f"{tag}:{arguments[0]}" in rules or kinds:
        if "unreadable" in kinds:
            print("Traceback (most recent call last):")
            return 1
        # The real CLI reports failures as a JSON envelope on stdout.
        kind = kinds[0] if kinds else "configuration"
        error = {"type": kind, "message": f"synthetic {arguments[0]} failure"}
        print(json.dumps({"ok": False, "error": error, "schema_version": 1}, indent=2))
        return 1
    return 0


def _is_query(tool: str, arguments: list[str]) -> bool:
    """Calls that only read state; failing one would just be a different answer."""
    # setpriv only runs its command as the account; that command is the step.
    if tool in ("uname", "id", "pgrep", "setpriv"):
        return True
    if tool == "launchctl":
        return arguments[:1] == ["print"]
    if tool == "systemctl":
        return arguments[:1] in (["show"], ["is-active"], ["is-enabled"])
    if tool == "venv-python":
        return arguments[1:4] == ["-m", "pip", "show"]
    return False


def _injected_failure(tool: str, arguments: list[str]) -> int:
    if tool == "sudo":
        print("sudo: a password is required", file=sys.stderr)
        return 1
    print(f"{tool}: synthetic failure", file=sys.stderr)
    if tool == "inky-bird":
        error = {"type": "internal", "message": "synthetic failure"}
        print(json.dumps({"ok": False, "error": error, "schema_version": 1}))
    return {"rsync": 23, "ssh": 255, "launchctl": 5}.get(tool, 1)


# The first line of every unit a system-service installation renders.
SYSTEM_MARKER = "# Inky Bird system service"
# Fixed group IDs for the administrator groups an account may be put in.
ADMIN_GROUP_IDS = {"sudo": 27, "wheel": 10, "admin": 80}


def _accounts() -> dict[str, dict[str, object]]:
    """The synthetic account database: name to uid, gid, group, home, shell, groups."""
    accounts = _state("accounts")
    return {name: entry for name, entry in accounts.items() if isinstance(entry, dict)}


def _account_by_uid(uid: int) -> tuple[str, dict[str, object]] | None:
    for name, entry in _accounts().items():
        if entry["uid"] == uid:
            return name, entry
    return None


def _group_ids() -> dict[str, int]:
    groups = dict(ADMIN_GROUP_IDS)
    for entry in _accounts().values():
        groups[str(entry["group"])] = int(str(entry["gid"]))
    return groups


def _seen(
    result: os.stat_result,
    path: str | bytes | os.PathLike[str],
    owners: dict[str, object],
) -> os.stat_result:
    """A file's status as the synthetic host has it: the owner ``chown``
    recorded; real modes inside the machine's folder, and outside it (the
    test's private temp folders, the checkout pytest runs in) the modes of a
    host's system folders, which everyone can read and search."""
    given = os.path.abspath(os.fsdecode(path))
    fields = list(result[:10])
    recorded = owners.get(given)
    if isinstance(recorded, dict):
        fields[4], fields[5] = recorded["uid"], recorded["gid"]
    if not any(given.startswith(machine + "/") for machine in _machine_roots()):
        fields[0] = int(fields[0]) | 0o755
    return os.stat_result(fields)


MACHINE_ROOTS: tuple[str, ...] = ()


def _machine_roots() -> tuple[str, ...]:
    """The machine's own folder, as given and resolved; found once, before
    the helper's stat functions are replaced (resolving uses them)."""
    global MACHINE_ROOTS
    if not MACHINE_ROOTS:
        MACHINE_ROOTS = (str(FAKE.parent), os.path.realpath(FAKE.parent))
    return MACHINE_ROOTS


def _account_may(path: str, entry: dict[str, object], wanted: int) -> bool:
    """Whether the account has ``wanted`` (r=4, w=2, x=1) on ``path``, by its
    real mode bits, as the kernel chooses owner, group or other."""
    info = _seen(os.stat(path), path, _state("owners"))
    listed = entry.get("groups", [])
    assert isinstance(listed, list)
    groups = {int(str(entry["gid"])), *(_group_ids()[str(group)] for group in listed)}
    if info.st_uid == entry["uid"]:
        bits = info.st_mode >> 6
    elif info.st_gid in groups:
        bits = info.st_mode >> 3
    else:
        bits = info.st_mode
    return bits & wanted == wanted


def _denied(path: str, entry: dict[str, object], *, run: bool) -> str | None:
    """The first folder on the way to ``path`` the account cannot search, or
    with ``run`` the file itself if it cannot execute it; None when allowed."""
    real = os.path.realpath(path)
    for folder in reversed([os.path.dirname(real), *Path(real).parents]):
        if not _account_may(str(folder), entry, 1):
            return str(folder)
    if run and not _account_may(real, entry, 1):
        return real
    return None


def run_with_accounts(arguments: list[str]) -> None:
    """Run a helper script as the host would, with the synthetic accounts in
    the account database and the owners ``chown`` recorded on the files."""
    import grp
    import pwd
    import runpy

    real_getpwnam, real_getgrgid = pwd.getpwnam, grp.getgrgid
    real_lstat, real_stat = os.lstat, os.stat
    _machine_roots()
    owners = _state("owners")
    accounts = _accounts()
    groups = _group_ids()

    def getpwnam(name: str) -> pwd.struct_passwd:
        entry = accounts.get(name)
        if entry is None:
            return real_getpwnam(name)
        fields = (name, "x", entry["uid"], entry["gid"], "", entry["home"], entry["shell"])
        return pwd.struct_passwd(fields)

    def getgrgid(id: int) -> grp.struct_group:
        for name, number in groups.items():
            if number == id:
                return grp.struct_group((name, "x", id, []))
        return real_getgrgid(id)

    def getgrouplist(name: str, gid: int) -> list[int]:
        entry = accounts.get(name)
        listed = entry.get("groups", []) if entry is not None else []
        assert isinstance(listed, list)
        return [gid, *(groups[str(group)] for group in listed)]

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        return _seen(real_lstat(path), path, owners)

    def stat(path: str | os.PathLike[str]) -> os.stat_result:
        return _seen(real_stat(path), path, owners)

    # The synthetic account database, and owners recorded by the fake chown.
    pwd.getpwnam = getpwnam
    grp.getgrgid = getgrgid
    os.getgrouplist = getgrouplist
    os.lstat = lstat  # type: ignore[assignment]  # stat_result keyword options are not used
    os.stat = stat  # type: ignore[assignment]
    sys.argv = arguments
    runpy.run_path(arguments[0], run_name="__main__")


def setpriv(arguments: list[str]) -> int:
    """Run a command as a synthetic account: only with its supplementary
    groups and a scrubbed environment (``env -i``), which it then sees alone,
    beside the fakes' own settings, and only a program the account can reach
    and run (exit 126, as a shell reports Permission denied)."""
    import shutil

    separator = arguments.index("--")
    options, command = arguments[:separator], arguments[separator + 1 :]
    values = dict(option.split("=", 1) for option in options if "=" in option)
    found = _account_by_uid(int(values.get("--reuid", "-1")))
    if (
        found is None
        or "--init-groups" not in options
        or values.get("--regid") != str(found[1]["gid"])
        or command[:2] != ["env", "-i"]
    ):
        print(f"setpriv: refused to run {command[:2]} as {values}", file=sys.stderr)
        return 64
    given: dict[str, str] = {}
    rest = command[2:]
    while rest and "=" in rest[0] and not rest[0].startswith("/"):
        name, value = rest.pop(0).split("=", 1)
        given[name] = value
    environment = {name: value for name, value in os.environ.items() if name.startswith("FAKE")}
    environment.update(given, FAKE_IDENTITY="account", FAKE_UID=str(found[1]["uid"]))
    # A helper started in the background leads its own process group, so the
    # installer can stop it by group alone.
    leader = "leader" if os.getpgid(0) == os.getpid() else "member"
    _log("as-account", found[0], json.dumps(sorted(given)), rest[0], *rest[1:5], leader)
    executable = shutil.which(rest[0], path=given.get("PATH", "")) or rest[0]
    # The account must be able to reach and run the program, and to reach the
    # folder it starts in, by the real modes of the files: as the kernel would.
    for path, run in ((executable, True), (os.getcwd(), False)):
        denied = _denied(path, found[1], run=run)
        if denied is not None:
            print(f"setpriv: {rest[0]}: Permission denied ({denied})", file=sys.stderr)
            return 126
    return _exec([executable, *rest[1:]], environment)


def chown(arguments: list[str]) -> int:
    """Give entries to a synthetic account, one at a time, never recursively."""
    if any(argument.startswith("-") for argument in arguments):
        print(f"chown: refused {arguments}: one entry at a time", file=sys.stderr)
        return 64
    owner, *paths = arguments
    entry = _accounts().get(owner.partition(":")[0])
    if entry is None or not paths or not all(os.path.lexists(path) for path in paths):
        print(f"chown: cannot give {paths} to {owner}", file=sys.stderr)
        return 1
    _as("chown")
    owners = _state("owners")
    for path in paths:
        owners[os.path.abspath(path)] = {"uid": entry["uid"], "gid": entry["gid"]}
    _save("owners", owners)
    return 0


def pgrep(arguments: list[str]) -> int:
    """The processes of one account: its services that run now, and with the
    rule ``account-process`` one of its own that never stops."""
    uid = int(arguments[arguments.index("-u") + 1])
    accounts = _accounts()
    found = []
    running = sorted({*_state("processes"), *_state("busy")})
    for number, unit in enumerate(running):
        path = _units_dir() / unit
        users = [
            line.partition("=")[2]
            for line in (path.read_text().splitlines() if path.exists() else ())
            if line.startswith("User=")
        ]
        entry = accounts.get(users[-1]) if users else None
        if entry is not None and entry["uid"] == uid:
            found.append(40000 + number)
    if "account-process" in _rules() and _account_by_uid(uid) is not None:
        found.append(49999)
    for pid in found:
        print(pid)
    return 0 if found else 1


def run() -> int:
    global KILL, STOP
    tool, arguments = sys.argv[1], sys.argv[2:]
    if tool == "build-venv":
        build_venv(Path(arguments[0]))
        return 0
    if tool == "kill-installer":
        kill_installer(*arguments)
        return 0
    recorded = arguments[1:] if tool in ("venv-python", "inky-bird") else arguments
    injected = step(tool, recorded, action=not _is_query(tool, arguments))
    if injected == "fail":
        return _injected_failure(tool, arguments)
    KILL = injected in STOP_SIGNALS
    STOP = injected if KILL else "kill"
    if tool == "venv-python":
        status = venv_python(arguments[0], arguments[1:])
    elif tool == "inky-bird":
        status = inky_bird(arguments[0], arguments[1:])
    else:
        handlers = {
            "uname": uname,
            "id": identity,
            "python3": system_python,
            "launchctl": launchctl,
            "plutil": plutil,
            "uv": uv,
            "systemctl": systemctl,
            "sudo": sudo,
            "rsync": rsync,
            "ssh": ssh,
            "stopped-hook": stopped_hook,
            "setpriv": setpriv,
            "chown": chown,
            "pgrep": pgrep,
        }
        status = handlers[tool](arguments)
    if KILL:
        kill_installer(STOP)
    return status


def main() -> int:
    """Each call runs whole: what it changes and logs, and a stop it injects,
    complete before a stop that reached it meanwhile ends it, as the real
    command's effect would; then it dies of that stop."""
    try:
        with deferred_stops():
            return run()
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
