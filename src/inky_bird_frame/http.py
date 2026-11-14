"""Small stdlib JSON HTTP helper."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from http.client import HTTPConnection, HTTPException, HTTPMessage, HTTPResponse, HTTPSConnection
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO, TYPE_CHECKING, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    BaseHandler,
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)

from . import __version__
from .errors import DataSourceError

if TYPE_CHECKING:
    from urllib.request import _HTTPConnectionProtocol

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
USER_AGENT = f"inky-bird/{__version__}"
# The timeout bounds each socket read. A server that trickles bytes inside it
# could hold a worker indefinitely, so a whole exchange, redirects included,
# also ends after this many timeouts unless the caller sets its own deadline.
DEADLINE_TIMEOUTS = 4


def read_private_json(path: Path, *, maximum: int) -> object:
    """Bound a private local document and refuse symlinks before opening it."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise DataSourceError("Private state must be a regular file with mode 0600")
            body = handle.read(maximum + 1)
        if len(body) > maximum:
            raise DataSourceError("Private state exceeds its supported size")
        return json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataSourceError("Private state cannot be read; restore its matching backup") from exc


def _read_capped(response: HTTPResponse, limit: int, display_url: str) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise DataSourceError(f"Response from {display_url} exceeds {limit} bytes")
    body = response.read(limit + 1)
    if len(body) > limit:
        raise DataSourceError(f"Response from {display_url} exceeds {limit} bytes")
    if declared is not None and declared.isdigit() and len(body) != int(declared):
        # read(amount) returns what arrived before the connection closed.
        raise HTTPException("response shorter than its declared length")
    return body


