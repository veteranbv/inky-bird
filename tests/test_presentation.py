from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from inky_bird_frame.access import AccessDeniedError, AccessRateLimitError, AccessStore
from inky_bird_frame.config import ControllerConfig, OwnerAccessMode
from inky_bird_frame.http import write_json_atomic
from inky_bird_frame.journal import (
    JournalConflictError,
    JournalError,
    JournalService,
    remove_collection,
    save_collection,
)
from inky_bird_frame.presentation import MAX_GRANTS, MAX_PENDING, PresentationAccess
from inky_bird_frame.server import project_catalog
from inky_bird_frame.web import create_app

ORIGIN = "https://inky.example.test"
STAMP = "2026-10-15T12:00:00+00:00"
PNG = b"synthetic approved image fixture"


def fixture(root: Path) -> tuple[ControllerConfig, JournalService, PresentationAccess]:
    config = ControllerConfig(
        root / "work",
        root / "catalog",
        root / "state",
        Path("codex"),
        None,
        "127.0.0.1",
        8000,
        3,
        1,
        3,
        owner_allowed_origins=(ORIGIN,),
        embed_allowed_origins=("https://dashboard.example.test",),
    )
    species = config.catalog_dir / "species/1-robin"
    species.mkdir(parents=True)
    for kind in ("portrait", "display"):
        (species / f"{kind}.png").write_bytes(PNG)
    entry = {
        "taxon_id": 1,
        "common_name": "Robin",
        "scientific_name": "Turdus migratorius",
        "slug": "robin",
        "portrait_path": "species/1-robin/portrait.png",
        "display_path": "species/1-robin/display.png",
        "portrait_sha256": hashlib.sha256(PNG).hexdigest(),
        "display_sha256": hashlib.sha256(PNG).hexdigest(),
        "approved_at": STAMP,
    }
    write_json_atomic(
        config.catalog_dir / "index.json",
        {"schema_version": 1, "generated_at": STAMP, "species": [entry]},
    )
    write_json_atomic(
        config.state_dir / "active-catalog.json",
        {"schema_version": 1, "generated_at": STAMP, "species": []},
    )
    journal = JournalService(config)
    return config, journal, PresentationAccess(config.state_dir, journal)


def test_pairing_scopes_and_revocation_are_durable_and_not_owner_authority(tmp_path: Path) -> None:
    config, journal, access = fixture(tmp_path)
    secret, pending = access.request_pair("")
    assert pending["status"] == "pending"
    assert access.request_pair(secret)[0] == secret
    with pytest.raises(AccessDeniedError):
        access.authenticate(secret, "viewer")
    status = cast(dict[str, Any], access.status())
    grant = status["grants"][0]
    access.update(
        revision=status["revision"],
        action="pair",
        id=grant["id"],
        name="Office screen",
        view="all",
        confirm=True,
    )
    assert access.authenticate(secret, "viewer").view == "all"
    with pytest.raises(AccessDeniedError):
        access.authenticate(secret, "status")
    body = access.path.read_text()
    assert secret not in body and access.path.stat().st_mode & 0o777 == 0o600
    assert "secret_hash" not in json.dumps(access.status())
    assert len(journal.view_entries("all")) == 1
    assert journal.view_entries("current") == []
    access.update(
        revision=access.status()["revision"], action="revoke", id=grant["id"], confirm=True
    )
    with pytest.raises(AccessDeniedError):
        PresentationAccess(config.state_dir, journal).authenticate(secret, "viewer")


def test_only_current_wall_retains_detection_priority_without_screen_disclosure(
    tmp_path: Path,
) -> None:
    config, journal, access = fixture(tmp_path)
    entry = journal.view_entries("all")[0]
    write_json_atomic(
        config.state_dir / "active-catalog.json",
        {
            "schema_version": 1,
            "generated_at": STAMP,
            "species": [{**entry, "latest_detection_at": STAMP}],
        },
    )
    assert access.selected("wall")[0]["latest_detection_at"] == STAMP
    assert "latest_detection_at" not in journal.view_entries("current")[0]
    assert "latest_detection_at" not in journal.view_entries("all")[0]


