"""Bird Buddy parsing, transport and local-state resilience driven through real parsers."""

from __future__ import annotations

import base64
import io
import json
import stat
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

from inky_bird_frame.birdbuddy import (
    _CONFIRMED_HISTORY,
    _FEED_QUERIES,
    _REFRESH_TOKEN,
    ArchivedPostcardLink,
    ArchivedSpecies,
    BirdBuddyFeeder,
    BirdBuddyPostcard,
    BirdBuddySyncResult,
    ConfirmedSpeciesEvidence,
    FeederHistory,
    PostcardSpecies,
    _ConfirmedHistoryFetch,
    _fetch_confirmed_history,
    _fetch_postcards,
    _reconcile_archived_postcards,
    _update_history,
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

    def test_postcard_malformed_on_a_later_page_of_the_same_variant_is_not_accepted(
        self,
    ) -> None:
        first_page = page(node("postcard-1", species=BLUEBIRD))
        feed = first_page["me"]
        assert isinstance(feed, dict) and isinstance(feed["feed"], dict)
        feed["feed"]["pageInfo"] = {"hasNextPage": True, "endCursor": "cursor-1"}
        responses = [
            first_page,
            page(node("postcard-1", created_at=None, species=BLUEBIRD)),
            page(node("postcard-1", variant="SightingRecognizedBirdUnlocked")),
        ]
        with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=responses):
            postcards, pages, _, _ = _fetch_postcards("access", "feeder-1")

        self.assertEqual(pages, 3)
        self.assertEqual(postcards, [])

    def test_postcards_without_media_never_reach_history(self) -> None:
        for medias in ([], None, [{"__typename": "MediaImage", "id": None}]):
            recognized = node("postcard-1", confidence="LOW_CONFIDENCE", species=BLUEBIRD)
            unlocked = node(
                "postcard-1", confidence="LOW_CONFIDENCE", variant="SightingRecognizedBirdUnlocked"
            )
            recognized["medias"] = medias
            unlocked["medias"] = medias
            api = FakeBirdBuddyApi(page(recognized), page(unlocked))
            with (
                self.subTest(medias=medias),
                patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=api),
            ):
                postcards, _, _, _ = _fetch_postcards("access", "feeder-1")
                self.assertEqual(postcards, [])

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


HUMMINGBIRD = PostcardSpecies(
    "species-hummingbird", "Ruby-throated Hummingbird", "Archilochus colubris"
)


def confirmed_record(media_id: str, *, origin: object = "POSTCARD") -> dict[str, object]:
    return {
        "origin": origin,
        "feeder": {"__typename": "FeederForMember", "id": "feeder-1"},
        "media": {
            "__typename": "MediaImage",
            "id": media_id,
            "createdAt": "2026-08-03T10:00:00Z",
        },
        "species": [
            {
                "__typename": "SpeciesBird",
                "id": HUMMINGBIRD.species_id,
                "name": HUMMINGBIRD.common_name,
                "scientificName": HUMMINGBIRD.scientific_name,
            }
        ],
    }


def confirmed_page(*records: object, next_cursor: str | None = None) -> dict[str, object]:
    return {
        "me": {
            "mediasOwned": {
                "edges": [{"node": item} for item in records],
                "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor},
            }
        }
    }


