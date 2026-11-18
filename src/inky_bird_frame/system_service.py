"""Checks a root-run installer makes for a system-service installation.

A system-service installation runs as a dedicated account and lives under one
install root: ``<root>/app`` holds the code and is root's alone; ``config``,
``home``, ``state`` and ``logs`` belong to the account. The installer runs this
file with the host's Python 3.9+ before any managed environment exists, so it
is standard-library-only and independent of the application. It answers
with fixed keys and shell-quoted values; it never changes anything.

``preflight`` checks the account, the install root and the source checkout.
``contained`` checks a path root is about to delete, move or replace.
``verify-code`` checks that the account cannot change the code it runs.
``readable`` checks that the account can reach and read a path and all it holds.
``remove-code`` deletes the code under the install root, for an uninstall.
``run-as`` replaces itself with a command run as the account, as ``setpriv``
does on Linux: macOS has no setpriv, and ``sudo -u`` keeps its own process
between the installer and the command (and may give it a terminal of its own),
so the installer could not stop the command by its process group or know its
process ID.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

# Run by path, this directory would come first on the import path and its
# modules would shadow standard ones.
if sys.path and sys.path[0] and Path(sys.path[0]).resolve() == Path(__file__).resolve().parent:
    del sys.path[0]

# An account name doubles as the services' name prefix, so the default account
# keeps today's unit names and a second account cannot share them.
ACCOUNT_NAME = re.compile(r"inky-bird(-[a-z0-9]+)*")
MAX_ACCOUNT_NAME = 32
ADMIN_GROUPS = frozenset({"root", "sudo", "wheel", "admin"})
NOLOGIN_SHELLS = frozenset({"nologin", "false"})
CODE_DIRECTORY = "app"
ACCOUNT_DIRECTORIES = ("config", "home", "state", "logs")


@dataclass(frozen=True)
class Account:
    name: str
    uid: int
    gid: int
    group: str
    home: Path
    shell: str
    groups: frozenset[int]


def valid_account_name(name: str) -> bool:
    return len(name) <= MAX_ACCOUNT_NAME and ACCOUNT_NAME.fullmatch(name) is not None


def lookup_account(name: str) -> Account | None:
    try:
        entry = pwd.getpwnam(name)
        group = grp.getgrgid(entry.pw_gid).gr_name
    except KeyError:
        return None
    groups = frozenset(os.getgrouplist(name, entry.pw_gid))
    return Account(
        name,
        entry.pw_uid,
        entry.pw_gid,
        group,
        Path(entry.pw_dir),
        entry.pw_shell,
        groups,
    )


def account_problem(account: Account | None, install_root: Path) -> str | None:
    """The first reason the account cannot run an installation, or None."""
    if account is None:
        return "account-missing"
    if account.uid == 0 or account.gid == 0:
        return "account-root"
    names = set()
    for gid in account.groups:
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    if names & ADMIN_GROUPS:
        return "account-admin"
    if Path(account.shell).name not in NOLOGIN_SHELLS:
        return "account-shell"
    if os.path.realpath(account.home) != str(install_root / "home"):
        return "account-home"
    return None


def trusted(info: os.stat_result) -> bool:
    """Owned by root, or by whoever runs this check (root, outside tests)."""
    return info.st_uid in (0, os.geteuid())


def changeable_by(info: os.stat_result, account: Account) -> bool:
    """Whether the account can change this entry, or what a directory holds."""
    if info.st_uid == account.uid:
        return True
    if stat.S_ISLNK(info.st_mode):
        # A link's own mode means nothing; only its folder can replace it.
        return False
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o020 and info.st_gid in account.groups:
        return True
    # A sticky directory only lets others remove what they own themselves.
    sticky = stat.S_ISDIR(info.st_mode) and bool(mode & stat.S_ISVTX)
    return bool(mode & 0o002) and not (sticky and trusted(info))


def safe_ancestry(path: Path, account: Account) -> bool:
    """Every directory from / down to ``path`` that exists: real, root's, and
    out of the account's reach."""
    for current in reversed((path, *path.parents)):
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return True
        if not stat.S_ISDIR(info.st_mode) or not trusted(info) or changeable_by(info, account):
            return False
    return True


