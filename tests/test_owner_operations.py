from __future__ import annotations

import json
import threading
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from inky_bird_frame import settings as settings_module
from inky_bird_frame.config import load_config
from inky_bird_frame.controller import exclusive_cycle_lock
from inky_bird_frame.ebird_archive import CSV_FILENAME, preview_ebird_checklists
from inky_bird_frame.errors import DataSourceError
from inky_bird_frame.owner_operations import (
    MAX_RECEIPT_BYTES,
    MAX_RECEIPTS,
    MAX_RESULT_BYTES,
    OperationKind,
    OperationReceipt,
    OwnerOperationBusyError,
    OwnerOperationError,
    OwnerOperations,
    execute_owner_operation,
)
from inky_bird_frame.retry_requests import RETRY_WAKE_LISTENER, retry_requests, retry_wake_token
from inky_bird_frame.runtime_state import state_lock
from inky_bird_frame.settings import SettingsConflictError, initialize_config, settings_status
from inky_bird_frame.web import create_app


def completed(read: Callable[[], dict[str, object]]) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        receipt = read()
        if receipt["status"] not in {"pending", "running"}:
            return receipt
        time.sleep(0.005)
    raise AssertionError("Synthetic operation did not finish")


def test_private_receipts_are_idempotent_and_do_not_replay(tmp_path: Path) -> None:
    worker = OwnerOperations(tmp_path)
    calls: list[str] = []
    key = "synthetic-request-key"

    def execute() -> dict[str, object]:
        calls.append("called")
        return {"count": 2}

    def duplicate_execute() -> dict[str, object]:
        calls.append("wrong")
        return {}

    try:
        receipt = worker.submit(
            "refresh",
            key=key,
            inputs={"revision": "a"},
            execute=execute,
        )
        result = completed(lambda: worker.status(str(receipt["operation_id"])))
        assert result["status"] == "succeeded"
        assert result["result"] == {"count": 2}
        assert "key_hash" not in json.dumps(result)
        assert worker.path.stat().st_mode & 0o777 == 0o600
        duplicate = worker.submit(
            "refresh",
            key=key,
            inputs={"revision": "a"},
            execute=duplicate_execute,
        )
        assert duplicate["operation_id"] == receipt["operation_id"]
        assert calls == ["called"]
        with pytest.raises(OwnerOperationError, match="different"):
            worker.submit("refresh", key=key, inputs={"revision": "b"}, execute=lambda: {})
    finally:
        worker.close()


def test_one_active_operation_across_workers_and_no_unbounded_queue(tmp_path: Path) -> None:
    first, second = OwnerOperations(tmp_path), OwnerOperations(tmp_path)
    release, started = threading.Event(), threading.Event()

    def execute() -> dict[str, object]:
        started.set()
        assert release.wait(5)
        return {}

    try:
        receipt = first.submit("refresh", key="first-synthetic-key", inputs={}, execute=execute)
        assert started.wait(2)
        assert second.status(str(receipt["operation_id"]))["status"] == "running"
        assert (
            second.submit("refresh", key="first-synthetic-key", inputs={}, execute=lambda: {})[
                "operation_id"
            ]
            == receipt["operation_id"]
        )
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(
                    second.submit,
                    "refresh",
                    key=f"another-synthetic-{n}",
                    inputs={},
                    execute=lambda: {},
                )
                for n in range(3)
            ]
            for future in futures:
                with pytest.raises(OwnerOperationBusyError):
                    future.result()
        release.set()
        completed(lambda: first.status(str(receipt["operation_id"])))
        follow = second.submit("refresh", key="next-synthetic-key", inputs={}, execute=lambda: {})
        completed(lambda: second.status(str(follow["operation_id"])))
        assert len(first.status()["operations"]) == 2  # type: ignore[arg-type]
    finally:
        release.set()
        first.close()
        second.close()