class ConfirmedHistoryTests(unittest.TestCase):
    def test_unrecognised_records_are_skipped_and_counted(self) -> None:
        missing_date = confirmed_record("media-2")
        assert isinstance(missing_date["media"], dict)
        missing_date["media"]["createdAt"] = None
        response = confirmed_page(
            confirmed_record("media-1"),
            missing_date,
            confirmed_record("media-3", origin="SOMETHING_NEW"),
            {"origin": "POSTCARD", "feeder": None},
            None,
        )
        with patch("inky_bird_frame.birdbuddy._graphql_request", return_value=response):
            fetched = _fetch_confirmed_history("access", "feeder-1", include_manual_sightings=False)

        self.assertEqual(fetched.records_processed, 5)
        self.assertEqual(fetched.records_accepted, 1)
        self.assertEqual(fetched.records_skipped, 4)
        self.assertFalse(fetched.complete)
        self.assertEqual(list(fetched.species_by_media), ["media-1"])
        self.assertEqual([item.species for item in fetched.evidence], [HUMMINGBIRD])

    def test_recognised_snapshot_is_complete(self) -> None:
        with patch(
            "inky_bird_frame.birdbuddy._graphql_request",
            return_value=confirmed_page(confirmed_record("media-1")),
        ):
            fetched = _fetch_confirmed_history("access", "feeder-1", include_manual_sightings=False)

        self.assertEqual(fetched.records_skipped, 0)
        self.assertTrue(fetched.complete)

    def test_large_history_stops_at_the_page_bound_instead_of_failing(self) -> None:
        pages = [
            confirmed_page(confirmed_record(f"media-{index}"), next_cursor=f"cursor-{index}")
            for index in range(3)
        ]
        with (
            patch("inky_bird_frame.birdbuddy.BIRDBUDDY_MAX_CONFIRMED_PAGES", 2),
            patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=pages) as request,
        ):
            fetched = _fetch_confirmed_history("access", "feeder-1", include_manual_sightings=False)

        self.assertEqual(request.call_count, 2)
        self.assertEqual(fetched.pages, 2)
        self.assertFalse(fetched.complete)
        self.assertEqual(sorted(fetched.species_by_media), ["media-0", "media-1"])

    def test_partial_snapshot_keeps_archived_links_it_did_not_see(self) -> None:
        observed_at = "2025-08-01T10:00:00+00:00"
        media_ids = ("media-1",)
        history = FeederHistory(
            "2025-01-01T00:00:00+00:00",
            None,
            None,
            {},
            {
                HUMMINGBIRD.species_id: ArchivedSpecies(
                    HUMMINGBIRD.species_id,
                    HUMMINGBIRD.common_name,
                    HUMMINGBIRD.scientific_name,
                    1,
                    observed_at,
                )
            },
            {},
            {media_ids: ArchivedPostcardLink(observed_at, media_ids, (HUMMINGBIRD.species_id,))},
        )

        _reconcile_archived_postcards(history, {}, complete=False)
        self.assertEqual(list(history.archived_postcards), [media_ids])
        _reconcile_archived_postcards(history, {})
        self.assertEqual(history.archived_postcards, {})

    def test_partial_snapshot_keeps_the_link_of_a_postcard_it_archives(self) -> None:
        old = BirdBuddyPostcard(
            "postcard-old",
            "2025-07-01T10:00:00+00:00",
            (HUMMINGBIRD,),
            media_ids=("media-old",),
        )
        bluebird = PostcardSpecies("species-bluebird", "Eastern Bluebird", "Sialia sialis")

        def update(
            state_dir: Path,
            incoming: list[BirdBuddyPostcard],
            species_by_media: dict[str, tuple[PostcardSpecies, ...]],
            *,
            complete: bool,
        ) -> FeederHistory:
            return _update_history(
                state_dir,
                "feeder-1",
                incoming,
                [],
                NOW,
                persist=True,
                replace_manual_evidence=False,
                confirmed_species_by_media=species_by_media,
                confirmed_complete=complete,
            ).history

        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            partial = update(state_dir, [old], {}, complete=False)
            linked = dict(partial.archived_postcards)
            unlinked = dict(partial.archived_unlinked_latest)
            corrected = update(state_dir, [], {"media-old": (bluebird,)}, complete=True)

        self.assertEqual(partial.postcards, {})
        self.assertEqual(list(linked), [("media-old",)])
        self.assertEqual(unlinked, {})
        # A later complete snapshot can still apply the confirmed correction.
        self.assertEqual(list(corrected.archived_species), ["species-bluebird"])
        self.assertEqual(
            corrected.archived_postcards[("media-old",)].species_ids, ("species-bluebird",)
        )

    def test_complete_snapshot_archives_an_unconfirmed_postcard_as_unlinked(self) -> None:
        old = BirdBuddyPostcard(
            "postcard-old",
            "2025-07-01T10:00:00+00:00",
            (HUMMINGBIRD,),
            media_ids=("media-old",),
        )
        with TemporaryDirectory() as temporary:
            history = _update_history(
                Path(temporary),
                "feeder-1",
                [old],
                [],
                NOW,
                persist=False,
                replace_manual_evidence=False,
                confirmed_species_by_media={},
            ).history

        self.assertEqual(history.archived_postcards, {})
        self.assertEqual(
            history.archived_unlinked_latest, {HUMMINGBIRD.species_id: old.observed_at}
        )


class PartialConfirmedSyncTests(unittest.TestCase):
    def test_partial_snapshot_keeps_retained_confirmed_evidence_and_reports_it(self) -> None:
        evidence = ConfirmedSpeciesEvidence(
            HUMMINGBIRD, "2026-08-03T10:00:00+00:00", "selected_feeder"
        )
        snapshots = [
            _ConfirmedHistoryFetch([evidence], 1, 1, 1, 0, 0),
            _ConfirmedHistoryFetch([], 1, 1, 0, 0, 1, records_skipped=1, complete=False),
            _ConfirmedHistoryFetch([], 1, 0, 0, 0, 0),
        ]
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            with (
                patch(
                    "inky_bird_frame.birdbuddy._graphql_request",
                    side_effect=FakeBirdBuddyApi(page(), page()),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_confirmed_history",
                    side_effect=snapshots,
                ),
                # Read confirmed history on each of these syncs, not once a day.
                patch(
                    "inky_bird_frame.birdbuddy.BIRDBUDDY_CONFIRMED_HISTORY_INTERVAL", timedelta(0)
                ),
            ):
                sync(state_dir)
                partial = sync_birdbuddy_detections(
                    state_dir, window=ObservationWindow.ALL_TIME, limit=20, now=NOW
                )
                complete = sync_birdbuddy_detections(
                    state_dir, window=ObservationWindow.ALL_TIME, limit=20, now=NOW
                )

        self.assertEqual(
            [item.scientific_name for item in partial.species], ["Archilochus colubris"]
        )
        self.assertEqual(partial.stats.as_dict()["confirmed_records_skipped"], 1)
        self.assertIs(partial.stats.as_dict()["confirmed_history_complete"], False)
        self.assertEqual(complete.species, [])
        self.assertIs(complete.stats.as_dict()["confirmed_history_complete"], True)