def tree_changeable(path: Path, account: Account) -> Path | None:
    """The first entry at or below ``path`` the account can change, or None."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if changeable_by(info, account):
        return path
    if not stat.S_ISDIR(info.st_mode):
        return None
    for directory, subdirectories, files in os.walk(path, followlinks=False):
        subdirectories.sort()
        for name in sorted(subdirectories + files):
            entry = Path(directory) / name
            try:
                if changeable_by(os.lstat(entry), account):
                    return entry
            except FileNotFoundError:
                continue
    return None


def layout_problem(install_root: Path, account: Account) -> str | None:
    """Existing folders of the layout: code is root's, the rest the account's,
    or root's and empty, as a run stopped before it gave one away leaves it."""
    code = install_root / CODE_DIRECTORY
    for path in (code, code / ".runtimes", code / "catalog", code / "deploy"):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or not trusted(info) or changeable_by(info, account):
            return str(path)
    for name in ACCOUNT_DIRECTORIES:
        path = install_root / name
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            return str(path)
        if info.st_uid != account.uid and not (trusted(info) and not any(path.iterdir())):
            return str(path)
    return None


def to_give(install_root: Path, account: Account) -> list[str]:
    """The account's folders that it does not own yet: missing, or root's and empty."""
    names = []
    for name in ACCOUNT_DIRECTORIES:
        try:
            owner = os.lstat(install_root / name).st_uid
        except FileNotFoundError:
            owner = None
        if owner != account.uid:
            names.append(name)
    return names


def source_problem(source: Path, account: Account) -> bool:
    """Whether the account can change the checkout the installer builds from."""
    source = Path(os.path.realpath(source))
    return not safe_ancestry(source.parent, account) or tree_changeable(source, account) is not None


def preflight(name: str, install_root: Path, source: Path) -> dict[str, str]:
    """Shell assignments for the account, and the first problem found, if any."""
    found: dict[str, str] = {"problem": ""}
    if not valid_account_name(name):
        found["problem"] = "account-name"
        return found
    if not install_root.is_absolute() or os.path.normpath(str(install_root)) != str(install_root):
        found["problem"] = "root-path"
        return found
    # Every later step uses the resolved root, so no link on the way can be
    # pointed elsewhere afterwards; it is the resolved path that is checked.
    install_root = Path(os.path.realpath(install_root))
    found["install_root"] = str(install_root)
    if install_root == Path("/"):
        found["problem"] = "root-path"
        return found
    account = lookup_account(name)
    problem = account_problem(account, install_root)
    if account is not None:
        found.update(
            account_uid=str(account.uid),
            account_gid=str(account.gid),
            account_group=account.group,
            account_home=str(account.home),
        )
    if problem is not None or account is None:
        found["problem"] = problem or "account-missing"
        return found
    if not safe_ancestry(install_root, account):
        found["problem"] = "root-unsafe"
    elif source_problem(source, account):
        found["problem"] = "source-writable"
    else:
        unsafe = layout_problem(install_root, account)
        # Folders root makes here get 0755; existing ones must let the
        # account reach its data and read its code.
        hidden = next(
            (
                found_path
                for found_path in (
                    unreadable(install_root, account, tree=False),
                    *(
                        unreadable(install_root / CODE_DIRECTORY / name, account, tree=False)
                        for name in ("", ".runtimes", "catalog", "deploy")
                    ),
                )
                if found_path is not None
            ),
            None,
        )
        if unsafe is not None:
            found.update(problem="layout-unsafe", unsafe_path=unsafe)
        elif hidden is not None:
            found.update(problem="layout-unreadable", unsafe_path=str(hidden))
        else:
            found["give_directories"] = " ".join(to_give(install_root, account))
    return found


def permits(info: os.stat_result, account: Account, read: int, search: int) -> bool:
    """Whether the account has these permission bits, as the kernel picks them."""
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid == account.uid:
        shift = 6
    elif info.st_gid in account.groups:
        shift = 3
    else:
        shift = 0
    wanted = (read | search) << shift
    return mode & wanted == wanted