def test_empty_or_removed_wall_selection_is_explicit_and_never_falls_back(tmp_path: Path) -> None:
    config, journal, access = fixture(tmp_path)
    group = save_collection(
        config.state_dir, revision=0, label="Temporary selection", taxon_ids=[1]
    )
    selected = group.collections[-1].collection_id
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        access.update(
            revision=access.status()["revision"], action="wall", view="favorites", confirm=True
        )
        empty = client.get("/wall/v1/catalog").json()
        assert empty["species"] == [] and empty["empty_selection"] is True
        access.update(
            revision=access.status()["revision"], action="wall", view=selected, confirm=True
        )
        assert client.get("/wall/v1/catalog").json()["empty_selection"] is False
        remove_collection(config.state_dir, revision=group.revision, collection_id=selected)
        removed = client.get("/wall/v1/catalog").json()
        assert removed["species"] == [] and removed["empty_selection"] is True
        assert len(journal.view_entries("all")) == 1


def test_grants_expire_without_poll_renewal_and_pairing_is_bounded(tmp_path: Path) -> None:
    _, _, access = fixture(tmp_path)
    first, _ = access.request_pair("")
    with pytest.raises(AccessRateLimitError):
        access.request_pair("")
    status = cast(dict[str, Any], access.status())
    access.update(
        revision=status["revision"],
        action="pair",
        id=status["grants"][0]["id"],
        name="Fixture",
        view="all",
        days=1,
        confirm=True,
    )
    initial = access.authenticate(first, "viewer")
    assert access.authenticate(first, "viewer").expires_at == initial.expires_at
    with patch("inky_bird_frame.presentation.datetime") as clock:
        clock.now.return_value = datetime.now(UTC) + timedelta(days=2)
        with pytest.raises(AccessDeniedError):
            access.authenticate(first, "viewer")
        assert access.status()["grants"] == []


def test_status_credentials_are_distinct_scoped_private_and_cas_guarded(tmp_path: Path) -> None:
    _, journal, access = fixture(tmp_path)
    group = save_collection(
        journal.config.state_dir, revision=0, label="Private trip name", taxon_ids=[]
    ).collections[0]
    result = access.update(
        revision=0, action="status", name="HA", view=group.collection_id, days=30, confirm=True
    )
    assert access.authenticate(cast(str, result["secret"]), "status").view == group.collection_id
    with pytest.raises(AccessDeniedError):
        access.authenticate(cast(str, result["secret"]), "viewer")
    with pytest.raises(JournalConflictError):
        access.update(revision=0, action="wall", view="all", confirm=True)
    with pytest.raises(JournalError):
        access.update(revision=1, action="wall", view="not-a-view", confirm=True)
    assert cast(str, result["secret"]) not in access.path.read_text()


@pytest.mark.parametrize("allow_origin", [False, True])
def test_local_wall_hostname_needs_its_documented_origin(
    tmp_path: Path, allow_origin: bool
) -> None:
    config, _, _ = fixture(tmp_path)
    origin = "http://inky-controller:8793"
    config = replace(
        config,
        bind_host="0.0.0.0",
        port=8793,
        owner_access_mode=OwnerAccessMode.LOCAL,
        owner_allowed_origins=(origin,) if allow_origin else (),
    )
    with TestClient(create_app(config), base_url=origin, client=("192.168.1.20", 42000)) as client:
        for path in ("/wall/health", "/wall/v1/catalog"):
            response = client.get(path)
            assert response.status_code == (200 if allow_origin else 403)