class ConfirmedHistoryApi(FakeBirdBuddyApi):
    """Serve an empty feed and the given confirmed-history pages in order."""

    def __init__(self, confirmed_pages: list[dict[str, object]]) -> None:
        super().__init__(page(), page())
        self.confirmed_pages = list(confirmed_pages)

    def __call__(
        self,
        query: str,
        variables: dict[str, object],
        operation: str,
        *,
        access_token: str | None = None,
    ) -> dict[str, object]:
        if query == _CONFIRMED_HISTORY:
            return self.confirmed_pages.pop(0)
        return super().__call__(query, variables, operation, access_token=access_token)


class LegacyPostcardMigrationTests(unittest.TestCase):
    """Postcards saved before media linkage are dropped only after a complete snapshot."""

    def sync_over_legacy_history(
        self, confirmed_pages: list[dict[str, object]], *, max_pages: int = 100
    ) -> tuple[list[str], list[dict[str, object]]]:
        legacy = {
            "id": "postcard-legacy",
            "observed_at": "2026-08-03T09:00:00+00:00",
            "species": [
                {
                    "id": "species-bluebird",
                    "common_name": "Eastern Bluebird",
                    "scientific_name": "Sialia sialis",
                }
            ],
        }
        payload = {
            "schema_version": 1,
            "feeders": {
                "feeder-1": {
                    "history_started_at": "2026-08-01T00:00:00+00:00",
                    "earliest_initial_feed_at": legacy["observed_at"],
                    "last_successful_sync_at": "2026-08-02T00:00:00+00:00",
                    "postcards": [legacy],
                    "archived_species": {},
                    "confirmed_species": [],
                }
            },
        }
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            history_path = state_dir / "birdbuddy-detections.json"
            history_path.write_text(json.dumps(payload))
            history_path.chmod(0o600)
            with (
                patch("inky_bird_frame.birdbuddy.BIRDBUDDY_MAX_CONFIRMED_PAGES", max_pages),
                patch(
                    "inky_bird_frame.birdbuddy._graphql_request",
                    side_effect=ConfirmedHistoryApi(confirmed_pages),
                ),
            ):
                result = sync_birdbuddy_detections(
                    state_dir, window=ObservationWindow.ALL_TIME, limit=20, now=NOW
                )
            stored = json.loads(history_path.read_text())["feeders"]["feeder-1"]["postcards"]
        return [item.scientific_name for item in result.species], stored

    def test_snapshot_with_a_skipped_record_keeps_the_legacy_postcard(self) -> None:
        malformed = confirmed_record("media-2")
        assert isinstance(malformed["media"], dict)
        malformed["media"]["createdAt"] = None
        species, stored = self.sync_over_legacy_history(
            [confirmed_page(confirmed_record("media-1"), malformed)]
        )

        self.assertIn("Sialia sialis", species)
        self.assertEqual([item["id"] for item in stored], ["postcard-legacy"])

    def test_snapshot_cut_at_the_page_bound_keeps_the_legacy_postcard(self) -> None:
        species, stored = self.sync_over_legacy_history(
            [confirmed_page(confirmed_record("media-1"), next_cursor="cursor-1")],
            max_pages=1,
        )

        self.assertIn("Sialia sialis", species)
        self.assertEqual([item["id"] for item in stored], ["postcard-legacy"])

    def test_complete_snapshot_drops_the_legacy_postcard(self) -> None:
        species, stored = self.sync_over_legacy_history(
            [confirmed_page(confirmed_record("media-1"))]
        )

        self.assertNotIn("Sialia sialis", species)
        self.assertEqual(stored, [])