def test_restart_marks_unknown_work_interrupted_without_replay(tmp_path: Path) -> None:
    worker = OwnerOperations(tmp_path)
    receipt = worker.submit(
        "retry", key="interrupted-synthetic-key", inputs={"taxon_id": 1}, execute=lambda: {}
    )
    completed(lambda: worker.status(str(receipt["operation_id"])))
    worker.close()
    raw = json.loads(worker.path.read_text())
    raw["receipts"][0]["status"] = "running"
    raw["receipts"][0]["result"] = None
    worker.path.write_text(json.dumps(raw))
    restarted = OwnerOperations(tmp_path)
    try:
        assert restarted.status(str(receipt["operation_id"]))["status"] == "interrupted"
        with patch("inky_bird_frame.owner_operations._stamp"):
            duplicate = restarted.submit(
                "retry",
                key="interrupted-synthetic-key",
                inputs={"taxon_id": 1},
                execute=lambda: pytest.fail("replayed"),
            )
        assert duplicate["status"] == "interrupted"
    finally:
        restarted.close()


def test_errors_and_oversized_results_are_sanitized(tmp_path: Path) -> None:
    worker = OwnerOperations(tmp_path)
    try:

        def fail() -> dict[str, object]:
            raise RuntimeError("DO_NOT_STORE email@example.test /private/path token=DO_NOT_STORE")

        receipt = worker.submit("refresh", key="failure-synthetic-key", inputs={}, execute=fail)
        result = completed(lambda: worker.status(str(receipt["operation_id"])))
        assert result["status"] == "failed"
        assert "DO_NOT_STORE" not in worker.path.read_text()
        receipt = worker.submit(
            "refresh",
            key="oversize-synthetic-key",
            inputs={},
            execute=lambda: {"too_large": "x" * MAX_RESULT_BYTES},
        )
        result = completed(lambda: worker.status(str(receipt["operation_id"])))
        assert result["status"] == "failed"
        assert "exceeds" in str(result["result"])
    finally:
        worker.close()


@pytest.mark.parametrize("kind", ["permissions", "symlink", "invalid", "version"])
def test_corrupt_or_insecure_receipts_fail_closed(tmp_path: Path, kind: str) -> None:
    worker = OwnerOperations(tmp_path)
    receipt = worker.submit("refresh", key="private-synthetic-key", inputs={}, execute=lambda: {})
    completed(lambda: worker.status(str(receipt["operation_id"])))
    worker.close()
    if kind == "permissions":
        worker.path.chmod(0o644)
    elif kind == "symlink":
        other = tmp_path / "private-backup.json"
        worker.path.rename(other)
        worker.path.symlink_to(other)
    elif kind == "invalid":
        worker.path.write_text("DO_NOT_ECHO")
    else:
        raw = json.loads(worker.path.read_text())
        raw["schema_version"] = True
        worker.path.write_text(json.dumps(raw))
    restarted = OwnerOperations(tmp_path)
    try:
        with pytest.raises(OwnerOperationError) as error:
            restarted.status()
        assert "DO_NOT_ECHO" not in str(error.value) and str(tmp_path) not in str(error.value)
    finally:
        restarted.close()


def test_adapter_refresh_uses_real_zero_source_logic_and_rejects_stale_settings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    initialize_config(path)
    config = load_config(path)
    revision = str(settings_status(config)["revision"])
    with patch("inky_bird_frame.controller.generate_candidate") as model:
        result = execute_owner_operation(config, "refresh", revision=revision)
    model.assert_not_called()
    assert result["species_count"] == 0
    assert result["providers"] == []
    with pytest.raises(OwnerOperationError, match="Settings changed"):
        execute_owner_operation(config, "refresh", revision="old")


