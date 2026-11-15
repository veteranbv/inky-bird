"""The suite-wide network guard in conftest.py: loopback only, in and out of process."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

# TEST-NET-1 (RFC 5737): documentation-only, never a real host.
OUTSIDE = ("192.0.2.1", 9)


def _refused(action: Callable[[], object], fragment: str) -> None:
    with pytest.raises(RuntimeError, match=fragment) as caught:
        action()
    assert type(caught.value).__name__ == "OutboundNetworkError"


def test_connect_ex_to_an_outside_address_is_refused() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.2)
        _refused(lambda: client.connect_ex(OUTSIDE), "outbound network connection")


def test_connect_to_an_outside_address_is_refused() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.2)
        _refused(lambda: client.connect(OUTSIDE), "outbound network connection")


def test_udp_datagrams_to_an_outside_address_are_refused() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        _refused(lambda: client.sendto(b"x", OUTSIDE), "outbound network connection")
        _refused(lambda: client.sendto(b"x", 0, OUTSIDE), "outbound network connection")
        _refused(lambda: client.sendmsg([b"x"], [], 0, OUTSIDE), "outbound network connection")


def test_name_lookups_are_refused() -> None:
    _refused(lambda: socket.getaddrinfo("example.com", 443), "DNS lookup")
    _refused(lambda: socket.gethostbyname("example.com"), "DNS lookup")
    _refused(lambda: socket.gethostbyname_ex("example.com"), "DNS lookup")
    _refused(lambda: socket.gethostbyaddr("192.0.2.1"), "reverse DNS lookup")
    _refused(lambda: socket.create_connection(("example.com", 443), timeout=0.2), "DNS lookup")


def test_loopback_still_works() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(2)
        port = server.getsockname()[1]
        # Accept each connection so the one-slot backlog is free for the next.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            assert client.connect_ex(("127.0.0.1", port)) == 0
            server.accept()[0].close()
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            server.accept()[0].close()
    assert socket.getaddrinfo("localhost", 0)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            assert sender.sendto(b"x", receiver.getsockname()) == 1
        assert receiver.recv(1) == b"x"
    assert socket.getaddrinfo(None, 0, socket.AF_INET, socket.SOCK_STREAM)


def test_this_machine_name_needs_no_reverse_lookup() -> None:
    # smtplib and email.utils.make_msgid call getfqdn(), which falls back to the
    # plain host name when the lookup fails with an OSError.
    assert socket.getfqdn() == socket.gethostname()
    _refused(lambda: socket.gethostbyaddr("192.0.2.1"), "reverse DNS lookup")


def test_child_git_may_use_only_local_repositories() -> None:
    result = subprocess.run(
        ["git", "ls-remote", "https://example.test/fixture.git"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "transport 'https' not allowed" in result.stderr


def test_child_http_clients_reach_only_a_closed_loopback_proxy() -> None:
    # A direct attempt would fail on DNS; through the closed proxy it is refused.
    script = (
        "import urllib.error, urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('http://example.test/', timeout=5)\n"
        "except urllib.error.URLError as error:\n"
        "    print(type(error.reason).__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == "ConnectionRefusedError"


def test_in_process_requests_through_the_closed_proxy_fail_loudly() -> None:
    # The proxy variables reach this process too; a product fetch must not turn
    # into an ordinary "connection refused" source failure.
    from inky_bird_frame.http import get_json

    _refused(lambda: get_json("https://example.test/x", 2), "outbound network connection")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.2)
        _refused(lambda: client.connect_ex(("127.0.0.1", 9)), "outbound network connection")


def test_free_space_checks_do_not_depend_on_the_host_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gib = 1024**3
    real = shutil.disk_usage(tmp_path)
    nearly_full = type(real)(total=10 * gib, used=10 * gib - 1, free=1)
    guard = sys.modules["conftest"]
    monkeypatch.setattr(guard, "_original_disk_usage", lambda path: nearly_full)
    reported = shutil.disk_usage(tmp_path)
    assert reported.free >= 64 * gib
    assert reported.used == nearly_full.used
    assert reported.total >= reported.used + reported.free