class HistoryFileTests(BirdBuddyResilienceTestCase):
    def test_each_write_keeps_the_previous_history_privately(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            first_feed = FakeBirdBuddyApi(
                page(node("postcard-1", species=BLUEBIRD)),
                page(node("postcard-1", variant="SightingRecognizedBirdUnlocked")),
            )
            with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=first_feed):
                sync(state_dir)
            history = state_dir / "birdbuddy-detections.json"
            previous = state_dir / "birdbuddy-detections.json.previous"
            self.assertFalse(previous.exists())
            first = history.read_bytes()
            with patch(
                "inky_bird_frame.birdbuddy._graphql_request",
                side_effect=FakeBirdBuddyApi(page(), page()),
            ):
                sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=datetime(2026, 8, 3, 12, 15, tzinfo=UTC),
                )
                kept = previous.read_bytes()
                mode = stat.S_IMODE(previous.stat().st_mode)
                second = history.read_bytes()
                sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    persist_history=False,
                )
                after_preview = previous.read_bytes()

        self.assertEqual(kept, first)
        self.assertNotEqual(second, first)
        self.assertEqual(mode, 0o600)
        self.assertEqual(after_preview, first)

    def test_previous_history_is_rewritten_only_when_the_history_changes(self) -> None:
        def sync_at(state_dir: Path, minute: int, api: FakeBirdBuddyApi) -> None:
            with patch("inky_bird_frame.birdbuddy._graphql_request", side_effect=api):
                sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=datetime(2026, 8, 3, 12, minute, tzinfo=UTC),
                )

        one_postcard = FakeBirdBuddyApi(
            page(node("postcard-1", species=BLUEBIRD)),
            page(node("postcard-1", variant="SightingRecognizedBirdUnlocked")),
        )
        two_postcards = FakeBirdBuddyApi(
            page(
                node("postcard-1", species=BLUEBIRD),
                node("postcard-2", created_at="2026-08-03T11:00:00+00:00", species=BLUEBIRD),
            ),
            page(
                node("postcard-1", variant="SightingRecognizedBirdUnlocked"),
                node(
                    "postcard-2",
                    created_at="2026-08-03T11:00:00+00:00",
                    variant="SightingRecognizedBirdUnlocked",
                ),
            ),
        )
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in(state_dir)
            history = state_dir / "birdbuddy-detections.json"
            previous = state_dir / "birdbuddy-detections.json.previous"
            sync_at(state_dir, 0, one_postcard)
            first = history.read_bytes()
            sync_at(state_dir, 15, one_postcard)
            kept_after_second = previous.read_bytes()
            second = history.read_bytes()
            sync_at(state_dir, 30, one_postcard)
            kept_after_unchanged = previous.read_bytes()
            third = history.read_bytes()
            sync_at(state_dir, 45, two_postcards)
            kept_after_change = previous.read_bytes()

        self.assertEqual(kept_after_second, first)
        # Only the sync time changed, so the earlier copy is still a full backup.
        self.assertNotEqual(third, second)
        self.assertEqual(kept_after_unchanged, first)
        self.assertEqual(kept_after_change, third)

    def test_invalid_history_names_the_failing_section(self) -> None:
        valid_feeder: dict[str, object] = {
            "history_started_at": "2026-08-01T00:00:00+00:00",
            "earliest_initial_feed_at": None,
            "last_successful_sync_at": None,
            "postcards": [],
            "archived_species": {},
            "archived_postcards": [],
            "archived_unlinked_latest": {},
            "confirmed_species": [],
        }
        for section, broken in (
            ("postcards", {"postcards": [{"id": "postcard-1"}]}),
            ("confirmed_species", {"confirmed_species": [{"id": "species-1"}]}),
            ("archived_species", {"archived_species": {"species-1": {"detection_count": 0}}}),
            ("sync dates", {"last_successful_sync_at": "yesterday"}),
        ):
            with TemporaryDirectory() as temporary, self.subTest(section=section):
                state_dir = Path(temporary)
                path = state_dir / "birdbuddy-detections.json"
                path.write_text(
                    json.dumps(
                        {"schema_version": 1, "feeders": {"feeder-1": valid_feeder | broken}}
                    )
                )
                path.chmod(0o600)
                with self.assertRaisesRegex(
                    DataSourceError, f"Invalid Bird Buddy detection history in {section}$"
                ):
                    read_birdbuddy_history_species(state_dir)


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

                expected = (
                    "Bird Buddy asked Inky to slow down"
                    if status == 429
                    else f"HTTP {status} from Bird Buddy API"
                )
                self.assertTrue(str(raised.exception).startswith(expected))
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

    def test_graphql_rejection_is_not_retried_or_reported_as_a_lost_session(self) -> None:
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
            after = auth_state(state_dir)

        # The refusal may be temporary; sending an already rotated token could
        # make Bird Buddy revoke the whole sign-in.
        self.assertEqual(sent, ["refresh-0"])
        self.assertEqual(after, state)
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


def signed_token(expires_at: datetime, label: str = "access") -> str:
    """An unsigned token shaped like Bird Buddy's, carrying only an expiry claim."""

    def part(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    claims = {"exp": int(expires_at.timestamp()), "sub": label}
    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


class _Reply:
    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode()
        self.headers = Message()

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class ScriptedTransport:
    """Stand-in for urllib's opener: records each GraphQL call and answers it."""

    def __init__(self, answer: Callable[[str, dict[str, object], str | None], object]) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str | None]] = []

    def open(self, request: object, timeout: float) -> object:
        data = getattr(request, "data", b"")
        assert isinstance(data, bytes)
        body = json.loads(data)
        assert isinstance(body, dict)
        authorization = getattr(request, "headers", {}).get("Authorization")
        token = authorization.removeprefix("Bearer ") if authorization else None
        query = str(body["query"])
        self.calls.append((query, token))
        return _Reply(self.answer(query, body["variables"], token))

    def count(self, query: str) -> int:
        return sum(1 for sent, _ in self.calls if sent == query)


