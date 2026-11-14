from __future__ import annotations

import io
import struct
import unittest
import zlib
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from inky_bird_frame import http
from inky_bird_frame.errors import DataSourceError, InsufficientReferencesError
from inky_bird_frame.references import (
    REFERENCE_FIELDS,
    ReferenceCandidate,
    download_references,
    fetch_reference_candidates,
    parse_reference_candidates,
    photo_url_for_size,
)


def observation(
    observation_id: int,
    observer: str,
    photo_id: int,
    license_code: str = "cc-by",
    width: int = 1600,
    height: int = 1200,
) -> dict[str, object]:
    return {
        "id": observation_id,
        "uri": f"https://www.inaturalist.org/observations/{observation_id}",
        "user": {"login": observer},
        "photos": [
            {
                "id": photo_id,
                "attribution": f"Photo by {observer}",
                "license_code": license_code,
                "url": f"https://static.example/photos/{photo_id}/square.jpg",
                "original_dimensions": {"width": width, "height": height},
            }
        ],
    }


def download_failure(error: BaseException) -> DataSourceError:
    """Return the error the real downloader raises for one transport failure."""
    url = "https://static.example/photos/9/large.png"
    with patch("inky_bird_frame.http.build_opener") as build_opener:
        build_opener.return_value.open.side_effect = error
        try:
            http.get_bytes(url)
        except DataSourceError as exc:
            return exc
    raise AssertionError("the downloader did not fail")


def http_error(code: int) -> HTTPError:
    return HTTPError("https://static.example/photos/9/large.png", code, "error", Message(), None)


class ReferenceTests(unittest.TestCase):
    def test_selects_permissive_photos_from_distinct_observers(self) -> None:
        payload = {
            "results": [
                observation(1, "alice", 11),
                observation(2, "alice", 12),
                observation(3, "bob", 13, "cc0"),
                observation(4, "carol", 14, "cc-by-nc"),
            ]
        }

        references = parse_reference_candidates(payload, 2)

        self.assertEqual([item.photo_id for item in references], [11, 13])
        self.assertEqual(references[0].image_url, "https://static.example/photos/11/large.jpg")

    def test_rejects_insufficient_licensed_references(self) -> None:
        with self.assertRaises(InsufficientReferencesError):
            parse_reference_candidates({"results": [observation(1, "alice", 11, "cc-by-nc")]}, 1)

    @patch("inky_bird_frame.references.get_json")
    def test_fetches_only_reference_selection_fields(self, get_json: MagicMock) -> None:
        get_json.return_value = {
            "results": [
                observation(1, "alice", 11),
                observation(2, "alice", 12),
                observation(3, "bob", 13, "cc0"),
            ]
        }

        references = fetch_reference_candidates(9602, 2, timeout_seconds=12.0)

        self.assertEqual([item.photo_id for item in references], [11, 13])
        get_json.assert_called_once()
        url, timeout = get_json.call_args.args
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "api.inaturalist.org")
        self.assertEqual(parsed.path, "/v2/observations")
        self.assertEqual(timeout, 12.0)
        self.assertEqual(query["order_by"], ["votes"])
        self.assertEqual(query["order"], ["desc"])
        self.assertEqual(query["per_page"], ["30"])
        self.assertEqual(query["fields"], [",".join(REFERENCE_FIELDS)])

    def test_photo_url_size_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            photo_url_for_size("https://example.test/1/square.jpg", "enormous")


def _png(width: int = 64, height: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, "PNG")
    return buffer.getvalue()


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.effect_noise((900, 900), 64).convert("RGB").save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def _png_with_bad_idat_checksum() -> bytes:
    content = _png()
    index = content.index(b"IDAT")
    return content[: index + 4] + bytes([content[index + 4] ^ 0xFF]) + content[index + 5 :]


def _png_with_undecodable_idat() -> bytes:
    content = _png()
    index = content.index(b"IDAT")
    length = struct.unpack(">I", content[index - 4 : index])[0]
    data = b"not compressed image data"
    chunk = (
        struct.pack(">I", len(data))
        + b"IDAT"
        + data
        + struct.pack(">I", zlib.crc32(b"IDAT" + data))
    )
    return content[: index - 4] + chunk + content[index + 4 + length + 4 :]


def _candidate(photo_id: int) -> ReferenceCandidate:
    return ReferenceCandidate(
        photo_id=photo_id,
        observation_id=photo_id + 1000,
        observer=f"observer-{photo_id}",
        attribution=f"Photo by observer-{photo_id}",
        license_code="cc-by",
        source_url=f"https://www.inaturalist.org/observations/{photo_id + 1000}",
        image_url=f"https://static.example/photos/{photo_id}/large.png",
        width=1600,
        height=1200,
    )


