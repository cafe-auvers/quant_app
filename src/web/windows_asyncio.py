"""Windows asyncio bootstrap used only by the web process.

Some Windows hosts intermittently raise WinError 10014 while asyncio creates
its loopback socket pair.  Retrying that one bootstrap operation keeps a web
server start (and the long regression suite) from failing for an unrelated OS
socket transient.  This module has no desktop, broker, or execution imports.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import time
from typing import Optional


_START_ATTEMPTS = 8
_RETRY_SECONDS = 0.25


class _ResilientWindowsSelectorEventLoop(asyncio.SelectorEventLoop):
    """Selector loop whose private self-pipe tolerates transient startup."""

    def __init__(self, *args, **kwargs) -> None:
        self._ssock = None
        self._csock = None
        super().__init__(*args, **kwargs)

    def _make_self_pipe(self) -> None:
        internal_fds_before = int(getattr(self, "_internal_fds", 0))
        last_error: Optional[OSError] = None
        for attempt in range(1, _START_ATTEMPTS + 1):
            try:
                self._ssock, self._csock = socket.socketpair()
                self._ssock.setblocking(False)
                self._csock.setblocking(False)
                self._internal_fds += 1
                self._add_reader(self._ssock.fileno(), self._read_from_self)
                return
            except OSError as exc:
                last_error = exc
                ssock = getattr(self, "_ssock", None)
                if ssock is not None:
                    with contextlib.suppress(Exception):
                        self._remove_reader(ssock.fileno())
                for attribute in ("_ssock", "_csock"):
                    candidate = getattr(self, attribute, None)
                    if candidate is not None:
                        with contextlib.suppress(OSError):
                            candidate.close()
                    setattr(self, attribute, None)
                self._internal_fds = internal_fds_before
                if attempt < _START_ATTEMPTS:
                    time.sleep(_RETRY_SECONDS * attempt)
        assert last_error is not None
        selector = getattr(self, "_selector", None)
        if selector is not None:
            selector.close()
            self._selector = None
        self._closed = True
        raise last_error

    def _close_self_pipe(self) -> None:
        ssock = getattr(self, "_ssock", None)
        csock = getattr(self, "_csock", None)
        if ssock is not None and csock is not None:
            super()._close_self_pipe()
            return
        for candidate in (ssock, csock):
            if candidate is not None:
                with contextlib.suppress(OSError):
                    candidate.close()
        self._ssock = None
        self._csock = None


class _ResilientWindowsEventLoopPolicy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self) -> asyncio.AbstractEventLoop:
        return _ResilientWindowsSelectorEventLoop()


def install_web_event_loop_policy() -> asyncio.AbstractEventLoopPolicy | None:
    """Install the retry-safe policy for this web process on Windows."""

    if sys.platform != "win32":
        return None
    previous = asyncio.get_event_loop_policy()
    if not isinstance(previous, _ResilientWindowsEventLoopPolicy):
        asyncio.set_event_loop_policy(_ResilientWindowsEventLoopPolicy())
    return previous
