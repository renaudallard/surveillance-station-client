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

"""WebSocket-to-pipe bridge for mpv playback of WebSocket streams.

Video is always piped through. Audio (mediaType=2 frames, dropped
entirely until now) is muxed in via an ffmpeg subprocess when DSM reports
a codec we know how to handle (currently PCMU/G.711 mu-law, and AAC via
the common "AAC-hbr" RTP framing -- see aac.py) -- mpv's fd:// pipe then
reads ffmpeg's own Matroska output instead of the raw Annex B video
stream directly. A camera whose audio is neither (or has none at all)
falls back to the original raw-video-only passthrough, unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import select
import ssl
import struct
import subprocess
import threading
import time
from statistics import median
from typing import Any

from surveillance.services.aac import (
    adts_header,
    detect_au_header_len,
    nearest_sample_rate,
    strip_au_header,
)

log = logging.getLogger(__name__)

# A connection counts as healthy once it has stayed up this long and
# delivered data, resetting the failure streak. Only a run of failures
# that never reach a working connection at all triggers giving up.
_FAST_FAILURE_THRESHOLD = 3.0  # seconds
_MAX_CONSECUTIVE_FAST_FAILURES = 5
_MAX_RECONNECT_DELAY = 2.0  # seconds

# How long to wait for a message before treating the connection as silently
# stalled. Disabling ping_interval (see the reconnect comment in _pump) fixed
# the NAS's routine session drops being cut short prematurely, but it also
# removed the one mechanism (ping_timeout) that would have caught a
# connection that never sends a close frame at all and just stops delivering
# data — confirmed happening against a real NAS: no exception, no clean
# close, just nothing, forever. This is an application-level idle timeout on
# recv(), not a protocol ping, so it doesn't reintroduce the premature-
# disconnect problem.
_IDLE_TIMEOUT = 10.0  # seconds

# ss_webstream_task drops a connection after roughly nine missed
# intervals of client silence, even while it keeps sending video --
# a client-to-server message resets that timer, so one gets sent on
# this cadence for as long as the connection lives.
#
# Equal to _IDLE_TIMEOUT above by coincidence, not design: that one
# watches inbound recv() silence, this one paces outbound send() --
# independent timers on opposite directions of the same socket.
_KEEPALIVE_INTERVAL = 10.0  # seconds

# How long a pipe write may block before treating the downstream reader
# (ffmpeg, or mpv on the raw-video-only pipe) as stalled rather than
# waiting on it forever. Generous: a healthy pipe write completes
# immediately, so this only ever matters when something downstream has
# genuinely stopped draining.
_WRITE_TIMEOUT = 5.0  # seconds

# Safety cap on how long AAC sample-rate detection buffers video before
# giving up on getting 5 real audio intervals and starting anyway with
# whatever's been measured so far (or the default guess, if audio never
# arrived at all) -- so a camera that claims AAC but doesn't actually
# deliver a steady audio stream can't block start()/mpv forever.
_AAC_DETECTION_VIDEO_FRAME_CAP = 60

# Inter-frame intervals to collect before locking the AAC sample rate.
# Odd, because the rate comes from their median: these are wall-clock
# arrival times measured on the one event loop that also serves every
# other camera, so a single scheduling hiccup is normal. The rates sit
# close together (48000 vs 44100 is 8.8% apart, about 0.9ms of interval
# at 48kHz), so a mean would let one late frame pick the wrong one and
# play the whole session at the wrong pitch.
_AAC_DETECTION_INTERVALS = 11

# ffmpeg -f value for each video codec DSM reports.
_FFMPEG_VIDEO_FORMAT = {"H264": "h264", "H265": "hevc"}
# ffmpeg input args for each audio codec DSM reports -- anything else
# falls back to the raw-video-only pipe rather than risk muxing a format
# never verified. PCMU is passed straight through (raw mulaw); AAC needs
# each frame transformed first (see _AAC_AUDIO_CODECS/aac.py) before
# ffmpeg's plain ADTS "aac" demuxer can read it.
_FFMPEG_AUDIO_ARGS = {
    "PCMU": ["-f", "mulaw", "-ar", "8000", "-ac", "1"],
    "MPEG4-GENERIC": ["-f", "aac"],
}
# Audio codecs that need per-frame transformation (DSM's RFC 3640
# AU-header stripped, a synthesized ADTS header prepended) rather than
# PCMU's raw passthrough. Confirmed against one real camera's "AAC-hbr"
# framing (the common default for IP camera RTP audio) -- a second
# camera model reporting the same adoCodec used different framing
# entirely, so this is known to not cover every AAC camera yet.
_AAC_AUDIO_CODECS = frozenset({"MPEG4-GENERIC"})

# AAC detection can buffer up to _AAC_DETECTION_VIDEO_FRAME_CAP frames
# before anything starts draining them, easily exceeding Linux's default
# 64KiB pipe buffer (H.265 keyframes alone can run into the hundreds of
# KB) and blocking our writer on a reader that hasn't attached yet.
# Growing the buffer to the system max (still unprivileged -- see
# /proc/sys/fs/pipe-max-size) gives the flush headroom to finish first.
_PIPE_CAPACITY = 1024 * 1024


# Linux-only (>= 2.6.35); CPython defines it only where the platform header
# does. Looking it up on the fcntl module directly raises AttributeError on
# the BSDs, which contextlib.suppress(OSError) would not catch.
_F_SETPIPE_SZ = getattr(fcntl, "F_SETPIPE_SZ", None)


def _grow_pipe_buffer(fd: int) -> None:
    if _F_SETPIPE_SZ is None:
        return  # BSD pipe buffers are not tunable from userland
    with contextlib.suppress(OSError):
        fcntl.fcntl(fd, _F_SETPIPE_SZ, _PIPE_CAPACITY)


class _StreamStalled(Exception):
    """Raised when a connected stream stops delivering data (idle timeout).

    Distinct from the TimeoutError that connect()'s own open_timeout raises,
    so _pump can keep the stall reason without mistaking a failed handshake
    for a stall.
    """


class _PipeWriteStalled(Exception):
    """Raised when a pipe write can't complete because the downstream
    reader (ffmpeg, or mpv on the raw-video-only pipe) has stopped
    draining it."""


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


def _parse_header(header: bytes) -> dict[str, str]:
    """Parse the Synology WebSocket frame's ASCII '&'-joined key=value header."""
    text = header.decode("ascii", errors="replace")
    out: dict[str, str] = {}
    for part in text.split("&"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k] = v
    return out


