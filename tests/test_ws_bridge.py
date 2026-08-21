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

"""Tests for the WebSocket-to-pipe bridge and its disconnect reporting.

start() now waits for the first codec-info frame (it has to know the
video/audio codecs before deciding whether to mux audio in via ffmpeg —
see ws_bridge.py's _setup_pipes) rather than returning instantly the way
it used to when it only ever piped raw video through. A fake connection
that never sends codec info and never gives up would make start() hang
forever, so every fake WS sequence below that's meant to succeed sends a
video-only codec-info frame first (avoiding ffmpeg/subprocess mocking
entirely for these unit tests); ones testing pure connection failure
expect start() to raise instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
import types
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest

# The bridge imports websockets.exceptions.ConnectionClosedOK lazily inside
# _pump; provide a stub so these tests run without the websockets package
# installed (the connect() itself is always monkeypatched below).
try:
    import websockets.exceptions  # noqa: F401
except ModuleNotFoundError:
    _ws = sys.modules.setdefault("websockets", types.ModuleType("websockets"))
    _exc = types.ModuleType("websockets.exceptions")

    class ConnectionClosedOK(Exception):
        def __init__(self, rcvd: object = None, sent: object = None) -> None:
            super().__init__("connection closed ok")

    _exc.ConnectionClosedOK = ConnectionClosedOK  # type: ignore[attr-defined]
    _ws.exceptions = _exc  # type: ignore[attr-defined]
    sys.modules["websockets.exceptions"] = _exc

from surveillance.services import ws_bridge
from surveillance.services.aac import adts_header
from surveillance.services.ws_bridge import WebSocketBridge

# Captured before any test can monkeypatch asyncio.create_subprocess_exec --
# _spawn_fake_mux_holder needs the real one regardless of what a given
# test has patched the module attribute to (patching it and then calling
# it *by name* from within the same test would just recurse into the
# patched version instead of ever spawning anything).
_REAL_SUBPROCESS_EXEC = asyncio.create_subprocess_exec


class _FakeWS:
    """Stand-in for a websockets client connection.

    _pump() calls recv() directly (not the async-iterator protocol), so this
    only needs to implement that.
    """

    def __init__(self, messages: list[bytes], hang: bool = False) -> None:
        self._messages = list(messages)
        self._hang = hang
        self.sent: list[Any] = []
        self.closed = False

    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def send(self, message: Any) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        """Like the real connection: once closed, a pending or subsequent
        recv() raises instead of waiting for data that will never come."""
        self.closed = True

    async def recv(self) -> bytes:
        from websockets.exceptions import ConnectionClosedOK

        if self.closed:
            raise ConnectionClosedOK(None, None)
        if self._messages:
            return self._messages.pop(0)
        while self._hang:
            await asyncio.sleep(0.01)
            if self.closed:
                raise ConnectionClosedOK(None, None)
        raise ConnectionClosedOK(None, None)


def _frame(header: bytes, payload: bytes) -> bytes:
    return len(header).to_bytes(4, "big") + header + payload


def _codec_frame(video: str = "H264") -> bytes:
    """A video-only codec-info frame -- enough for _setup_pipes to fall
    back to the plain raw-video pipe (no audio codec means no ffmpeg
    muxing, so these tests don't need to mock a subprocess at all)."""
    return _frame(f"vdoCodec={video}".encode(), b"")


async def _start_expecting_failure(bridge: WebSocketBridge) -> None:
    """start() now raises if the bridge gives up before ever becoming
    ready (see module docstring) -- the fakes in these tests never send
    codec info, so that's exactly what happens here."""
    with pytest.raises(RuntimeError):
        await bridge.start()


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
        await _start_expecting_failure(bridge)
        reason = await bridge.wait_closed()
        assert "ConnectionRefusedError" in reason
        await bridge.stop()

    async def test_reports_open_handshake_timeout(self, connect: Any) -> None:
        """The open_timeout TimeoutError must keep its own reason, not be
        mistaken for the idle-stall TimeoutError and reported as empty."""
        connect(TimeoutError("timed out during opening handshake"))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await _start_expecting_failure(bridge)
        reason = await bridge.wait_closed()
        assert "handshake" in reason
        assert "stalled" not in reason
        await bridge.stop()

    async def test_gives_up_after_repeated_clean_closes(self, connect: Any) -> None:
        """A single clean close (e.g. code 1005) is absorbed and reconnected
        internally, since the keepalive makes one rare enough to be a network
        hiccup rather than a failure. But a run of closes that never last long
        enough to look like a real connection must still eventually surface,
        rather than retrying forever in a tight loop."""
        connect(_FakeWS([_codec_frame(), _frame(b"mediaType=1", b"frame")]))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()  # codec info arrives on the very first connect
        reason = await bridge.wait_closed()
        assert reason
        await bridge.stop()

    async def test_absorbs_a_single_clean_close_and_reconnects(self, connect: Any) -> None:
        """One clean close must not surface as a drop: the bridge should
        reconnect on the same pipe and keep running silently, exactly like
        the NAS's routine WebSocket session rotation."""
        connect(
            [
                _FakeWS([_codec_frame(), _frame(b"mediaType=1", b"frame")]),
                _FakeWS([], hang=True),
            ]
        )
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        await asyncio.sleep(0.5)  # let it clean-close once and reconnect into the hanging fake
        assert bridge._pump_task is not None
        assert not bridge._pump_task.done()
        await bridge.stop()
        assert await bridge.wait_closed() == ""

    async def test_reconnects_on_a_silent_stall(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No exception, no close frame — just no data at all — must still be
        detected and reconnected, not left hanging forever. This is distinct
        from ping_timeout (disabled) since it's an application-level read
        timeout, not a protocol ping."""
        monkeypatch.setattr(ws_bridge, "_IDLE_TIMEOUT", 0.1)
        connect(_FakeWS([], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        # Never sends codec info, so every attempt stalls out after 0.1s —
        # eventually gives up (same _fast_failures path as a real repeated
        # connection failure), and start() raises once it does.
        await _start_expecting_failure(bridge)
        reason = await bridge.wait_closed()
        assert "stalled" in reason
        await bridge.stop()

    async def test_silent_when_we_stop_it(self, connect: Any) -> None:
        connect(_FakeWS([_codec_frame()], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        await bridge.stop()
        assert await bridge.wait_closed() == ""

    async def test_silent_when_write_end_closed(self, connect: Any) -> None:
        """A slot tears a stream down by closing the write end first.

        The pump then ends on its own, which must not be reported as the
        NAS dropping the session.
        """
        connect(_FakeWS([_codec_frame()], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        bridge.close_write_end()
        assert await bridge.wait_closed() == ""
        await bridge.stop()


class TestKeepalive:
    async def test_sends_keepalive_on_the_configured_cadence(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bridge must send a keepalive on _KEEPALIVE_INTERVAL to
        keep the connection from being dropped for client silence."""
        monkeypatch.setattr(ws_bridge, "_KEEPALIVE_INTERVAL", 0.05)
        ws = _FakeWS([_codec_frame()], hang=True)
        connect(ws)
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        await asyncio.sleep(0.23)
        await bridge.stop()
        assert ws.sent.count("keepAlive") >= 3

    async def test_keepalive_failure_triggers_a_reconnect(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed keepalive send must surface as a normal
        drop/reconnect rather than silently killing the pump task, even
        though the read side has no way to notice on its own."""
        monkeypatch.setattr(ws_bridge, "_KEEPALIVE_INTERVAL", 0.05)

        class _DeadSendWS(_FakeWS):
            async def send(self, message: Any) -> None:
                raise ConnectionResetError("connection reset")

        connect(
            [
                _DeadSendWS([_codec_frame()], hang=True),
                _FakeWS([], hang=True),
            ]
        )
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        await asyncio.sleep(0.3)
        assert bridge._pump_task is not None
        assert not bridge._pump_task.done()
        await bridge.stop()

    async def test_keepalive_failure_never_abandons_an_in_flight_write(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed keepalive must close the connection and let the read
        loop unwind between writes, never cancel it mid-write.

        Cancelling an asyncio.to_thread does not stop the worker thread,
        so an abandoned write would carry on into the same pipe the
        reconnected session is already writing to, interleaving two
        frames into one stream. Only one write may ever be in flight.
        """
        monkeypatch.setattr(ws_bridge, "_KEEPALIVE_INTERVAL", 0.05)
        lock = threading.Lock()
        live = peak = writes = 0

        def _slow_write(self: Any, audio: bool, data: bytes) -> None:
            nonlocal live, peak, writes
            with lock:
                live += 1
                writes += 1
                peak = max(peak, live)
            time.sleep(0.4)
            with lock:
                live -= 1

        monkeypatch.setattr(WebSocketBridge, "_write_pipe", _slow_write)

        class _DeadSendWS(_FakeWS):
            async def send(self, message: Any) -> None:
                raise ConnectionResetError("connection reset")

        def _session(cls: type[_FakeWS]) -> _FakeWS:
            return cls([_codec_frame(), _video_frame()], hang=True)

        connect([_session(_DeadSendWS), _session(_FakeWS)])
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        await asyncio.sleep(1.4)
        await bridge.stop()
        # The second write proves the reconnect really happened, so peak
        # is not 1 merely because nothing followed the abandoned write.
        assert writes >= 2
        assert peak == 1


class TestUptime:
    async def test_zero_when_never_connected(self, connect: Any) -> None:
        connect(ConnectionRefusedError("nope"))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await _start_expecting_failure(bridge)
        await bridge.wait_closed()
        assert bridge.uptime == 0.0
        await bridge.stop()

    async def test_positive_once_connected(self, connect: Any) -> None:
        # start() only resolves once codec info arrives, which happens
        # strictly after _connected_at is set on entering the connected
        # async-with block -- no need to wait for the connection to end
        # (it never does here, hang=True) just to check uptime.
        connect(_FakeWS([_codec_frame()], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.uptime > 0.0
        await bridge.stop()


class TestWriteStall:
    async def test_stalled_pipe_write_gives_up_instead_of_reconnecting(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A downstream reader that stops draining the pipe must make the
        bridge give up outright, not endlessly reconnect the WebSocket and
        refeed data into the same stuck pipe."""
        monkeypatch.setattr(ws_bridge, "_WRITE_TIMEOUT", 0.05)
        # Pin the pipe small rather than sizing the frame against whatever
        # the buffer happens to be: nothing ever reads the other end here,
        # so the write only blocks while the frame cannot fit, and a test
        # written around the 64KiB default silently stops testing anything
        # the moment the pipe is grown.
        monkeypatch.setattr(ws_bridge, "_PIPE_CAPACITY", 4096)
        big_frame = _frame(b"mediaType=1", b"\xaa" * (200 * 1024))
        connect(_FakeWS([_codec_frame(), big_frame], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        reason = await bridge.wait_closed()
        # The exact fault, not just "stalled": the idle timeout reports a
        # stall too, and would pass this assertion having tested nothing
        # about pipe writes at all.
        assert "video pipe write stalled" in reason, reason
        await bridge.stop()


class TestMuxSetupCleanup:
    """Building the mux hands six pipe fds around before any of them is
    stored on the bridge, so whatever goes wrong in between has to close
    them itself."""

    async def test_a_teardown_during_ffmpeg_startup_leaks_no_fds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # stop() cancels the pump task, and _start_muxed runs on it, so a
        # camera switch or page exit within the start grace lands a
        # CancelledError inside _spawn_ffmpeg. That is not an OSError.
        async def _fake_exec(*args: Any, **kwargs: Any) -> Any:
            return _FakeFfmpegProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        monkeypatch.setattr(ws_bridge, "_FFMPEG_START_GRACE", 5.0)
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        before = set(os.listdir("/proc/self/fd"))

        task = asyncio.create_task(bridge._start_muxed("H264", "PCMU"))
        await asyncio.sleep(0.05)  # inside the start grace
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        leaked = sorted(set(os.listdir("/proc/self/fd")) - before)
        assert not leaked, f"leaked fds {leaked}"
        assert bridge._read_fd == -1
        assert bridge._video_write_fd == -1
        assert bridge._audio_write_fd == -1


class TestAudioGapWatchdog:
    """A muxed camera that stops sending audio must lose its audio, not
    its whole stream: ffmpeg holds the video input while any input has
    nothing to deliver."""

    async def test_a_silent_camera_loses_only_its_audio_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ws_bridge, "_AUDIO_GAP_TIMEOUT", 0.05)
        monkeypatch.setattr(ws_bridge, "_AUDIO_GAP_CHECK_INTERVAL", 0.01)
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        audio_r, audio_w = os.pipe()
        video_r, video_w = os.pipe()
        bridge._audio_write_fd = audio_w
        bridge._video_write_fd = video_w
        bridge._last_audio_at = time.monotonic()

        async def keep_video_arriving() -> None:
            # Only a camera still sending video can be wedging the mux.
            while True:
                await asyncio.sleep(0.005)
                bridge._last_video_at = time.monotonic()

        feeder = asyncio.create_task(keep_video_arriving())
        watch = asyncio.create_task(bridge._watch_audio_gap())
        await asyncio.wait_for(watch, timeout=2.0)
        feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feeder

        assert bridge._audio_write_fd == -1, "the audio write end should be closed"
        assert bridge._video_write_fd == video_w, "the video pipe must be left alone"
        # Non-blocking, so a regression that swaps the descriptor to -1
        # without closing it fails here instead of hanging the suite on a
        # pipe whose write end is still open.
        os.set_blocking(audio_r, False)
        assert os.read(audio_r, 1) == b"", "ffmpeg should see EOF on the audio input"
        for fd in (audio_r, video_r, video_w):
            os.close(fd)

    async def test_audio_that_keeps_arriving_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ws_bridge, "_AUDIO_GAP_TIMEOUT", 0.2)
        monkeypatch.setattr(ws_bridge, "_AUDIO_GAP_CHECK_INTERVAL", 0.01)
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        audio_r, audio_w = os.pipe()
        bridge._audio_write_fd = audio_w
        bridge._last_audio_at = time.monotonic()

        watch = asyncio.create_task(bridge._watch_audio_gap())
        # Both clocks: with only audio fed, the video term alone would
        # hold the watchdog off and the test would pass without ever
        # exercising the audio one.
        for _ in range(20):
            await asyncio.sleep(0.02)
            bridge._last_audio_at = bridge._last_video_at = time.monotonic()

        assert not watch.done(), "a camera still sending audio must keep it"
        assert bridge._audio_write_fd == audio_w
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch
        os.close(audio_r)
        os.close(audio_w)

    async def test_a_blocked_video_write_still_counts_as_video_arriving(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The wedge this whole watchdog exists for IS a video write that
        # started and cannot finish, which freezes _last_video_at for the
        # whole write timeout. Read literally, the video clock then says
        # "video stopped too" and the watchdog never fires, which is the
        # bug it was meant to fix.
        monkeypatch.setattr(ws_bridge, "_AUDIO_GAP_TIMEOUT", 0.05)
        monkeypatch.setattr(ws_bridge, "_AUDIO_GAP_CHECK_INTERVAL", 0.01)
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        audio_r, audio_w = os.pipe()
        bridge._audio_write_fd = audio_w
        stale = time.monotonic() - 10.0  # both clocks long stale
        bridge._last_audio_at = stale
        bridge._last_video_at = stale
        bridge._video_write_in_flight = True

        watch = asyncio.create_task(bridge._watch_audio_gap())
        await asyncio.wait_for(watch, timeout=2.0)

        assert bridge._audio_write_fd == -1
        os.set_blocking(audio_r, False)
        assert os.read(audio_r, 1) == b""
        os.close(audio_r)

    async def test_the_muxer_starts_the_gap_watchdog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_exec(*args: Any, **kwargs: Any) -> Any:
            return _FakeFfmpegProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge._start_muxed("H264", "PCMU")

        assert bridge._audio_gap_watch is not None, "nothing would watch for the wedge"
        assert not bridge._audio_gap_watch.done()
        await bridge.stop()
        assert bridge._audio_gap_watch is None, "stop() must not leave the task running"

    async def test_a_reconnect_gives_the_new_session_time_to_send_audio(self, connect: Any) -> None:
        # Nothing else resets the stamp per session, so after an outage
        # longer than the gap timeout the first video frame back would
        # make the reconnect itself look like a camera gone quiet.
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        stale = time.monotonic() - 30.0
        bridge._last_audio_at = stale
        # Two sessions: the first ends at once, the second holds open.
        connect([_FakeWS([]), _FakeWS([], hang=True)])

        pump = asyncio.create_task(bridge._pump())
        await asyncio.sleep(0.6)  # past the 0.25s reconnect delay
        refreshed = bridge._last_audio_at
        bridge._stopping = True
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump

        assert refreshed > stale, "the reconnect left the audio clock stale"
        assert bridge._connected_at is not None
        assert refreshed >= bridge._connected_at

    async def test_a_silent_session_keeps_its_audio_for_the_reconnect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A session the NAS drops silently delivers neither stream until
        # recv() times out after _IDLE_TIMEOUT and the pump reconnects.
        # Nothing is wedged while both are quiet, and the camera comes
        # back, so ending its audio here would mute it for no reason.
        monkeypatch.setattr(ws_bridge, "_AUDIO_GAP_TIMEOUT", 0.05)
        monkeypatch.setattr(ws_bridge, "_AUDIO_GAP_CHECK_INTERVAL", 0.01)
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        audio_r, audio_w = os.pipe()
        bridge._audio_write_fd = audio_w
        stopped_at = time.monotonic()
        bridge._last_audio_at = stopped_at
        bridge._last_video_at = stopped_at

        watch = asyncio.create_task(bridge._watch_audio_gap())
        await asyncio.sleep(0.3)  # six times the gap timeout

        assert not watch.done(), "both streams quiet is an idle mux, not a wedged one"
        assert bridge._audio_write_fd == audio_w
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch
        os.close(audio_r)
        os.close(audio_w)


class TestFfmpegExitReporting:
    """A muxer that exits reports itself only while it is the news."""

    async def test_a_finished_pump_keeps_its_own_give_up_reason(self) -> None:
        # Closing the write ends on the way out of the pump is what makes
        # ffmpeg exit, so the watcher runs on every muxed give-up. Left
        # unguarded it overwrites the reason with its own consequence,
        # and wait_closed reads _error after the pump has returned.
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        bridge._error = "video pipe write stalled for 5s"
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        done.set_result(None)
        bridge._pump_task = done  # type: ignore[assignment]
        proc = _ExitingFfmpegProc(after=0)
        bridge._ffmpeg_proc = proc  # type: ignore[assignment]

        await bridge._watch_ffmpeg(proc)  # type: ignore[arg-type]

        assert bridge._error == "video pipe write stalled for 5s"

    async def test_a_running_pump_is_still_told_ffmpeg_died(self) -> None:
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        running = asyncio.create_task(asyncio.sleep(30))
        bridge._pump_task = running
        proc = _ExitingFfmpegProc(after=0)
        bridge._ffmpeg_proc = proc  # type: ignore[assignment]

        await bridge._watch_ffmpeg(proc)  # type: ignore[arg-type]

        assert "ffmpeg exited" in bridge._error
        assert running.cancelled() or running.cancelling()
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running


class TestAttemptScoring:
    """A long attempt only counts as healthy if it actually delivered data."""

    def test_connected_but_silent_never_resets_the_streak(self) -> None:
        import time

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        # Longer than _FAST_FAILURE_THRESHOLD, which is what the idle
        # timeout produces: without the data check this scored as healthy
        # every pass and the bridge reconnected forever.
        long_ago = time.monotonic() - (ws_bridge._FAST_FAILURE_THRESHOLD + 5.0)
        outcomes = [bridge._note_attempt_outcome(True, long_ago) for _ in range(10)]
        assert any(outcomes), "a connected-but-silent stream must eventually give up"
        assert bridge._error

    def test_a_long_attempt_that_delivered_data_stays_healthy(self) -> None:
        import time

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        long_ago = time.monotonic() - (ws_bridge._FAST_FAILURE_THRESHOLD + 5.0)
        for _ in range(10):
            bridge._attempt_got_data = True
            assert bridge._note_attempt_outcome(True, long_ago) is False
        assert bridge._fast_failures == 0


class _FakeFfmpegProc:
    """Stand-in for asyncio.subprocess.Process -- avoids spawning a real
    ffmpeg in these unit tests, which only care about the mux/fallback
    decision in _setup_pipes, not ffmpeg's actual behavior.

    returncode is None because a spawned ffmpeg is still running: the
    bridge reads it to tell a live muxer from one that rejected its
    arguments and exited. wait() still returns at once, so a teardown
    doesn't have to sit out the drain timeout.

    stderr is None the way a real Process reports it when the caller did
    not ask for a pipe, which is every one of these tests: they run at
    default log level, and the bridge only pipes it on a debug run."""

    returncode: int | None = None
    stderr: Any = None

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        pass


class _DeadFfmpegProc(_FakeFfmpegProc):
    """An ffmpeg that started and exited immediately, the way one does
    when it doesn't understand the arguments it was given."""

    returncode: int | None = 1


class _ExitingFfmpegProc(_FakeFfmpegProc):
    """An ffmpeg that starts fine and dies later, the way a crash or an
    OOM kill mid-session looks."""

    def __init__(self, after: float = 0.05) -> None:
        self.returncode: int | None = None
        self._after = after

    async def wait(self) -> int:
        await asyncio.sleep(self._after)
        self.returncode = 9
        return 9


class _FakeValidationProc:
    """Stand-in for the throwaway ffmpeg process _aac_frames_look_valid
    spawns to sanity-check the AU-header/ADTS transform."""

    def __init__(self, stderr: bytes = b"") -> None:
        self._stderr = stderr

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return b"", self._stderr

    def kill(self) -> None:
        pass


def _aac_audio_frame(payload_len: int = 50, first_frame_byte: int = 0xAA) -> bytes:
    """A mediaType=2 frame with a 3-byte prefix, like the real camera
    that needed one: the third byte reads as AAC's "no elements follow"
    marker, so a 2-byte prefix is ruled out and 3 is what detection
    settles on. The payload itself is arbitrary, since these tests
    exercise the mux/fallback decision, not real AAC decoding -- except
    for *first_frame_byte*, whose top three bits are the id_syn_ele
    detect_channel_count reads the layout off (0xAA names none)."""
    return _frame(
        b"mediaType=2", b"\x00\x00\xe0" + bytes([first_frame_byte]) + b"\xaa" * (payload_len - 1)
    )


def _undetectable_prefix_frame(payload_len: int = 50, header_suffix: bytes = b"") -> bytes:
    """A mediaType=2 frame where every byte reads as AAC's "immediate
    end, zero elements" marker, so no prefix length in
    detect_frame_prefix_len's search range can ever pass, mirroring a
    camera using an entirely different framing scheme.

    *header_suffix* extends the header past the fields the bridge parses,
    for tests that care which end of it the frame's own bytes come from."""
    return _frame(b"mediaType=2" + header_suffix, b"\xe0" * payload_len)


def _video_frame() -> bytes:
    return _frame(b"mediaType=1", b"\x00" * 10)


def _aac_codec_frame_with_config(channels: int, sample_rate: int) -> bytes:
    """A codec-info frame whose payload carries the audio-config trailer
    a camera confirmed to send this puts after its video config -- see
    parse_audio_config in aac.py. The two bytes after the second "|"
    stand in for the AudioSpecificConfig: parse_audio_config never reads
    them, so they are fixed filler rather than tracking the arguments."""
    trailer = f"{channels}|{sample_rate}|".encode() + b"\x14\x08"
    header = f"vdoCodec=H264&adoCodec=MPEG4-GENERIC&adoExtra={len(trailer)}".encode()
    return _frame(header, b"\x00\x00\x00\x01\x67\x64\x00\x33" + trailer)


async def _spawn_fake_mux_holder(**kwargs: Any) -> Any:
    """Stand-in for the real ffmpeg mux spawn in tests that write
    buffered frames afterward (AAC detection's flush) -- a plain fake
    object leaves nothing holding the passed-through pipe read ends open
    once _start_muxed closes its own copies, so every write after that
    fails with EPIPE. A real, trivial process that inherits the same
    fds and sleeps keeps them open without needing anything
    ffmpeg-specific.

    The sleep has to outlast every wait a test does on work that happens
    after the spawn, or the holder dies first, _monitor_ffmpeg cancels
    the pump and the wait can never be satisfied. It is deliberately far
    longer than _wait_until's deadline rather than equal to it; nothing
    waits for it to finish, since stop() terminates it."""
    return await _REAL_SUBPROCESS_EXEC(
        "sleep",
        "30",
        pass_fds=kwargs.get("pass_fds", []),
        stdout=kwargs.get("stdout"),
        stdin=kwargs.get("stdin"),
        stderr=kwargs.get("stderr"),
    )


# AAC detection normally completes once 5 real inter-frame intervals are
# measured -- but a fake WS with no real delay between messages can see
# back-to-back time.monotonic() calls return an identical value (clock
# resolution, not a real race), so intervals never accumulate. Padding
# with enough video frames to hit _AAC_DETECTION_VIDEO_FRAME_CAP makes
# detection finish deterministically either way.
_AAC_DETECTION_PADDING = [_video_frame() for _ in range(ws_bridge._AAC_DETECTION_VIDEO_FRAME_CAP)]


async def _wait_until(done: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll until *done* or give up, so a test can wait on work the pump
    task does after start() returns without pinning a sleep to whatever
    pacing that work happens to use."""
    deadline = time.monotonic() + timeout
    while not done():
        assert time.monotonic() < deadline, "timed out waiting for the bridge"
        await asyncio.sleep(0.01)


class TestAudioMuxDecision:
    """_setup_pipes must only spawn ffmpeg for a codec combination it
    actually knows how to mux (currently H264/H265 video + PCMU audio) --
    anything else keeps the original raw-video-only passthrough, so a
    camera with an unsupported/no audio track sees no behavior change at
    all from before this feature existed."""

    async def test_no_ffmpeg_for_unsupported_audio_codec(self, connect: Any) -> None:
        connect(_FakeWS([_frame(b"vdoCodec=H264&adoCodec=AAC", b"")], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is False
        assert bridge._ffmpeg_proc is None
        await bridge.stop()

    async def test_no_ffmpeg_for_video_only_camera(self, connect: Any) -> None:
        connect(_FakeWS([_codec_frame()], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is False
        assert bridge._ffmpeg_proc is None
        await bridge.stop()

    async def test_mux_active_for_pcmu(self, connect: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeFfmpegProc:
            return _FakeFfmpegProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        connect(_FakeWS([_frame(b"vdoCodec=H265&adoCodec=PCMU", b"")], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is True
        assert bridge._ffmpeg_proc is not None
        await bridge.stop()

    async def test_ffmpeg_that_exits_at_once_falls_back_to_video_only(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ffmpeg that starts and immediately quits, the way one that
        rejects its arguments does, must drop the camera to video-only.

        The spawn itself succeeds, so nothing raises: without the check
        the bridge would report muxed audio and hand mpv a pipe fed by a
        process that is already gone.
        """

        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeFfmpegProc:
            return _DeadFfmpegProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        connect(_FakeWS([_frame(b"vdoCodec=H265&adoCodec=PCMU", b"")], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is False
        assert bridge._read_fd >= 0, "the raw-video pipe must still be there to play"
        await bridge.stop()

    async def test_ffmpeg_dying_mid_session_ends_the_bridge_with_a_reason(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A muxer that dies after the stream is running must end the
        bridge, and say why.

        mpv plays ffmpeg's output, so nothing else can recover the
        session. Left alone, the pump answers the resulting EPIPE by
        reconnecting onto the same dead pipes until its failure streak
        runs out, reporting a broken pipe rather than a dead muxer.
        """

        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeFfmpegProc:
            return _ExitingFfmpegProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        connect(_FakeWS([_frame(b"vdoCodec=H265&adoCodec=PCMU", b"")], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is True, "it must get as far as a running muxer"
        reason = await bridge.wait_closed()
        assert "ffmpeg" in reason, f"the reason must name the muxer, got {reason!r}"
        await bridge.stop()

    async def test_mux_active_for_valid_aac(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            if "null" in args:
                return _FakeValidationProc(stderr=b"")  # transform "decodes" cleanly
            return await _spawn_fake_mux_holder(**kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        frames = [_frame(b"vdoCodec=H264&adoCodec=MPEG4-GENERIC", b"")]
        frames += [_aac_audio_frame() for _ in range(6)]
        frames += _AAC_DETECTION_PADDING
        connect(_FakeWS(frames, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is True
        assert bridge._ffmpeg_proc is not None
        assert bridge._frame_prefix_len == 3
        assert bridge._aac_use_header_prepend is False
        await bridge.stop()

    async def test_mux_active_when_the_frame_is_split_across_the_header(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A camera whose payload doesn't validate under any prefix
        length (see _undetectable_prefix_frame), because the payload is
        missing its own leading bytes -- those are the last 4 bytes of
        the WS message's own header instead (see _reconstruct_aac_frame).
        Once the payload-only model is ruled out, the bridge must
        reconstruct frames from the header tail and mux with that instead
        of giving up.

        This is the artificial door into that fallback, not the one a
        real camera comes through -- see
        test_header_prepend_is_reached_when_a_detected_prefix_does_not_decode.

        The header here starts with "medi" and ends in the four bytes the
        frame is missing, so the bytes that reach the pipe also pin which
        end of the header the read loop takes them from."""
        audio_writes: list[bytes] = []
        real_write_pipe = WebSocketBridge._write_pipe

        def _recording_write_pipe(self: Any, audio: bool, data: bytes) -> None:
            if audio:
                audio_writes.append(data)
            real_write_pipe(self, audio, data)

        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            if "null" in args:
                return _FakeValidationProc(stderr=b"")  # transform "decodes" cleanly
            return await _spawn_fake_mux_holder(**kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        monkeypatch.setattr(WebSocketBridge, "_write_pipe", _recording_write_pipe)
        frames = [_frame(b"vdoCodec=H264&adoCodec=MPEG4-GENERIC", b"")]
        frames += [
            _undetectable_prefix_frame(header_suffix=b"&stamp=" + bytes([1, 2, 3, i]))
            for i in range(6)
        ]
        frames += _AAC_DETECTION_PADDING
        connect(_FakeWS(frames, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is True
        assert bridge._ffmpeg_proc is not None
        assert bridge._aac_use_header_prepend is True

        # start() returns before the buffered frames are flushed -- the
        # pipeline sets its ready event first, on purpose (see
        # _start_aac_pipeline), so wait for the drain rather than racing it.
        await _wait_until(lambda: len(audio_writes) >= 6)
        first_frame = b"\x01\x02\x03\x00" + b"\xe0" * 50
        # 0x01's top 3 bits are an SCE, so the settled layout is mono.
        assert bridge._aac_channels == 1
        expected = adts_header(len(first_frame), bridge._aac_sample_rate, 1)
        assert audio_writes[0] == expected + first_frame
        await bridge.stop()

    async def test_header_prepend_is_reached_when_a_detected_prefix_does_not_decode(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The route a real header-split camera takes. Its payloads look
        perfectly ordinary to detect_frame_prefix_len -- the scan only
        eliminates, so it hands back a length that happens to survive
        rather than nothing at all. What rules the payload-only model out
        is ffmpeg refusing the result, and only then is the header tail
        tried. Both attempts have to happen, in that order."""
        validations = 0

        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            nonlocal validations
            if "null" in args:
                validations += 1
                # Payload-only decodes to nonsense for this camera; the
                # reconstruction from the header tail decodes cleanly.
                stderr = b"" if validations > 1 else b"[aac] Reserved bit set.\n"
                return _FakeValidationProc(stderr=stderr)
            return await _spawn_fake_mux_holder(**kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        frames = [_frame(b"vdoCodec=H264&adoCodec=MPEG4-GENERIC", b"")]
        frames += [_aac_audio_frame() for _ in range(6)]
        frames += _AAC_DETECTION_PADDING
        connect(_FakeWS(frames, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is True
        assert bridge._ffmpeg_proc is not None
        assert bridge._aac_use_header_prepend is True
        assert validations == 2
        await bridge.stop()

    async def test_falls_back_to_video_only_when_aac_validation_fails(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A camera reporting the same adoCodec but using different (here,
        unrecognized) framing must not get a broken muxed session --
        this is the exact scenario a second real camera model hit live:
        same adoCodec string as a working one, but frames that don't
        decode through the AU-header/ADTS transform at all."""

        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            if "null" in args:
                return _FakeValidationProc(stderr=b"[aac] Reserved bit set.\n")
            return _FakeFfmpegProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        frames = [_frame(b"vdoCodec=H264&adoCodec=MPEG4-GENERIC", b"")]
        frames += [_aac_audio_frame() for _ in range(6)]
        frames += _AAC_DETECTION_PADDING
        connect(_FakeWS(frames, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is False
        assert bridge._ffmpeg_proc is None
        await bridge.stop()

    async def test_falls_back_to_video_only_when_the_prefix_is_undetectable(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """detect_frame_prefix_len failing outright must still try the
        header-prepend reconstruction (see _reconstruct_aac_frame) before
        giving up -- some cameras split the frame across the WS message
        itself, so an undetectable payload-only prefix doesn't yet mean
        there's no way to recover the frame. Only once that also fails
        to validate does the bridge fall back to video-only."""
        subprocess_calls = 0

        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            nonlocal subprocess_calls
            subprocess_calls += 1
            return _FakeValidationProc(stderr=b"[aac] Reserved bit set.\n")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        frames = [_frame(b"vdoCodec=H264&adoCodec=MPEG4-GENERIC", b"")]
        frames += [_undetectable_prefix_frame() for _ in range(6)]
        frames += _AAC_DETECTION_PADDING
        connect(_FakeWS(frames, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is False
        assert bridge._ffmpeg_proc is None
        assert subprocess_calls == 1
        await bridge.stop()

    async def test_falls_back_to_video_only_when_no_aac_audio_ever_arrives(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Detection also ends on the video-frame cap, so a camera that
        announces AAC and then sends none finishes with an empty buffer.
        There is nothing to validate, so ffmpeg must not be spawned to
        look at it."""
        subprocess_calls = 0

        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            nonlocal subprocess_calls
            subprocess_calls += 1
            return _FakeValidationProc(stderr=b"")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        frames = [_frame(b"vdoCodec=H264&adoCodec=MPEG4-GENERIC", b"")]
        frames += _AAC_DETECTION_PADDING
        connect(_FakeWS(frames, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is False
        assert bridge._ffmpeg_proc is None
        assert subprocess_calls == 0
        await bridge.stop()

    async def test_detection_ends_on_its_deadline_when_nothing_arrives(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither detection counter is guaranteed to fill. A camera that
        announces AAC and then goes quiet moves neither, and the idle
        timeout is no help: a stall only reconnects, so detection would
        start over and start() would wait on it for good."""
        monkeypatch.setattr(ws_bridge, "_AAC_DETECTION_TIMEOUT", 0.2)
        frames = [_frame(b"vdoCodec=H264&adoCodec=MPEG4-GENERIC", b"")]
        connect(_FakeWS(frames, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await asyncio.wait_for(bridge.start(), timeout=5.0)
        assert bridge.audio_active is False
        assert bridge._aac_detecting is False
        # Ended as a deadline, not as a stall: a stall would have recorded
        # its reason and dropped the connection instead.
        assert bridge._error == ""
        await bridge.stop()

    async def test_detection_ends_on_its_deadline_while_frames_keep_arriving(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Traffic alone doesn't move either counter: control frames keep
        the connection from ever looking stalled while leaving the video
        buffer and the interval count exactly where they were."""
        monkeypatch.setattr(ws_bridge, "_AAC_DETECTION_TIMEOUT", 0.0)
        codec_info = _frame(b"vdoCodec=H264&adoCodec=MPEG4-GENERIC", b"")
        connect(_FakeWS([codec_info] * 4, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await asyncio.wait_for(bridge.start(), timeout=5.0)
        assert bridge.audio_active is False
        assert bridge._aac_detecting is False
        assert bridge._error == ""
        await bridge.stop()

    async def test_gives_up_at_once_on_frames_too_long_for_an_adts_header(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A payload no ADTS header can describe is not one frame, so this
        camera uses neither framing: video-only, and ffmpeg never spawns
        because the transform fails before there's anything to validate.

        Header-prepend produces a strictly longer frame than the
        payload-only strip does, so it can only overflow the same way --
        the transform must be attempted once, not once per framing mode."""
        subprocess_calls = 0
        transform_attempts = 0
        real_adts_header = ws_bridge.adts_header

        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            nonlocal subprocess_calls
            subprocess_calls += 1
            return _FakeValidationProc(stderr=b"")

        def _counting_adts_header(payload_length: int, sample_rate: int, channels: int) -> bytes:
            nonlocal transform_attempts
            transform_attempts += 1
            return real_adts_header(payload_length, sample_rate, channels)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        monkeypatch.setattr(ws_bridge, "adts_header", _counting_adts_header)
        frames = [_frame(b"vdoCodec=H264&adoCodec=MPEG4-GENERIC", b"")]
        frames += [_aac_audio_frame(payload_len=8200) for _ in range(6)]
        frames += _AAC_DETECTION_PADDING
        connect(_FakeWS(frames, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is False
        assert bridge._ffmpeg_proc is None
        assert subprocess_calls == 0
        assert transform_attempts == 1
        await bridge.stop()

    async def test_uses_channels_and_sample_rate_from_the_codec_info_payload(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A camera confirmed to send the audio-config trailer (see
        parse_audio_config in aac.py) must end up muxed with those exact
        values, not whatever runtime detection would have guessed --
        _aac_audio_frame's frames read as stereo under detect_channel_count
        (no frame names a layout, so it falls back to its stereo default),
        so a mono result here can only have come from the header."""

        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            if "null" in args:
                return _FakeValidationProc(stderr=b"")  # transform "decodes" cleanly
            return await _spawn_fake_mux_holder(**kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        frames = [_aac_codec_frame_with_config(channels=1, sample_rate=32000)]
        frames += [_aac_audio_frame() for _ in range(6)]
        frames += _AAC_DETECTION_PADDING
        connect(_FakeWS(frames, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is True
        assert bridge._aac_config_from_header is True
        assert bridge._aac_channels == 1
        assert bridge._aac_sample_rate == 32000
        await bridge.stop()

    async def test_a_mono_frame_beats_a_stereo_count_from_the_codec_info_payload(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DSM declares what the camera negotiated, the frames carry what
        it actually sends, and ffmpeg accepts either labelling in silence
        -- so a frame that names a layout has to win. Synology's own
        decoder resolves the same disagreement the same way."""

        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            if "null" in args:
                return _FakeValidationProc(stderr=b"")
            return await _spawn_fake_mux_holder(**kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        frames = [_aac_codec_frame_with_config(channels=2, sample_rate=32000)]
        frames += [_aac_audio_frame(first_frame_byte=0x00) for _ in range(6)]
        frames += _AAC_DETECTION_PADDING
        connect(_FakeWS(frames, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is True
        assert bridge._aac_channels == 1  # the frames say single_channel_element
        assert bridge._aac_sample_rate == 32000  # the rate still comes from the payload
        await bridge.stop()

    async def test_a_rejected_framing_does_not_supply_the_channel_fallback(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_aac_frames_look_valid runs once per framing model, and the
        first model here reconstructs frames that read as stereo before
        ffmpeg throws them out. What that rejected reconstruction claimed
        must not become the fallback for the model that is kept: the
        camera declared mono and no surviving frame names a layout."""
        validations = 0

        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            nonlocal validations
            if "null" in args:
                validations += 1
                stderr = b"" if validations > 1 else b"[aac] Reserved bit set.\n"
                return _FakeValidationProc(stderr=stderr)
            return await _spawn_fake_mux_holder(**kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        frames = [_aac_codec_frame_with_config(channels=1, sample_rate=32000)]
        # Stripped of its 3-byte prefix each payload opens on a
        # channel_pair_element; put back behind the header tail, which is
        # what the kept model does, it opens on nothing that names a layout.
        frames += [_aac_audio_frame(first_frame_byte=0x20) for _ in range(6)]
        frames += _AAC_DETECTION_PADDING
        connect(_FakeWS(frames, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert validations == 2
        assert bridge._aac_use_header_prepend is True
        assert bridge._aac_channels == 1  # what DSM declared, not the rejected model
        await bridge.stop()


class TestAudioConfigFromHeader:
    """_setup_pipes' own decision to trust (or not) the codec-info
    payload's audio-config trailer, isolated from the rest of AAC
    detection -- see TestAudioMuxDecision for the full pipeline."""

    async def test_reads_channels_and_sample_rate_immediately(self) -> None:
        """Available before a single audio frame has arrived, unlike
        every value runtime detection produces."""
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        trailer = b"1|32000|\x14\x08"
        await bridge._setup_pipes("H264", "MPEG4-GENERIC", str(len(trailer)), b"\x00" * 4 + trailer)
        assert bridge._aac_config_from_header is True
        assert bridge._aac_declared_channels == 1
        assert bridge._aac_sample_rate == 32000
        # Still true: the trailer says nothing about how a raw frame is
        # split across payload/header, so that still has to be detected.
        assert bridge._aac_detecting is True

    async def test_falls_back_to_detection_without_a_recognized_trailer(self) -> None:
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge._setup_pipes("H264", "MPEG4-GENERIC", "", b"")
        assert bridge._aac_config_from_header is False
        assert bridge._aac_declared_channels == 2  # untouched stereo default
        assert bridge._aac_detecting is True


class TestAacFrameReconstruction:
    """The two framing modes are nothing but the bytes they produce --
    everything else in the AAC path only decides which one to use. Assert
    the bytes: a mode flag set correctly while the reconstruction is
    wrong passes every mux/fallback test in this file."""

    def test_payload_only_mode_strips_the_detected_prefix(self) -> None:
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        bridge._frame_prefix_len = 3
        frame = bridge._reconstruct_aac_frame(b"\x01\x02\x03\x04", b"\x34\x1f\xfc\x21\x1a")
        assert frame == b"\x21\x1a"

    def test_header_prepend_mode_puts_the_header_tail_back_in_front(self) -> None:
        """The payload is missing its own leading bytes; the WS message's
        header ends in exactly those bytes. Nothing is stripped."""
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        bridge._aac_use_header_prepend = True
        frame = bridge._reconstruct_aac_frame(b"\x01\x2e\x35\xa8", b"\xaa\xbb")
        assert frame == b"\x01\x2e\x35\xa8\xaa\xbb"

    async def test_writes_an_adts_header_in_front_of_the_reconstructed_frame(self) -> None:
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        bridge._aac_use_header_prepend = True
        bridge._aac_sample_rate = 16000
        written: list[bytes] = []
        bridge._write_pipe = lambda audio, data: written.append(data)  # type: ignore[method-assign]

        await bridge._handle_aac_audio_frame(b"\x01\x2e\x35\xa8", b"\xaa" * 20)

        frame = b"\x01\x2e\x35\xa8" + b"\xaa" * 20
        # Nothing settled a layout here, so the untouched stereo default.
        assert written == [adts_header(len(frame), 16000, 2) + frame]

    async def test_the_settled_channel_count_applies_to_every_frame(self) -> None:
        """Read the layout off each frame instead and a stream that opens
        with a fill element -- as everything libavcodec writes does --
        announces a channel_configuration that changes mid-stream, which
        no valid ADTS stream does."""
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        bridge._aac_sample_rate = 16000
        bridge._frame_prefix_len = 0
        bridge._aac_channels = 1
        written: list[bytes] = []
        bridge._write_pipe = lambda audio, data: written.append(data)  # type: ignore[method-assign]

        await bridge._handle_aac_audio_frame(b"", b"\xc0" + b"\xaa" * 20)  # FIL first
        await bridge._handle_aac_audio_frame(b"", b"\x00" + b"\xaa" * 20)  # SCE first

        layouts = {((w[2] & 0x1) << 2) | ((w[3] >> 6) & 0x3) for w in written}
        assert layouts == {1}


def _input_sections(args: tuple[str, ...]) -> list[list[str]]:
    """Split an ffmpeg argv into one option list per -i input. Everything
    after the last -i is output options, so it forms no section."""
    sections: list[list[str]] = []
    current: list[str] = []
    for arg in args:
        if arg == "-i":
            sections.append(current)
            current = []
        else:
            current.append(arg)
    return sections


async def _capture_spawn_args(monkeypatch: pytest.MonkeyPatch, audio_codec: str) -> tuple[str, ...]:
    """Run _spawn_ffmpeg against a mocked exec and return the argv it built."""
    captured: list[tuple[str, ...]] = []

    async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeFfmpegProc:
        captured.append(args)
        return _FakeFfmpegProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
    bridge = WebSocketBridge("wss://nas/stream", False, "sid")
    await bridge._spawn_ffmpeg("H264", audio_codec, 11, 12, 13)
    return captured[0]


class TestFfmpegTimestampArgs:
    """Raw Annex B NALs carry no timing, so ffmpeg stamps video from the
    host clock. Audio has to come off that same clock: timestamps derived
    from the byte count follow the camera's own sampling clock instead,
    and since mpv paces playback off the audio track, any offset between
    the two clocks (plus every dropped audio packet) becomes playback
    permanently slower than arrival, with the backlog growing for as long
    as the stream runs."""

    @pytest.mark.parametrize("audio_codec", ["PCMU", "MPEG4-GENERIC"])
    async def test_both_inputs_are_stamped_from_the_host_clock(
        self, monkeypatch: pytest.MonkeyPatch, audio_codec: str
    ) -> None:
        sections = _input_sections(await _capture_spawn_args(monkeypatch, audio_codec))
        assert len(sections) == 2
        for section in sections:
            assert "-use_wallclock_as_timestamps" in section

    @pytest.mark.parametrize("audio_codec", ["PCMU", "MPEG4-GENERIC"])
    async def test_audio_is_decoded_and_resampled(
        self, monkeypatch: pytest.MonkeyPatch, audio_codec: str
    ) -> None:
        """The resampler absorbs the scheduling jitter that byte-count
        timestamps used to hide, and it can only run on a decoded stream."""
        args = await _capture_spawn_args(monkeypatch, audio_codec)
        assert args[args.index("-c:a") + 1] == "pcm_s16le"
        assert args[args.index("-af") + 1].startswith("aresample=async=")


class TestPipeLifetime:
    async def test_descriptors_are_recycled(self, connect: Any) -> None:
        """Why playback must stop before a bridge closes its pipe.

        The next bridge gets the same descriptor numbers back, so an mpv
        demuxer still holding the old ones would read the new stream.
        """
        connect(_FakeWS([_codec_frame()], hang=True))
        first = WebSocketBridge("wss://nas/stream", False, "sid")
        url = await first.start()
        fd = int(url.removeprefix("fd://"))
        await first.stop()

        # A fresh fake with its own codec-info message: the first bridge's
        # fake already consumed its one message, so reusing it here would
        # leave the second bridge waiting forever for codec info that will
        # never arrive.
        connect(_FakeWS([_codec_frame()], hang=True))
        second = WebSocketBridge("wss://nas/stream", False, "sid")
        assert await second.start() == f"fd://{fd}"
        await second.stop()

    async def test_no_leak_when_a_later_pipe_fails(self) -> None:
        """_start_muxed opens three pipes before spawning ffmpeg. A failure
        on the second or third must not strand the ones already open: this
        runs again on every reconnect until the bridge gives up."""
        real_pipe = os.pipe
        calls = {"n": 0}

        def _third_pipe_fails() -> tuple[int, int]:
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError(24, "Too many open files")
            return real_pipe()

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        before = set(os.listdir("/proc/self/fd"))
        with (
            patch("os.pipe", _third_pipe_fails),
            pytest.raises(OSError),
        ):
            await bridge._start_muxed("H264", "PCMU")
        assert not set(os.listdir("/proc/self/fd")) - before

    async def test_stop_closes_both_ends(self, connect: Any) -> None:
        connect(_FakeWS([_codec_frame()], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        url = await bridge.start()
        fd = int(url.removeprefix("fd://"))
        await bridge.stop()
        with pytest.raises(OSError):
            os.fstat(fd)
