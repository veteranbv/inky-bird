from __future__ import annotations

import errno
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import Message
from http.client import HTTPMessage, HTTPResponse, IncompleteRead, RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import MagicMock, PropertyMock, patch
from urllib.error import HTTPError
from urllib.request import Request

import inky_bird_frame.http
from inky_bird_frame import __version__
from inky_bird_frame.errors import DataSourceError
from inky_bird_frame.http import get_bytes, get_json, post_json, write_json_atomic


class _RedirectingHandler(BaseHTTPRequestHandler):
    last_post_body = b""
    last_user_agent = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).last_user_agent = self.headers.get("User-Agent", "")
        if self.path in {"/slow-body", "/slow-headers"}:
            self._trickle(headers=self.path == "/slow-headers")
            return
        if self.path == "/short-body":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"truncated")
            self.close_connection = True
            return
        if self.path == "/chunked-with-length":
            # Recipients ignore Content-Length when the body is chunked.
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b'8\r\n{"a": 1}\r\n0\r\n\r\n')
            self.close_connection = True
            return
        if self.path in {"/declared-large", "/undeclared-large"}:
            body = b'"' + b"x" * 64 + b'"'
            self.send_response(200)
            if self.path == "/declared-large":
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/unavailable":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/ftp-redirect":
            self.send_response(302)
            self.send_header("Location", "ftp://127.0.0.1/pub/file.bin")
            self.end_headers()
            return
        if self.path == "/http-redirect":
            self.send_response(302)
            self.send_header("Location", "/payload")
            self.end_headers()
            return
        body = b"payload-bytes"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _trickle(self, *, headers: bool) -> None:
        # Every write arrives well inside the per-read timeout, so only a total
        # deadline can stop the exchange before the server gives up (4 s).
        try:
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            if not headers:
                self.wfile.write(b"Content-Length: 1000\r\n\r\n")
            for _ in range(80):
                self.wfile.write(b"X-Slow: 1\r\n" if headers else b" ")
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass
        self.close_connection = True

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).last_user_agent = self.headers.get("User-Agent", "")
        if self.path == "/slow-body":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self._trickle(headers=False)
            return
        if self.path == "/external-redirect":
            self.send_response(307)
            self.send_header("Location", "https://other.example.test/graphql")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        type(self).last_post_body = self.rfile.read(length)
        body = b'{"accepted": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message_format: str, *args: object) -> None:
        pass


@contextmanager
def _serving() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


class AtomicWriteTests(unittest.TestCase):
    def test_failed_write_preserves_destination_and_removes_temporary_file(self) -> None:
        for operation, mode in (("fchmod", 0o600), ("fsync", None)):
            with self.subTest(operation=operation), TemporaryDirectory() as temporary:
                path = Path(temporary) / "index.json"
                original = '{"previous": true}\n'
                path.write_text(original)

                with (
                    patch(
                        f"inky_bird_frame.http.os.{operation}",
                        side_effect=OSError(errno.ENOSPC, "No space left on device"),
                    ),
                    self.assertRaises(OSError),
                ):
                    write_json_atomic(path, {"current": True}, mode=mode)

                self.assertEqual(path.read_text(), original)
                self.assertEqual(
                    [entry.name for entry in Path(temporary).iterdir()], ["index.json"]
                )


