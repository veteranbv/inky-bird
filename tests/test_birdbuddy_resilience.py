"""Bird Buddy parsing, transport and local-state resilience driven through real parsers."""

from __future__ import annotations

import io
import json
import stat
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

from inky_bird_frame.birdbuddy import (
    _FEED_QUERIES,
    _REFRESH_TOKEN,
    BirdBuddyFeeder,
    _ConfirmedHistoryFetch,
    _fetch_postcards,
    birdbuddy_status,
    login_birdbuddy,
    logout_birdbuddy,
    read_birdbuddy_history_species,
    sync_birdbuddy_detections,
)
from inky_bird_frame.birds import ObservationWindow
from inky_bird_frame.errors import DataSourceError

FEEDER = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

BLUEBIRD: dict[str, object] = {
    "__typename": "SpeciesBird",
    "id": "species-bluebird",
    "name": "Eastern Bluebird",
    "scientificName": "Sialia sialis",
}


def node(
    postcard_id: str,
    *,
    feeder_id: str = "feeder-1",
    created_at: object = "2026-08-03T10:00:00+00:00",
    confidence: object = "HIGH_CONFIDENCE",
    variant: str = "SightingRecognizedBird",
    species: dict[str, object] | None = None,
) -> dict[str, object]:
    """One feed node as the given query variant returns it."""
    other = (
        "SightingRecognizedBirdUnlocked"
        if variant == "SightingRecognizedBird"
        else "SightingRecognizedBird"
    )
    sightings: list[dict[str, object]] = [{"__typename": other}]
    if species is not None:
        sightings.append({"__typename": variant, "species": species})
    return {
        "__typename": "FeedItemNewPostcard",
        "id": postcard_id,
        "createdAt": created_at,
        "inferenceConfidenceLevel": confidence,
        "feeder": {"__typename": "FeederForMember", "id": feeder_id},
        "medias": [{"__typename": "MediaImage", "id": f"media-{postcard_id}"}],
        "sightingReportPreview": {"sightings": sightings},
    }


