from __future__ import annotations

import asyncio

from src.web import windows_asyncio


def test_web_event_loop_recovers_from_transient_windows_socketpair(monkeypatch):
    real_socketpair = windows_asyncio.socket.socketpair
    attempts = []

    def flaky_socketpair(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError(10014, "temporary socket-pair startup failure")
        return real_socketpair(*args, **kwargs)

    monkeypatch.setattr(windows_asyncio.socket, "socketpair", flaky_socketpair)
    monkeypatch.setattr(windows_asyncio, "_RETRY_SECONDS", 0)

    loop = windows_asyncio._ResilientWindowsSelectorEventLoop()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
        assert len(attempts) >= 2
    finally:
        loop.close()
