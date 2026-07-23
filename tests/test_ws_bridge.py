# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Tests for the WebSocket-to-pipe bridge and its disconnect reporting."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from surveillance.services import ws_bridge
from surveillance.services.ws_bridge import WebSocketBridge


class _FakeWS:
    """Stand-in for a websockets client connection."""

    def __init__(self, messages: list[bytes], hang: bool = False) -> None:
        self._messages = list(messages)
        self._hang = hang

    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def __aiter__(self) -> _FakeWS:
        return self

    async def __anext__(self) -> bytes:
        if self._messages:
            return self._messages.pop(0)
        if self._hang:
            await asyncio.sleep(3600)
        raise StopAsyncIteration


def _frame(header: bytes, payload: bytes) -> bytes:
    return len(header).to_bytes(4, "big") + header + payload


@pytest.fixture
def connect(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace the real WebSocket connect with a configurable fake."""

    def _install(result: Any) -> None:
        """*result* is a single value reused for every call, or a list
        consumed one per call (the last entry is reused once exhausted) —
        used to simulate a sequence of reconnect attempts with different
        outcomes."""
        sequence = list(result) if isinstance(result, list) else None

        def _fake(url: str, **kwargs: Any) -> Any:
            value = (
                sequence.pop(0)
                if sequence and len(sequence) > 1
                else (sequence[0] if sequence else result)
            )
            if isinstance(value, BaseException):
                raise value
            return value

        monkeypatch.setattr(ws_bridge, "_ws_connect", _fake)

    return _install


class TestWaitClosed:
    async def test_reports_connection_failure(self, connect: Any) -> None:
        connect(ConnectionRefusedError("connection refused"))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        reason = await bridge.wait_closed()
        assert "ConnectionRefusedError" in reason
        await bridge.stop()

    async def test_gives_up_after_repeated_clean_closes(self, connect: Any) -> None:
        """A single clean close (e.g. code 1005) is absorbed and reconnected
        internally — that's the NAS's normal ~15-25s session rotation, not a
        failure. But a run of closes that never last long enough to look
        like a real connection must still eventually surface, rather than
        retrying forever in a tight loop."""
        connect(_FakeWS([_frame(b"mediaType=1", b"frame")]))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        reason = await bridge.wait_closed()
        assert reason
        await bridge.stop()

    async def test_absorbs_a_single_clean_close_and_reconnects(self, connect: Any) -> None:
        """One clean close must not surface as a drop: the bridge should
        reconnect on the same pipe and keep running silently, exactly like
        the NAS's routine WebSocket session rotation."""
        connect([_FakeWS([_frame(b"mediaType=1", b"frame")]), _FakeWS([], hang=True)])
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        await asyncio.sleep(0.5)  # let it clean-close once and reconnect into the hanging fake
        assert bridge._pump_task is not None
        assert not bridge._pump_task.done()
        await bridge.stop()
        assert await bridge.wait_closed() == ""

    async def test_silent_when_we_stop_it(self, connect: Any) -> None:
        connect(_FakeWS([], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        await bridge.stop()
        assert await bridge.wait_closed() == ""

    async def test_silent_when_write_end_closed(self, connect: Any) -> None:
        """A slot tears a stream down by closing the write end first.

        The pump then ends on its own, which must not be reported as the
        NAS dropping the session.
        """
        connect(_FakeWS([]))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        bridge.close_write_end()
        assert await bridge.wait_closed() == ""
        await bridge.stop()


class TestUptime:
    async def test_zero_when_never_connected(self, connect: Any) -> None:
        connect(ConnectionRefusedError("nope"))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        await bridge.wait_closed()
        assert bridge.uptime == 0.0
        await bridge.stop()

    async def test_positive_once_connected(self, connect: Any) -> None:
        connect(_FakeWS([]))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        await bridge.wait_closed()
        assert bridge.uptime > 0.0
        await bridge.stop()


class TestPipeLifetime:
    async def test_descriptors_are_recycled(self, connect: Any) -> None:
        """Why playback must stop before a bridge closes its pipe.

        The next bridge gets the same descriptor numbers back, so an mpv
        demuxer still holding the old ones would read the new stream.
        """
        connect(_FakeWS([], hang=True))
        first = WebSocketBridge("wss://nas/stream", False, "sid")
        url = await first.start()
        fd = int(url.removeprefix("fd://"))
        await first.stop()

        second = WebSocketBridge("wss://nas/stream", False, "sid")
        assert await second.start() == f"fd://{fd}"
        await second.stop()

    async def test_stop_closes_both_ends(self, connect: Any) -> None:
        connect(_FakeWS([], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        url = await bridge.start()
        fd = int(url.removeprefix("fd://"))
        await bridge.stop()
        with pytest.raises(OSError):
            os.fstat(fd)
