from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from inky_bird_frame.config import AppConfig, load_config
from inky_bird_frame.imports import HistoryImports
from inky_bird_frame.owner_operations import OwnerOperationError
from inky_bird_frame.settings import initialize_config
from inky_bird_frame.web import create_app


def configuration(root: Path) -> AppConfig:
    path = root / "config.toml"
    initialize_config(path)
    return load_config(path, load_secrets=False)


def partial_upload(imports: HistoryImports, *, age_seconds: float) -> None:
    _identifier, handle = imports.begin("ebird-archive")
    with handle:
        handle.write(b"Submission ID,partial")
    started = time.time() - age_seconds
    os.utime(imports.metadata_path, (started, started))
    os.utime(imports.path, (started, started))


def test_cleanup_removes_an_upload_that_never_finished(tmp_path: Path) -> None:
    imports = HistoryImports(configuration(tmp_path))
    partial_upload(imports, age_seconds=2 * 60 * 60)

    imports.cleanup_expired()

    assert not imports.path.exists()
    assert not imports.metadata_path.exists()


def test_cleanup_keeps_an_upload_that_may_still_be_receiving(tmp_path: Path) -> None:
    imports = HistoryImports(configuration(tmp_path))
    partial_upload(imports, age_seconds=60)

    imports.cleanup_expired()

    assert imports.path.exists()
    assert json.loads(imports.metadata_path.read_text())["status"] == "receiving"


def test_cleanup_removes_a_raw_file_without_a_preview(tmp_path: Path) -> None:
    imports = HistoryImports(configuration(tmp_path))
    imports.path.write_bytes(b"orphaned raw export")

    imports.cleanup_expired()

    assert not imports.path.exists()


def test_startup_removes_an_interrupted_upload(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    imports = HistoryImports(config)
    partial_upload(imports, age_seconds=2 * 60 * 60)

    with TestClient(create_app(config.controller, app_config=config)):
        assert not imports.path.exists()
        assert not imports.metadata_path.exists()


def test_unexpected_cleanup_failure_is_an_owner_operation_error(tmp_path: Path) -> None:
    imports = HistoryImports(configuration(tmp_path))
    partial_upload(imports, age_seconds=2 * 60 * 60)

    with (
        patch.object(Path, "unlink", side_effect=PermissionError("denied")),
        pytest.raises(OwnerOperationError, match="cleanup") as raised,
    ):
        imports.cleanup_expired()
    assert isinstance(raised.value.__cause__, PermissionError)


def test_unreadable_staging_directory_is_an_owner_operation_error(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    with (
        patch.object(Path, "lstat", side_effect=PermissionError("denied")),
        pytest.raises(OwnerOperationError, match="Import staging"),
    ):
        HistoryImports(config)
