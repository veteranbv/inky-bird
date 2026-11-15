from __future__ import annotations

import asyncio
import gzip
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from joserfc import jwt
from joserfc.jwk import ECKey, RSAKey

from inky_bird_frame import oidc
from inky_bird_frame.access import CLOCK_SKEW_SECONDS, RECENT_AUTH_SECONDS, OIDCTransaction
from inky_bird_frame.config import OIDCConfig
from inky_bird_frame.oidc import (
    MAX_OIDC_RESPONSE_BYTES,
    BoundedTransport,
    OIDCError,
    authorization_url,
    metadata,
    provider_id,
    verified_identity,
)


@pytest.fixture(autouse=True)
def _fresh_metadata_cache() -> Iterator[None]:
    oidc._metadata_cache.clear()
    yield
    oidc._metadata_cache.clear()


CONFIG = OIDCConfig(
    "https://id.example.test/application/o/inky/",
    "synthetic-client",
    "https://inky.example.test/v1/owner/oidc/callback",
    "SYNTHETIC_OIDC_SECRET",
)
TX = OIDCTransaction(
    "synthetic-state",
    "browser-hash",
    "synthetic-nonce",
    "v" * 64,
    provider_id(CONFIG),
    time.time(),
    False,
)


def _document() -> dict[str, object]:
    return {
        "issuer": CONFIG.issuer,
        "authorization_endpoint": "https://id.example.test/authorize",
        "token_endpoint": "https://id.example.test/token",
        "jwks_uri": "https://id.example.test/keys",
        "code_challenge_methods_supported": ["S256"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }


@pytest.mark.parametrize("confirm", [False, True])
def test_authorization_uses_code_pkce_minimal_scope_and_exact_callback(confirm: bool) -> None:
    with (
        patch.dict("os.environ", {CONFIG.client_secret_env: "synthetic-secret"}),
        patch(
            "inky_bird_frame.oidc.httpx2.AsyncHTTPTransport",
            return_value=httpx2.MockTransport(
                lambda request: httpx2.Response(200, json=_document())
            ),
        ),
    ):
        url = asyncio.run(authorization_url(CONFIG, replace(TX, return_to_access=confirm)))
    query = parse_qs(urlsplit(url).query)
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid"]
    assert query["redirect_uri"] == [CONFIG.callback_url]
    assert query["state"] == [TX.state]
    assert query["nonce"] == [TX.nonce]
    assert query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in query
    assert query["max_age"] == [str(RECENT_AUTH_SECONDS)]
    assert query.get("prompt") == (["login"] if confirm else None)
    assert "synthetic-secret" not in url and TX.verifier not in url


@pytest.mark.parametrize(
    "bad_claim",
    [
        None,
        "iss",
        "aud",
        "nonce",
        "exp",
        "iat",
        "sub",
        "azp",
        "signature",
        "missing_auth_time",
        "stale_auth_time",
        "future_auth_time",
        "boolean_auth_time",
    ],
)
@pytest.mark.parametrize("algorithm", ["RS256", "ES256"])
def test_real_signed_identity_tokens_are_validated_and_secrets_are_redacted(
    bad_claim: str | None,
    algorithm: str,
) -> None:
    _check_token(bad_claim, algorithm)


def _check_token(bad_claim: str | None, algorithm: str, *, skew: int = 0) -> None:
    key = (
        RSAKey.generate_key(2048, parameters={"kid": "synthetic-signing-key"})
        if algorithm == "RS256"
        else ECKey.generate_key("P-256", parameters={"kid": "synthetic-signing-key"})
    )
    claims: dict[str, str | int] = {
        "iss": CONFIG.issuer,
        "aud": CONFIG.client_id,
        "sub": "owner-subject",
        "nonce": TX.nonce,
        "iat": int(time.time()) + skew,
        "exp": int(time.time()) + 300,
        "auth_time": int(time.time()) - 30 + skew,
    }
    if bad_claim in {"iss", "aud", "nonce", "azp"}:
        claims[str(bad_claim)] = "private-bad-claim"
    elif bad_claim == "exp":
        claims["exp"] = int(time.time()) - CLOCK_SKEW_SECONDS - 1
    elif bad_claim == "iat":
        claims["iat"] = int(time.time()) + CLOCK_SKEW_SECONDS + 300
    elif bad_claim == "sub":
        claims.pop("sub")
    elif bad_claim == "missing_auth_time":
        claims.pop("auth_time")
    elif bad_claim == "stale_auth_time":
        claims["auth_time"] = int(time.time()) - RECENT_AUTH_SECONDS
    elif bad_claim == "future_auth_time":
        claims["auth_time"] = int(time.time()) + CLOCK_SKEW_SECONDS + 300
    elif bad_claim == "boolean_auth_time":
        claims["auth_time"] = True
    token = jwt.encode({"alg": algorithm, "kid": "synthetic-signing-key"}, claims, key)
    if bad_claim == "signature":
        different = (
            RSAKey.generate_key(2048, parameters={"kid": "synthetic-signing-key"})
            if algorithm == "RS256"
            else ECKey.generate_key("P-256", parameters={"kid": "synthetic-signing-key"})
        )
        token = jwt.encode({"alg": algorithm, "kid": "synthetic-signing-key"}, claims, different)

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("openid-configuration"):
            document = _document()
            document["id_token_signing_alg_values_supported"] = [algorithm]
            return httpx2.Response(200, json=document)
        if request.url.path == "/keys":
            return httpx2.Response(200, json={"keys": [key.as_dict()]})
        assert request.url.path == "/token" and request.method == "POST"
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["authorization_code"]
        assert form["code_verifier"] == [TX.verifier]
        assert form["redirect_uri"] == [CONFIG.callback_url]
        assert request.headers["authorization"].startswith("Basic ")
        assert "synthetic-secret" not in str(request.url)
        return httpx2.Response(
            200,
            json={
                "id_token": token,
                "access_token": "private-access-token",
                "token_type": "Bearer",
            },
        )

    with (
        patch.dict("os.environ", {CONFIG.client_secret_env: "synthetic-secret"}),
        patch(
            "inky_bird_frame.oidc.httpx2.AsyncHTTPTransport",
            return_value=httpx2.MockTransport(respond),
        ),
    ):
        if bad_claim is None:
            identity = asyncio.run(verified_identity(CONFIG, TX, "private-authorization-code"))
            assert identity.subject == "owner-subject"
            assert identity.authenticated_at == claims["auth_time"]
        else:
            with pytest.raises(OIDCError) as raised:
                asyncio.run(verified_identity(CONFIG, TX, "private-authorization-code"))
            for secret in (
                token,
                "private-access-token",
                "private-bad-claim",
                "private-authorization-code",
                "synthetic-secret",
            ):
                assert secret not in str(raised.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer", "https://wrong.example.test/"),
        ("token_endpoint", "http://id.example.test/token"),
        ("jwks_uri", "file:///keys"),
        ("code_challenge_methods_supported", ["plain"]),
        ("id_token_signing_alg_values_supported", ["HS256"]),
        ("authorization_endpoint", "https://user:password@id.example.test/auth"),
    ],
)
def test_metadata_fails_closed(field: str, value: object) -> None:
    document = _document()
    document[field] = value
    with (
        patch(
            "inky_bird_frame.oidc.httpx2.AsyncHTTPTransport",
            return_value=httpx2.MockTransport(lambda request: httpx2.Response(200, json=document)),
        ),
        pytest.raises(OIDCError),
    ):
        asyncio.run(metadata(CONFIG))