def throttled(retry_after: str | None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("https://example.test/graphql", 429, "slow down", headers, io.BytesIO())


def feed_and_history(
    query: str, variables: dict[str, object], token: str | None
) -> dict[str, object]:
    if query == _REFRESH_TOKEN:
        return {
            "data": {
                "authRefreshToken": {
                    "accessToken": signed_token(NOW + timedelta(days=30), "renewed"),
                    "refreshToken": "refresh-renewed",
                }
            }
        }
    if query == _CONFIRMED_HISTORY:
        return {"data": confirmed_page(confirmed_record("media-1"))}
    return {"data": EMPTY_FEED}


def logged_in_with_token(state_dir: Path, access_token: str) -> None:
    with patch(
        "inky_bird_frame.birdbuddy._authenticate",
        return_value=(access_token, "refresh-0", [FEEDER]),
    ):
        login_birdbuddy(
            state_dir,
            email="birder@example.test",
            password="private-password",
            authorization_confirmed=True,
            now=NOW,
        )


def sync_at(
    state_dir: Path,
    when: datetime,
    transport: ScriptedTransport,
    *,
    persist_history: bool = True,
    include_manual_sightings: bool = False,
) -> BirdBuddySyncResult:
    with patch("inky_bird_frame.http.build_opener", return_value=transport):
        return sync_birdbuddy_detections(
            state_dir,
            window=ObservationWindow.ALL_TIME,
            limit=20,
            now=when,
            persist_history=persist_history,
            include_manual_sightings=include_manual_sightings,
        )


class AccessTokenReuseTests(unittest.TestCase):
    def test_access_token_is_reused_until_shortly_before_it_expires(self) -> None:
        login_token = signed_token(NOW + timedelta(hours=1), "login")
        transport = ScriptedTransport(feed_and_history)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, login_token)
            sync_at(state_dir, NOW, transport)
            sync_at(state_dir, NOW + timedelta(minutes=50), transport)
            self.assertEqual(transport.count(_REFRESH_TOKEN), 0)
            sync_at(state_dir, NOW + timedelta(minutes=56), transport)
            saved = auth_state(state_dir)

        self.assertEqual(transport.count(_REFRESH_TOKEN), 1)
        feed_tokens = [token for query, token in transport.calls if query in _FEED_QUERIES]
        self.assertEqual(set(feed_tokens[:4]), {login_token})
        self.assertNotIn(login_token, feed_tokens[4:])
        self.assertEqual(saved["refresh_token"], "refresh-renewed")
        self.assertEqual(saved["previous_refresh_token"], "refresh-0")

    def test_a_rejected_saved_token_is_renewed_once_and_replaced(self) -> None:
        stale = signed_token(NOW + timedelta(hours=1), "stale")

        def answer(query: str, variables: dict[str, object], token: str | None) -> object:
            if token == stale:
                raise HTTPError(
                    "https://example.test/graphql", 401, "expired", Message(), io.BytesIO()
                )
            return feed_and_history(query, variables, token)

        transport = ScriptedTransport(answer)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, stale)
            sync_at(state_dir, NOW, transport)
            saved = auth_state(state_dir)
            sync_at(state_dir, NOW + timedelta(minutes=1), transport)

        self.assertEqual(transport.count(_REFRESH_TOKEN), 1)
        self.assertNotEqual(saved.get("access_token"), stale)
        self.assertEqual(saved["refresh_token"], "refresh-renewed")

    def test_a_refused_token_is_forgotten_even_when_renewal_fails(self) -> None:
        stale = signed_token(NOW + timedelta(hours=1), "stale")

        def answer(query: str, variables: dict[str, object], token: str | None) -> object:
            if token == stale:
                raise HTTPError(
                    "https://example.test/graphql", 401, "expired", Message(), io.BytesIO()
                )
            if query == _REFRESH_TOKEN:
                raise HTTPError(
                    "https://example.test/graphql", 503, "unavailable", Message(), io.BytesIO()
                )
            return feed_and_history(query, variables, token)

        transport = ScriptedTransport(answer)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, stale)
            with self.assertRaises(DataSourceError):
                sync_at(state_dir, NOW, transport)
            saved = auth_state(state_dir)

        self.assertNotIn("access_token", saved)
        self.assertEqual(saved["refresh_token"], "refresh-0")

    def test_a_saved_token_is_not_reused_for_more_than_twelve_hours(self) -> None:
        transport = ScriptedTransport(feed_and_history)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            sync_at(state_dir, NOW + timedelta(hours=11, minutes=50), transport)
            self.assertEqual(transport.count(_REFRESH_TOKEN), 0)
            sync_at(state_dir, NOW + timedelta(hours=11, minutes=56), transport)

        self.assertEqual(transport.count(_REFRESH_TOKEN), 1)

    def test_a_token_renewed_by_another_sync_is_used_instead_of_renewing_again(self) -> None:
        stale = signed_token(NOW + timedelta(hours=1), "stale")
        fresh = signed_token(NOW + timedelta(hours=1), "fresh")

        def answer(query: str, variables: dict[str, object], token: str | None) -> object:
            if token == stale:
                # Another sync renewed the sign-in while this one was reading.
                state = auth_state(state_dir)
                state["access_token"] = fresh
                state["refresh_token"] = "refresh-other"
                path = state_dir / "birdbuddy-auth.json"
                path.write_text(json.dumps(state))
                path.chmod(0o600)
                raise HTTPError(
                    "https://example.test/graphql", 401, "expired", Message(), io.BytesIO()
                )
            return feed_and_history(query, variables, token)

        transport = ScriptedTransport(answer)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, stale)
            sync_at(state_dir, NOW, transport, persist_history=False)
            saved = auth_state(state_dir)

        self.assertEqual(transport.count(_REFRESH_TOKEN), 0)
        self.assertIn(fresh, [token for query, token in transport.calls if query in _FEED_QUERIES])
        self.assertEqual(saved["access_token"], fresh)
        self.assertEqual(saved["refresh_token"], "refresh-other")

    def test_a_token_without_a_readable_expiry_is_not_kept(self) -> None:
        def answer(query: str, variables: dict[str, object], token: str | None) -> object:
            if query == _REFRESH_TOKEN:
                return {
                    "data": {"authRefreshToken": {"accessToken": "opaque", "refreshToken": "r"}}
                }
            return feed_and_history(query, variables, token)

        transport = ScriptedTransport(answer)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, "opaque-login")
            sync_at(state_dir, NOW, transport)
            sync_at(state_dir, NOW + timedelta(minutes=1), transport)
            saved = auth_state(state_dir)

        self.assertEqual(transport.count(_REFRESH_TOKEN), 2)
        self.assertNotIn("access_token", saved)

    def test_saved_token_is_private_and_readable_by_the_earlier_reader(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(hours=1)))
            mode = stat.S_IMODE((state_dir / "birdbuddy-auth.json").stat().st_mode)
            saved = auth_state(state_dir)
            status = birdbuddy_status(state_dir)

        self.assertEqual(mode, 0o600)
        self.assertEqual(saved["refresh_token"], "refresh-0")
        self.assertIn("access_token_expires_at", saved)
        self.assertNotIn("access_token", json.dumps(status))


