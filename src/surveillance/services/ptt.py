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

"""Push-to-talk (two-way audio) upload session.

Wire protocol reverse-engineered from a genuine mobile-app traffic capture:

  - wss://<nas>/ss_webstream_task/?method=AudioOut&dsId=0&id=<cam>&type=1
  - preceded by SYNO.SurveillanceStation.AudioOut::CheckOccupied (v2)
  - no framing header at all -- raw binary WebSocket messages
  - each message = 640 mu-law samples (80ms @ 8kHz), every sample byte sent
    twice in a row (1280 bytes/message) -- a mono->stereo duplication quirk
    in the mobile SDK; harmless and cheap to replicate

Currently assumes G.711 mu-law (PCMU), confirmed against Cam 58. NOT every
camera necessarily accepts the same codec on its speaker (Cam 59, for
example, reports a different codec -- MPEG4-GENERIC/AAC -- for its own
*download* audio), so a camera whose speaker expects something other than
PCMU won't produce intelligible sound yet. No per-camera codec check exists
yet.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
from typing import TYPE_CHECKING, Any

from surveillance.services.g711 import lin2ulaw
from surveillance.util.async_bridge import get_loop

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI

log = logging.getLogger(__name__)

SAMPLE_RATE = 8000  # G.711 standard rate
CHUNK_SAMPLES = 640  # 80ms @ 8kHz -- confirmed real packet size
CHUNK_SECONDS = CHUNK_SAMPLES / SAMPLE_RATE

# A send that makes no progress for this long means the socket is wedged:
# the NAS rebooted, the Wi-Fi dropped, or the TCP connection went half
# open. ping_interval is off (see the connect call), so nothing else would
# ever notice, and without a bound stop() cannot get the microphone back.
SEND_TIMEOUT = 5.0

# Live speech nobody can hear is not worth queueing. Capped at a couple of
# seconds so a stalled uplink cannot grow the backlog without limit.
MAX_QUEUED_CHUNKS = 25


class PttOccupiedError(Exception):
    """The camera's speaker is already in use by someone else."""


def _double_bytes(data: bytes) -> bytes:
    """Real wire format: every mu-law sample byte sent twice in a row."""
    out = bytearray(len(data) * 2)
    out[0::2] = data
    out[1::2] = data
    return bytes(out)


def _build_ws_url(api: SurveillanceAPI, camera_id: int) -> str:
    scheme = "wss" if api.base_url.startswith("https") else "ws"
    host_port = api.base_url.split("://", 1)[1]
    return f"{scheme}://{host_port}/ss_webstream_task/?method=AudioOut&dsId=0&id={camera_id}&type=1"


def _build_ssl_context(api: SurveillanceAPI, ws_url: str) -> ssl.SSLContext | None:
    if not ws_url.startswith("wss://"):
        return None
    ctx = ssl.create_default_context()
    if not api.profile.verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def check_occupied(api: SurveillanceAPI, camera_id: int) -> bool:
    """Whether another client is already talking through this camera's speaker."""
    data = await api.request(
        api="SYNO.SurveillanceStation.AudioOut",
        method="CheckOccupied",
        version=2,
        extra_params={"devType": 1, "devId": camera_id},
    )
    return bool(data.get("isOccupied"))


