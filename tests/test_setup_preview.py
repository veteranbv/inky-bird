"""Setup preview refuses exactly what the native installers would refuse."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from inky_bird_frame.config import load_config
from inky_bird_frame.errors import InstallationError
from inky_bird_frame.installation import InstallationRole, setup

ROOT = Path(__file__).resolve().parents[1]

BASE = """
[discovery]
zip_code = "12345"
radius_km = 8
species_limit = 50
window = "last-30-days"

[controller]
workspace_dir = "workspace"
catalog_dir = "catalog"
state_dir = "controller-state"
codex_path = "{codex}"
bind_host = "0.0.0.0"
port = 8793
references_per_species = 4
generations_per_cycle = 1
max_generation_attempts = 3
{controller}

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display-state"
{tail}
"""

OIDC = """owner_allowed_origins = ["https://inky.example.test"]

[controller.oidc]
issuer = "https://id.example.test/"
client_id = "client"
client_secret_env = "SETUP_PREVIEW_OIDC_SECRET"
callback_url = "https://inky.example.test/v1/owner/oidc/callback"
"""

NOTIFICATIONS = """
[notifications]
enabled = true

[[notifications.destinations]]
name = "synthetic-env"
url_env = "SETUP_PREVIEW_NOTIFY_URL"
events = ["terminal_error"]
"""


def _config(tmp_path: Path, *, controller: str = "", tail: str = "") -> Path:
    codex = tmp_path / "codex"
    codex.touch(mode=0o700)
    path = tmp_path / "config.toml"
    path.write_text(BASE.format(codex=codex, controller=controller, tail=tail))
    path.chmod(0o600)
    return path


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    for name in ("install-controller.sh", "install-controller-systemd.sh"):
        script = source / "deploy" / name
        script.parent.mkdir(parents=True, exist_ok=True)
        script.touch(mode=0o700)
    return source


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("apply", [False, True])
def test_environment_notification_url_is_refused_before_any_installer_step(
    tmp_path: Path, system: str, apply: bool
) -> None:
    path = _config(tmp_path, tail=NOTIFICATIONS)
    with (
        patch.dict(os.environ, {"SETUP_PREVIEW_NOTIFY_URL": "json://example.test/hook"}),
        patch("inky_bird_frame.installation.platform.system", return_value=system),
        patch("inky_bird_frame.installation.subprocess.run") as run,
        pytest.raises(InstallationError, match="synthetic-env"),
    ):
        setup(InstallationRole.CONTROLLER, path, apply=apply, source_dir=_source(tmp_path))
    run.assert_not_called()


def test_disabled_notifications_do_not_block_setup(tmp_path: Path) -> None:
    path = _config(tmp_path, tail=NOTIFICATIONS.replace("enabled = true", "enabled = false"))
    with (
        patch.dict(os.environ, {"SETUP_PREVIEW_NOTIFY_URL": "json://example.test/hook"}),
        patch("inky_bird_frame.installation.platform.system", return_value="Darwin"),
    ):
        result = setup(InstallationRole.CONTROLLER, path, apply=False, source_dir=_source(tmp_path))
    assert result["applied"] is False


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("apply", [False, True])
def test_missing_oidc_secret_is_refused_before_any_installer_step(
    tmp_path: Path, system: str, apply: bool
) -> None:
    path = _config(tmp_path, controller=OIDC)
    environment = dict(os.environ)
    environment.pop("SETUP_PREVIEW_OIDC_SECRET", None)
    with (
        patch.dict(os.environ, environment, clear=True),
        patch("inky_bird_frame.installation.platform.system", return_value=system),
        patch("inky_bird_frame.installation.subprocess.run") as run,
        pytest.raises(InstallationError, match="OIDC client secret"),
    ):
        setup(InstallationRole.CONTROLLER, path, apply=apply, source_dir=_source(tmp_path))
    run.assert_not_called()
    assert not (tmp_path / "native-access.env").exists()


def test_supplied_oidc_secret_previews_without_writing_the_cache(tmp_path: Path) -> None:
    path = _config(tmp_path, controller=OIDC)
    with (
        patch.dict(os.environ, {"SETUP_PREVIEW_OIDC_SECRET": "synthetic-secret"}),
        patch("inky_bird_frame.installation.platform.system", return_value="Darwin"),
        patch("inky_bird_frame.installation.subprocess.run") as run,
    ):
        result = setup(InstallationRole.CONTROLLER, path, apply=False, source_dir=_source(tmp_path))
    assert result["applied"] is False
    run.assert_not_called()
    assert not (tmp_path / "native-access.env").exists()


def test_unsafe_oidc_cache_is_refused_in_preview(tmp_path: Path) -> None:
    path = _config(tmp_path, controller=OIDC)
    cache = tmp_path / "native-access.env"
    cache.write_text('SETUP_PREVIEW_OIDC_SECRET="cached"\n')
    cache.chmod(0o644)
    with (
        patch.dict(os.environ, {"SETUP_PREVIEW_OIDC_SECRET": "synthetic-secret"}),
        patch("inky_bird_frame.installation.platform.system", return_value="Darwin"),
        pytest.raises(InstallationError, match="Native access cache"),
    ):
        setup(InstallationRole.CONTROLLER, path, apply=False, source_dir=_source(tmp_path))


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("writable", "unsafe or cannot be read"),
        ("wrong-key", "only SSL_CERT_FILE"),
    ],
)
def test_unsafe_native_tls_file_is_refused_before_any_installer_step(
    tmp_path: Path, system: str, apply: bool, case: str, message: str
) -> None:
    path = _config(tmp_path)
    trust = tmp_path / "native-tls.env"
    if case == "writable":
        bundle = tmp_path / "roots.pem"
        bundle.write_text("synthetic certificate")
        bundle.chmod(0o644)
        trust.write_text(f'SSL_CERT_FILE="{bundle}"\n')
        trust.chmod(0o666)
    else:
        trust.write_text('UNRELATED_SECRET="sensitive-test-value"\n')
        trust.chmod(0o644)
    with (
        patch("inky_bird_frame.installation.platform.system", return_value=system),
        patch("inky_bird_frame.installation.subprocess.run") as run,
        pytest.raises(InstallationError, match=message) as error,
    ):
        setup(InstallationRole.CONTROLLER, path, apply=apply, source_dir=_source(tmp_path))
    run.assert_not_called()
    assert "sensitive-test-value" not in str(error.value)


@pytest.mark.parametrize("apply", [False, True])
def test_environment_provider_credential_is_refused_before_any_installer_step(
    tmp_path: Path, apply: bool
) -> None:
    path = _config(tmp_path)
    path.write_text(
        path.read_text().replace(
            "[discovery]\n",
            '[discovery]\nsource = "ebird"\nebird_api_key_env = "SETUP_PREVIEW_TOKEN"\n',
        )
    )
    with (
        patch.dict(os.environ, {"SETUP_PREVIEW_TOKEN": "synthetic"}),
        patch("inky_bird_frame.installation.platform.system", return_value="Linux"),
        patch("inky_bird_frame.installation.subprocess.run") as run,
        pytest.raises(InstallationError, match="ebird_api_key_env"),
    ):
        setup(InstallationRole.CONTROLLER, path, apply=apply, source_dir=_source(tmp_path))
    run.assert_not_called()


def test_display_setup_does_not_apply_controller_refusals(tmp_path: Path) -> None:
    path = _config(tmp_path, tail=NOTIFICATIONS)
    source = tmp_path / "source"
    script = source / "deploy/install-display-local.sh"
    script.parent.mkdir(parents=True)
    script.touch(mode=0o700)
    with (
        patch.dict(os.environ, {"SETUP_PREVIEW_NOTIFY_URL": "json://example.test/hook"}),
        patch("inky_bird_frame.installation.platform.system", return_value="Linux"),
    ):
        result = setup(InstallationRole.DISPLAY, path, apply=False, source_dir=source)
    assert result["applied"] is False
    assert "generation_enabled" not in result


@pytest.mark.parametrize("enabled", [True, False])
def test_controller_setup_reports_whether_generation_is_on(tmp_path: Path, enabled: bool) -> None:
    path = _config(
        tmp_path,
        controller=f"generation_enabled = {'true' if enabled else 'false'}",
    )
    with patch("inky_bird_frame.installation.platform.system", return_value="Darwin"):
        result = setup(InstallationRole.CONTROLLER, path, apply=False, source_dir=_source(tmp_path))
    assert result["generation_enabled"] is enabled
    if enabled:
        assert "generation" not in result
    else:
        assert "generation_enabled = true" in str(result["generation"])


def test_new_config_copied_from_the_example_has_generation_off(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    assert load_config(path).controller.generation_enabled is False


def test_existing_managed_config_without_the_key_keeps_generation_on(tmp_path: Path) -> None:
    example = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    existing = "\n".join(
        line for line in example.splitlines() if not line.startswith("generation_enabled")
    )
    assert "generation_enabled" not in tomllib.loads(existing)["controller"]
    path = tmp_path / "config.toml"
    path.write_text(existing)
    assert load_config(path).controller.generation_enabled is True


def test_a_system_service_preview_accepts_settings_read_from_the_environment(
    tmp_path: Path,
) -> None:
    """A system service loads its private environment file in every unit, so
    provider keys, notification URLs and the OIDC secret may come from there."""
    path = _config(tmp_path, controller=OIDC, tail=NOTIFICATIONS)
    path.write_text(
        path.read_text().replace(
            "[discovery]\n",
            '[discovery]\nsource = "ebird"\nebird_api_key_env = "SETUP_PREVIEW_TOKEN"\n',
        )
    )
    values = {
        "SETUP_PREVIEW_TOKEN": "synthetic",
        "SETUP_PREVIEW_NOTIFY_URL": "json://example.test/hook",
        "SETUP_PREVIEW_OIDC_SECRET": "synthetic-secret",
    }
    with (
        patch.dict(os.environ, values),
        patch("inky_bird_frame.installation.platform.system", return_value="Linux"),
        patch("inky_bird_frame.installation.subprocess.run") as run,
    ):
        result = setup(
            InstallationRole.CONTROLLER,
            path,
            apply=False,
            source_dir=_source(tmp_path),
            service_account="inky-bird",
        )
        with pytest.raises(InstallationError, match="--service-account inky-bird"):
            setup(
                InstallationRole.CONTROLLER,
                path,
                apply=True,
                source_dir=_source(tmp_path),
                service_account="inky-bird",
            )
    run.assert_not_called()
    assert result["applied"] is False
    assert result["service_account"] == "inky-bird"
    assert "--service-account inky-bird" in str(result["confirmation"])
    changes = result["changes"]
    assert isinstance(changes, list) and not any("OIDC" in change for change in changes)
    assert not (tmp_path / "native-access.env").exists()


@pytest.mark.parametrize("name", ["native-access.env", "native-tls.env"])
def test_a_system_service_preview_refuses_the_serving_only_native_files(
    tmp_path: Path, name: str
) -> None:
    path = _config(tmp_path)
    (tmp_path / name).write_text('UNRELATED_SECRET="sensitive-test-value"\n')
    (tmp_path / name).chmod(0o600)
    with (
        patch("inky_bird_frame.installation.platform.system", return_value="Linux"),
        pytest.raises(InstallationError, match="private environment file") as error,
    ):
        setup(
            InstallationRole.CONTROLLER,
            path,
            apply=False,
            source_dir=_source(tmp_path),
            service_account="inky-bird",
        )
    assert name in str(error.value)
    assert "sensitive-test-value" not in str(error.value)