def test_deleted_status_view_is_empty_without_falling_back_to_other_art(tmp_path: Path) -> None:
    config, journal, access = fixture(tmp_path)
    group = save_collection(config.state_dir, revision=0, label="Selected", taxon_ids=[1])
    selected = group.collections[0]
    credential = access.update(
        revision=0, action="status", name="Status", view=selected.collection_id, confirm=True
    )
    headers = {"Authorization": "Bearer " + cast(str, credential["secret"])}
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        assert (
            client.get("/v1/integrations/status", headers=headers).json()["data"][
                "view_ready_count"
            ]
            == 1
        )
        remove_collection(
            config.state_dir, revision=group.revision, collection_id=selected.collection_id
        )
        result = client.get("/v1/integrations/status", headers=headers)
        assert result.status_code == 200
        assert result.json()["data"]["view_ready_count"] == 0
        assert result.json()["data"]["approved_count"] == 1
        assert len(journal.view_entries("all")) == 1


def test_readonly_http_boundaries_selected_wall_and_minimal_embed(tmp_path: Path) -> None:
    config, _, access = fixture(tmp_path)
    with (
        TestClient(create_app(config), base_url=ORIGIN) as owner,
        TestClient(create_app(config), base_url=ORIGIN) as screen,
    ):
        # Pairing is off until the owner exists: not a sign-in problem.
        early = screen.post("/v1/screen/pair", json={}, headers={"Origin": ORIGIN})
        assert (early.status_code, early.json()["error"]["code"]) == (403, "feature_unavailable")
        AccessStore(config.state_dir).setup_owner("fixture-owner", "long synthetic password")
        login = owner.post(
            "/v1/owner/login",
            json={"username": "fixture-owner", "passphrase": "long synthetic password"},
            headers={"Origin": ORIGIN},
        )
        headers = {"Origin": ORIGIN, "X-Inky-CSRF": login.json()["data"]["csrf"]}
        assert screen.post("/v1/screen/pair", json={}).status_code == 403
        assert (
            screen.post("/v1/screen/pair", json={}, headers={"Origin": ORIGIN}).status_code == 200
        )
        state = owner.get("/v1/owner/presentation").json()["data"]
        assert (
            owner.post(
                "/v1/owner/presentation",
                json={
                    "revision": state["revision"],
                    "action": "pair",
                    "id": state["grants"][0]["id"],
                    "name": "Read only",
                    "view": "all",
                    "confirm": True,
                },
                headers=headers,
            ).status_code
            == 200
        )
        view = screen.get("/v1/screen/view").json()["data"]
        plate_url = view["species"][0]["portrait_url"]
        assert (
            plate_url.startswith("/v1/screen/plates/1?revision=") and "thumbnail" not in plate_url
        )
        plate = screen.get(plate_url)
        assert plate.content == PNG and plate.headers["content-type"] == "image/png"
        # The paired screen keeps the full plate; its revisioned URL may be cached.
        assert plate.headers["cache-control"] == "private, max-age=31536000, immutable"
        assert screen.get("/v1/owner/journal").status_code == 401
        assert screen.get("/v1/screen/plates/999?revision=" + "a" * 64).status_code == 404
        art_off = screen.get("/v1/dashboard/art")
        assert (art_off.status_code, art_off.json()["error"]["code"]) == (
            403,
            "feature_unavailable",
        )
        assert (
            owner.post(
                "/v1/owner/presentation",
                json={
                    "revision": access.status()["revision"],
                    "action": "dashboard",
                    "view": "all",
                    "confirm": True,
                },
                headers=headers,
            ).status_code
            == 200
        )
        embed = screen.get("/v1/dashboard/art")
        assert embed.status_code == 200 and PNG not in embed.content
        directives = {
            parts[0]: parts[1:]
            for directive in embed.headers["content-security-policy"].split(";")
            if (parts := directive.split())
        }
        assert directives["frame-ancestors"] == ["'self'", "https://dashboard.example.test"]
        assert screen.get("/v1/dashboard/assets/species/1-robin/portrait.png").content == PNG
        assert screen.get("/v1/dashboard/assets/../presentation-access.json").status_code == 404
        legacy = owner.get("/v1/catalog").json()
        assert (
            owner.post(
                "/v1/owner/presentation",
                json={
                    "revision": access.status()["revision"],
                    "action": "wall",
                    "view": "all",
                    "confirm": True,
                },
                headers=headers,
            ).status_code
            == 200
        )
        wall = owner.get("/wall/v1/catalog").json()
        project_catalog(wall)
        assert wall["empty_selection"] is False
        assert len(wall["species"]) == 1 and owner.get("/v1/catalog").json() == legacy
        health = owner.get("/wall/health")
        assert health.status_code == 200 and health.json()["ok"] is True
        assert health.json()["active_species"] == 1
        assert owner.get("/health").json()["active_species"] == 0
        wall_asset = owner.get("/wall/v1/assets/species/1-robin/display.png")
        assert wall_asset.content == PNG and wall_asset.headers["content-type"] == "image/png"
        # Display-node assets are unchanged: the full approved bytes, never cached.
        assert wall_asset.headers["cache-control"] == "no-store"
        report = owner.post("/wall/v1/display-success", json={"schema_version": 1})
        assert report.status_code == 200
        credentials = owner.post(
            "/v1/owner/presentation",
            json={
                "revision": access.status()["revision"],
                "action": "status",
                "name": "Fixture sensor",
                "view": "all",
                "confirm": True,
            },
            headers=headers,
        ).json()["data"]
        aggregate = screen.get(
            "/v1/integrations/status", headers={"Authorization": "Bearer " + credentials["secret"]}
        )
        assert aggregate.status_code == 200
        assert set(aggregate.json()["data"]) == {
            "version",
            "service_ready",
            "approved_count",
            "view_ready_count",
            "discovery_state",
            "panel_health",
        }
        assert (
            screen.get("/v1/integrations/status?token=" + credentials["secret"]).status_code == 401
        )
        assert (
            screen.get(
                "/v1/owner/journal", headers={"Authorization": "Bearer " + credentials["secret"]}
            ).status_code
            == 401
        )