def page(*nodes: dict[str, object]) -> dict[str, object]:
    return {
        "me": {
            "feed": {
                "edges": [{"node": item} for item in nodes],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }


class FakeBirdBuddyApi:
    """Answer GraphQL operations by query text; never touches the network."""

    def __init__(
        self,
        recognized: dict[str, object],
        unlocked: dict[str, object],
        refresh: object = None,
    ) -> None:
        self.pages = {_FEED_QUERIES[0]: recognized, _FEED_QUERIES[1]: unlocked}
        self.refresh = refresh
        self.refresh_tokens: list[str] = []

    def __call__(
        self,
        query: str,
        variables: dict[str, object],
        operation: str,
        *,
        access_token: str | None = None,
    ) -> dict[str, object]:
        if query == _REFRESH_TOKEN:
            refresh_input = variables["input"]
            assert isinstance(refresh_input, dict)
            self.refresh_tokens.append(str(refresh_input["token"]))
            if isinstance(self.refresh, BaseException):
                raise self.refresh
            return {
                "authRefreshToken": {
                    "accessToken": "access-token",
                    "refreshToken": f"refresh-{len(self.refresh_tokens)}",
                }
            }
        return self.pages[query]


def logged_in(state_dir: Path) -> None:
    with patch(
        "inky_bird_frame.birdbuddy._authenticate",
        return_value=("access", "refresh-0", [FEEDER]),
    ):
        login_birdbuddy(
            state_dir,
            email="birder@example.test",
            password="private-password",
            authorization_confirmed=True,
            now=NOW,
        )


class BirdBuddyResilienceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        confirmed = patch(
            "inky_bird_frame.birdbuddy._fetch_confirmed_history",
            return_value=_ConfirmedHistoryFetch([], 1, 0, 0, 0, 0),
        )
        confirmed.start()
        self.addCleanup(confirmed.stop)


class FeedParsingTests(BirdBuddyResilienceTestCase):
    def test_other_feeder_postcards_are_skipped_in_both_query_variants(self) -> None:
        api = FakeBirdBuddyApi(
            page(
                node("postcard-1", species=BLUEBIRD),
                node("postcard-2", feeder_id="feeder-2", species=BLUEBIRD),
            ),
            page(
                node("postcard-1", variant="SightingRecognizedBirdUnlocked"),
                node(
                    "postcard-2",
                    feeder_id="feeder-2",
                    variant="SightingRecognizedBirdUnlocked",
                ),
            ),
        )
        with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=api):
            postcards, pages, processed, ignored = _fetch_postcards("access", "feeder-1")

        self.assertEqual([item.postcard_id for item in postcards], ["postcard-1"])
        self.assertEqual([item.species_id for item in postcards[0].species], ["species-bluebird"])
        self.assertEqual((pages, processed, ignored), (2, 2, 1))

    def test_malformed_timestamps_are_incomplete_and_do_not_fail_the_feed(self) -> None:
        for created_at in (None, "", "2026-08-03T10:00:00", "not-a-date"):
            with self.subTest(created_at=created_at):
                api = FakeBirdBuddyApi(
                    page(
                        node("postcard-1", species=BLUEBIRD),
                        node("postcard-2", created_at=created_at, species=BLUEBIRD),
                    ),
                    page(
                        node("postcard-1", variant="SightingRecognizedBirdUnlocked"),
                        node(
                            "postcard-2",
                            created_at=created_at,
                            variant="SightingRecognizedBirdUnlocked",
                        ),
                    ),
                )
                with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=api):
                    postcards, _, processed, ignored = _fetch_postcards("access", "feeder-1")

                self.assertEqual([item.postcard_id for item in postcards], ["postcard-1"])
                self.assertEqual((processed, ignored), (2, 1))

    def test_postcard_malformed_in_one_variant_is_not_accepted(self) -> None:
        api = FakeBirdBuddyApi(
            page(node("postcard-1", species=BLUEBIRD)),
            page(
                node(
                    "postcard-1",
                    created_at=None,
                    variant="SightingRecognizedBirdUnlocked",
                )
            ),
        )
        with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=api):
            postcards, _, processed, ignored = _fetch_postcards("access", "feeder-1")

        self.assertEqual(postcards, [])
        self.assertEqual((processed, ignored), (1, 1))

    def test_unknown_confidence_level_does_not_withdraw_a_retained_postcard(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            first = FakeBirdBuddyApi(
                page(node("postcard-1", species=BLUEBIRD)),
                page(node("postcard-1", variant="SightingRecognizedBirdUnlocked")),
            )
            with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=first):
                sync_birdbuddy_detections(
                    state_dir, window=ObservationWindow.ALL_TIME, limit=20, now=NOW
                )
            renamed = FakeBirdBuddyApi(
                page(node("postcard-1", confidence="HIGH_CONFIDENCE_V2", species=BLUEBIRD)),
                page(
                    node(
                        "postcard-1",
                        confidence="HIGH_CONFIDENCE_V2",
                        variant="SightingRecognizedBirdUnlocked",
                    )
                ),
            )
            with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=renamed):
                second = sync_birdbuddy_detections(
                    state_dir, window=ObservationWindow.ALL_TIME, limit=20, now=NOW
                )

        self.assertEqual([item.scientific_name for item in second.species], ["Sialia sialis"])
        self.assertEqual(second.stats.reclassified_postcards, 0)

    def test_low_confidence_still_withdraws_a_retained_postcard(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            first = FakeBirdBuddyApi(
                page(node("postcard-1", species=BLUEBIRD)),
                page(node("postcard-1", variant="SightingRecognizedBirdUnlocked")),
            )
            with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=first):
                sync_birdbuddy_detections(
                    state_dir, window=ObservationWindow.ALL_TIME, limit=20, now=NOW
                )
            withdrawn = FakeBirdBuddyApi(
                page(node("postcard-1", confidence="LOW_CONFIDENCE", species=BLUEBIRD)),
                page(
                    node(
                        "postcard-1",
                        confidence="LOW_CONFIDENCE",
                        variant="SightingRecognizedBirdUnlocked",
                    )
                ),
            )
            with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=withdrawn):
                second = sync_birdbuddy_detections(
                    state_dir, window=ObservationWindow.ALL_TIME, limit=20, now=NOW
                )

        self.assertEqual(second.species, [])
        self.assertEqual(second.stats.reclassified_postcards, 1)