@pytest.mark.parametrize("compressed", [False, True])
def test_network_transport_caps_decoded_responses(compressed: bool) -> None:
    content = b"x" * (MAX_OIDC_RESPONSE_BYTES + 1)
    headers = {"Content-Encoding": "gzip"} if compressed else {}
    if compressed:
        content = gzip.compress(content)
    transport = BoundedTransport(
        httpx2.MockTransport(lambda request: httpx2.Response(200, content=content, headers=headers))
    )

    async def read() -> None:
        async with httpx2.AsyncClient(transport=transport) as client:
            await client.get("https://id.example.test/metadata")

    with pytest.raises(OIDCError, match="uncompressed" if compressed else "too large"):
        asyncio.run(read())


def test_missing_secret_and_provider_timeout_are_actionable_and_redacted() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch(
            "inky_bird_frame.oidc.httpx2.AsyncHTTPTransport",
            return_value=httpx2.MockTransport(
                lambda request: httpx2.Response(200, json=_document())
            ),
        ),
        pytest.raises(OIDCError, match="secret is unavailable"),
    ):
        asyncio.run(authorization_url(CONFIG, TX))

    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("sensitive-provider-payload", request=request)

    oidc._metadata_cache.clear()  # The first part cached a good document.

    with patch(
        "inky_bird_frame.oidc.httpx2.AsyncHTTPTransport", return_value=httpx2.MockTransport(timeout)
    ):
        with pytest.raises(OIDCError) as raised:
            asyncio.run(metadata(CONFIG))
        assert "sensitive-provider-payload" not in str(raised.value)


def test_identity_configuration_changes_have_a_new_session_binding() -> None:
    assert provider_id(CONFIG) != provider_id(replace(CONFIG, owner_subject="replacement-owner"))
    assert provider_id(CONFIG) != provider_id(
        replace(CONFIG, issuer="https://replacement.example.test/")
    )


@pytest.mark.parametrize("value", ["", " ", "\t\n "])
def test_blank_client_secret_counts_as_missing(value: str) -> None:
    with (
        patch.dict("os.environ", {CONFIG.client_secret_env: value}, clear=True),
        patch(
            "inky_bird_frame.oidc.httpx2.AsyncHTTPTransport",
            return_value=httpx2.MockTransport(
                lambda request: httpx2.Response(200, json=_document())
            ),
        ),
        pytest.raises(OIDCError, match="secret is unavailable"),
    ):
        asyncio.run(authorization_url(CONFIG, TX))