def test_insecure_or_corrupt_presentation_state_fails_closed(tmp_path: Path) -> None:
    _, _, access = fixture(tmp_path)
    access.path.write_text('{"private":"MUST_NOT_LEAK"}')
    access.path.chmod(0o644)
    with pytest.raises(JournalError, match="Presentation access state") as error:
        access.status()
    assert "MUST_NOT_LEAK" not in str(error.value)
    access.path.unlink()
    access.path.symlink_to(tmp_path / "missing.json")
    with pytest.raises(JournalError):
        access.status()


def test_https_only_blocks_direct_backend_for_all_new_surfaces(tmp_path: Path) -> None:
    config, _, _ = fixture(tmp_path)
    with TestClient(
        create_app(replace(config, owner_access_mode=OwnerAccessMode.HTTPS_ONLY)),
        base_url="http://inky.example.test",
    ) as client:
        for path in (
            "/v1/screen/view",
            "/v1/integrations/status",
            "/v1/dashboard/art",
            "/wall/v1/catalog",
        ):
            assert client.get(path).status_code == 403


def test_local_owner_access_does_not_permit_plain_lan_screen_credentials(tmp_path: Path) -> None:
    config, _, access = fixture(tmp_path)
    origin = "http://inky.example.test"
    config = replace(
        config, owner_access_mode=OwnerAccessMode.LOCAL, owner_allowed_origins=(origin,)
    )
    with TestClient(create_app(config), base_url=origin, client=("192.168.1.20", 41000)) as client:
        assert client.get("/v1/access").status_code == 200
        result = client.post("/v1/screen/pair", json={}, headers={"Origin": origin})
        assert (result.status_code, result.json()["error"]["code"]) == (
            403,
            "secure_access_required",
        )
        assert "HTTPS or" in result.json()["error"]["message"]
        assert "inky-local-screen" not in client.cookies
        view = client.get("/v1/screen/view")
        assert (view.status_code, view.json()["error"]["code"]) == (403, "secure_access_required")
        assert access.status()["grants"] == []