class MixedFeederSyncTests(BirdBuddyResilienceTestCase):
    def test_sync_succeeds_and_persists_only_the_selected_feeder(self) -> None:
        api = FakeBirdBuddyApi(
            page(
                node("postcard-1", species=BLUEBIRD),
                node("postcard-2", feeder_id="feeder-2", species=BLUEBIRD),
                node("postcard-3", created_at="2026-08-03T10:00:00", species=BLUEBIRD),
            ),
            page(
                node("postcard-1", variant="SightingRecognizedBirdUnlocked"),
                node(
                    "postcard-2",
                    feeder_id="feeder-2",
                    variant="SightingRecognizedBirdUnlocked",
                ),
                node(
                    "postcard-3",
                    created_at="2026-08-03T10:00:00",
                    variant="SightingRecognizedBirdUnlocked",
                ),
            ),
        )
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=api):
                result = sync_birdbuddy_detections(
                    state_dir, window=ObservationWindow.ALL_TIME, limit=20, now=NOW
                )
            history = json.loads((state_dir / "birdbuddy-detections.json").read_text())

        self.assertEqual([item.scientific_name for item in result.species], ["Sialia sialis"])
        self.assertEqual(result.species[0].detection_count, 1)
        self.assertEqual(result.stats.postcards_processed, 3)
        self.assertEqual(result.stats.ignored_postcards, 2)
        self.assertEqual(list(history["feeders"]), ["feeder-1"])
        self.assertEqual(
            [item["id"] for item in history["feeders"]["feeder-1"]["postcards"]],
            ["postcard-1"],
        )

    def test_feed_errors_are_still_data_source_errors(self) -> None:
        with (
            patch(
                "inky_bird_frame.birdbuddy._graphql_request",
                return_value={"me": {"feed": None}},
            ),
            self.assertRaisesRegex(DataSourceError, "feed response was incomplete"),
        ):
            _fetch_postcards("access", "feeder-1")