class _Deadline:
    """Shut down this exchange's sockets once its total time is spent."""

    def __init__(self, seconds: float) -> None:
        self.ends_at = time.monotonic() + seconds
        self.expired = False
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.cancel()

    def watch(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.append(sock)
            if self.expired:
                _shut_down(sock)

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            for sock in self._sockets:
                _shut_down(sock)


def _shut_down(sock: socket.socket) -> None:
    # Shut down the descriptor itself, not the TLS layer: a read blocked in
    # another thread then returns instead of racing a TLS teardown.
    with suppress(OSError):
        socket.socket.shutdown(sock, socket.SHUT_RDWR)


_current_deadline: ContextVar[_Deadline | None] = ContextVar("inky_http_deadline", default=None)


def _watch_connection(connection: HTTPConnection) -> None:
    deadline = _current_deadline.get()
    if deadline is not None and connection.sock is not None:
        deadline.watch(connection.sock)


def _bound_connect_timeout(connection: HTTPConnection) -> None:
    deadline = _current_deadline.get()
    if deadline is None:
        return
    remaining = deadline.ends_at - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("timed out")
    current = connection.timeout
    if not isinstance(current, (int, float)) or current > remaining:
        connection.timeout = remaining


class _DeadlineHTTPConnection(HTTPConnection):
    def connect(self) -> None:
        _bound_connect_timeout(self)
        super().connect()
        _watch_connection(self)


class _DeadlineHTTPSConnection(HTTPSConnection):
    def connect(self) -> None:
        _bound_connect_timeout(self)
        super().connect()
        _watch_connection(self)


class _DeadlineHTTPHandler(HTTPHandler):
    def do_open(
        self, http_class: _HTTPConnectionProtocol, req: Request, **http_conn_args: object
    ) -> HTTPResponse:
        return super().do_open(_DeadlineHTTPConnection, req, **http_conn_args)


class _DeadlineHTTPSHandler(HTTPSHandler):
    def do_open(
        self, http_class: _HTTPConnectionProtocol, req: Request, **http_conn_args: object
    ) -> HTTPResponse:
        return super().do_open(_DeadlineHTTPSConnection, req, **http_conn_args)


@contextmanager
def _total_deadline(seconds: float) -> Iterator[_Deadline]:
    deadline = _Deadline(seconds)
    token = _current_deadline.set(deadline)
    deadline.start()
    try:
        yield deadline
    finally:
        deadline.stop()
        _current_deadline.reset(token)


def _checked_request(
    url: str,
    headers: Mapping[str, str],
    *,
    data: bytes | None = None,
    method: str | None = None,
) -> Request:
    scheme = urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise DataSourceError(f"Refusing to fetch non-HTTP URL scheme: {scheme or 'none'}")
    return Request(url, data=data, headers=dict(headers), method=method)


class _HTTPOnlyRedirectHandler(HTTPRedirectHandler):
    # urllib otherwise follows redirects onto FTP, bypassing the scheme check.
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        scheme = urlsplit(newurl).scheme
        if scheme not in ("http", "https"):
            raise DataSourceError(f"Refusing redirect to non-HTTP URL scheme: {scheme or 'none'}")
        if urlsplit(req.full_url).scheme == "https" and scheme == "http":
            raise DataSourceError("Refusing HTTPS-to-HTTP redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _SameOriginRedirectHandler(_HTTPOnlyRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        original = urlsplit(req.full_url)
        redirected = urlsplit(newurl)
        if (original.scheme, original.hostname, original.port) != (
            redirected.scheme,
            redirected.hostname,
            redirected.port,
        ):
            raise DataSourceError("Refusing cross-origin JSON POST redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _transport_error(exc: Exception, display_url: str) -> DataSourceError:
    if isinstance(exc, HTTPError):
        return DataSourceError(f"HTTP {exc.code} from {display_url}")
    if isinstance(exc, URLError):
        return DataSourceError(f"Could not reach {display_url}: {exc.reason}")
    if isinstance(exc, TimeoutError):
        return DataSourceError(f"Timed out reading {display_url}")
    if isinstance(exc, HTTPException):
        return DataSourceError(f"Invalid HTTP response from {display_url}")
    return DataSourceError(f"Could not read response from {display_url}")


def _fetch(
    request: Request,
    redirects: type[BaseHandler],
    *,
    timeout_seconds: float,
    deadline_seconds: float | None,
    limit: int,
    display_url: str,
) -> bytes:
    total = timeout_seconds * DEADLINE_TIMEOUTS if deadline_seconds is None else deadline_seconds
    with _total_deadline(total) as deadline:
        try:
            # Python 3.14 snapshots default CA trust when creating the handler.
            # Build it after the caller has selected this process's native trust.
            opener = build_opener(redirects, _DeadlineHTTPHandler, _DeadlineHTTPSHandler)
            with opener.open(request, timeout=timeout_seconds) as response:
                body = _read_capped(cast(HTTPResponse, response), limit, display_url)
        except DataSourceError:
            raise
        except (URLError, HTTPException, OSError) as exc:
            # A spent deadline shuts the socket down; whatever the interrupted
            # step raised, it is the timeout.
            if deadline.expired:
                raise DataSourceError(f"Timed out reading {display_url}") from exc
            raise _transport_error(exc, display_url) from exc
        if deadline.expired:
            raise DataSourceError(f"Timed out reading {display_url}")
        return body


def get_json(
    url: str,
    timeout_seconds: float = 10.0,
    *,
    headers: Mapping[str, str] | None = None,
    error_label: str | None = None,
    deadline_seconds: float | None = None,
) -> object:
    request_headers = {"User-Agent": USER_AGENT}
    if headers is not None:
        request_headers.update(headers)
    display_url = error_label or url
    request = _checked_request(url, request_headers)
    body = _fetch(
        request,
        _HTTPOnlyRedirectHandler,
        timeout_seconds=timeout_seconds,
        deadline_seconds=deadline_seconds,
        limit=MAX_JSON_BYTES,
        display_url=display_url,
    )
    try:
        return cast(object, json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DataSourceError(f"Invalid JSON from {display_url}") from exc


def post_json(
    url: str,
    value: object,
    timeout_seconds: float = 10.0,
    *,
    headers: Mapping[str, str] | None = None,
    error_label: str | None = None,
    deadline_seconds: float | None = None,
) -> object:
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers is not None:
        request_headers.update(headers)
    display_url = error_label or url
    try:
        encoded = json.dumps(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("JSON request body was not serializable") from exc
    request = _checked_request(url, request_headers, data=encoded, method="POST")
    body = _fetch(
        request,
        _SameOriginRedirectHandler,
        timeout_seconds=timeout_seconds,
        deadline_seconds=deadline_seconds,
        limit=MAX_JSON_BYTES,
        display_url=display_url,
    )
    try:
        return cast(object, json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DataSourceError(f"Invalid JSON from {display_url}") from exc


def get_bytes(
    url: str, timeout_seconds: float = 30.0, *, deadline_seconds: float | None = None
) -> bytes:
    request = _checked_request(url, {"User-Agent": USER_AGENT})
    return _fetch(
        request,
        _HTTPOnlyRedirectHandler,
        timeout_seconds=timeout_seconds,
        deadline_seconds=deadline_seconds,
        limit=MAX_ASSET_BYTES,
        display_url=url,
    )


def _fsync_directory(directory: Path) -> None:
    # Rename durability needs the parent directory synced; skip filesystems
    # that cannot fsync a directory handle rather than failing the write.
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_bytes_atomic(path: Path, content: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json_atomic(path: Path, value: object, *, mode: int | None = None) -> None:
    write_bytes_atomic(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=mode,
    )