class ConfirmedHistoryCadenceTests(unittest.TestCase):
    def test_full_confirmed_history_is_read_at_most_once_a_day(self) -> None:
        transport = ScriptedTransport(feed_and_history)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            first = sync_at(state_dir, NOW, transport)
            later = sync_at(state_dir, NOW + timedelta(hours=23), transport)
            next_day = sync_at(state_dir, NOW + timedelta(hours=24, minutes=1), transport)

        self.assertEqual(transport.count(_CONFIRMED_HISTORY), 2)
        for result, checked in ((first, True), (later, False), (next_day, True)):
            stats = result.stats.as_dict()
            self.assertIs(stats["confirmed_history_checked"], checked)
        # Evidence from the earlier full read is kept on the days in between.
        species = [item.scientific_name for item in later.species]
        self.assertIn(HUMMINGBIRD.scientific_name, species)

    def test_turning_on_manual_sightings_reads_history_again(self) -> None:
        transport = ScriptedTransport(feed_and_history)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            sync_at(state_dir, NOW, transport)
            sync_at(
                state_dir,
                NOW + timedelta(hours=1),
                transport,
                include_manual_sightings=True,
            )

        self.assertEqual(transport.count(_CONFIRMED_HISTORY), 2)

    def test_a_preview_reads_history_but_does_not_postpone_the_daily_read(self) -> None:
        transport = ScriptedTransport(feed_and_history)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            sync_at(state_dir, NOW, transport, persist_history=False)
            sync_at(state_dir, NOW + timedelta(minutes=5), transport)

        self.assertEqual(transport.count(_CONFIRMED_HISTORY), 2)

    def test_an_owner_correction_survives_syncs_that_skip_the_full_history(self) -> None:
        confirms = {"media": True}

        def answer(query: str, variables: dict[str, object], token: str | None) -> object:
            if query == _CONFIRMED_HISTORY:
                records = [confirmed_record("media-p1")] if confirms["media"] else []
                return {"data": confirmed_page(*records)}
            if query == _FEED_QUERIES[1]:
                return {"data": page(node("p1", variant="SightingRecognizedBirdUnlocked"))}
            if query in _FEED_QUERIES:
                return {"data": page(node("p1", species=BLUEBIRD))}
            return feed_and_history(query, variables, token)

        transport = ScriptedTransport(answer)
        names: list[list[str]] = []
        reclassified: list[int] = []
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            for hours in (0, 1, 2):
                result = sync_at(state_dir, NOW + timedelta(hours=hours), transport)
                names.append([item.common_name for item in result.species])
                reclassified.append(result.stats.reclassified_postcards)
            stored = json.loads((state_dir / "birdbuddy-detections.json").read_text())
            postcards = stored["feeders"]["feeder-1"]["postcards"]
            # The owner withdraws the correction; the next full read follows it.
            confirms["media"] = False
            withdrawn = sync_at(state_dir, NOW + timedelta(hours=25), transport)

        self.assertEqual(transport.count(_CONFIRMED_HISTORY), 2)
        self.assertEqual(names, [[HUMMINGBIRD.common_name]] * 3)
        self.assertEqual(reclassified[1:], [0, 0])
        self.assertEqual(
            [species["common_name"] for species in postcards[0]["species"]],
            [HUMMINGBIRD.common_name],
        )
        self.assertEqual([item.common_name for item in withdrawn.species], ["Eastern Bluebird"])

    def test_a_confirmation_that_matches_the_feed_survives_a_later_feed_change(self) -> None:
        hummingbird: dict[str, object] = {
            "__typename": "SpeciesBird",
            "id": HUMMINGBIRD.species_id,
            "name": HUMMINGBIRD.common_name,
            "scientificName": HUMMINGBIRD.scientific_name,
        }
        feed: dict[str, dict[str, object]] = {"species": hummingbird}

        def answer(query: str, variables: dict[str, object], token: str | None) -> object:
            if query == _CONFIRMED_HISTORY:
                return {"data": confirmed_page(confirmed_record("media-p1"))}
            if query == _FEED_QUERIES[1]:
                return {"data": page(node("p1", variant="SightingRecognizedBirdUnlocked"))}
            if query in _FEED_QUERIES:
                return {"data": page(node("p1", species=feed["species"]))}
            return feed_and_history(query, variables, token)

        transport = ScriptedTransport(answer)
        names: list[list[str]] = []
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            for hours, species in ((0, hummingbird), (1, BLUEBIRD), (25, BLUEBIRD)):
                feed["species"] = species
                result = sync_at(state_dir, NOW + timedelta(hours=hours), transport)
                names.append([item.common_name for item in result.species])

        self.assertEqual(transport.count(_CONFIRMED_HISTORY), 2)
        self.assertEqual(names, [[HUMMINGBIRD.common_name]] * 3)

    def test_a_correction_saved_by_the_earlier_release_survives_a_later_feed_change(
        self,
    ) -> None:
        feed: dict[str, dict[str, object]] = {"species": BLUEBIRD}

        def answer(query: str, variables: dict[str, object], token: str | None) -> object:
            if query == _CONFIRMED_HISTORY:
                return {"data": confirmed_page(confirmed_record("media-p1"))}
            if query == _FEED_QUERIES[1]:
                return {"data": page(node("p1", variant="SightingRecognizedBirdUnlocked"))}
            if query in _FEED_QUERIES:
                return {"data": page(node("p1", species=feed["species"]))}
            return feed_and_history(query, variables, token)

        transport = ScriptedTransport(answer)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            sync_at(state_dir, NOW, transport)
            # The earlier release stored the corrected species with no marker
            # and no record of when it last read the confirmed history.
            path = state_dir / "birdbuddy-detections.json"
            stored = json.loads(path.read_text())
            feeder = stored["feeders"]["feeder-1"]
            for key in [key for key in feeder if key.startswith("confirmed_history_")]:
                del feeder[key]
            for postcard in feeder["postcards"]:
                postcard.pop("owner_confirmed")
            path.write_text(json.dumps(stored))
            # The feed now agrees with the correction on the first full read.
            feed["species"] = {
                "__typename": "SpeciesBird",
                "id": HUMMINGBIRD.species_id,
                "name": HUMMINGBIRD.common_name,
                "scientificName": HUMMINGBIRD.scientific_name,
            }
            upgraded = sync_at(state_dir, NOW + timedelta(hours=1), transport)
            feed["species"] = BLUEBIRD
            later = sync_at(state_dir, NOW + timedelta(hours=2), transport)

        self.assertIs(upgraded.stats.confirmed_history_checked, True)
        self.assertIs(later.stats.confirmed_history_checked, False)
        self.assertEqual([item.common_name for item in later.species], [HUMMINGBIRD.common_name])

    def test_a_withdrawn_correction_follows_the_feed_on_incomplete_full_reads(self) -> None:
        confirms = {"media": True}

        def answer(query: str, variables: dict[str, object], token: str | None) -> object:
            if query == _CONFIRMED_HISTORY:
                # A record Inky does not know is skipped on every read.
                records = [confirmed_record("media-other", origin="NEW")]
                if confirms["media"]:
                    records.append(confirmed_record("media-p1"))
                return {"data": confirmed_page(*records)}
            if query == _FEED_QUERIES[1]:
                return {"data": page(node("p1", variant="SightingRecognizedBirdUnlocked"))}
            if query in _FEED_QUERIES:
                return {"data": page(node("p1", species=BLUEBIRD))}
            return feed_and_history(query, variables, token)

        transport = ScriptedTransport(answer)
        names: list[list[str]] = []
        stored_names: list[list[str]] = []
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=5)))
            first = sync_at(state_dir, NOW, transport)
            confirms["media"] = False
            for hours in (25, 50, 75):
                result = sync_at(state_dir, NOW + timedelta(hours=hours), transport)
                self.assertIs(result.stats.confirmed_history_checked, True)
                self.assertIs(result.stats.confirmed_history_complete, False)
                names.append([item.common_name for item in result.species])
                stored = json.loads((state_dir / "birdbuddy-detections.json").read_text())
                postcard = stored["feeders"]["feeder-1"]["postcards"][0]
                stored_names.append([species["common_name"] for species in postcard["species"]])

        self.assertEqual([item.common_name for item in first.species], [HUMMINGBIRD.common_name])
        for listed in names:
            self.assertIn("Eastern Bluebird", listed)
        self.assertEqual(stored_names, [["Eastern Bluebird"]] * 3)

    def test_skipped_days_report_the_completeness_of_the_last_full_read(self) -> None:
        for records, complete in (
            ([confirmed_record("media-1")], True),
            ([confirmed_record("media-1"), confirmed_record("media-2", origin="NEW")], False),
        ):

            def answer(
                query: str,
                variables: dict[str, object],
                token: str | None,
                records: list[dict[str, object]] = records,
            ) -> object:
                if query == _CONFIRMED_HISTORY:
                    return {"data": confirmed_page(*records)}
                return feed_and_history(query, variables, token)

            transport = ScriptedTransport(answer)
            with TemporaryDirectory() as temporary, self.subTest(complete=complete):
                state_dir = Path(temporary)
                logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
                first = sync_at(state_dir, NOW, transport)
                later = sync_at(state_dir, NOW + timedelta(hours=1), transport)
                self.assertIs(first.stats.confirmed_history_complete, complete)
                self.assertIs(later.stats.confirmed_history_checked, False)
                self.assertIs(later.stats.confirmed_history_complete, complete)


