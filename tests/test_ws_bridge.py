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
import os
import sys
import types
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

    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def send(self, message: Any) -> None:
        self.sent.append(message)

    async def recv(self) -> bytes:
        if self._messages:
            return self._messages.pop(0)
        if self._hang:
            await asyncio.sleep(3600)
        from websockets.exceptions import ConnectionClosedOK

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
        internally — that's the NAS's normal ~15-25s session rotation, not a
        failure. But a run of closes that never last long enough to look
        like a real connection must still eventually surface, rather than
        retrying forever in a tight loop."""
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
        # Bigger than the pipe's default kernel buffer (64KiB) and nothing
        # ever reads the other end in this test, so the write actually
        # blocks instead of completing immediately.
        big_frame = _frame(b"mediaType=1", b"\xaa" * (200 * 1024))
        connect(_FakeWS([_codec_frame(), big_frame], hang=True))
        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        reason = await bridge.wait_closed()
        assert "stalled" in reason
        await bridge.stop()


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
    decision in _setup_pipes, not ffmpeg's actual behavior."""

    returncode: int | None = 0

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        pass


class _FakeValidationProc:
    """Stand-in for the throwaway ffmpeg process _aac_frames_look_valid
    spawns to sanity-check the AU-header/ADTS transform."""

    def __init__(self, stderr: bytes = b"") -> None:
        self._stderr = stderr

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return b"", self._stderr

    def kill(self) -> None:
        pass


def _aac_audio_frame(payload_len: int = 50) -> bytes:
    """A mediaType=2 frame with a 3-byte AU-header prefix (real content
    doesn't matter for these tests -- they only exercise the mux/
    fallback decision, not real AAC decoding)."""
    return _frame(b"mediaType=2", b"\x00\x00\x00" + b"\xaa" * payload_len)


def _undetectable_au_header_frame(payload_len: int = 50) -> bytes:
    """A mediaType=2 frame where every byte reads as AAC's "immediate
    end, zero elements" marker -- no AU-header length in
    detect_au_header_len's search range can ever pass, mirroring a
    camera using an entirely different framing scheme."""
    return _frame(b"mediaType=2", b"\xe0" * payload_len)


def _video_frame() -> bytes:
    return _frame(b"mediaType=1", b"\x00" * 10)


async def _spawn_fake_mux_holder(**kwargs: Any) -> Any:
    """Stand-in for the real ffmpeg mux spawn in tests that write
    buffered frames afterward (AAC detection's flush) -- a plain fake
    object leaves nothing holding the passed-through pipe read ends open
    once _start_muxed closes its own copies, so every write after that
    fails with EPIPE. A real, trivial process that inherits the same
    fds and sleeps keeps them open without needing anything
    ffmpeg-specific."""
    return await _REAL_SUBPROCESS_EXEC(
        "sleep",
        "5",
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

    async def test_falls_back_to_video_only_when_au_header_length_is_undetectable(
        self, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """detect_au_header_len failing outright must short-circuit
        straight to the video-only fallback, without ever spawning
        ffmpeg's throwaway validation subprocess -- there's no AU-header
        length left to build a transform from."""
        subprocess_calls = 0

        async def _fake_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            nonlocal subprocess_calls
            subprocess_calls += 1
            return _FakeFfmpegProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess_exec)
        frames = [_frame(b"vdoCodec=H264&adoCodec=MPEG4-GENERIC", b"")]
        frames += [_undetectable_au_header_frame() for _ in range(6)]
        frames += _AAC_DETECTION_PADDING
        connect(_FakeWS(frames, hang=True))

        bridge = WebSocketBridge("wss://nas/stream", False, "sid")
        await bridge.start()
        assert bridge.audio_active is False
        assert bridge._ffmpeg_proc is None
        assert subprocess_calls == 0
        await bridge.stop()


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