def test_adapter_test_delivery_never_claims_failed_destinations_received_it(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    initialize_config(path)
    config = load_config(path)
    with patch(
        "inky_bird_frame.notifications.send_notification_test",
        return_value={
            "delivered": ["one"],
            "failures": [{"name": "two", "error_type": "RuntimeError"}],
        },
    ):
        result = execute_owner_operation(
            config, "notification-test", revision=str(settings_status(config)["revision"])
        )
    assert result["failed_count"] == result["delivered_count"] == 1
    assert "failed" in str(result["message"])


def test_concurrent_save_cannot_change_confirmed_notification_destinations(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    initialize_config(path)
    path.write_text(
        path.read_text() + "\n[notifications]\nenabled=true\n"
        '[[notifications.destinations]]\nname="Confirmed"\nurl="json://confirmed.test"\n'
    )
    config = load_config(path, load_secrets=False)
    revision = str(settings_status(config)["revision"])
    original_read = settings_module._read

    def replace_after_read(candidate: Path) -> tuple[bytes, dict[str, object], object]:
        captured = original_read(candidate)
        candidate.write_text(candidate.read_text().replace("confirmed.test", "changed.test"))
        return captured

    with (
        patch("inky_bird_frame.settings._read", side_effect=replace_after_read),
        patch(
            "inky_bird_frame.notifications.send_notification_test",
            return_value={
                "delivered": ["Confirmed"],
                "failures": [],
            },
        ) as deliver,
    ):
        execute_owner_operation(config, "notification-test", revision=revision)
    assert deliver.call_args.args[0].notifications.destinations[0].url == "json://confirmed.test"
    assert "changed.test" in path.read_text()


@pytest.mark.parametrize("ownership", ["app", "managed"])
def test_http_operation_boundary_and_no_secret_or_model_operation(
    tmp_path: Path, ownership: str
) -> None:
    path = tmp_path / "config.toml"
    initialize_config(path)
    origin = "https://inky.example.test"
    path.write_text(
        path.read_text()
        .replace('["controller"]', '["controller"]\nowner_allowed_origins = ["' + origin + '"]')
        .replace('"generation_enabled" = false', '"generation_enabled" = true')
        .replace('"configuration_ownership" = "app"', f'"configuration_ownership" = "{ownership}"')
    )
    config = load_config(path)
    assert config.controller.generation_enabled
    assert config.controller.configuration_ownership == ownership
    with TestClient(create_app(config.controller, app_config=config), base_url=origin) as client:
        assert client.get("/v1/owner/operations").status_code == 401
        assert client.get("/v1/owner/management").status_code == 401
        signed = client.post(
            "/v1/owner/register",
            json={
                "username": "fixture-owner",
                "passphrase": "long synthetic password",
            },
            headers={"Origin": origin},
        )
        headers = {"Origin": origin, "X-Inky-CSRF": signed.json()["data"]["csrf"]}
        body = {
            "kind": "refresh",
            "key": "http-synthetic-operation",
            "revision": settings_status(config)["revision"],
            "confirm": True,
        }
        assert client.post("/v1/owner/operations", json=body).status_code == 403
        assert (
            client.post(
                "/v1/owner/operations", json={**body, "password": "DO_NOT_STORE"}, headers=headers
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/v1/owner/operations", json={**body, "kind": "generate"}, headers=headers
            ).status_code
            == 400
        )
        response = client.post("/v1/owner/operations", json=body, headers=headers)
        assert response.status_code == 200
        receipt_id = response.json()["data"]["operation_id"]
        result = completed(lambda: client.get("/v1/owner/operations/" + receipt_id).json()["data"])
        assert result["status"] == "succeeded"
        assert (
            client.post("/v1/owner/operations", json=body, headers=headers).json()["data"][
                "operation_id"
            ]
            == receipt_id
        )
        assert (
            "DO_NOT_STORE"
            not in (config.controller.state_dir / "owner-operations.json").read_text()
        )
        assert not (config.controller.state_dir / "generation-queue.json").exists()
        assert client.get("/v1/owner/management").json()["data"]["available"] is True
        assert client.get("/app/work-status.js").status_code == 200
        retry_body = {
            **body,
            "kind": "retry-now",
            "key": "http-synthetic-batch-retry",
            "taxon_ids": [101, 202],
        }
        for ids in ([True], [101, 101], [], ["101"]):
            assert (
                client.post(
                    "/v1/owner/operations", json={**retry_body, "taxon_ids": ids}, headers=headers
                ).status_code
                == 400
            )
        assert (
            client.post(
                "/v1/owner/operations", json={**retry_body, "taxon_id": 101}, headers=headers
            ).status_code
            == 400
        )
        listener = config.controller.state_dir / RETRY_WAKE_LISTENER
        assert client.get("/v1/owner/management").json()["data"]["retry_wake_available"] is False
        assert not listener.exists()
        response = client.post(
            "/v1/owner/operations",
            json={**retry_body, "key": "synthetic-no-scheduler"},
            headers=headers,
        )
        receipt_id = response.json()["data"]["operation_id"]
        unavailable = completed(
            lambda: client.get("/v1/owner/operations/" + receipt_id).json()["data"]
        )
        assert unavailable["status"] == "failed"
        failure = unavailable["result"]
        assert isinstance(failure, dict) and "scheduled check" in failure["message"]
        assert retry_requests(config.controller.state_dir) == []
        response = client.post(
            "/v1/owner/operations",
            json={**retry_body, "kind": "retry", "key": "synthetic-scheduled", "taxon_ids": [303]},
            headers=headers,
        )
        receipt_id = response.json()["data"]["operation_id"]
        scheduled = completed(
            lambda: client.get("/v1/owner/operations/" + receipt_id).json()["data"]
        )
        assert scheduled["status"] == "succeeded"
        assert retry_wake_token(config.controller.state_dir) is None
        with state_lock(listener), exclusive_cycle_lock(config.controller.state_dir):
            assert client.get("/v1/owner/management").json()["data"]["retry_wake_available"] is True
            response = client.post("/v1/owner/operations", json=retry_body, headers=headers)
            assert response.status_code == 200
            receipt_id = response.json()["data"]["operation_id"]
            result = completed(
                lambda: client.get("/v1/owner/operations/" + receipt_id).json()["data"]
            )
            assert result["status"] == "succeeded"
        assert client.get("/v1/owner/management").json()["data"]["retry_wake_available"] is False
        assert retry_wake_token(config.controller.state_dir) is not None
        assert [row.taxon_id for row in retry_requests(config.controller.state_dir)] == [
            303,
            101,
            202,
        ]


def _finished_receipts(count: int, *, result_bytes: int, age: timedelta) -> dict[str, object]:
    stamp = (datetime.now(UTC) - age).replace(microsecond=0).isoformat()
    return {
        "schema_version": 1,
        "receipts": [
            {
                "operation_id": f"{index:032x}",
                "key_hash": f"{index:064x}",
                "input_hash": "0" * 64,
                "kind": "saved-collection",
                "status": "succeeded",
                "created_at": stamp,
                "updated_at": stamp,
                "result": {"message": "x" * result_bytes},
            }
            for index in range(count)
        ],
    }


def test_ordinary_use_never_fills_the_receipt_store(tmp_path: Path) -> None:
    worker = OwnerOperations(tmp_path)
    try:
        last = ""
        for index in range(MAX_RECEIPTS + 44):
            receipt = worker.submit(
                "saved-collection",
                key=f"saved-birds-toggle-{index:04d}",
                inputs={"taxon_id": index},
                execute=lambda: {"message": "Saved birds updated."},
            )
            last = str(receipt["operation_id"])
            assert completed(partial(worker.status, last))["status"] == "succeeded"
        rows = json.loads(worker.path.read_text())["receipts"]
        assert len(rows) == MAX_RECEIPTS
        assert rows[-1]["operation_id"] == last
    finally:
        worker.close()


def test_final_result_evicts_old_outcomes_instead_of_reporting_interrupted(
    tmp_path: Path,
) -> None:
    # A nearly full file of recent outcomes, then a result at the size limit.
    count = 70
    while True:
        seeded = json.dumps(
            _finished_receipts(count, result_bytes=16_000, age=timedelta(hours=1)),
            indent=2,
            sort_keys=True,
        )
        if len(seeded.encode()) <= MAX_RECEIPT_BYTES - 1024:
            break
        count -= 1
    assert len(seeded.encode()) + MAX_RESULT_BYTES > MAX_RECEIPT_BYTES
    path = tmp_path / "owner-operations.json"
    path.write_text(seeded)
    path.chmod(0o600)
    worker = OwnerOperations(tmp_path)
    try:
        receipt = worker.submit(
            "refresh",
            key="large-final-result-key",
            inputs={},
            execute=lambda: {"message": "y" * (MAX_RESULT_BYTES - 64)},
        )
        result = completed(lambda: worker.status(str(receipt["operation_id"])))
        assert result["status"] == "succeeded"
        assert result["result"] == {"message": "y" * (MAX_RESULT_BYTES - 64)}
        body = path.read_bytes()
        assert len(body) <= MAX_RECEIPT_BYTES
        kept = [row["operation_id"] for row in json.loads(body)["receipts"]]
        # Only the oldest outcomes went, in order.
        assert kept[-1] == receipt["operation_id"]
        assert len(kept) < count + 1
        assert kept[:-1] == [f"{index:032x}" for index in range(count - len(kept) + 1, count)]
    finally:
        worker.close()


def test_finished_receipt_survives_eviction_after_the_clock_moves_back(tmp_path: Path) -> None:
    # Saved outcomes stamped later than now (the clock was corrected) sort
    # after the receipt being finished; its own large result must not evict it.
    count = 70
    while True:
        seeded = json.dumps(
            _finished_receipts(count, result_bytes=16_000, age=-timedelta(hours=1)),
            indent=2,
            sort_keys=True,
        )
        if len(seeded.encode()) <= MAX_RECEIPT_BYTES - 1024:
            break
        count -= 1
    path = tmp_path / "owner-operations.json"
    path.write_text(seeded)
    path.chmod(0o600)
    worker = OwnerOperations(tmp_path)
    try:
        receipt = worker.submit(
            "refresh",
            key="clock-moved-back-key",
            inputs={},
            execute=lambda: {"message": "y" * (MAX_RESULT_BYTES - 64)},
        )
        result = completed(lambda: worker.status(str(receipt["operation_id"])))
        assert result["status"] == "succeeded"
        assert len(path.read_bytes()) <= MAX_RECEIPT_BYTES
    finally:
        worker.close()


def test_status_poll_after_a_crash_never_evicts_the_polled_receipt(tmp_path: Path) -> None:
    # A store just under the size limit with one receipt left running by a
    # crash. Recording "Interrupted" during a status poll must not evict the
    # finished receipt being polled, even though it is the oldest.
    seeded = _finished_receipts(60, result_bytes=16_000, age=timedelta(hours=1))
    rows = cast(list[dict[str, object]], seeded["receipts"])
    rows[-1].update(status="running", result=None)

    def size() -> int:
        return len(json.dumps(seeded, indent=2, sort_keys=True).encode())

    first = cast(dict[str, object], rows[0]["result"])
    first["message"] = str(first["message"]) + "x" * (MAX_RECEIPT_BYTES - 16 - size())
    assert MAX_RECEIPT_BYTES - 32 < size() <= MAX_RECEIPT_BYTES
    path = tmp_path / "owner-operations.json"
    path.write_text(json.dumps(seeded, indent=2, sort_keys=True))
    path.chmod(0o600)
    worker = OwnerOperations(tmp_path)
    try:
        polled = worker.status(str(rows[0]["operation_id"]))
        assert polled["status"] == "succeeded"
        saved = {row["operation_id"]: row for row in json.loads(path.read_text())["receipts"]}
        assert saved[rows[0]["operation_id"]]["status"] == "succeeded"
        assert saved[rows[-1]["operation_id"]]["status"] == "interrupted"
        assert len(path.read_bytes()) <= MAX_RECEIPT_BYTES
    finally:
        worker.close()


def test_eviction_keeps_unfinished_and_current_receipts(tmp_path: Path) -> None:
    worker = OwnerOperations(tmp_path)
    old = datetime.now(UTC) - timedelta(days=1)
    rows = [
        OperationReceipt(
            f"{index:032x}",
            f"{index:064x}",
            "0" * 64,
            "refresh",
            "running" if index == 0 else "succeeded",
            old.isoformat(),
            (old + timedelta(seconds=index)).isoformat(),
        )
        for index in range(MAX_RECEIPTS)
    ]
    newest = replace(rows[-1], operation_id="f" * 32, key_hash="f" * 64, status="pending")
    worker._write([*rows, newest], keep=frozenset({newest.operation_id}))
    kept = {row.operation_id for row in worker._read()}
    assert rows[0].operation_id in kept  # oldest, but still running
    assert newest.operation_id in kept
    assert rows[1].operation_id not in kept  # oldest finished outcome
    assert len(kept) == MAX_RECEIPTS
    unfinished = [replace(row, status="running") for row in rows]
    with pytest.raises(OwnerOperationError, match="unfinished"):
        worker._write([*unfinished, newest], keep=frozenset({newest.operation_id}))
    worker.close()


@pytest.mark.parametrize(
    ("kind", "error", "shown"),
    [
        (
            "import-preview",
            DataSourceError("eBird archive is missing required columns: Date"),
            "eBird archive is missing required columns: Date",
        ),
        (
            "import-commit",
            SettingsConflictError("Settings changed. Reload and review this action again."),
            "Settings changed. Reload and review this action again.",
        ),
        (
            "import-preview",
            DataSourceError("Invalid taxonomy crosswalk: /private/state/crosswalk.json"),
            None,
        ),
        ("refresh", DataSourceError("HTTP 500 from api.example.test"), None),
    ],
)
def test_receipts_keep_safe_import_and_settings_messages(
    tmp_path: Path, kind: OperationKind, error: Exception, shown: str | None
) -> None:
    worker = OwnerOperations(tmp_path)

    def fail() -> dict[str, object]:
        raise error

    try:
        receipt = worker.submit(kind, key="safe-message-synthetic", inputs={}, execute=fail)
        result = completed(lambda: worker.status(str(receipt["operation_id"])))
        assert result["status"] == "failed"
        message = cast(dict[str, object], result["result"])["message"]
        if shown is None:
            assert str(message).startswith("The operation did not finish.")
            assert "private" not in worker.path.read_text()
        else:
            assert message == shown
        assert cast(dict[str, object], result["result"])["error_type"] == type(error).__name__
    finally:
        worker.close()


@pytest.mark.parametrize(
    "message",
    [
        "Could not open ~/exports/MyEBirdData.csv",
        "Lookup failed at https://api.example.test/v2/ref",
        "Invalid taxonomy crosswalk: C:\\state\\crosswalk.json",
        "Invalid taxonomy crosswalk (/state/crosswalk.json)",
    ],
)
def test_receipts_hide_import_messages_that_name_a_path_or_url(
    tmp_path: Path, message: str
) -> None:
    worker = OwnerOperations(tmp_path)

    def fail() -> dict[str, object]:
        raise DataSourceError(message)

    try:
        receipt = worker.submit(
            "import-preview", key="path-message-synthetic", inputs={}, execute=fail
        )
        result = completed(lambda: worker.status(str(receipt["operation_id"])))
        shown = str(cast(dict[str, object], result["result"])["message"])
        assert shown.startswith("The operation did not finish.")
    finally:
        worker.close()


def test_receipts_show_real_missing_column_list_from_a_wrong_upload(tmp_path: Path) -> None:
    archive = tmp_path / "upload.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(CSV_FILENAME, "Name,Value\na,b\n")
    worker = OwnerOperations(tmp_path / "state")
    try:
        receipt = worker.submit(
            "import-preview",
            key="wrong-upload-synthetic",
            inputs={},
            execute=lambda: {"rows": len(preview_ebird_checklists(archive))},
        )
        result = completed(lambda: worker.status(str(receipt["operation_id"])))
        assert result["status"] == "failed"
        message = str(cast(dict[str, object], result["result"])["message"])
        assert message.startswith("eBird archive is missing required columns: ")
        assert "State/Province" in message
        assert "Date" in message
    finally:
        worker.close()