class HttpStatusOpener:
    """Stand-in for urllib's opener that answers every request with one HTTP status."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.refresh_tokens: list[str] = []

    def open(self, request: object, timeout: float) -> object:
        data = getattr(request, "data", b"")
        assert isinstance(data, bytes)
        body = json.loads(data)
        assert isinstance(body, dict)
        variables = body["variables"]
        assert isinstance(variables, dict)
        self.refresh_tokens.append(str(variables["input"]["token"]))
        raise HTTPError(
            "https://example.test/graphql", self.status, "status", Message(), io.BytesIO()
        )


def auth_state(state_dir: Path) -> dict[str, object]:
    value = json.loads((state_dir / "birdbuddy-auth.json").read_text())
    assert isinstance(value, dict)
    return value


def sync(state_dir: Path) -> object:
    return sync_birdbuddy_detections(
        state_dir, window=ObservationWindow.ALL_TIME, limit=20, now=NOW
    )


EMPTY_FEED = page()


class TokenRefreshTests(BirdBuddyResilienceTestCase):
    def test_success_keeps_the_token_that_worked_as_the_fallback(self) -> None:
        api = FakeBirdBuddyApi(EMPTY_FEED, EMPTY_FEED)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=api):
                sync(state_dir)
                sync(state_dir)
            state = auth_state(state_dir)
            mode = stat.S_IMODE((state_dir / "birdbuddy-auth.json").stat().st_mode)

        self.assertEqual(api.refresh_tokens, ["refresh-0", "refresh-1"])
        self.assertEqual(state["refresh_token"], "refresh-2")
        self.assertEqual(state["previous_refresh_token"], "refresh-1")
        self.assertEqual(mode, 0o600)

    def test_rejected_token_is_retried_once_with_the_previous_token(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            with patch(
                "inky_bird_frame.birdbuddy._graphql_request",
                side_effect=FakeBirdBuddyApi(EMPTY_FEED, EMPTY_FEED),
            ):
                sync(state_dir)
            # The saved token was never accepted (for example a lost reply).
            state = auth_state(state_dir) | {"refresh_token": "lost-token"}
            (state_dir / "birdbuddy-auth.json").write_text(json.dumps(state))

            calls: list[str] = []

            def api(
                query: str,
                variables: dict[str, object],
                operation: str,
                *,
                access_token: str | None = None,
            ) -> dict[str, object]:
                if query != _REFRESH_TOKEN:
                    return EMPTY_FEED
                refresh_input = variables["input"]
                assert isinstance(refresh_input, dict)
                calls.append(str(refresh_input["token"]))
                if refresh_input["token"] == "lost-token":
                    raise DataSourceError("HTTP 401 from Bird Buddy API") from HTTPError(
                        "https://example.test/graphql", 401, "status", Message(), io.BytesIO()
                    )
                return {"authRefreshToken": {"accessToken": "access", "refreshToken": "recovered"}}

            with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=api):
                sync(state_dir)
            recovered = auth_state(state_dir)

        self.assertEqual(calls, ["lost-token", "refresh-0"])
        self.assertEqual(recovered["refresh_token"], "recovered")
        self.assertEqual(recovered["previous_refresh_token"], "refresh-0")

    def test_real_http_401_asks_for_login_after_one_fallback_attempt(self) -> None:
        opener = HttpStatusOpener(401)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            state = auth_state(state_dir) | {"previous_refresh_token": "older-token"}
            (state_dir / "birdbuddy-auth.json").write_text(json.dumps(state))
            before = (state_dir / "birdbuddy-auth.json").read_bytes()
            with (
                patch("inky_bird_frame.http.build_opener", return_value=opener),
                self.assertRaisesRegex(DataSourceError, "run birdbuddy login again") as raised,
            ):
                sync(state_dir)
            after = (state_dir / "birdbuddy-auth.json").read_bytes()

        self.assertEqual(opener.refresh_tokens, ["refresh-0", "older-token"])
        self.assertEqual(after, before)
        self.assertNotIn("refresh-0", str(raised.exception))

    def test_throttling_and_server_errors_do_not_ask_for_login_or_retry(self) -> None:
        for status in (429, 500, 503):
            with TemporaryDirectory() as temporary, self.subTest(status=status):
                state_dir = Path(temporary)
                logged_in(state_dir)
                state = auth_state(state_dir) | {"previous_refresh_token": "older-token"}
                (state_dir / "birdbuddy-auth.json").write_text(json.dumps(state))
                opener = HttpStatusOpener(status)
                with (
                    patch("inky_bird_frame.http.build_opener", return_value=opener),
                    self.assertRaises(DataSourceError) as raised,
                ):
                    sync(state_dir)

                self.assertEqual(str(raised.exception), f"HTTP {status} from Bird Buddy API")
                self.assertEqual(opener.refresh_tokens, ["refresh-0"])

    def test_unexpected_refresh_reply_does_not_claim_the_session_is_invalid(self) -> None:
        for reply in (
            {"authRefreshToken": None},
            {"authRefreshToken": {"accessToken": "access"}},
        ):
            with TemporaryDirectory() as temporary, self.subTest(reply=reply):
                state_dir = Path(temporary)
                logged_in(state_dir)
                with (
                    patch("inky_bird_frame.birdbuddy._graphql_request", return_value=reply),
                    self.assertRaises(DataSourceError) as raised,
                ):
                    sync(state_dir)

                self.assertNotIn("session is invalid", str(raised.exception))
                self.assertEqual(auth_state(state_dir)["refresh_token"], "refresh-0")

    def test_graphql_rejection_is_retried_and_not_reported_as_a_lost_session(self) -> None:
        sent: list[str] = []

        def post_json(url: str, value: object, **_: object) -> object:
            assert isinstance(value, dict)
            sent.append(str(value["variables"]["input"]["token"]))
            return {"errors": [{"message": "private upstream detail"}], "data": None}

        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            state = auth_state(state_dir) | {"previous_refresh_token": "older-token"}
            (state_dir / "birdbuddy-auth.json").write_text(json.dumps(state))
            with (
                patch("inky_bird_frame.birdbuddy.post_json", side_effect=post_json),
                self.assertRaisesRegex(DataSourceError, "did not renew the sign-in") as raised,
            ):
                sync(state_dir)

        self.assertEqual(sent, ["refresh-0", "older-token"])
        self.assertNotIn("session is invalid", str(raised.exception))
        self.assertNotIn("private upstream detail", str(raised.exception))

    def test_failed_save_of_a_renewed_token_is_explained(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            with (
                patch(
                    "inky_bird_frame.birdbuddy._graphql_request",
                    side_effect=FakeBirdBuddyApi(EMPTY_FEED, EMPTY_FEED),
                ),
                patch(
                    "inky_bird_frame.birdbuddy.write_json_atomic",
                    side_effect=OSError(28, "No space left on device"),
                ),
                self.assertRaisesRegex(DataSourceError, "could not save") as raised,
            ):
                sync(state_dir)

        self.assertNotIn(temporary, str(raised.exception))

    def test_auth_state_written_by_earlier_releases_still_loads(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            state = auth_state(state_dir)
            state.pop("previous_refresh_token", None)
            (state_dir / "birdbuddy-auth.json").write_text(json.dumps(state))
            api = FakeBirdBuddyApi(EMPTY_FEED, EMPTY_FEED)
            with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=api):
                sync(state_dir)
            status = birdbuddy_status(state_dir)

        self.assertEqual(api.refresh_tokens, ["refresh-0"])
        self.assertNotIn("refresh", json.dumps(status))


class LocalStateFailureTests(BirdBuddyResilienceTestCase):
    def test_unusable_state_folder_is_a_data_source_error(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            state_dir.write_text("not a folder")
            actions: tuple[Callable[[], object], ...] = (
                lambda: sync(state_dir),
                lambda: logout_birdbuddy(state_dir),
                lambda: read_birdbuddy_history_species(state_dir),
            )
            for action in actions:
                with self.assertRaisesRegex(DataSourceError, "could not use its state") as raised:
                    action()
                self.assertNotIn(temporary, str(raised.exception))
            with (
                patch(
                    "inky_bird_frame.birdbuddy._authenticate",
                    return_value=("access", "refresh", [FEEDER]),
                ),
                self.assertRaisesRegex(DataSourceError, "could not use its state"),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                )

    def test_non_utf8_state_is_a_data_source_error_without_its_path(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            (state_dir / "birdbuddy-auth.json").write_bytes(b'{"refresh_token": "\xff"}')
            with self.assertRaisesRegex(DataSourceError, "Invalid Bird Buddy") as raised:
                birdbuddy_status(state_dir)

        self.assertNotIn(temporary, str(raised.exception))

    def test_state_errors_do_not_contain_absolute_paths(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            auth = state_dir / "birdbuddy-auth.json"
            auth.chmod(0o644)
            with self.assertRaisesRegex(DataSourceError, "mode 0600") as insecure:
                birdbuddy_status(state_dir)
            auth.chmod(0o600)
            history = state_dir / "birdbuddy-detections.json"
            history.symlink_to(state_dir / "elsewhere.json")
            with self.assertRaisesRegex(DataSourceError, "Refusing symlinked") as linked:
                birdbuddy_status(state_dir)

        for raised in (insecure, linked):
            self.assertNotIn(temporary, str(raised.exception))

    def test_failed_history_write_is_a_data_source_error(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            history_path = state_dir / "birdbuddy-detections.json"
            from inky_bird_frame.http import write_json_atomic

            def failing_write(path: Path, value: object, *, mode: int | None = None) -> None:
                if path == history_path:
                    raise OSError(28, "No space left on device")
                write_json_atomic(path, value, mode=mode)

            with (
                patch(
                    "inky_bird_frame.birdbuddy._graphql_request",
                    side_effect=FakeBirdBuddyApi(EMPTY_FEED, EMPTY_FEED),
                ),
                patch("inky_bird_frame.birdbuddy.write_json_atomic", side_effect=failing_write),
                self.assertRaisesRegex(DataSourceError, "could not use its state") as raised,
            ):
                sync(state_dir)

        self.assertNotIn(temporary, str(raised.exception))
        self.assertFalse(history_path.exists())


if __name__ == "__main__":
    unittest.main()