class ReferenceValidationTests(unittest.TestCase):
    def _download_one(self, content: bytes) -> None:
        with TemporaryDirectory() as temporary:
            destination = Path(temporary)
            with patch("inky_bird_frame.references.get_bytes", return_value=content):
                try:
                    download_references([_candidate(1)], destination)
                finally:
                    self.assertEqual(list(destination.iterdir()), [])

    def test_invalid_images_raise_data_source_errors(self) -> None:
        jpeg = _jpeg()
        cases = {
            "bad chunk checksum": _png_with_bad_idat_checksum(),
            "undecodable pixel data": _png_with_undecodable_idat(),
            "truncated jpeg": jpeg[: len(jpeg) // 3],
            "not an image": b"plain text",
        }
        for name, content in cases.items():
            with self.subTest(name), self.assertRaises(DataSourceError):
                self._download_one(content)

    def test_decompression_bomb_raises_data_source_error(self) -> None:
        for limit in (100, 3000):
            with (
                self.subTest(limit=limit),
                patch.object(Image, "MAX_IMAGE_PIXELS", limit),
                self.assertRaises(DataSourceError),
            ):
                self._download_one(_png(64, 64))

    def test_valid_image_is_accepted(self) -> None:
        with TemporaryDirectory() as temporary:
            destination = Path(temporary)
            with patch("inky_bird_frame.references.get_bytes", return_value=_png()):
                references = download_references([_candidate(1)], destination)

            self.assertEqual([item.filename for item in references], ["01-1.png"])
            self.assertTrue((destination / "01-1.png").is_file())


class ReferenceFallbackTests(unittest.TestCase):
    def test_bad_missing_or_oversized_photo_falls_back_to_next_candidate(self) -> None:
        responses: dict[int, bytes | DataSourceError] = {
            1: _png(),
            2: download_failure(http_error(404)),
            3: _png_with_bad_idat_checksum(),
            4: DataSourceError("Response exceeds the supported size"),
            5: _png(),
            6: _png(),
        }

        def fetch(url: str, _timeout: float) -> bytes:
            photo_id = int(urlsplit(url).path.split("/")[2])
            response = responses[photo_id]
            if isinstance(response, DataSourceError):
                raise response
            return response

        payload = {"results": [observation(index, f"user-{index}", index) for index in range(1, 7)]}
        candidates = parse_reference_candidates(payload, 3)
        self.assertEqual([item.photo_id for item in candidates], [1, 2, 3])

        with TemporaryDirectory() as temporary:
            destination = Path(temporary)
            with patch("inky_bird_frame.references.get_bytes", side_effect=fetch):
                references = download_references(candidates, destination)

            self.assertEqual([item.photo_id for item in references], [1, 5, 6])
            self.assertEqual(
                sorted(path.name for path in destination.iterdir()),
                sorted(item.filename for item in references),
            )
        self.assertEqual(len({item.observer for item in references}), 3)

    def test_exhausted_candidates_with_only_bad_images_are_insufficient(self) -> None:
        payload = {"results": [observation(index, f"user-{index}", index) for index in range(1, 4)]}
        candidates = parse_reference_candidates(payload, 2)

        with (
            TemporaryDirectory() as temporary,
            patch(
                "inky_bird_frame.references.get_bytes",
                side_effect=[_png(), b"plain text", _png_with_bad_idat_checksum()],
            ),
            self.assertRaises(InsufficientReferencesError),
        ):
            download_references(candidates, Path(temporary))

    def test_exhausted_candidates_after_download_failure_stay_retryable(self) -> None:
        payload = {"results": [observation(index, f"user-{index}", index) for index in range(1, 3)]}
        candidates = parse_reference_candidates(payload, 2)

        with (
            TemporaryDirectory() as temporary,
            patch(
                "inky_bird_frame.references.get_bytes",
                side_effect=[_png(), download_failure(TimeoutError())],
            ),
            self.assertRaises(DataSourceError) as raised,
        ):
            download_references(candidates, Path(temporary))

        self.assertNotIsInstance(raised.exception, InsufficientReferencesError)

    def test_source_wide_download_failure_stops_without_fallback(self) -> None:
        failures: dict[str, BaseException] = {
            "rate limited": http_error(429),
            "server error": http_error(503),
            "unreachable": URLError("connection refused"),
            "timed out": TimeoutError(),
        }
        payload = {"results": [observation(index, f"user-{index}", index) for index in range(1, 9)]}
        for name, failure in failures.items():
            with self.subTest(name), TemporaryDirectory() as temporary:
                candidates = parse_reference_candidates(payload, 2)
                fetch = MagicMock(side_effect=[_png(), download_failure(failure), _png(), _png()])
                with (
                    patch("inky_bird_frame.references.get_bytes", fetch),
                    self.assertRaises(DataSourceError) as raised,
                ):
                    download_references(candidates, Path(temporary))

                self.assertNotIsInstance(raised.exception, InsufficientReferencesError)
                self.assertEqual(fetch.call_count, 2)

    def test_photo_fallback_is_capped(self) -> None:
        payload = {
            "results": [observation(index, f"user-{index}", index) for index in range(1, 21)]
        }
        candidates = parse_reference_candidates(payload, 2)
        missing = download_failure(http_error(404))
        fetch = MagicMock(side_effect=[_png(), *([missing] * 19)])

        with (
            TemporaryDirectory() as temporary,
            patch("inky_bird_frame.references.get_bytes", fetch),
            self.assertRaises(DataSourceError),
        ):
            download_references(candidates, Path(temporary))

        self.assertEqual(fetch.call_count, 4)


if __name__ == "__main__":
    unittest.main()
