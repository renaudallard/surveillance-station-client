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
from collections.abc import Callable

import websockets.asyncio.client as ws_client

log = logging.getLogger(__name__)


class WebSocketBridge:
    """Bridge a WebSocket video stream to an in-memory pipe for mpv.

    The bridge connects to a Synology WebSocket stream, strips the proprietary
    4-byte-length framing, and writes raw Annex-B NAL units to a Unix pipe so
    mpv can play them via fd://<read_fd>.

    Connection failures (including HTTP 502) are caught, logged, and reported
    via the optional *on_error* callback.  They never propagate as unhandled
    exceptions.
    """

    def __init__(
        self,
        ws_url: str,
        verify_ssl: bool,
        sid: str,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._verify_ssl = verify_ssl
        self._sid = sid
        self._on_error = on_error
        self._read_fd: int = -1
        self._write_fd: int = -1
        self._fd_lock = threading.Lock()
        self._pump_task: asyncio.Task[None] | None = None
        self._error: str | None = None

    @property
    def error(self) -> str | None:
        """Most recent error message, or None if the bridge is healthy."""
        return self._error

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

    def _classify_error(self, exc: BaseException) -> str:
        """Return a human-readable description for a WebSocket connection error."""
        exc_type = type(exc).__name__
        exc_str = str(exc)

        # HTTP 502 Bad Gateway: NAS overloaded, camera stream not ready, or
        # Surveillance Station proxy rejected the connection.
        if "502" in exc_str or "Bad Gateway" in exc_str.lower():
            return (
                "WebSocket connection rejected with HTTP 502 — "
                "the NAS may be overloaded or the camera stream is not ready. "
                "Try RTSP or MJPEG protocol as a fallback."
            )

        # Other non-2xx HTTP status during WebSocket handshake
        if "InvalidStatus" in exc_type or "reject" in exc_str.lower():
            return f"WebSocket handshake failed: {exc_str}"

        # TLS/SSL issues
        if "ssl" in exc_type.lower() or "SSL" in exc_str:
            return f"WebSocket TLS error: {exc_str}"

        # Generic connection failure
        return f"WebSocket error ({exc_type}): {exc_str}"

    async def _pump(self) -> None:
        """Connect to the WebSocket and write video frames to the pipe.

        Strips the Synology proprietary framing (4-byte length + ASCII
        header) from each WebSocket message and writes only the raw
        video payload (Annex B H.264/H.265 NAL units) to the pipe.
        Audio frames are dropped since mpv cannot demux interleaved
        raw audio in a raw video byte stream.

        Any connection failure is caught, classified, and reported via
        the on_error callback.  The write end of the pipe is always
        closed in the finally block so mpv sees EOF and terminates cleanly.
        """
        ssl_ctx: ssl.SSLContext | bool | None = None
        if self._ws_url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if not self._verify_ssl:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

        headers = {"Cookie": f"id={self._sid}"}

        try:
            log.debug("WebSocket connecting: %s", self._ws_url)

            async with ws_client.connect(
                self._ws_url,
                ssl=ssl_ctx,
                additional_headers=headers,
                max_size=2**22,
                open_timeout=15,
                close_timeout=2,
            ) as ws:
                log.debug("WebSocket connected")
                async for message in ws:
                    if isinstance(message, bytes):
                        payload = self._extract_payload(message)
                        if payload:
                            await asyncio.to_thread(os.write, self._write_fd, payload)
        except (asyncio.CancelledError, BrokenPipeError):
            log.debug("WebSocket bridge cancelled")
        except Exception as exc:
            error_msg = self._classify_error(exc)
            log.error("surveillance.services.ws_bridge: %s", error_msg)  # noqa: TRY400
            self._error = error_msg
            if self._on_error is not None:
                try:
                    self._on_error(error_msg)
                except Exception:
                    log.debug("ws_bridge on_error callback raised", exc_info=True)
        finally:
            self._close_write_fd()

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