def test_provider_clock_up_to_a_minute_ahead_is_accepted() -> None:
    # A token issued and authenticated just "in the future" by a provider whose
    # clock runs ahead of this controller is still a valid, recent sign-in.
    _check_token(None, "RS256", skew=CLOCK_SKEW_SECONDS - 15)


class _Provider:
    """Counts discovery requests and can be switched to fail or stall."""

    def __init__(self) -> None:
        self.requests = 0
        self.mode = "ok"

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests += 1
        if self.mode == "down":
            raise httpx2.ConnectError("private-provider-detail", request=request)
        return httpx2.Response(200, json=_document())


def _with_provider(provider: _Provider) -> AbstractContextManager[object]:
    return patch(
        "inky_bird_frame.oidc.httpx2.AsyncHTTPTransport",
        return_value=httpx2.MockTransport(provider),
    )


def test_provider_metadata_is_cached_and_shared_by_concurrent_requests() -> None:
    provider = _Provider()

    async def many() -> list[object]:
        return list(await asyncio.gather(*(metadata(CONFIG) for _ in range(8))))

    with _with_provider(provider):
        results = asyncio.run(many())
        assert asyncio.run(metadata(CONFIG)) == results[0]
    assert provider.requests == 1
    assert len(set(map(id, results))) == 1


def test_expired_provider_metadata_is_served_while_it_refreshes() -> None:
    provider = _Provider()
    with _with_provider(provider):
        first = asyncio.run(metadata(CONFIG))
        provider.mode = "down"
        later = oidc.monotonic() + oidc.METADATA_FRESH_SECONDS + 1

        async def stale_read() -> object:
            value = await metadata(CONFIG)
            # The refresh runs in the background and does not delay the answer.
            assert provider.requests == 1
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return value

        with patch("inky_bird_frame.oidc.monotonic", return_value=later):
            assert asyncio.run(stale_read()) == first
            assert provider.requests == 2
            # A failed refresh keeps the last good document and is not retried
            # on every request.
            assert asyncio.run(metadata(CONFIG)) == first
            assert provider.requests == 2


def test_a_provider_failure_is_remembered_briefly_and_then_retried() -> None:
    provider = _Provider()
    provider.mode = "down"
    with _with_provider(provider):
        for _ in range(3):
            with pytest.raises(OIDCError, match="Cannot connect") as raised:
                asyncio.run(metadata(CONFIG))
            assert "private-provider-detail" not in str(raised.value)
        assert provider.requests == 1
        provider.mode = "ok"
        retry = oidc.monotonic() + oidc.METADATA_FAILURE_SECONDS + 1
        with patch("inky_bird_frame.oidc.monotonic", return_value=retry):
            assert asyncio.run(metadata(CONFIG)).token_endpoint == "https://id.example.test/token"
        assert provider.requests == 2


def test_metadata_for_a_different_issuer_is_not_reused() -> None:
    provider = _Provider()
    other = replace(CONFIG, issuer="https://other.example.test/")
    with _with_provider(provider):
        asyncio.run(metadata(CONFIG))
        with pytest.raises(OIDCError, match="does not match"):
            asyncio.run(metadata(other))
    assert provider.requests == 2


def test_an_unexpected_background_refresh_failure_is_logged_once_and_paused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _Provider()
    with _with_provider(provider):
        first = asyncio.run(metadata(CONFIG))
    attempts = 0

    async def broken(config: OIDCConfig) -> oidc.ProviderMetadata:
        nonlocal attempts
        attempts += 1
        raise KeyError("synthetic-missing-field")

    async def reads() -> list[oidc.ProviderMetadata]:
        values = []
        for _ in range(5):
            values.append(await metadata(CONFIG))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        return values

    later = oidc.monotonic() + oidc.METADATA_FRESH_SECONDS + 1
    with (
        patch("inky_bird_frame.oidc._fetch_metadata", broken),
        patch("inky_bird_frame.oidc.monotonic", return_value=later),
        caplog.at_level("ERROR", logger="inky_bird_frame.oidc"),
    ):
        assert asyncio.run(reads()) == [first] * 5
    assert attempts == 1
    assert oidc._metadata_cache[CONFIG.issuer].failure is not None
    logged = [record for record in caplog.records if record.name == "inky_bird_frame.oidc"]
    assert len(logged) == 1
    assert logged[0].exc_info is not None
    assert logged[0].exc_info[0] is KeyError


def test_an_unexpected_first_fetch_failure_is_a_safe_sign_in_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(config: OIDCConfig) -> oidc.ProviderMetadata:
        raise KeyError("synthetic-missing-field")

    with (
        patch("inky_bird_frame.oidc._fetch_metadata", broken),
        caplog.at_level("ERROR", logger="inky_bird_frame.oidc"),
    ):
        for _ in range(2):
            with pytest.raises(OIDCError) as raised:
                asyncio.run(metadata(CONFIG))
            assert "synthetic-missing-field" not in str(raised.value)
    assert len([r for r in caplog.records if r.name == "inky_bird_frame.oidc"]) == 1
