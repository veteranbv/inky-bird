"""One table over every web route: unauthenticated, wrong Origin, wrong method and HEAD."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from httpx2 import Response
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from inky_bird_frame.config import ControllerConfig
from inky_bird_frame.web import HTTP_ERROR_CODES, STATIC_PAGES, OwnerBoundary, create_app

ORIGIN = "https://inky.example.test"
WEB_ASSETS = Path(__file__).resolve().parents[1] / "src/inky_bird_frame/web"
SAMPLES = {"int": "1", "path": "species/1-example/portrait.png"}

# What a secure, signed-out browser receives on each route. Routes that must
# answer without an owner session are listed with their success status.
UNAUTHENTICATED = {
    ("GET", "/"): 307,
    **{("GET", path): 200 for path in STATIC_PAGES},
    ("GET", "/v1/access"): 200,
    ("POST", "/v1/owner/setup"): 400,
    ("POST", "/v1/owner/register"): 400,
    ("POST", "/v1/owner/login"): 400,
    ("GET", "/v1/owner/session"): 401,
    ("POST", "/v1/owner/logout"): 401,
    ("GET", "/v1/owner/sessions"): 401,
    ("POST", "/v1/owner/activity"): 401,
    ("POST", "/v1/owner/sessions/revoke"): 401,
    ("POST", "/v1/owner/confirm-password"): 401,
    ("GET", "/v1/owner/access"): 401,
    ("POST", "/v1/owner/account"): 401,
    # Single sign-on is not configured here: the feature is off, not signed out.
    ("POST", "/v1/owner/oidc/start"): 403,
    ("POST", "/v1/owner/oidc/link"): 403,
    ("POST", "/v1/owner/oidc/unlink"): 401,
    ("GET", "/v1/owner/oidc/callback"): 303,
    ("GET", "/v1/owner/journal"): 401,
    ("GET", "/v1/owner/management"): 401,
    ("GET", "/v1/owner/health"): 401,
    ("GET", "/v1/owner/settings"): 401,
    ("POST", "/v1/owner/settings"): 401,
    ("GET", "/v1/owner/operations"): 401,
    ("POST", "/v1/owner/operations"): 401,
    ("GET", "/v1/owner/operations/{operation_id}"): 401,
    ("GET", "/v1/owner/imports"): 401,
    ("GET", "/v1/owner/community"): 401,
    ("POST", "/v1/owner/community/{action}"): 401,
    ("GET", "/v1/owner/presentation"): 401,
    ("POST", "/v1/owner/presentation"): 401,
    ("POST", "/v1/owner/saved/{action}"): 401,
    ("POST", "/v1/screen/pair"): 401,
    ("POST", "/v1/screen/forget"): 200,
    ("GET", "/v1/screen/view"): 401,
    ("GET", "/v1/screen/plates/{taxon_id:int}"): 401,
    ("GET", "/v1/integrations/status"): 401,
    ("GET", "/v1/dashboard/art"): 401,
    ("GET", "/v1/dashboard/assets/{asset:path}"): 401,
    ("GET", "/wall/health"): 200,
    ("GET", "/wall/v1/catalog"): 200,
    ("GET", "/wall/v1/assets/{asset:path}"): 404,
    # Display telemetry refuses any browser Origin.
    ("POST", "/wall/v1/display-fetch"): 403,
    ("POST", "/wall/v1/display-success"): 403,
    ("GET", "/v1/owner/birdbuddy"): 401,
    ("POST", "/v1/owner/birdbuddy/{action}"): 401,
    ("POST", "/v1/owner/imports/upload/{kind}"): 401,
    ("POST", "/v1/owner/imports/{action}"): 401,
    ("GET", "/v1/owner/journal/birds/{taxon_id:int}/{kind}"): 401,
    ("GET", "/v1/owner/candidates/{taxon_id:int}"): 401,
    ("GET", "/v1/owner/candidates/{taxon_id:int}/{action}"): 401,
    ("POST", "/v1/owner/candidates/{taxon_id:int}/{action}"): 401,
    ("GET", "/v1/owner/journal/collections/{action}"): 401,
    ("POST", "/v1/owner/journal/collections/{action}"): 401,
}
LEGACY_ENVELOPE_ROUTES = {"/wall/v1/display-fetch", "/wall/v1/display-success"}


def _config(root: Path) -> ControllerConfig:
    return ControllerConfig(
        workspace_dir=root / "work",
        catalog_dir=root / "catalog",
        state_dir=root / "state",
        codex_path=Path("codex"),
        codex_model=None,
        bind_host="127.0.0.1",
        port=8000,
        references_per_species=3,
        generations_per_cycle=1,
        max_generation_attempts=3,
        owner_allowed_origins=(ORIGIN,),
    )


def _routes(tmp_path: Path) -> list[Route]:
    boundary = create_app(_config(tmp_path))
    assert isinstance(boundary, OwnerBoundary) and isinstance(boundary.app, Starlette)
    routes = [route for route in boundary.app.routes if isinstance(route, Route)]
    assert routes[-1].path == "/{path:path}"
    return routes[:-1]


def _sample(route: str) -> str:
    return re.sub(
        r"\{(\w+)(?::(\w+))?\}", lambda m: SAMPLES.get(m.group(2) or "", "example"), route
    )


def _assert_envelope(response: Response, path: str) -> None:
    if response.status_code < 400:
        return
    body = response.json()
    if path in LEGACY_ENVELOPE_ROUTES:
        assert body["ok"] is False and isinstance(body["error"], str)
        assert body["schema_version"] == 1
        return
    assert set(body) == {"ok", "error", "schema_version"}, (path, body)
    assert body["ok"] is False and body["schema_version"] == 1
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] in HTTP_ERROR_CODES, (path, body)


def test_route_table_lists_every_route(tmp_path: Path) -> None:
    served = {
        (method, route.path)
        for route in _routes(tmp_path)
        for method in route.methods or ()
        if method != "HEAD"
    }
    assert served == set(UNAUTHENTICATED)


def test_every_route_answers_a_signed_out_browser_with_an_envelope(tmp_path: Path) -> None:
    with TestClient(create_app(_config(tmp_path)), base_url=ORIGIN) as client:
        for (method, route), expected in UNAUTHENTICATED.items():
            path = _sample(route)
            response = client.request(
                method,
                path,
                json={} if method == "POST" else None,
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            assert response.status_code == expected, (method, path, response.text)
            _assert_envelope(response, route)
            assert "__Host-inky-owner" not in response.headers.get("set-cookie", "")


def test_every_private_change_refuses_a_foreign_origin(tmp_path: Path) -> None:
    changes = [route for method, route in UNAUTHENTICATED if method == "POST"]
    assert changes
    with TestClient(create_app(_config(tmp_path)), base_url=ORIGIN) as client:
        for route in changes:
            path = _sample(route)
            for headers in (
                {"Origin": "https://attacker.example.test"},
                {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
                {},
            ):
                if route in LEGACY_ENVELOPE_ROUTES and not headers.get("Origin"):
                    continue  # Display nodes send no Origin; that is their contract.
                response = client.post(path, json={}, headers=headers)
                assert response.status_code == 403, (path, headers, response.text)
                _assert_envelope(response, route)
                if route not in LEGACY_ENVELOPE_ROUTES:
                    assert response.json()["error"]["code"] == "origin_denied"


def test_every_route_refuses_an_unsupported_method_with_an_envelope(tmp_path: Path) -> None:
    routes = _routes(tmp_path)
    with TestClient(create_app(_config(tmp_path)), base_url=ORIGIN) as client:
        for route in routes:
            path = _sample(route.path)
            allowed = {
                method
                for candidate in routes
                if candidate.path_regex.match(path)
                for method in candidate.methods or ()
            }
            for method in sorted({"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"} - allowed):
                response = client.request(method, path, headers={"Origin": ORIGIN})
                assert response.status_code == 405, (method, path, response.text)
                assert response.json()["error"]["code"] == "method_not_allowed"
                assert set(response.headers["allow"].split(", ")) == allowed, (method, path)
                _assert_envelope(response, "")


def test_head_answers_like_get_without_a_body(tmp_path: Path) -> None:
    with TestClient(create_app(_config(tmp_path)), base_url=ORIGIN) as client:
        for (method, route), _ in UNAUTHENTICATED.items():
            if method != "GET" or route == "/v1/owner/oidc/callback":
                continue
            path = _sample(route)
            get = client.get(path, follow_redirects=False)
            head = client.head(path, follow_redirects=False)
            assert head.status_code == get.status_code, (path, head.status_code)
            assert head.content == b""
            assert head.headers.get("content-type") == get.headers.get("content-type")
            assert head.headers.get("content-length") == get.headers.get("content-length")
        callback = client.head("/v1/owner/oidc/callback")
        assert callback.status_code == 405
        assert callback.headers["allow"] == "GET"


@pytest.mark.parametrize(
    "path",
    ["/v1/owner/nope", "/v1/owner/journal/birds/1/portrait/extra", "/v1/screen/nope", "/wall/nope"],
)
def test_unknown_private_paths_are_enveloped_not_found(tmp_path: Path, path: str) -> None:
    with TestClient(create_app(_config(tmp_path)), base_url=ORIGIN) as client:
        for method in ("GET", "POST"):
            response = client.request(method, path, headers={"Origin": ORIGIN})
            assert response.status_code == 404, (method, path)
            assert response.json() == {
                "ok": False,
                "error": {"code": "not_found", "message": response.json()["error"]["message"]},
                "schema_version": 1,
            }


def test_static_map_matches_routes_and_shipped_files(tmp_path: Path) -> None:
    pages = {route.path for route in _routes(tmp_path) if route.endpoint.__name__ == "page"}
    assert pages == set(STATIC_PAGES)
    shipped = {path.name for path in WEB_ASSETS.iterdir() if path.is_file()}
    # The access guide is the bundled copy of docs/browser-access.md.
    assert set(STATIC_PAGES.values()) - {"access-guide.txt"} == shipped


def test_published_error_codes_cover_every_code_web_py_returns() -> None:
    source = (Path(__file__).resolve().parents[1] / "src/inky_bird_frame/web.py").read_text()
    used = set(re.findall(r'_error\(\s*\d{3},\s*"([a-z_]+)"', source))
    assert used
    assert used <= set(HTTP_ERROR_CODES)
    assert len(HTTP_ERROR_CODES) == len(set(HTTP_ERROR_CODES))
    assert json.dumps(sorted(HTTP_ERROR_CODES))  # Plain strings only.