class JsonHttpTests(unittest.TestCase):
    def test_posts_bounded_json_payload(self) -> None:
        with _serving() as port:
            result = post_json(
                f"http://127.0.0.1:{port}/graphql",
                {"query": "query Test { ok }", "variables": {"first": 50}},
            )

        self.assertEqual(result, {"accepted": True})
        self.assertEqual(
            _RedirectingHandler.last_post_body,
            b'{"query": "query Test { ok }", "variables": {"first": 50}}',
        )

    def test_post_error_label_redacts_graphql_endpoint(self) -> None:
        url = "https://private.example.test/graphql"
        error = HTTPError(url, 429, "Rate limited", Message(), None)

        with (
            patch("inky_bird_frame.http.build_opener") as opener,
            self.assertRaises(DataSourceError) as raised,
        ):
            opener.return_value.open.side_effect = error
            post_json(url, {"secret": "private"}, error_label="Bird Buddy API")

        self.assertEqual(str(raised.exception), "HTTP 429 from Bird Buddy API")
        self.assertNotIn("private.example.test", str(raised.exception))

    def test_post_rejects_cross_origin_redirect(self) -> None:
        with (
            _serving() as port,
            self.assertRaisesRegex(DataSourceError, "cross-origin"),
        ):
            post_json(
                f"http://127.0.0.1:{port}/external-redirect",
                {"secret": "private"},
            )

    def test_error_label_redacts_sensitive_url(self) -> None:
        url = "https://example.test/stations/private-token/species"
        error = HTTPError(url, 401, "Unauthorized", Message(), None)

        with (
            patch("inky_bird_frame.http.build_opener") as opener,
            self.assertRaises(DataSourceError) as raised,
        ):
            opener.return_value.open.side_effect = error
            get_json(url, error_label="BirdWeather API")

        self.assertEqual(str(raised.exception), "HTTP 401 from BirdWeather API")
        self.assertNotIn("private-token", str(raised.exception))

    def test_get_json_converts_incomplete_transport_responses(self) -> None:
        url = "http://private-controller.local/api"
        for error in (RemoteDisconnected("closed"), IncompleteRead(b"{}", 10)):
            with (
                self.subTest(error=type(error).__name__),
                patch("inky_bird_frame.http.build_opener") as opener,
                self.assertRaises(DataSourceError) as raised,
            ):
                opener.return_value.open.side_effect = error
                get_json(url, error_label="BirdNET-Go API")

            self.assertEqual(str(raised.exception), "Invalid HTTP response from BirdNET-Go API")
            self.assertNotIn("private-controller.local", str(raised.exception))

    def test_get_json_converts_invalid_utf8_to_sanitized_error(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'\xff{"invalid": true}'
        response.headers = Message()
        with (
            patch("inky_bird_frame.http.build_opener") as opener,
            self.assertRaises(DataSourceError) as raised,
        ):
            opener.return_value.open.return_value = response
            get_json(
                "http://private-controller.local/api",
                error_label="BirdNET-Go API",
            )

        self.assertEqual(str(raised.exception), "Invalid JSON from BirdNET-Go API")
        self.assertNotIn("private-controller.local", str(raised.exception))

    def test_rejects_non_http_url_scheme(self) -> None:
        with self.assertRaisesRegex(DataSourceError, "non-HTTP URL scheme"):
            get_bytes("file:///etc/hostname")

    def test_rejects_redirect_to_non_http_scheme(self) -> None:
        with (
            _serving() as port,
            self.assertRaisesRegex(DataSourceError, "Refusing redirect"),
        ):
            get_bytes(f"http://127.0.0.1:{port}/ftp-redirect")

    def test_follows_same_scheme_redirects(self) -> None:
        with _serving() as port:
            body = get_bytes(f"http://127.0.0.1:{port}/http-redirect")

        self.assertEqual(body, b"payload-bytes")

    def test_rejects_https_downgrades_before_creating_redirect_request(self) -> None:
        handler = inky_bird_frame.http._HTTPOnlyRedirectHandler()
        for code in (301, 302, 303, 307, 308):
            for method in ("GET", "HEAD"):
                with self.subTest(code=code, method=method):
                    request = Request("https://example.test/private?secret=value", method=method)
                    with self.assertRaisesRegex(DataSourceError, "HTTPS-to-HTTP") as caught:
                        handler.redirect_request(
                            request,
                            MagicMock(),
                            code,
                            "Redirect",
                            HTTPMessage(),
                            "http://example.test/private",
                        )
                    self.assertNotIn("secret", str(caught.exception))
                    self.assertNotIn("example.test", str(caught.exception))

    def test_allows_https_redirects_and_http_upgrades(self) -> None:
        handler = inky_bird_frame.http._HTTPOnlyRedirectHandler()
        for source in ("http://example.test/start", "https://example.test/start"):
            with self.subTest(source=source):
                redirected = handler.redirect_request(
                    Request(source),
                    MagicMock(),
                    302,
                    "Redirect",
                    HTTPMessage(),
                    "https://example.test/end",
                )
                self.assertIsNotNone(redirected)
                assert redirected is not None
                self.assertEqual(redirected.full_url, "https://example.test/end")


class DeadlineAndLimitTests(unittest.TestCase):
    def test_trickling_upstream_is_stopped_by_the_total_deadline(self) -> None:
        for path in ("/slow-body", "/slow-headers"):
            for fetch in (get_json, get_bytes):
                with self.subTest(path=path, fetch=fetch.__name__), _serving() as port:
                    started = time.monotonic()
                    with self.assertRaisesRegex(DataSourceError, "^Timed out reading "):
                        fetch(f"http://127.0.0.1:{port}{path}", 0.4)
                    self.assertLess(time.monotonic() - started, 3.0)

    def test_post_has_the_same_total_deadline(self) -> None:
        with _serving() as port:
            started = time.monotonic()
            with self.assertRaisesRegex(DataSourceError, "^Timed out reading Fixture API$"):
                post_json(
                    f"http://127.0.0.1:{port}/slow-body",
                    {},
                    0.4,
                    error_label="Fixture API",
                    deadline_seconds=0.6,
                )
            self.assertLess(time.monotonic() - started, 3.0)

    def test_an_explicit_deadline_shorter_than_one_read_is_honoured(self) -> None:
        with _serving() as port:
            started = time.monotonic()
            with self.assertRaisesRegex(DataSourceError, "Timed out"):
                get_bytes(f"http://127.0.0.1:{port}/slow-body", 10.0, deadline_seconds=0.5)
            self.assertLess(time.monotonic() - started, 3.0)

    def test_response_size_limits_are_enforced_with_and_without_a_length(self) -> None:
        for path in ("/declared-large", "/undeclared-large"):
            with self.subTest(path=path), _serving() as port:
                url = f"http://127.0.0.1:{port}{path}"
                self.assertEqual(get_json(url), "x" * 64)
                with (
                    patch("inky_bird_frame.http.MAX_JSON_BYTES", 65),
                    self.assertRaisesRegex(DataSourceError, "exceeds 65 bytes"),
                ):
                    get_json(url)
                with (
                    patch("inky_bird_frame.http.MAX_ASSET_BYTES", 65),
                    self.assertRaisesRegex(DataSourceError, "exceeds 65 bytes"),
                ):
                    get_bytes(url)
                with patch("inky_bird_frame.http.MAX_ASSET_BYTES", 66):
                    self.assertEqual(len(get_bytes(url)), 66)

    def test_a_body_shorter_than_its_declared_length_is_not_returned(self) -> None:
        with (
            _serving() as port,
            self.assertRaisesRegex(DataSourceError, "^Invalid HTTP response from "),
        ):
            get_bytes(f"http://127.0.0.1:{port}/short-body")
        with _serving() as port:
            self.assertEqual(get_bytes(f"http://127.0.0.1:{port}/chunked-with-length"), b'{"a": 1}')

    def test_a_deadline_spent_before_connecting_reads_as_a_timeout(self) -> None:
        # The deadline starts at 0; every later clock reading is past it.
        clock = iter([0.0])
        with (
            _serving() as port,
            patch("inky_bird_frame.http.monotonic", lambda: next(clock, 1e9)),
            self.assertRaisesRegex(DataSourceError, "^Timed out reading "),
        ):
            get_bytes(f"http://127.0.0.1:{port}/payload", deadline_seconds=60)

    def test_a_deadline_that_ends_after_a_complete_body_keeps_the_response(self) -> None:
        read_capped = inky_bird_frame.http._read_capped

        def read_then_expire(response: HTTPResponse, limit: int, display_url: str) -> bytes:
            body = read_capped(response, limit, display_url)
            deadline = inky_bird_frame.http._current_deadline.get()
            assert deadline is not None
            deadline._expire()
            return body

        cases = (("/payload", b"payload-bytes"), ("/chunked-with-length", b'{"a": 1}'))
        for path, expected in cases:
            with (
                self.subTest(path=path),
                _serving() as port,
                patch("inky_bird_frame.http._read_capped", read_then_expire),
            ):
                self.assertEqual(get_bytes(f"http://127.0.0.1:{port}{path}"), expected)
        # A body that ends only when the connection closes cannot show it is
        # complete, so a deadline that has ended still makes it a timeout.
        with (
            _serving() as port,
            patch("inky_bird_frame.http._read_capped", read_then_expire),
            self.assertRaisesRegex(DataSourceError, "^Timed out reading "),
        ):
            get_bytes(f"http://127.0.0.1:{port}/undeclared-large")

    def test_a_status_that_arrives_after_the_deadline_is_still_reported(self) -> None:
        # A complete status line is an answer, not an interrupted read.
        with (
            _serving() as port,
            patch.object(
                inky_bird_frame.http._Deadline,
                "spent",
                new_callable=PropertyMock,
                return_value=True,
            ),
            self.assertRaisesRegex(DataSourceError, "^HTTP 503 from "),
        ):
            get_bytes(f"http://127.0.0.1:{port}/unavailable", deadline_seconds=60)

    def test_requests_identify_this_release(self) -> None:
        expected = f"inky-bird/{__version__}"
        with _serving() as port:
            get_bytes(f"http://127.0.0.1:{port}/payload")
            self.assertEqual(_RedirectingHandler.last_user_agent, expected)
            post_json(f"http://127.0.0.1:{port}/graphql", {})
            self.assertEqual(_RedirectingHandler.last_user_agent, expected)


if __name__ == "__main__":
    unittest.main()