class WebSocketBridge:
    """Bridge a WebSocket video (+ optional audio) stream to a pipe for mpv."""

    def __init__(self, ws_url: str, verify_ssl: bool, sid: str, label: str = "") -> None:
        self._ws_url = ws_url
        self._verify_ssl = verify_ssl
        self._sid = sid
        # Purely for logging — lets a "dropped"/"stalled"/"gave up" line be
        # traced back to a specific camera after the fact, since the bridge
        # itself only ever sees a bare URL.
        self._label = label or ws_url
        self._read_fd: int = -1
        self._video_write_fd: int = -1
        self._audio_write_fd: int = -1
        self._ffmpeg_proc: asyncio.subprocess.Process | None = None
        self._audio_active = False
        self._audio_codec: str = ""
        self._ready_event = asyncio.Event()
        self._last_audio_write_time: float | None = None
        # AAC's real sample rate isn't in the codec-info frame anywhere,
        # but ffmpeg's Matroska muxer needs a correct, stable rate from
        # its very first probe to write valid output -- a wrong initial
        # guess that self-corrects a few frames in still poisons the
        # muxer's extradata detection. So video+audio are buffered here
        # (see _aac_detecting) until real inter-frame timing reveals the
        # rate, and only then does ffmpeg start.
        self._aac_sample_rate = 16000
        # RFC 3640 AAC-hbr default; overwritten by detect_au_header_len()
        # in _finish_aac_detection before any real frame is ever stripped.
        self._au_header_len = 2
        self._aac_intervals: list[float] = []
        self._aac_detecting = False
        self._pending_video_codec: str = ""
        self._aac_video_buffer: list[bytes] = []
        self._aac_audio_buffer: list[bytes] = []
        self._fd_lock = threading.Lock()
        self._pump_task: asyncio.Task[None] | None = None
        self._error: str = ""
        self._stopping = False
        self._connected_at: float | None = None
        self._fast_failures = 0
        self._attempt_got_data = False

    @property
    def audio_active(self) -> bool:
        """Whether this session ended up muxing real audio in.

        Only meaningful once the first codec-info frame has arrived (see
        _setup_pipes) — False before that, and permanently False for a
        camera whose audio codec isn't muxable (see _FFMPEG_AUDIO_ARGS)
        or that has no audio at all.
        """
        return self._audio_active

    def _note_attempt_outcome(self, connected: bool, attempt_start: float) -> bool:
        """Track consecutive failed-to-connect attempts; return True to give up.

        A connection that stayed up a little while *and delivered data* is a
        fresh, healthy attempt — only a run of failures that never establish
        a working stream should give up, so a camera that is genuinely
        unreachable doesn't retry forever.

        Uptime alone is not enough to call an attempt healthy: the idle
        timeout is longer than the fast-failure threshold, so a socket the
        NAS accepts but never feeds would score as healthy on every pass,
        reset the streak forever, and leave start() waiting on a codec-info
        frame that is never coming.
        """
        attempt_uptime = (
            time.monotonic() - attempt_start if connected and self._attempt_got_data else 0.0
        )
        if attempt_uptime >= _FAST_FAILURE_THRESHOLD:
            self._fast_failures = 0
            return False
        self._fast_failures += 1
        if self._fast_failures < _MAX_CONSECUTIVE_FAST_FAILURES:
            return False
        if not self._error:
            self._error = "repeated connection failures"
        log.error(
            "WebSocket for %s failed to establish %d times in a row — giving up: %s",
            self._label,
            self._fast_failures,
            self._error,
        )
        return True

    async def _setup_pipes(self, video_codec: str, audio_codec: str) -> None:
        """One-time setup on the first codec-info frame of the bridge's
        lifetime: decide whether DSM's audio track can be muxed in, and
        create whatever pipe(s) mpv will read from.

        If audio can't be muxed (an unrecognized codec, or no audio at
        all), falls back to the original raw-video-only passthrough —
        mpv auto-detects H.264/H.265 straight from the Annex B stream,
        no container needed — so a camera without usable audio sees no
        behavior change at all from before this feature existed.

        AAC is a third case: video+audio are buffered rather than piped
        anywhere yet, until real frame timing reveals the sample rate
        (see _accumulate_aac_detection_frame) — ffmpeg only starts once
        that's known, so `start()` (and mpv) stay blocked a little
        longer for these cameras specifically.
        """
        self._audio_codec = audio_codec
        if video_codec in _FFMPEG_VIDEO_FORMAT and audio_codec in _AAC_AUDIO_CODECS:
            self._pending_video_codec = video_codec
            self._aac_detecting = True
            log.debug(
                "WebSocket bridge for %s: detecting AAC sample rate before muxing", self._label
            )
            return
        muxable = video_codec in _FFMPEG_VIDEO_FORMAT and audio_codec in _FFMPEG_AUDIO_ARGS
        if muxable:
            try:
                await self._start_muxed(video_codec, audio_codec)
            except OSError:
                # No ffmpeg on PATH, or it could not be executed. Audio is
                # the optional half here: this camera played video-only
                # before the muxer existed, so drop to that rather than
                # taking the whole stream down with it.
                log.warning(
                    "WebSocket bridge for %s: cannot run ffmpeg, streaming video without audio",
                    self._label,
                )
                muxable = False
        if not muxable:
            self._read_fd, self._video_write_fd = os.pipe()
            self._audio_active = False
        log.debug(
            "WebSocket bridge pipe for %s: fd://%d (audio_active=%s)",
            self._label,
            self._read_fd,
            self._audio_active,
        )
        self._ready_event.set()

    async def _start_muxed(self, video_codec: str, audio_codec: str) -> None:
        """Spawn ffmpeg to mux raw video NALs + raw audio samples (fed in
        live via separate input pipes) into a Matroska stream on stdout,
        which becomes the pipe mpv actually plays.

        -thread_queue_size raises ffmpeg's default per-input packet queue
        (8), far too small for this bursty live-piped setup — once full,
        ffmpeg stops draining that input's pipe, and once the pipe's own
        OS buffer fills too, our write to it blocks forever.

        -use_wallclock_as_timestamps is set on both inputs, and has to
        be. Raw Annex B NALs carry no timing, so the host clock is the
        only timeline video can have; putting audio on that same clock
        is what stops the two from diverging. Deriving audio timestamps
        from the byte count instead (as this did for PCMU, whose 8kHz
        mulaw is fixed-rate enough that ffmpeg can) paces audio off the
        camera's sampling clock while video runs off ours, so any offset
        between the two, plus every dropped audio packet, shifts the
        audio timeline permanently earlier. mpv paces playback off the
        audio track, so that shift is not a one-off skew: it is a
        playback rate slower than the arrival rate, and the backlog
        grows for as long as the stream runs.

        aresample=async=1000 then covers what the byte count used to.
        It resamples away the millisecond-scale jitter our own
        asyncio/thread-pool scheduling leaves in the wallclock stamps,
        and only falls back to inserting silence for a gap large enough
        to be a genuine packet loss. Both codecs decode to PCM for it,
        since a filter cannot run on a copied stream.

        The video -probesize is deliberately large (2MB, versus 16KB for
        audio): a single H.265 keyframe from an 8MP/4K-class camera can
        exceed 500KB on its own, and a too-small probesize leaves
        ffmpeg's stream analysis unable to get past the first real frame.
        """
        video_r = video_w = audio_r = audio_w = out_r = out_w = -1
        try:
            video_r, video_w = os.pipe()
            audio_r, audio_w = os.pipe()
            out_r, out_w = os.pipe()
            for fd in (video_r, video_w, audio_r, audio_w, out_r, out_w):
                _grow_pipe_buffer(fd)
            await self._spawn_ffmpeg(video_codec, audio_codec, video_r, audio_r, out_w)
        except OSError:
            # Either a pipe or the spawn itself failed. Whichever fds exist
            # have no subprocess to inherit them now, and this runs again on
            # every reconnect attempt until the bridge gives up, so leaking
            # them here would compound quickly.
            for fd in (video_r, video_w, audio_r, audio_w, out_r, out_w):
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            raise
        os.close(video_r)
        os.close(audio_r)
        os.close(out_w)
        self._video_write_fd = video_w
        self._audio_write_fd = audio_w
        self._read_fd = out_r
        self._audio_active = True

    async def _spawn_ffmpeg(
        self, video_codec: str, audio_codec: str, video_r: int, audio_r: int, out_w: int
    ) -> None:
        """Build ffmpeg's argument list around the caller's pipe fds and
        start it. Kept apart from _start_muxed only so the fd cleanup there
        covers the pipes and the spawn under one except OSError."""
        audio_args = [
            "-probesize",
            "16384",
            "-analyzeduration",
            "300000",
            "-use_wallclock_as_timestamps",
            "1",
            "-thread_queue_size",
            "4096",
            *_FFMPEG_AUDIO_ARGS[audio_codec],
            "-i",
            f"pipe:{audio_r}",
        ]
        args = [
            "ffmpeg",
            "-loglevel",
            "warning",
            "-nostdin",
            "-probesize",
            "2000000",
            "-analyzeduration",
            "300000",
            "-use_wallclock_as_timestamps",
            "1",
            "-thread_queue_size",
            "4096",
            "-f",
            _FFMPEG_VIDEO_FORMAT[video_codec],
            "-i",
            f"pipe:{video_r}",
            *audio_args,
            "-c:v",
            "copy",
            # Decoding to PCM is what lets -af run at all, and it is
            # required for AAC regardless: stream-copying it straight
            # from ffmpeg's ADTS demuxer into Matroska fails outright
            # however correct the ADTS headers are (confirmed live:
            # "Error parsing AAC extradata, unable to determine
            # samplerate" / "Could not write header" even with a
            # verified-correct, consistent sample rate from the very
            # first frame), because that demuxer does not populate the
            # extradata Matroska's muxer needs for -c:a copy.
            "-c:a",
            "pcm_s16le",
            # Keeps the audio timeline glued to the wallclock stamps
            # without passing our scheduling jitter through as clicks.
            # See _start_muxed for why both inputs are on one clock.
            "-af",
            "aresample=async=1000",
            "-f",
            "matroska",
            "-live",
            "1",
            "-flush_packets",
            "1",
            "pipe:1",
        ]
        self._ffmpeg_proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=subprocess.DEVNULL,
            stdout=out_w,
            stderr=subprocess.DEVNULL,
            pass_fds=[video_r, audio_r],
        )

    async def _handle_control_frame(self, fields: dict[str, str], header: bytes) -> None:
        """Handle a close notice or codec-info frame (anything that isn't
        a video/audio payload) — pipe/ffmpeg setup happens here, once,
        the first time codec info arrives for this bridge's lifetime."""
        if "close" in fields:
            log.debug("WebSocket stream close: %s", header.decode(errors="replace"))
            return
        if self._read_fd < 0 and not self._aac_detecting:
            await self._setup_pipes(fields.get("vdoCodec", ""), fields.get("adoCodec", ""))

    async def _handle_pcmu_audio_frame(self, payload: bytes) -> None:
        """Write a real PCMU audio payload to ffmpeg's audio input."""
        await asyncio.to_thread(self._write_pipe, True, payload)

    async def _handle_aac_audio_frame(self, payload: bytes) -> None:
        """Write a real AAC frame to ffmpeg's audio input, after
        stripping DSM's RFC 3640 AU-header and prepending a synthesized
        ADTS header (see aac.py) — ffmpeg's plain "aac" demuxer needs
        ADTS framing, not the bare AU-header-prefixed frames DSM sends.

        The sample rate and AU-header length are already known by the
        time this ever runs (detection happens before ffmpeg starts at
        all — see _accumulate_aac_detection_frame).
        """
        frame = strip_au_header(payload, self._au_header_len)
        header = adts_header(len(frame), self._aac_sample_rate)
        await asyncio.to_thread(self._write_pipe, True, header + frame)

    async def _accumulate_aac_detection_frame(self, payload: bytes) -> None:
        """Buffer a raw (still AU-header-prefixed) AAC frame while
        determining the camera's real sample rate from real inter-frame
        timing — called instead of _handle_aac_audio_frame until the
        rate locks in and ffmpeg actually starts (see _setup_pipes)."""
        now = time.monotonic()
        if self._last_audio_write_time is not None:
            interval = now - self._last_audio_write_time
            if 0 < interval < 0.5:  # skip anything spanning a reconnect gap
                self._aac_intervals.append(interval)
        self._last_audio_write_time = now
        self._aac_audio_buffer.append(payload)
        if len(self._aac_intervals) >= _AAC_DETECTION_INTERVALS:
            await self._finish_aac_detection()

    async def _aac_frames_look_valid(self) -> bool:
        """Quick sanity check: does our AU-header-strip + ADTS-header
        transform actually produce decodable AAC for this camera?

        detect_au_header_len (see _finish_aac_detection) already rules
        out a wrong AU-header length; this is the secondary check for
        cameras using different framing entirely -- feeding ffmpeg the
        wrongly-transformed result doesn't just produce bad audio, it
        stalls the whole muxed pipeline outright. This catches that with
        a throwaway decode attempt before ever committing to a real
        session, so an unsupported camera falls back to video-only
        instead of a broken one.
        """
        buf = bytearray()
        try:
            for raw in self._aac_audio_buffer:
                frame = strip_au_header(raw, self._au_header_len)
                buf += adts_header(len(frame), self._aac_sample_rate) + frame
        except ValueError:
            # A frame too long for an ADTS header to describe means this
            # camera is not using the framing we assume -- which is exactly
            # what this check exists to catch, so treat it as "not ours"
            # rather than letting it escape and kill the pump.
            log.info(
                "WebSocket bridge for %s: AAC frames are not in the expected framing",
                self._label,
            )
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "aac",
                "-i",
                "pipe:0",
                "-f",
                "null",
                "-",
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError:
            # Can't validate without ffmpeg; say yes and let _start_muxed's
            # own OSError handling drop us to video-only.
            return True
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(bytes(buf)), timeout=3.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return False
        return not stderr.strip()

    async def _fall_back_to_video_only(self) -> None:
        """Give up on muxing this camera's AAC in — its framing didn't
        validate (see _aac_frames_look_valid) — and use the original
        raw-video-only passthrough instead, flushing the video buffered
        during detection into it.

        Unlike the muxed path (where ffmpeg is already running and
        draining its input pipes before any flush happens), nothing
        reads this pipe until mpv opens it, which only happens once
        start() returns and the caller acts on it — which only happens
        once _ready_event is set. Flushing buffered frames before
        setting the event would fill the pipe with no reader ever
        coming, a permanent deadlock. Setting the event first lets the
        event loop hand control back to mpv (via the asyncio.to_thread
        yield points in the flush loop below) before that can happen.
        """
        log.warning(
            "WebSocket bridge for %s: this camera's AAC framing doesn't match the "
            "AU-header transform this app knows — falling back to video-only audio",
            self._label,
        )
        self._read_fd, self._video_write_fd = os.pipe()
        _grow_pipe_buffer(self._read_fd)
        _grow_pipe_buffer(self._video_write_fd)
        self._audio_active = False
        self._ready_event.set()
        for nal in self._aac_video_buffer:
            await asyncio.to_thread(self._write_pipe, False, nal)
        self._aac_video_buffer.clear()
        self._aac_audio_buffer.clear()

    async def _finish_aac_detection(self) -> None:
        """Lock in the detected (or, failing that, default) AAC sample
        rate and AU-header length, verify the resulting AU-header-strip
        + ADTS-header transform actually produces valid AAC for this
        camera, then either start the muxed ffmpeg pipeline or fall back
        to video-only — flushing everything buffered during detection
        either way."""
        if self._aac_intervals:
            self._aac_sample_rate = nearest_sample_rate(median(self._aac_intervals))
        self._aac_detecting = False
        # Detection is over either way. Left populated, these would make a
        # reconnect that re-enters detection finish instantly off the
        # previous session's measurements.
        self._aac_intervals.clear()
        self._last_audio_write_time = None

        header_len = detect_au_header_len(self._aac_audio_buffer)
        if header_len is None:
            log.info(
                "WebSocket bridge for %s: AAC frames are not in a recognized AU-header framing",
                self._label,
            )
            await self._fall_back_to_video_only()
            return
        if header_len != self._au_header_len:
            log.debug(
                "WebSocket bridge for %s: detected a %d-byte AU-header (default is 2)",
                self._label,
                header_len,
            )
        self._au_header_len = header_len

        if not await self._aac_frames_look_valid():
            await self._fall_back_to_video_only()
            return

        log.debug(
            "WebSocket bridge for %s: AAC sample rate %dHz, starting muxed pipeline "
            "(%d buffered video, %d buffered audio frames)",
            self._label,
            self._aac_sample_rate,
            len(self._aac_video_buffer),
            len(self._aac_audio_buffer),
        )
        try:
            await self._start_muxed(self._pending_video_codec, self._audio_codec)
        except OSError:
            log.warning(
                "WebSocket bridge for %s: cannot run ffmpeg, streaming video without audio",
                self._label,
            )
            await self._fall_back_to_video_only()
            return
        # Signal readiness before flushing, not after: mpv doesn't open
        # ffmpeg's muxed output pipe until start() returns, which only
        # happens once _ready_event is set. If nothing reads that output
        # while we flush a large buffer, ffmpeg's stdout write blocks
        # once its pipe fills, which stops it draining our input pipes,
        # which then blocks our flush writes too -- a backpressure
        # deadlock through ffmpeg. Setting the event first lets the
        # event loop hand control to mpv (via the asyncio.to_thread/sleep
        # yield points below) concurrently with the flush.
        self._ready_event.set()
        # Both buffers drain together, not video-then-audio: ffmpeg probes
        # its inputs in order and will not start draining the video pipe
        # until the audio input has satisfied -probesize/-analyzeduration.
        # Writing every buffered NAL first can therefore fill the video
        # pipe against an ffmpeg that is still waiting for audio that this
        # coroutine has not sent yet, wedging the slot with no error. It
        # also keeps wall-clock timestamps aligned, since both inputs use
        # -use_wallclock_as_timestamps.
        await asyncio.gather(self._flush_aac_video(), self._flush_aac_audio())

    async def _flush_aac_video(self) -> None:
        """Drain the video buffered during detection, paced at ~25fps.

        Not dumped instantly: -use_wallclock_as_timestamps means ffmpeg
        derives frame timing from real elapsed time between writes, and a
        whole buffer written with near-zero time between frames looks like
        a degenerate rate to its estimation and can stall it entirely.
        """
        for nal in self._aac_video_buffer:
            await asyncio.to_thread(self._write_pipe, False, nal)
            await asyncio.sleep(0.04)
        self._aac_video_buffer.clear()

    async def _flush_aac_audio(self) -> None:
        """Drain the audio buffered during detection, paced at the frame
        duration implied by the sample rate just detected."""
        audio_frame_duration = 1024 / self._aac_sample_rate
        for buffered_payload in self._aac_audio_buffer:
            await self._handle_aac_audio_frame(buffered_payload)
            await asyncio.sleep(audio_frame_duration)
        self._aac_audio_buffer.clear()

    async def _handle_video_frame(self, nal: bytes) -> None:
        """Route a video NAL to ffmpeg's input, or buffer it if still
        waiting on AAC sample-rate detection (see _setup_pipes)."""
        if self._aac_detecting:
            self._aac_video_buffer.append(nal)
            if len(self._aac_video_buffer) >= _AAC_DETECTION_VIDEO_FRAME_CAP:
                await self._finish_aac_detection()
        else:
            await asyncio.to_thread(self._write_pipe, False, nal)

    async def _dispatch_audio_frame(self, payload: bytes) -> None:
        """Route a real audio payload to whichever handler matches the
        current codec/detection state."""
        if self._aac_detecting:
            await self._accumulate_aac_detection_frame(payload)
        elif self._audio_codec in _AAC_AUDIO_CODECS:
            await self._handle_aac_audio_frame(payload)
        else:
            await self._handle_pcmu_audio_frame(payload)

    async def _read_messages(self, ws: Any) -> None:
        """Read messages until the connection ends, routing video/audio
        payloads to the appropriate pipe(s) and setting up pipes/ffmpeg on
        the very first codec-info frame this bridge ever sees.

        Behaves like a bare `async for message in ws:` — raises whatever
        the connection raises — except a stall (no message at all for
        _IDLE_TIMEOUT) also raises, with self._error left set to a
        distinctly greppable reason first.
        """
        while True:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=_IDLE_TIMEOUT)
            except TimeoutError:
                self._error = f"stalled: no data for {_IDLE_TIMEOUT:.0f}s"
                raise _StreamStalled(self._error) from None
            self._attempt_got_data = True
            if not isinstance(message, bytes) or len(message) < 4:
                continue
            (hdr_len,) = struct.unpack(">I", message[:4])
            if 4 + hdr_len > len(message):
                continue
            header = message[4 : 4 + hdr_len]
            payload = message[4 + hdr_len :]
            fields = _parse_header(header)

            if "close" in fields or "vdoCodec" in fields or "adoCodec" in fields:
                await self._handle_control_frame(fields, header)
                continue

            if (self._read_fd < 0 and not self._aac_detecting) or not payload:
                continue  # haven't seen codec info yet, or an empty frame

            media_type = fields.get("mediaType")
            if media_type == "1":
                # The Synology header embeds the Annex B start code
                # (00 00 00 01) as its last 4 bytes — prepend it so
                # mpv/ffmpeg can detect NAL boundaries.
                await self._handle_video_frame(b"\x00\x00\x00\x01" + payload)
            elif media_type == "2" and (self._audio_active or self._aac_detecting):
                await self._dispatch_audio_frame(payload)

    async def _send_keepalive_loop(self, ws: Any) -> None:
        """Send a keepalive every _KEEPALIVE_INTERVAL for as long as the
        connection lives."""
        while True:
            await asyncio.sleep(_KEEPALIVE_INTERVAL)
            await ws.send("keepAlive")

    async def _read_messages_with_keepalive(self, ws: Any) -> None:
        """Run the read loop and the keepalive loop concurrently;
        whichever raises first ends the connection, and the other is
        cancelled and reaped so a cancellation here can't leak either
        task."""
        read_task = asyncio.create_task(self._read_messages(ws))
        keepalive_task = asyncio.create_task(self._send_keepalive_loop(ws))
        tasks = {read_task, keepalive_task}
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        for t in done:
            t.result()

    def _log_reconnect(self, clean_close: bool) -> None:
        if clean_close:
            log.debug(
                "WebSocket for %s closed cleanly after %.0fs — reconnecting on the same pipe",
                self._label,
                self.uptime,
            )
        else:
            log.warning(
                "WebSocket for %s dropped after %.0fs (%s) — reconnecting on the same pipe",
                self._label,
                self.uptime,
                self._error,
            )

    async def start(self) -> str:
        """Start the pump task and wait for the first codec-info frame —
        which determines whether audio can be muxed in — before returning
        the fd:// URL mpv should play.

        Raises whatever the pump task's last failure was if it gives up
        (see _note_attempt_outcome) before ever becoming ready, rather
        than waiting forever for a camera that never delivers anything.
        """
        self._pump_task = asyncio.create_task(self._pump())
        ready_task = asyncio.create_task(self._ready_event.wait())
        await asyncio.wait({ready_task, self._pump_task}, return_when=asyncio.FIRST_COMPLETED)
        if self._ready_event.is_set():
            ready_task.cancel()
            return f"fd://{self._read_fd}"
        ready_task.cancel()
        raise RuntimeError(self._error or "WebSocket bridge exited before becoming ready")

    async def _pump(self) -> None:
        """Connect to the WebSocket and write video (+ audio) frames to
        the pipe(s).

        Reconnects internally on the same pipe(s) whenever the session
        drops instead of closing them — see the comment at the reconnect
        site for why. Only exits (letting the pipe close and
        `wait_closed()` return a reason) on a deliberate stop or after
        repeated attempts that never establish a real connection at all.
        """
        ssl_ctx: ssl.SSLContext | bool | None = None
        if self._ws_url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if not self._verify_ssl:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

        from websockets.exceptions import ConnectionClosedOK  # noqa: PLC0415

        headers = {"Cookie": f"id={self._sid}"}
        delay = 0.0

        try:
            while not self._stopping:
                clean_close = False
                connected = False
                give_up_now = False
                self._attempt_got_data = False
                attempt_start = time.monotonic()
                try:
                    log.debug("WebSocket connecting for %s: %s", self._label, self._ws_url)

                    async with _ws_connect(
                        self._ws_url,
                        ssl=ssl_ctx,
                        additional_headers=headers,
                        max_size=2**22,
                        open_timeout=15,
                        close_timeout=2,
                        ping_interval=None,
                    ) as ws:
                        log.debug("WebSocket connected for %s", self._label)
                        connected = True
                        self._connected_at = time.monotonic()
                        delay = 0.0
                        await self._read_messages_with_keepalive(ws)
                except _PipeWriteStalled as exc:
                    # Unlike a WS-level drop, reconnecting on the same pipe
                    # can't help here -- the downstream reader (ffmpeg, or
                    # mpv on the raw-video pipe) is what's stuck, not the
                    # socket. Give up on this bridge immediately so the
                    # caller tears down and rebuilds the whole pipeline
                    # (fresh ffmpeg, fresh pipes, fresh mpv play()) instead
                    # of endlessly refeeding a pipe that will only stall
                    # again.
                    self._error = str(exc)
                    give_up_now = True
                except ConnectionClosedOK:
                    # Server closed cleanly (e.g. code 1005 "no status received").
                    clean_close = True
                except _StreamStalled:
                    pass  # self._error already holds the stall reason
                except Exception as exc:
                    self._error = _classify_error(exc)

                if self._stopping:
                    break

                if give_up_now or self._note_attempt_outcome(connected, attempt_start):
                    break

                # Reconnect on the SAME pipe(s) rather than closing them,
                # whether the session ended cleanly or with an error.
                # Closing the write end here would deliver a real EOF to
                # mpv, which — with keep_open=yes on a raw fd:// stream —
                # never resumes decoding again even after a fresh play()
                # call on a new pipe. Keeping the pipe(s) open and just
                # resuming writes after a short reconnect makes this
                # look like an ordinary buffering stall to mpv instead
                # of a terminal end-of-file, so it recovers on its own
                # with no player/render-context teardown needed at all.
                self._log_reconnect(clean_close)
                delay = min(delay * 2, _MAX_RECONNECT_DELAY) if delay else 0.25
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
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

    def _write_pipe(self, audio: bool, data: bytes) -> None:
        """Write to one of the pipes through a private copy of the fd.

        Runs in a worker thread. The descriptor is read and duplicated
        under the lock _close_write_fd() takes, so a teardown racing this
        cannot close it between the read and the write(2) -- the next
        bridge's os.pipe() gets the same numbers back, so a late write
        would land in another camera's stream, or in whatever else
        happened to claim the number.

        A duplicate rather than holding the lock across the write: a write
        to a pipe mpv has not drained blocks until it does, and
        close_write_end() is called from the GTK main thread.

        Non-blocking with its own timeout (_WRITE_TIMEOUT) rather than a
        plain blocking os.write(): a pipe whose reader has stopped
        draining it (ffmpeg or mpv wedged downstream) would otherwise
        block here forever, with no way back to ws.recv() and so no way
        to ever raise, reconnect, or hand off to the stream-lost recovery
        path that's built for exactly this.
        """
        with self._fd_lock:
            fd = self._audio_write_fd if audio else self._video_write_fd
            if fd < 0:
                return
            dup = os.dup(fd)
        try:
            os.set_blocking(dup, False)
            view = memoryview(data)
            deadline = time.monotonic() + _WRITE_TIMEOUT
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _PipeWriteStalled(
                        f"pipe write stalled for {_WRITE_TIMEOUT:.0f}s "
                        "-- downstream reader stopped draining"
                    )
                select.select([], [dup], [], remaining)
                try:
                    n = os.write(dup, view)
                except BlockingIOError:
                    continue
                view = view[n:]
        finally:
            os.close(dup)

    def _close_write_fd(self) -> None:
        """Atomically close the write fd(s). Thread-safe, idempotent."""
        with self._fd_lock:
            vfd, self._video_write_fd = self._video_write_fd, -1
            afd, self._audio_write_fd = self._audio_write_fd, -1
        if vfd >= 0:
            with contextlib.suppress(OSError):
                os.close(vfd)
        if afd >= 0:
            with contextlib.suppress(OSError):
                os.close(afd)

    def close_write_end(self) -> None:
        """Close the write end of the pipe(s) immediately.

        Signals EOF to ffmpeg (if muxing) or mpv (if not) on the read end.
        A thread pool thread already blocked in os.write() on that fd does
        not come back here: on Linux, closing an fd does not interrupt a
        write another thread is inside. It returns once the readers are
        gone, which stop() arranges by closing _read_fd. Safe to call from
        any thread, idempotent.
        """
        self._stopping = True
        self._close_write_fd()

    async def stop(self) -> None:
        """Cancel the pump task, let ffmpeg (if any) drain and exit, and
        close pipe fds."""
        self.close_write_end()

        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task
            self._pump_task = None

        if self._ffmpeg_proc is not None:
            proc = self._ffmpeg_proc
            self._ffmpeg_proc = None
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()

        if self._read_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._read_fd)
            self._read_fd = -1