def unreadable(path: Path, account: Account, *, tree: bool = True) -> Path | None:
    """The first folder on the way to ``path`` the account cannot search, or
    entry at or below it (only ``path`` itself without ``tree``) that it
    cannot read; folders must also be searchable."""
    for current in reversed((path, *path.parents)):
        if current == path:
            break
        try:
            info = os.stat(current)
        except FileNotFoundError:
            return None
        if not permits(info, account, 0, 1):
            return current
    if not path.exists():
        return None
    entries = [path]
    if tree and path.is_dir() and not path.is_symlink():
        for directory, subdirectories, files in os.walk(path, followlinks=False):
            subdirectories.sort()
            entries.extend(Path(directory) / name for name in sorted(subdirectories + files))
    for entry in entries:
        try:
            info = os.lstat(entry)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            continue
        search = 1 if stat.S_ISDIR(info.st_mode) else 0
        if not permits(info, account, 4, search):
            return entry
    return None


def contained(install_root: Path, base: Path, target: Path, account: Account) -> bool:
    """Whether root may delete, move or replace ``target``: it lies inside
    ``base``, under the install root, through real directories the account
    cannot change, so it cannot be turned into a way out between this check
    and its use. ``target`` itself may be a link: removing one removes only it."""
    for path in (install_root, base, target):
        if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
            return False
    try:
        target.relative_to(base)
        base.relative_to(install_root)
    except ValueError:
        return False
    if target == base and base == install_root:
        return False
    return safe_ancestry(target.parent, account)


def code_problem(install_root: Path, account: Account) -> Path | None:
    """The first path of the code the account could change, or None."""
    if not safe_ancestry(install_root, account):
        return install_root
    return tree_changeable(install_root / CODE_DIRECTORY, account)


def _assignments(values: dict[str, str]) -> str:
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())


def remove_code(install_root: Path, account: Account) -> bool:
    """Delete ``<install root>/app``, if it is still the real folder root's
    code is in: checked and removed here in one step, without following links."""
    import shutil

    code = install_root / CODE_DIRECTORY
    try:
        info = os.lstat(code)
    except FileNotFoundError:
        return True
    if (
        not stat.S_ISDIR(info.st_mode)
        or not trusted(info)
        or not contained(install_root, install_root, code, account)
    ):
        return False
    shutil.rmtree(code)
    return True


def run_as(name: str, command: list[str]) -> int:
    """Become the account, for good, and run ``command`` in this process."""
    account = lookup_account(name)
    if account is None or not command:
        return 1
    os.initgroups(name, account.gid)
    os.setgid(account.gid)
    os.setuid(account.uid)
    if os.getuid() != account.uid or os.geteuid() != account.uid:
        return 1
    try:
        os.setuid(0)
    except PermissionError:
        os.execvp(command[0], command)
    return 1


def main(arguments: list[str]) -> int:
    command, *rest = arguments
    if command == "run-as":
        name, separator, *program = rest
        if separator != "--":
            return 64
        return run_as(name, program)
    if command == "preflight":
        name, install_root, source = rest
        print(_assignments(preflight(name, Path(install_root), Path(source))), end="")
        return 0
    if command in ("contained", "verify-code", "readable", "remove-code"):
        name, install_root, *paths = rest
        account = lookup_account(name)
        if account is None:
            return 1
        root = Path(install_root)
        if command == "remove-code":
            return 0 if remove_code(root, account) else 1
        if command == "readable":
            (path,) = paths
            hidden = unreadable(Path(path), account)
            if hidden is not None:
                print(_assignments({"unsafe_path": str(hidden)}), end="")
                return 1
            return 0
        if command == "contained":
            base, target = (Path(path) for path in paths)
            return 0 if contained(root, base, target, account) else 1
        found = code_problem(root, account)
        if found is not None:
            print(_assignments({"unsafe_path": str(found)}), end="")
            return 1
        return 0
    print(f"Unknown command: {command}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
