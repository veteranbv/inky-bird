"""Synthetic service managers and package tools for installer behaviour tests.

Every tool records its call in ``$FAKE/log`` and keeps its state under
``$FAKE``. Lines in ``$FAKE/fail`` name the calls that should fail. Nothing
here contacts a network, a real service manager or a real package index.

Every call is also numbered in ``$FAKE/steps``. Each line of ``$FAKE/inject``
(``<step> <mode>``) makes the call with that number fail, makes it and every later sudo
call fail (``expire``), or stops the installer once it completes: SIGTERM
(``kill``), SIGHUP (``hangup``) or SIGINT to its whole process group
(``interrupt``). Status queries are numbered but never injected.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Every installer step starts a fresh interpreter; importing what only some
# tools need where they use it takes about a quarter off each call.

FAKE = Path(os.environ.get("FAKE", "/nonexistent"))
TOOLS = Path(__file__).resolve()


def _rules() -> set[str]:
    path = FAKE / "fail"
    return set(path.read_text().split()) if path.exists() else set()


def _log(*entry: object) -> None:
    with (FAKE / "log").open("a") as handle:
        handle.write(json.dumps([str(item) for item in entry]) + "\n")


def _state(name: str) -> dict[str, object]:
    path = FAKE / f"{name}.json"
    loaded: dict[str, object] = json.loads(path.read_text()) if path.exists() else {}
    return loaded


def _save(name: str, value: dict[str, object]) -> None:
    (FAKE / f"{name}.json").write_text(json.dumps(value, sort_keys=True))


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
            return lock
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.001)


def step(tool: str, arguments: list[str], *, action: bool) -> str:
    """Number this call and return what the matrix injects at it, if anything."""
    steps = FAKE / "steps"
    if not FAKE.is_dir():
        return ""
    lock = _step_lock()
    try:
        index = len(steps.read_text().splitlines()) if steps.exists() else 0
        with steps.open("a") as handle:
            kind = "action" if action else "query"
            handle.write("\t".join([str(index), kind, tool, *arguments]).replace("\n", " ") + "\n")
    finally:
        lock.rmdir()
    inject = FAKE / "inject"
    if not action or not inject.exists():
        return ""
    # One line per injected fault; a second one lands in the first one's cleanup.
    for line in inject.read_text().splitlines():
        at_text, mode = line.split()
        at = int(at_text)
        if mode == "expire" and tool == "sudo" and index >= at:
            (FAKE / "sudo-expired").touch()
            return "fail"
        if index == at:
            return "fail" if mode == "expire" else mode
    return ""


def _installer_pid() -> int | None:
    """The nearest installer script above this call, not one of its subshells."""

    import subprocess

    def parent_and_command(pid: int) -> tuple[int, str]:
        output = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not output:
            return 0, ""
        parent, _, command = output.partition(" ")
        return int(parent), command.strip()

    pid = os.getppid()
    while pid > 1:
        parent, command = parent_and_command(pid)
        if "/deploy/install-" in command or "/deploy/uninstall-" in command:
            while parent > 1 and parent_and_command(parent)[1] == command:
                pid = parent
                parent = parent_and_command(pid)[0]
            return pid
        pid = parent
    return None


STOP_SIGNALS = {"kill": "SIGTERM", "hangup": "SIGHUP", "interrupt": "SIGINT"}


def kill_installer(mode: str = "kill") -> None:
    """Stop the installer: TERM or HUP to the script itself, or INT to its whole
    process group as Ctrl-C does. This call ignores the interrupt it sends."""
    import signal

    pid = _installer_pid()
    if pid is None:
        return
    if mode == "interrupt":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        os.killpg(os.getpgid(pid), signal.SIGINT)
    else:
        os.kill(pid, getattr(signal, STOP_SIGNALS[mode]))


def _exec(arguments: list[str], environment: dict[str, str] | None = None) -> int:
    """Replace this process, or run and wait when the installer is stopped after."""
    environment = dict(os.environ) if environment is None else environment
    if KILL:
        import subprocess

        return subprocess.run(arguments, env=environment, check=False).returncode
    os.execvpe(arguments[0], arguments, environment)
    return 1


def _hold_then_stop(command: list[str], environment: dict[str, str], ready: Path) -> int:
    """The cycle lock helper runs in the background until the installer ends it;
    stop the installer once the helper holds the lock, and end with it."""
    import signal
    import subprocess
    import time

    global KILL
    KILL = False
    helper = subprocess.Popen(command, env=environment)

    def finish(signum: int, frame: object) -> None:
        # Only unwind here; waiting inside a handler can deadlock on Popen's lock.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, finish)
    try:
        deadline = time.monotonic() + STEP_LOCK_TIMEOUT_SECONDS
        while not ready.exists() and helper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        kill_installer(STOP)
        return helper.wait()
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if helper.poll() is None:
            helper.terminate()
        helper.wait()


def _cycle_lock_held() -> bool:
    import fcntl

    lock = Path(os.environ["FAKE_STATE_DIR"]) / "controller.lock"
    if not lock.exists():
        return False
    with lock.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


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
    pth = site / "_inky_bird.pth"
    # A controller venv carries its tag; it runs from the venv directory itself.
    if (Path(os.path.realpath(venv)) / "tag").exists():
        tag, directory = runtime_tag(executable), Path(os.path.realpath(venv))
    # A regular install in site-packages is imported before any editable one.
    elif (site / "inky_bird_frame").is_dir():
        tag, directory = "regular", site / "inky_bird_frame"
    elif pth.exists():
        source = Path(pth.read_text().split()[0])
        tag_file = source / "runtime-tag"
        tag = tag_file.read_text().strip() if tag_file.exists() else "missing"
        directory = Path(os.path.realpath(source))
    else:
        tag, directory = runtime_tag(executable), Path(os.path.realpath(venv))
    inode = directory.stat().st_ino if directory.exists() else 0
    return {"runtime": tag, "directory": str(directory), "inode": inode}


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
    for name, tool in (("python", "venv-python"), ("inky-bird", "inky-bird")):
        path = bin_dir / name
        path.write_text(wrapper(tool))
        path.chmod(0o755)
    tag = project / "src/runtime-tag"
    (project / ".venv/tag").write_text(tag.read_text() if tag.exists() else "missing\n")
    # uv installs the project editable: the venv imports this source tree by
    # absolute path, wherever the venv itself is later moved.
    site = site_packages(project / ".venv")
    site.mkdir(parents=True, exist_ok=True)
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
        _log("launchctl", "bootout", label, "lock-held" if _cycle_lock_held() else "lock-free")
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
        if "--property=ActiveState" in arguments:
            remaining = int(str(busy.get(unit, 0)))
            if remaining > 0:
                busy[unit] = remaining - 1
                _save("busy", busy)
                _log("busy-poll", unit, remaining - 1)
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
    if f"systemctl:{verb}:{unit}" in rules or f"systemctl:{verb}" in rules:
        return 1
    if verb == "daemon-reload":
        return 0
    exists = (_units_dir() / unit).exists()
    if verb in ("start", "restart", "enable") and not exists:
        return 5
    if verb == "disable" and not exists:
        return 1
    if verb in ("start", "restart"):
        # start leaves an already running process, and its runtime, alone.
        if verb == "restart" or not record["active"]:
            _record_process(unit, running=True)
        record["active"] = True
    elif verb == "stop":
        if unit.endswith("scheduler.service") or unit.endswith("generate.service"):
            _log("lock-at-stop", unit, "lock-held" if _cycle_lock_held() else "lock-free")
        record["active"] = False
        _record_process(unit, running=False)
    elif verb == "enable":
        record["enabled"] = True
    elif verb == "disable":
        record["enabled"] = False
        if "--now" in arguments:
            record["active"] = False
            _record_process(unit, running=False)
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
        counter.write_text(str(count + 1))
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


def ssh(arguments: list[str]) -> int:
    import subprocess

    command = arguments[-1]
    _log("ssh", command)
    result = subprocess.run(
        [os.environ["FAKE_BASH"], "-c", command],
        stdin=sys.stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stderr.write(result.stderr)
    output = result.stdout
    if (
        "ssh-stage-noise" in _rules()
        and command.startswith("bash -s --")
        and result.returncode == 0
    ):
        output = "Welcome to the synthetic display\n" + output
    sys.stdout.write(output)
    return result.returncode


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


def _remove(venv: Path, name: str) -> None:
    import shutil

    site = site_packages(venv)
    if name == "inky-bird-frame":
        shutil.rmtree(site / "inky_bird_frame", ignore_errors=True)
    else:
        (site / "_inky_bird.pth").unlink(missing_ok=True)


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
        dists["inky-bird"] = {"source": source}
        _save("pip", dists)
        site = site_packages(venv)
        site.mkdir(parents=True, exist_ok=True)
        (site / "_inky_bird.pth").write_text(f"{source}/src\n")
        return 0
    if verb == "uninstall":
        dists.pop(arguments[-1], None)
        _save("pip", dists)
        _remove(venv, arguments[-1])
        return 0
    return 0


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
    if arguments[:1] == ["-I"]:
        arguments = arguments[1:]
    if arguments[:1] == ["-c"]:
        program = arguments[1]
        if "distribution(" in program:
            removed, kept = arguments[2:4]
            _log("remove-distribution", removed, kept)
            dists = _state("pip")
            dists.pop(removed, None)
            _save("pip", dists)
            _remove(venv, removed)
            return 0
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
        if f"{tag}:heredoc" in rules:
            print("synthetic configuration rejection", file=sys.stderr)
            return 1
        prefix = os.environ["INKY_BIRD_SERVICE_PREFIX"]
        if "sync_public_catalog" in source:
            # The real program syncs the catalog from the install source first.
            root = Path(arguments[7] if "plistlib" in source else arguments[2])
            if not (root / "catalog").is_dir():
                print("synthetic catalog source is missing", file=sys.stderr)
                return 1
        if "plistlib" in source:
            import plistlib

            paths = arguments[1:7]
            app_dir = Path(arguments[8])
            order = (
                "serve",
                "refresh",
                "generate",
                "catalog-publish",
                "notifications",
                "scheduler",
            )
            wanted = os.environ.get("FAKE_AGENTS", "serve refresh").split()
            for label, path in zip(order, paths, strict=True):
                if label in wanted:
                    payload = {
                        "Label": f"com.{prefix}.{label}",
                        "ProgramArguments": [str(app_dir / ".venv/bin/inky-bird"), label],
                        "RunAtLoad": True,
                        "Comment": "new",
                    }
                    Path(path).write_bytes(plistlib.dumps(payload))
                else:
                    Path(path).unlink(missing_ok=True)
        elif "controller_systemd_units" in source:
            unit_dir, app_dir, user = Path(arguments[1]), Path(arguments[3]), arguments[6]
            for name in os.environ["FAKE_UNITS"].split():
                content = "[Unit]\n# new\n"
                if name.endswith(".service"):
                    content += f"[Service]\nUser={user}\nExecStart={app_dir}/.venv/bin/inky-bird\n"
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
            ).returncode
        return 0
    return 0


def inky_bird(executable: str, arguments: list[str]) -> int:
    tag = runtime_tag(executable)
    _log("inky-bird", tag, *arguments)
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
    if tool in ("uname", "id"):
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


def main() -> int:
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
        }
        status = handlers[tool](arguments)
    if KILL:
        kill_installer(STOP)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