class PttSession:
    """One push-to-talk session: mic capture -> mu-law encode -> upload.

    Constructed on the GTK main thread, run() submitted to the background
    asyncio loop via util.async_bridge.run_async(). stop() is safe to call
    from the GTK main thread while run() is in progress on the other one.
    """

    def __init__(self, camera_id: int) -> None:
        self.camera_id = camera_id
        self._stop_event = asyncio.Event()
        self._stream: Any = None

    def stop(self) -> None:
        """Signal a running session to wind down and close. Thread-safe."""
        get_loop().call_soon_threadsafe(self._stop_event.set)

    def _open_stream(self, queue: "asyncio.Queue[bytes]", loop: Any) -> None:
        """Open the PortAudio input stream. Runs in a worker thread: opening
        a capture device talks to ALSA/PulseAudio and routinely blocks for
        a hundred milliseconds or more, and this app has one event loop
        serving every camera."""
        import sounddevice as sd  # noqa: PLC0415

        def enqueue(data: bytes) -> None:
            # Runs on the loop thread. Dropping the oldest chunk keeps the
            # backlog bounded when the uplink stalls, and losing the oldest
            # audio beats letting the queue grow for as long as the socket
            # stays wedged.
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(data)

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            if status:
                log.warning("PTT mic capture status: %s", status)
            loop.call_soon_threadsafe(enqueue, bytes(indata))

        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=CHUNK_SAMPLES,
            callback=callback,
        )
        self._stream.start()

    async def _start_capture(self) -> "asyncio.Queue[bytes]":
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MAX_QUEUED_CHUNKS)
        await asyncio.to_thread(self._open_stream, queue, asyncio.get_running_loop())
        return queue

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def _stop_capture(self) -> None:
        """Closing the device blocks for the same reasons opening it does."""
        await asyncio.to_thread(self._close_stream)

    async def run(self, api: SurveillanceAPI) -> None:
        """Open the AudioOut channel and stream mic audio until stop() is called.

        Mic capture starts immediately, before CheckOccupied/connecting --
        otherwise anything said during that handshake (confirmed live: about
        a second) is simply never captured. If the camera turns out to be
        occupied, the buffered capture is discarded and PttOccupiedError is
        raised without ever opening the AudioOut socket.
        """
        # Inside the try: _open_stream() assigns self._stream before calling
        # start() on it, so a device that opens but fails to start is already
        # ours to close, and only the finally below does that.
        try:
            queue = await self._start_capture()
            # Pacing clock starts here, at the moment capture actually begins --
            # not once CheckOccupied/connect finish -- so that whatever buffers
            # up in the queue during that handshake has *already-past* targets
            # below and drains immediately instead of being paced out on top of
            # an extra artificial delay.
            start = time.monotonic()
            # The handshake below runs for about a second, and connect()
            # alone may wait up to open_timeout. Only the send loop used to
            # look at the stop flag, so tapping the button off during that
            # window left the mic recording and still opened the camera's
            # AudioOut channel afterwards.
            if self._stop_event.is_set():
                return
            if await check_occupied(api, self.camera_id):
                raise PttOccupiedError(f"Camera {self.camera_id}'s speaker is already in use")
            if self._stop_event.is_set():
                return

            ws_url = _build_ws_url(api, self.camera_id)
            ssl_ctx = _build_ssl_context(api, ws_url)
            headers = {"Cookie": f"id={api.sid}"}

            import websockets.asyncio.client as ws_client  # noqa: PLC0415

            async with ws_client.connect(
                ws_url,
                ssl=ssl_ctx,
                additional_headers=headers,
                open_timeout=15,
                close_timeout=2,
                ping_interval=None,
            ) as ws:
                sent = 0
                while not self._stop_event.is_set():
                    try:
                        chunk = await asyncio.wait_for(queue.get(), timeout=0.5)
                    except TimeoutError:
                        continue
                    ulaw = lin2ulaw(chunk)
                    try:
                        await asyncio.wait_for(ws.send(_double_bytes(ulaw)), timeout=SEND_TIMEOUT)
                    except TimeoutError:
                        # send() waits on the transport draining, which a
                        # wedged socket never does. Without this the loop
                        # never comes back around to the stop flag and the
                        # finally below never closes the microphone.
                        raise RuntimeError(
                            f"push-to-talk upload stalled for {SEND_TIMEOUT:.0f}s"
                        ) from None
                    sent += 1
                    # Pace against a monotonic target rather than accumulating
                    # asyncio.sleep(CHUNK_SECONDS) calls each loop, which
                    # drift and cause choppy playback. A backlog built up
                    # during the handshake above has targets already in the
                    # past, so this naturally sends it immediately instead
                    # of waiting -- no separate catch-up case needed.
                    target = start + sent * CHUNK_SECONDS
                    delay = target - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
        finally:
            await self._stop_capture()