class RateLimitTests(unittest.TestCase):
    def throttled_once(self, retry_after: str | None) -> Callable[..., object]:
        sent = {"done": False}

        def answer(query: str, variables: dict[str, object], token: str | None) -> object:
            if not sent["done"] and query in _FEED_QUERIES:
                sent["done"] = True
                raise throttled(retry_after)
            return feed_and_history(query, variables, token)

        return answer

    def test_retry_after_is_honoured_by_later_runs_without_contacting_bird_buddy(self) -> None:
        transport = ScriptedTransport(self.throttled_once("600"))
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            with self.assertRaisesRegex(DataSourceError, "asked Inky to slow down") as first:
                sync_at(state_dir, NOW, transport)
            backoff = state_dir / "birdbuddy-backoff.json"
            mode = stat.S_IMODE(backoff.stat().st_mode)
            calls = len(transport.calls)
            with self.assertRaisesRegex(DataSourceError, "slow down"):
                sync_at(state_dir, NOW + timedelta(minutes=9), transport)
            with self.assertRaisesRegex(DataSourceError, "slow down"):
                sync_at(state_dir, NOW + timedelta(minutes=9), transport, persist_history=False)
            self.assertEqual(len(transport.calls), calls)
            sync_at(state_dir, NOW + timedelta(minutes=11), transport)
            cleared = not backoff.exists()

        self.assertEqual(mode, 0o600)
        self.assertIn("2026-08-03T12:10:00+00:00", str(first.exception))
        self.assertTrue(cleared)

    def test_throttling_without_a_hint_backs_off_longer_each_time(self) -> None:
        def answer(query: str, variables: dict[str, object], token: str | None) -> object:
            if query in _FEED_QUERIES:
                raise throttled(None)
            return feed_and_history(query, variables, token)

        transport = ScriptedTransport(answer)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            messages = []
            when = NOW
            for _ in range(3):
                with self.assertRaises(DataSourceError) as raised:
                    sync_at(state_dir, when, transport)
                messages.append(str(raised.exception))
                stored = json.loads((state_dir / "birdbuddy-backoff.json").read_text())
                when = datetime.fromisoformat(str(stored["not_before"]))

        self.assertIn("2026-08-03T12:15:00+00:00", messages[0])
        self.assertIn("2026-08-03T12:45:00+00:00", messages[1])
        self.assertIn("2026-08-03T13:45:00+00:00", messages[2])

    def test_an_excessive_or_dated_retry_hint_is_bounded(self) -> None:
        for hint, expected in (
            ("999999999", "2026-08-04T12:00:00+00:00"),
            ("Mon, 03 Aug 2026 13:00:00 GMT", "2026-08-03T13:00:00+00:00"),
            ("soon", "2026-08-03T12:15:00+00:00"),
        ):
            transport = ScriptedTransport(self.throttled_once(hint))
            with TemporaryDirectory() as temporary, self.subTest(hint=hint):
                state_dir = Path(temporary)
                logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
                with self.assertRaises(DataSourceError) as raised:
                    sync_at(state_dir, NOW, transport)
                self.assertIn(expected, str(raised.exception))

    def test_a_wait_beyond_the_longest_allowed_is_ignored(self) -> None:
        transport = ScriptedTransport(feed_and_history)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            backoff = state_dir / "birdbuddy-backoff.json"
            # Written while the clock read far ahead of the real time.
            backoff.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "not_before": (NOW + timedelta(days=366)).isoformat(),
                        "failures": 1,
                    }
                )
            )
            backoff.chmod(0o600)
            sync_at(state_dir, NOW, transport)
            cleared = not backoff.exists()

        self.assertGreater(len(transport.calls), 0)
        self.assertTrue(cleared)

    def test_a_backoff_path_that_cannot_be_removed_does_not_fail_the_sync(self) -> None:
        transport = ScriptedTransport(feed_and_history)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            (state_dir / "birdbuddy-backoff.json").mkdir()
            result = sync_at(state_dir, NOW, transport)

        self.assertEqual(
            [item.scientific_name for item in result.species], [HUMMINGBIRD.scientific_name]
        )

    def test_a_preview_does_not_clear_a_wait_recorded_while_it_ran(self) -> None:
        def answer(query: str, variables: dict[str, object], token: str | None) -> object:
            if query == _CONFIRMED_HISTORY and not backoff.exists():
                # A scheduled sync is throttled while the preview is reading.
                backoff.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "not_before": (NOW + timedelta(minutes=15)).isoformat(),
                            "failures": 1,
                        }
                    )
                )
                backoff.chmod(0o600)
            return feed_and_history(query, variables, token)

        transport = ScriptedTransport(answer)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            backoff = state_dir / "birdbuddy-backoff.json"
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            sync_at(state_dir, NOW, transport, persist_history=False)
            kept = backoff.exists()

        self.assertTrue(kept)

    def test_an_unreadable_backoff_record_does_not_stop_the_sync(self) -> None:
        transport = ScriptedTransport(feed_and_history)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            logged_in_with_token(state_dir, signed_token(NOW + timedelta(days=3)))
            backoff = state_dir / "birdbuddy-backoff.json"
            backoff.write_text("{not json")
            backoff.chmod(0o600)
            sync_at(state_dir, NOW, transport)
            cleared = not backoff.exists()

        self.assertTrue(cleared)


if __name__ == "__main__":
    unittest.main()
