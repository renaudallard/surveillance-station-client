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

"""WebSocket-to-pipe bridge for mpv playback of WebSocket streams."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import ssl
import struct
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

# Internal reconnect policy for the routine ~15-25s session drops the NAS's
# WebSocket streaming backend does by design (confirmed against a real NAS
# and against DSM's own web client, which reconnects the same way). A
# connection that stays up at least this long counts as healthy, resetting
# the failure streak — only a run of failures that never reach a real
# connection at all triggers giving up.
_FAST_FAILURE_THRESHOLD = 3.0  # seconds
_MAX_CONSECUTIVE_FAST_FAILURES = 5
_MAX_RECONNECT_DELAY = 2.0  # seconds


def _ws_connect(url: str, **kwargs: Any) -> Any:
    """Open a WebSocket connection.

    websockets is imported here rather than at module scope so this module
    stays importable without it, and so nothing drags it onto the startup
    import path.
    """
    import websockets.asyncio.client as ws_client  # noqa: PLC0415

    return ws_client.connect(url, **kwargs)


def _classify_error(exc: BaseException) -> str:
    """Return a human-readable description of a WebSocket connection failure."""
    exc_type = type(exc).__name__
    exc_str = str(exc)
    low = exc_str.lower()
    if "502" in exc_str or "bad gateway" in low:
        return "HTTP 502 (NAS overloaded or camera stream not ready)"
    if "invalidstatus" in exc_type.lower() or "reject" in low:
        return f"handshake failed: {exc_str}"
    if "ssl" in exc_type.lower() or "ssl" in low:
        return f"TLS error: {exc_str}"
    return f"{exc_type}: {exc_str}"


class WebSocketBridge:
    """Bridge a WebSocket video stream to an in-memory pipe for mpv."""

    def __init__(self, ws_url: str, verify_ssl: bool, sid: str) -> None:
        self._ws_url = ws_url
        self._verify_ssl = verify_ssl
        self._sid = sid
        self._read_fd: int = -1
        self._write_fd: int = -1
        self._fd_lock = threading.Lock()
        self._pump_task: asyncio.Task[None] | None = None
        self._error: str = ""
        self._stopping = False
        self._connected_at: float | None = None
        self._fast_failures = 0

    def _note_attempt_outcome(self, connected: bool, attempt_start: float) -> bool:
        """Track consecutive failed-to-connect attempts; return True to give up.

        A connection that stayed up a little while is a fresh, healthy
        attempt — only a run of failures that never even establish a real
        connection should give up, so a camera that is genuinely
        unreachable doesn't retry forever.
        """
        attempt_uptime = time.monotonic() - attempt_start if connected else 0.0
        if attempt_uptime >= _FAST_FAILURE_THRESHOLD:
            self._fast_failures = 0
            return False
        self._fast_failures += 1
        if self._fast_failures < _MAX_CONSECUTIVE_FAST_FAILURES:
            return False
        if not self._error:
            self._error = "repeated connection failures"
        log.error(
            "WebSocket failed to establish %d times in a row — giving up: %s",
            self._fast_failures,
            self._error,
        )
        return True

    def _log_reconnect(self, clean_close: bool) -> None:
        if clean_close:
            log.debug(
                "WebSocket closed cleanly after %.0fs — reconnecting on the same pipe", self.uptime
            )
        else:
            log.warning(
                "WebSocket dropped after %.0fs (%s) — reconnecting on the same pipe",
                self.uptime,
                self._error,
            )

    async def start(self) -> str:
        """Create pipe, start pump task, return fd:// URL for mpv."""
        self._read_fd, self._write_fd = os.pipe()
        log.debug("WebSocket bridge pipe: fd://%d", self._read_fd)

        self._pump_task = asyncio.create_task(self._pump())
        return f"fd://{self._read_fd}"

    @staticmethod
    def _extract_payload(message: bytes) -> bytes | None:
        """Strip Synology framing from a WebSocket message.

        Frame format: [4-byte BE header_len][ASCII header][binary payload]

        Returns the binary payload for video frames and codec init data.
        Returns None for audio frames (mediaType=2) and control messages
        (close=...) which should not be written to the video pipe.
        """
        if len(message) < 4:
            return None
        (hdr_len,) = struct.unpack(">I", message[:4])
        if 4 + hdr_len > len(message):
            # Malformed: header extends beyond message
            return None
        header = message[4 : 4 + hdr_len]
        payload = message[4 + hdr_len :]
        # Skip audio frames and control messages
        if b"mediaType=2" in header or b"close=" in header:
            if b"close=" in header:
                log.debug("WebSocket stream close: %s", header.decode(errors="replace"))
            return None
        if not payload:
            return None
        # The Synology header embeds the Annex B start code (00 00 00 01)
        # as its last 4 bytes.  Prepend it so mpv can detect NAL boundaries.
        return b"\x00\x00\x00\x01" + payload

    async def _pump(self) -> None:
        """Connect to the WebSocket and write video frames to the pipe.

        Reconnects internally on the same pipe whenever the NAS drops the
        session (its normal behavior, every ~15-25s) instead of closing the
        pipe — see the comment at the reconnect site for why. Only exits
        (letting the pipe close and `wait_closed()` return a reason) on a
        deliberate stop or after repeated attempts that never establish a
        real connection at all.

        Strips the Synology proprietary framing (4-byte length + ASCII
        header) from each WebSocket message and writes only the raw
        video payload (Annex B H.264/H.265 NAL units) to the pipe.
        Audio frames are dropped since mpv cannot demux interleaved
        raw audio in a raw video byte stream.
        """
        ssl_ctx: ssl.SSLContext | bool | None = None
        if self._ws_url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if not self._verify_ssl:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

        headers = {"Cookie": f"id={self._sid}"}
        delay = 0.0

        try:
            while not self._stopping:
                clean_close = False
                connected = False
                attempt_start = time.monotonic()
                try:
                    log.debug("WebSocket connecting: %s", self._ws_url)

                    async with _ws_connect(
                        self._ws_url,
                        ssl=ssl_ctx,
                        additional_headers=headers,
                        max_size=2**22,
                        open_timeout=15,
                        close_timeout=2,
                        ping_interval=None,
                    ) as ws:
                        log.debug("WebSocket connected")
                        connected = True
                        self._connected_at = time.monotonic()
                        delay = 0.0
                        async for message in ws:
                            if isinstance(message, bytes):
                                payload = self._extract_payload(message)
                                if payload:
                                    await asyncio.to_thread(os.write, self._write_fd, payload)
                    # Reached with no exception: the server closed the
                    # WebSocket cleanly (confirmed via a real NAS capture —
                    # this is the COMMON case, e.g. code 1005/"no status
                    # received", not an error path).
                    clean_close = True
                except Exception as exc:
                    self._error = _classify_error(exc)

                if self._stopping:
                    break

                if self._note_attempt_outcome(connected, attempt_start):
                    break

                # Reconnect on the SAME pipe rather than closing it, whether
                # the session ended cleanly or with an error: the NAS drops
                # this WebSocket session routinely, every ~15-25s, as normal
                # behavior (confirmed against a real NAS — not a rare
                # failure). Closing the write end here would deliver a real
                # EOF to mpv, which — with keep_open=yes on a raw fd://
                # stream — never resumes decoding again even after a fresh
                # play() call on a new pipe (confirmed via a standalone
                # repro). Keeping the pipe open and just resuming writes
                # after a short reconnect makes this look like an ordinary
                # buffering stall to mpv instead of a terminal end-of-file,
                # so it recovers on its own with no player/render-context
                # teardown needed at all.
                self._log_reconnect(clean_close)
                delay = min(delay * 2, _MAX_RECONNECT_DELAY) if delay else 0.25
                await asyncio.sleep(delay)
        except (asyncio.CancelledError, BrokenPipeError):
            log.debug("WebSocket bridge cancelled")
        finally:
            self._close_write_fd()

    @property
    def uptime(self) -> float:
        """Seconds the WebSocket stayed connected, 0 if it never connected."""
        if self._connected_at is None:
            return 0.0
        return time.monotonic() - self._connected_at

    async def wait_closed(self) -> str:
        """Wait for the bridge to give up for good, and describe why.

        Routine NAS-side session drops are reconnected internally by
        `_pump()` and never reach here — this only resolves on a deliberate
        stop (empty string) or once repeated attempts have failed to
        establish a real connection at all (see `_note_attempt_outcome`).
        """
        if self._pump_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task
        if self._stopping:
            return ""
        return self._error or "stream ended"

    def _close_write_fd(self) -> None:
        """Atomically close the write fd. Thread-safe, idempotent."""
        with self._fd_lock:
            fd = self._write_fd
            self._write_fd = -1
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)

    def close_write_end(self) -> None:
        """Close the write end of the pipe immediately.

        This unblocks any os.write call stuck in the thread pool
        and signals EOF to mpv on the read end. Safe to call from
        any thread, idempotent.
        """
        self._stopping = True
        self._close_write_fd()

    async def stop(self) -> None:
        """Cancel the pump task and close pipe fds."""
        self.close_write_end()

        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task
            self._pump_task = None

        if self._read_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._read_fd)
            self._read_fd = -1
