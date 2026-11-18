"""Shared test hardening: no inherited git configuration, no outbound network,
no dependence on the host's free disk space.

In this process, TCP connections, UDP datagrams and name lookups may reach only
loopback. Child processes cannot inherit a Python patch, so they get an
environment that refuses network transports instead: git may use only local
repositories, and proxy-aware tools (curl, gh, Python HTTP clients, and Node's
built-in fetch from Node 24, through NODE_USE_ENV_PROXY) are sent to a closed
loopback proxy for everything except loopback. Tools that ignore proxy
settings, such as ssh, nc or dig, are not contained.

Free-space checks see at least 64 GiB free, so a nearly full developer disk
cannot fail them; tests of the low-space branch patch disk_usage themselves.
"""

from __future__ import annotations

import os
import shutil
import socket
from collections.abc import Iterator
from ipaddress import ip_address
from typing import Any

import pytest

from inky_bird_frame import lock_wait

# Keep tests hermetic against the developer's git configuration; global
# url.<base>.insteadOf rewrites otherwise change `git remote get-url` output
# inside publisher tests. Subprocesses inherit this environment.
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_SYSTEM"] = os.devnull
os.environ["GIT_ALLOW_PROTOCOL"] = "file"
# Port 9 (discard) on loopback: nothing listens, so a proxied request fails fast.
# In this process the guard refuses it outright, so a proxied fetch raises
# OutboundNetworkError instead of an ordinary "connection refused".
_CLOSED_PROXY_PORT = 9
_CLOSED_PROXY = f"http://127.0.0.1:{_CLOSED_PROXY_PORT}"
for _name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ[_name] = os.environ[_name.lower()] = _CLOSED_PROXY
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1,::1"
os.environ["NODE_USE_ENV_PROXY"] = "1"


class OutboundNetworkError(RuntimeError):
    """A test tried to reach something other than this machine's loopback."""


def _host_text(host: object) -> str | None:
    if isinstance(host, bytes):
        return host.decode("ascii", "replace")
    return host if isinstance(host, str) else None


def _is_ip(text: str) -> bool:
    try:
        ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    return True


def _loopback(host: object) -> bool:
    text = _host_text(host)
    if text is None:
        return False
    if text.lower() == "localhost":
        return True
    return _is_ip(text) and ip_address(text.split("%", 1)[0]).is_loopback


def _check_address(address: Any) -> None:
    # Unix-domain addresses are paths (str/bytes), not (host, port) tuples.
    if not isinstance(address, tuple) or not address:
        return
    if not _loopback(address[0]):
        raise OutboundNetworkError(
            f"Test attempted an outbound network connection to {address[0]!r}"
        )
    if len(address) > 1 and address[1] == _CLOSED_PROXY_PORT:
        raise OutboundNetworkError(
            "Test attempted an outbound network connection through the closed test proxy"
        )


def _check_lookup(host: object) -> None:
    # None and "" mean this machine (bind addresses). A literal IP needs no
    # resolver; connecting to it is checked separately.
    text = _host_text(host)
    if host is None or text == "" or _loopback(host) or (text is not None and _is_ip(text)):
        return
    raise OutboundNetworkError(f"Test attempted a DNS lookup of {host!r}")


_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_sendto = socket.socket.sendto
_original_sendmsg = socket.socket.sendmsg
_original_getaddrinfo = socket.getaddrinfo
_original_gethostbyname = socket.gethostbyname
_original_gethostbyname_ex = socket.gethostbyname_ex
_original_gethostbyaddr = socket.gethostbyaddr


def _guarded_connect(self: socket.socket, address: Any) -> None:
    _check_address(address)
    _original_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    _check_address(address)
    return _original_connect_ex(self, address)


def _guarded_sendto(self: socket.socket, data: Any, *args: Any) -> int:
    # sendto(data, address) or sendto(data, flags, address).
    if args:
        _check_address(args[-1])
    return _original_sendto(self, data, *args)


def _guarded_sendmsg(self: socket.socket, buffers: Any, *args: Any) -> int:
    # sendmsg(buffers[, ancdata[, flags[, address]]]).
    if len(args) >= 3:
        _check_address(args[2])
    return _original_sendmsg(self, buffers, *args)


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    _check_lookup(host)
    return _original_getaddrinfo(host, *args, **kwargs)


def _guarded_gethostbyname(host: str) -> str:
    _check_lookup(host)
    return _original_gethostbyname(host)


def _guarded_gethostbyname_ex(host: str) -> tuple[str, list[str], list[str]]:
    _check_lookup(host)
    return _original_gethostbyname_ex(host)


def _guarded_gethostbyaddr(host: str) -> tuple[str, list[str], list[str]]:
    # This machine's own name answers as an offline machine would, so getfqdn()
    # (smtplib, email.utils.make_msgid) falls back to it without a lookup.
    if host == socket.gethostname():
        raise socket.herror(1, "Unknown host")
    if not _loopback(host):
        raise OutboundNetworkError(f"Test attempted a reverse DNS lookup of {host!r}")
    return _original_gethostbyaddr(host)


_AMPLE_FREE_BYTES = 64 * 1024**3
_original_disk_usage = shutil.disk_usage


def _ample_disk_usage(path: Any) -> Any:
    real = _original_disk_usage(path)
    free = max(real.free, _AMPLE_FREE_BYTES)
    return type(real)(total=max(real.total, real.used + free), used=real.used, free=free)


# The socket stubs declare overloads a plain wrapper cannot restate; each
# replacement accepts every form its original does and delegates unchanged.
socket.socket.connect = _guarded_connect  # type: ignore[method-assign, assignment]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign, assignment]
socket.socket.sendto = _guarded_sendto  # type: ignore[method-assign, assignment]
socket.socket.sendmsg = _guarded_sendmsg  # type: ignore[method-assign, assignment]
socket.getaddrinfo = _guarded_getaddrinfo
socket.gethostbyname = _guarded_gethostbyname
socket.gethostbyname_ex = _guarded_gethostbyname_ex
socket.gethostbyaddr = _guarded_gethostbyaddr
shutil.disk_usage = _ample_disk_usage


@pytest.fixture(autouse=True)
def _no_stop_request_carries_over() -> Iterator[None]:
    """A test that stops the scheduler or web server in this process records a
    stop for the whole process, as a real stop would; the next test starts as a
    process that was never asked to stop."""
    yield
    lock_wait._stop_signal = None