def test_screen_plate_replaced_after_authorization_is_not_cached_as_the_old_revision(
    tmp_path: Path,
) -> None:
    config, _, access = fixture(tmp_path)
    secret, _ = access.request_pair("")
    status = cast(dict[str, Any], access.status())
    access.update(
        revision=status["revision"],
        action="pair",
        id=status["grants"][0]["id"],
        name="Office screen",
        view="all",
        confirm=True,
    )
    old = hashlib.sha256(PNG).hexdigest()
    replacement = b"synthetic replacement image fixture"
    original = JournalService.view_entries

    def replace_after_read(
        self: JournalService, view: str, **options: bool
    ) -> list[dict[str, object]]:
        entries = original(self, view, **options)
        # The plate changes between authorizing the revision and reading the bytes.
        (config.catalog_dir / "species/1-robin/portrait.png").write_bytes(replacement)
        index = json.loads((config.catalog_dir / "index.json").read_text())
        index["species"][0]["portrait_sha256"] = hashlib.sha256(replacement).hexdigest()
        write_json_atomic(config.catalog_dir / "index.json", index)
        return entries

    with TestClient(create_app(config), base_url=ORIGIN) as screen:
        screen.cookies.set("__Host-inky-screen", secret)
        with patch.object(JournalService, "view_entries", replace_after_read):
            plate = screen.get(f"/v1/screen/plates/1?revision={old}")
        assert plate.status_code == 200
        assert plate.content == replacement
        assert plate.headers["cache-control"] == "no-store"
        current = screen.get(
            f"/v1/screen/plates/1?revision={hashlib.sha256(replacement).hexdigest()}"
        )
        assert current.content == replacement
        assert current.headers["cache-control"] == "private, max-age=31536000, immutable"


class _Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 15, 12, tzinfo=UTC)

    def now(self, tz: object = None) -> datetime:
        return self.at


def test_a_waiting_screen_keeps_its_code_while_another_client_keeps_asking(
    tmp_path: Path,
) -> None:
    # Called exactly as the web route calls it: no client is named.
    _, _, access = fixture(tmp_path)
    clock = _Clock()
    with patch("inky_bird_frame.presentation.datetime", clock):
        screen, first = access.request_pair("")
        for _ in range(MAX_PENDING - 1):
            clock.at += timedelta(seconds=16)
            access.request_pair("")
        saved = access.path.read_bytes()
        clock.at += timedelta(seconds=16)
        # The pending allowance is full: refuse the newcomer, never evict the
        # screen whose code the owner may be about to approve.
        with pytest.raises(AccessRateLimitError):
            access.request_pair("")
        assert access.path.read_bytes() == saved
        resumed = access.request_pair(screen)[1]
        assert resumed == {"status": "pending", "pairing_code": first["pairing_code"]}


def test_pairing_pause_is_per_client_and_forgets_finished_pauses(tmp_path: Path) -> None:
    _, _, access = fixture(tmp_path)
    clock = _Clock()
    with patch("inky_bird_frame.presentation.datetime", clock):
        access.request_pair("", client="198.51.100.7")
        access.request_pair("", client="192.0.2.1")
        saved = access.path.read_bytes()
        with pytest.raises(AccessRateLimitError):
            access.request_pair("", client="198.51.100.7")
        assert access.path.read_bytes() == saved
        clock.at += timedelta(seconds=16)
        access.request_pair("", client="198.51.100.7")
        # Only pauses still running are remembered, so the table stays as
        # small as the pending allowance and no entry is dropped early.
        assert set(access._pair_requests) == {"198.51.100.7"}


def test_a_full_screen_list_still_refuses_new_pairing(tmp_path: Path) -> None:
    _, _, access = fixture(tmp_path)
    for index in range(MAX_GRANTS):
        access.update(
            revision=access.status()["revision"],
            action="status",
            name=f"Credential {index}",
            view="all",
            confirm=True,
        )
    with pytest.raises(AccessRateLimitError):
        access.request_pair("", client="198.51.100.7")
