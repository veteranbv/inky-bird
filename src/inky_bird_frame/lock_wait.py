"""Wait for a file lock in a way that a stop request ends.

A blocking ``flock`` call is restarted after a signal handler returns (PEP 475).
A process whose stop handler only records the request, such as the scheduler or
the web server, would keep waiting for the lock until its service manager killed
it. A thread other than the main one never sees the signal at all. These waits
poll instead, and give up without the lock once a stop is requested.
"""

from __future__ import annotations

import fcntl
import time

from .errors import InkyBirdFrameError

FIRST_POLL_SECONDS = 0.005
LAST_POLL_SECONDS = 0.1

# Set from a signal handler, read by any thread; a plain value, because a
# handler must not take a lock the thread it interrupted may hold.
_stop_signal: int | None = None


class StopRequestedError(InkyBirdFrameError):
    """A stop signal arrived while waiting for a lock; the lock was not taken."""

    def __init__(self, signum: int) -> None:
        super().__init__("Inky is stopping")
        self.signum = signum


def request_stop(signum: int) -> None:
    """Record a stop signal: from now on, a wait for a lock held elsewhere ends
    with ``StopRequestedError``; a free lock is still taken.

    Safe to call from a signal handler.
    """
    global _stop_signal
    if _stop_signal is None:
        _stop_signal = signum


def stop_signal() -> int | None:
    """The stop signal recorded so far, if any."""
    return _stop_signal


def flock(descriptor: int, operation: int, *, deadline: float | None = None) -> bool:
    """Take ``operation`` (``LOCK_EX`` or ``LOCK_SH``) on ``descriptor``.

    Returns True once the lock is held, or False when ``deadline`` (a
    ``time.monotonic`` value) passes first. Raises ``StopRequestedError``, without
    the lock, when the lock is held elsewhere and a stop has been requested.
    """
    delay = FIRST_POLL_SECONDS
    while True:
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            return True
        # A free lock is still taken after a stop, so a stopping process can
        # record how its work ended; only waiting for a holder ends.
        if _stop_signal is not None:
            raise StopRequestedError(_stop_signal)
        pause = delay
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            pause = min(pause, remaining)
        time.sleep(pause)
        delay = min(delay * 2, LAST_POLL_SECONDS)
