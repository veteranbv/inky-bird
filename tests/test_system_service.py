"""A system service's checks, units and configuration read, one at a time.

``system_service.py`` runs as root before any change; these tests give it an
account whose user ID is not this process's, so files this test creates
count as root's, as they would be on a host. The installer's use of it is in
``tests/test_installer_system.py`` and the installer matrix.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_installation import native_oidc_config, write_config

from inky_bird_frame import upgrade_gates
from inky_bird_frame.config import load_config
from inky_bird_frame.errors import ConfigurationError, InstallationError
from inky_bird_frame.installation import (
    SYSTEM_SERVICE_MARKER,
    SystemService,
    controller_systemd_units,
    read_account_config,
)
from inky_bird_frame.system_service import (
    Account,
    account_problem,
    code_problem,
    contained,
    layout_problem,
    preflight,
    source_problem,
    to_give,
    valid_account_name,
)

OTHER_UID = os.getuid() + 1000


def account(root: Path, **changes: object) -> Account:
    fields: dict[str, object] = {
        "name": "inky-bird",
        "uid": OTHER_UID,
        "gid": OTHER_UID,
        "group": "inky-bird",
        "home": root / "home",
        "shell": "/usr/sbin/nologin",
        "groups": frozenset({OTHER_UID}),
    }
    fields.update(changes)
    return Account(**fields)  # type: ignore[arg-type]  # the fields above, by name


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("inky-bird", True),
        ("inky-bird-qa", True),
        ("inky-bird-frame", True),
        ("inky-bird-qa-2", True),
        ("inky-birds", False),
        ("Inky-bird", False),
        ("inky-bird-", False),
        ("inky-bird-" + "x" * 30, False),
        ("root", False),
    ],
)
def test_the_account_name_is_the_service_name(name: str, valid: bool) -> None:
    assert valid_account_name(name) is valid


def test_an_account_passes_only_without_root_administrators_or_a_login(tmp_path: Path) -> None:
    root = tmp_path / "srv"
    assert account_problem(account(root), root) is None
    assert account_problem(None, root) == "account-missing"
    assert account_problem(account(root, uid=0), root) == "account-root"
    assert account_problem(account(root, gid=0), root) == "account-root"
    # Group 0 is root's on every host.
    assert account_problem(account(root, groups=frozenset({OTHER_UID, 0})), root) == (
        "account-admin"
    )
    assert account_problem(account(root, shell="/bin/bash"), root) == "account-shell"
    assert account_problem(account(root, home=tmp_path / "elsewhere"), root) == "account-home"


def test_a_root_path_that_is_not_absolute_and_plain_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for root in ("relative/root", f"{tmp_path}/a/../root", "/"):
        assert preflight("inky-bird", Path(root), source)["problem"] == "root-path"
    assert preflight("inky-birds", tmp_path, source)["problem"] == "account-name"


def test_a_folder_the_account_can_change_on_the_way_is_refused(tmp_path: Path) -> None:
    owner = account(tmp_path / "srv")
    srv = tmp_path / "srv"
    (srv / "app/.runtimes/runtime.a").mkdir(parents=True)
    assert code_problem(srv, owner) is None
    srv.chmod(0o777)
    assert code_problem(srv, owner) == srv
    # A sticky folder only lets the account remove what it owns.
    srv.chmod(0o1777)
    assert code_problem(srv, owner) is None
    srv.chmod(0o755)
    (srv / "app/.runtimes/runtime.a").chmod(0o775)
    assert code_problem(srv, owner) is None
    assert code_problem(srv, account(srv, groups=frozenset({os.getgid()}))) == (
        srv / "app/.runtimes/runtime.a"
    )


def test_links_count_only_by_the_folder_that_holds_them(tmp_path: Path) -> None:
    owner = account(tmp_path / "srv")
    venv = tmp_path / "srv/app/.runtimes/runtime.a/.venv/bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to("/usr/bin/python3")
    assert code_problem(tmp_path / "srv", owner) is None


def test_the_layout_is_root_code_and_account_data(tmp_path: Path) -> None:
    root = tmp_path / "srv"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "app").mkdir(parents=True)
    assert layout_problem(root, account(root)) is None
    (root / "app/catalog").symlink_to(outside)
    assert layout_problem(root, account(root)) == str(root / "app/catalog")
    (root / "app/catalog").unlink()
    # This test's own folders are root's here: an empty one, as a stopped run
    # leaves it, is given to the account; one with anything in it is refused.
    (root / "state").mkdir()
    assert layout_problem(root, account(root)) is None
    assert to_give(root, account(root)) == ["config", "home", "state", "logs"]
    (root / "state/kept").write_text("")
    assert layout_problem(root, account(root)) == str(root / "state")
    # As the account's, the data passes, and the code does not.
    assert layout_problem(root, account(root, uid=os.getuid())) == str(root / "app")
    (root / "app").rmdir()
    assert layout_problem(root, account(root, uid=os.getuid())) is None
    assert to_give(root, account(root, uid=os.getuid())) == ["config", "home", "logs"]


def test_a_source_checkout_the_account_can_change_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    (source / "src").mkdir(parents=True)
    (source / "src/module.py").write_text("")
    owner = account(tmp_path / "srv")
    assert not source_problem(source, owner)
    (source / "src/module.py").chmod(0o666)
    assert source_problem(source, owner)
    (source / "src/module.py").chmod(0o644)
    tmp_path.chmod(0o777)
    try:
        assert source_problem(source, owner)
    finally:
        tmp_path.chmod(0o755)


def test_root_may_only_remove_inside_the_folder_it_names(tmp_path: Path) -> None:
    root = tmp_path / "srv"
    runtimes = root / "app/.runtimes"
    (runtimes / "runtime.a").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    owner = account(root)
    assert contained(root, runtimes, runtimes / "runtime.a", owner)
    # A link itself may be removed: rm takes the link, not what it names.
    (runtimes / "runtime.b").symlink_to(outside)
    assert contained(root, runtimes, runtimes / "runtime.b", owner)
    for target in (outside, runtimes / "../../../outside", root / "app"):
        assert not contained(root, runtimes, Path(str(target)), owner)
    assert not contained(root, tmp_path, outside, owner)
    # Through a link, or a folder the account can change, the path can lead out.
    (root / "app/linked").symlink_to(outside)
    assert not contained(root, root / "app", root / "app/linked/file", owner)
    runtimes.chmod(0o777)
    assert not contained(root, runtimes, runtimes / "runtime.a", owner)


def system(tmp_path: Path) -> SystemService:
    writable = (tmp_path / "config", tmp_path / "home", tmp_path / "state")
    return SystemService("inky-bird", tmp_path / "config/private.env", writable)


def test_system_units_name_the_private_file_and_run_as_the_account(tmp_path: Path) -> None:
    config, path = native_oidc_config(tmp_path)
    (tmp_path / "native-tls.env").write_text("ignored by a system service\n")
    units = controller_systemd_units(
        config,
        executable=Path("/srv/inky-bird/app/.venv/bin/inky-bird"),
        app_dir=Path("/srv/inky-bird/app"),
        config_path=path,
        home=Path("/srv/inky-bird/home"),
        user="inky-bird",
        service_prefix="inky-bird",
        system=system(tmp_path),
    )
    assert set(units) == {"inky-bird-controller.service", "inky-bird-scheduler.service"}
    for text in units.values():
        assert text.startswith(f"{SYSTEM_SERVICE_MARKER}\n[Unit]\n")
        assert "User=inky-bird\nGroup=inky-bird\n" in text
        assert f'Environment="INKY_BIRD_ENV_FILE={tmp_path}/config/private.env"' in text
        assert "EnvironmentFile=" not in text
        assert "SSL_CERT_FILE" not in text
    serve = units["inky-bird-controller.service"]
    assert "NoNewPrivileges=true\nPrivateTmp=true\n" in serve
    assert "ProtectSystem=strict\n" in serve
    assert f'ReadWritePaths="-{tmp_path}/config" "-{tmp_path}/home" "-{tmp_path}/state"\n' in serve
    # The scheduler starts Codex in a sandbox of its own: no file system limits.
    assert "ProtectSystem=" not in units["inky-bird-scheduler.service"]


def test_a_system_service_is_named_after_its_account(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    codex.touch(mode=0o700)
    config = load_config(write_config(tmp_path, codex))
    options = {
        "executable": Path("/srv/x/app/.venv/bin/inky-bird-qa"),
        "app_dir": Path("/srv/x/app"),
        "config_path": tmp_path / "config.toml",
        "home": Path("/srv/x/home"),
    }
    units = controller_systemd_units(
        config,
        user="inky-bird-qa",
        service_prefix="inky-bird-qa",
        system=system(tmp_path),
        **options,
    )
    assert "inky-bird-qa-refresh.timer" in units
    for user, prefix in (("inky-bird-qa", "inky-bird"), ("root", "root")):
        with pytest.raises(InstallationError, match="named after the account"):
            controller_systemd_units(
                config, user=user, service_prefix=prefix, system=system(tmp_path), **options
            )


def test_root_reads_the_account_config_itself_never_through_a_link(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    codex.touch(mode=0o700)
    path = write_config(tmp_path, codex)
    assert read_account_config(path, os.getuid()).controller.port == 8793
    with pytest.raises(ConfigurationError, match="the service account owns"):
        read_account_config(path, OTHER_UID)
    link = tmp_path / "linked.toml"
    link.symlink_to(path)
    with pytest.raises(ConfigurationError, match="not a link"):
        read_account_config(link, os.getuid())
    path.write_text("#" * (1024 * 1024 + 1))
    with pytest.raises(ConfigurationError, match="larger than 1 MiB"):
        read_account_config(path, os.getuid())


def test_the_health_check_loads_the_private_file_it_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = tmp_path / "private.env"
    environment.write_text("SYSTEM_SERVICE_TRUST=synthetic\n")
    environment.chmod(0o600)
    seen: list[str | None] = []

    def fetch(*arguments: object, **options: object) -> object:
        seen.append(os.environ.get("SYSTEM_SERVICE_TRUST"))
        return {"ok": True, "runtime": upgrade_gates.runtime_id()}

    codex = tmp_path / "codex"
    codex.touch(mode=0o700)
    config = write_config(tmp_path, codex)
    monkeypatch.delenv("SYSTEM_SERVICE_TRUST", raising=False)
    monkeypatch.delenv("INKY_BIRD_ENV_FILE", raising=False)
    monkeypatch.setattr(upgrade_gates, "fetch_health", fetch)
    assert upgrade_gates.main(["check-serve", str(config), "1"]) == 0
    assert upgrade_gates.main(["check-serve", str(config), "1", str(environment)]) == 0
    assert seen == [None, "synthetic"]
