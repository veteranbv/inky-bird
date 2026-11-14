"""Synthetic service managers and package tools for installer behaviour tests.

Every tool records its call in ``$FAKE/log`` and keeps its state under
``$FAKE``. Lines in ``$FAKE/fail`` name the calls that should fail. Nothing
here contacts a network, a real service manager or a real package index.
"""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

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


def _cycle_lock_held() -> bool:
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


def wrapper(tool: str) -> str:
    """A venv executable that passes its own path so the fake knows its runtime."""
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(TOOLS))} {tool}"
    return f'#!/bin/sh\nexec {command} "$0" "$@"\n'


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


def uname(arguments: list[str]) -> int:
    print(os.environ["FAKE_UNAME"])
    return 0


def identity(arguments: list[str]) -> int:
    if arguments == ["-u"]:
        print(os.environ.get("FAKE_UID", str(os.getuid())))
        return 0
    if arguments == ["-un"]:
        print("fixture")
        return 0
    os.execv("/usr/bin/id", ["id", *arguments])
    return 1


def system_python(arguments: list[str]) -> int:
    if arguments and arguments[0].endswith("native_names.py"):
        for name in ("service_prefix", "app_dir", "support_dir", "config_path"):
            print(f"{name}={shlex.quote(os.environ['FAKE_LAYOUT_' + name.upper()])}")
        return 0
    os.execv(sys.executable, [sys.executable, *arguments])
    return 1


def launchctl(arguments: list[str]) -> int:
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
        tag = runtime_tag(payload["ProgramArguments"][0])
        loaded[label] = {
            "generation": generation,
            "runtime": tag,
            "run_at_load": payload.get("RunAtLoad"),
        }
        _save("launchd", loaded)
        _log("launchctl", "bootstrap", label, generation, tag)
        return 0
    print(f"unexpected launchctl call: {arguments}", file=sys.stderr)
    return 64


def plutil(arguments: list[str]) -> int:
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
        record["active"] = True
    elif verb == "stop":
        if unit.endswith("scheduler.service") or unit.endswith("generate.service"):
            _log("lock-at-stop", unit, "lock-held" if _cycle_lock_held() else "lock-free")
        record["active"] = False
    elif verb == "enable":
        record["enabled"] = True
    elif verb == "disable":
        record["enabled"] = False
        if "--now" in arguments:
            record["active"] = False
    _save("systemd", units)
    return 0


def sudo(arguments: list[str]) -> int:
    if arguments[:1] == ["-n"]:
        arguments = arguments[1:]
    _log("sudo", *arguments)
    rules = _rules()
    if arguments == ["-v"]:
        return 1 if "sudo:-v" in rules else 0
    expired = FAKE / "sudo-expired"
    if "sudo-expires-at-install" in rules and (arguments[0] == "install" or expired.exists()):
        expired.touch()
        print("sudo: a password is required", file=sys.stderr)
        return 1
    if f"sudo:{arguments[0]}" in rules:
        print("sudo: a password is required", file=sys.stderr)
        return 1
    os.execvp(arguments[0], arguments)
    return 1


def rsync(arguments: list[str]) -> int:
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
    command = arguments[-1]
    _log("ssh", command)
    result = subprocess.run(
        ["bash", "-c", command], stdin=sys.stdin, capture_output=True, text=True, check=False
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


def _pip(arguments: list[str]) -> int:
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
        return 0
    if verb == "uninstall":
        dists.pop(arguments[-1], None)
        _save("pip", dists)
        return 0
    return 0


def venv_python(executable: str, arguments: list[str]) -> int:
    runtime = Path(os.path.realpath(executable)).parent.parent.parent
    tag = runtime_tag(executable)
    rules = _rules()
    if arguments[:2] == ["-m", "pip"]:
        return _pip(arguments[2:])
    if arguments[:1] == ["-c"]:
        # Inline programs must run isolated (-I) so the caller's directory and
        # PYTHON* variables cannot shadow the installed package.
        print("inline program run without -I", file=sys.stderr)
        return 64
    if arguments[:2] == ["-I", "-c"]:
        arguments = arguments[1:]
        program = arguments[1]
        if "distribution(" in program:
            removed, kept = arguments[2:4]
            _log("remove-distribution", removed, kept)
            dists = _state("pip")
            dists.pop(removed, None)
            _save("pip", dists)
            return 0
        environment = {
            name: value for name, value in os.environ.items() if not name.startswith("PYTHON")
        }
        if (runtime / "src").is_dir():
            environment["PYTHONPATH"] = os.pathsep.join(
                filter(None, [str(runtime / "src"), environment.get("PYTHONPATH")])
            )
        _log("python-c", tag)
        return subprocess.run(
            [sys.executable, "-c", program, *arguments[2:]], env=environment, check=False
        ).returncode
    if arguments[:1] == ["-"]:
        source = sys.stdin.read()
        _log("heredoc", tag)
        if f"{tag}:heredoc" in rules:
            print("synthetic configuration rejection", file=sys.stderr)
            return 1
        prefix = os.environ["INKY_BIRD_SERVICE_PREFIX"]
        if "plistlib" in source:
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
            for suffix in ("service", "timer"):
                (Path(arguments[1]) / f"{prefix}-display.{suffix}").write_text("# new\n")
        return 0
    return 0


def inky_bird(executable: str, arguments: list[str]) -> int:
    tag = runtime_tag(executable)
    _log("inky-bird", tag, *arguments)
    if f"{tag}:{arguments[0]}" in _rules():
        # The real CLI reports failures as a JSON envelope on stdout.
        error = {"type": "configuration", "message": f"synthetic {arguments[0]} failure"}
        print(json.dumps({"ok": False, "error": error, "schema_version": 1}, indent=2))
        return 1
    return 0


def main() -> int:
    tool, arguments = sys.argv[1], sys.argv[2:]
    if tool == "venv-python":
        return venv_python(arguments[0], arguments[1:])
    if tool == "inky-bird":
        return inky_bird(arguments[0], arguments[1:])
    if tool == "build-venv":
        build_venv(Path(arguments[0]))
        return 0
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
    return handlers[tool](arguments)


if __name__ == "__main__":
    raise SystemExit(main())
