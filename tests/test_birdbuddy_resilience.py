"""Bird Buddy parsing, transport and local-state resilience driven through real parsers."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.birdbuddy import (
    _FEED_QUERIES,
    _REFRESH_TOKEN,
    BirdBuddyFeeder,
    _ConfirmedHistoryFetch,
    _fetch_postcards,
    login_birdbuddy,
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


if __name__ == "__main__":
    unittest.main()
