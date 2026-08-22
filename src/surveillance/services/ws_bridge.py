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
a codec we know how to handle (currently PCMU/G.711 mu-law, and AAC once
the frame is recovered from what DSM sent, see aac.py) -- mpv's fd://
pipe then reads ffmpeg's own Matroska output instead of the raw Annex B
video stream directly. A camera whose audio is neither (or has none at
all) falls back to the original raw-video-only passthrough, unchanged.

History (recorded) playback (WebSocketBridge's history_recording/
history_target constructor arguments and seek() -- see LiveView for
how a Live View slot drives it) uses this same /ss_webstream_task/
endpoint and 4-byte-length-prefixed message framing as Live. Confirmed
by sniffing DSM's own Monitor Center web client's WebSocket traffic
directly (Chrome DevTools), not by reading its JS. Live and History
are the same `method=MixStream` protocol with a different connect mode
and one extra in-band control message before video/audio frames start:

- Connect params: `blMux=true&browser=2&stmSrc=2&relay_rec_auth=false&
  id=0`. Nothing here identifies a camera or recording -- `id=0` is a
  placeholder, unlike Live's connect-time `id=<cameraId>`.
- Immediately after connecting, one in-band string selects the
  recording and the starting point within it:
  `action=play&method=MixStream&blMux=true&browser=2&stmSrc=1&
  blAudio=true&mute=<bool>&speed=1&reverse=false&
  recEvtType=<eventType>&mountId=<mountId>&archId=<archId>&
  start=<secondsIntoFile>&end=<fileDurationSeconds>&autoDrop=true&
  restart=true&pause=false&stamp=1&id=<recordingId>`. Only `mute`
  varied across captures; every other field held the value shown above
  every time, so whether e.g. `speed`/`reverse`/`blAudio` ever differ
  at connect time (as opposed to via a later in-band message, see
  below) isn't confirmed.
- `recordingId`/`mountId`/`archId`/`eventType` are exactly
  `Recording.id`/`mount_id`/`arch_id`/`event_type` (api/models.py) --
  the same fields services/recording.py's HTTP EventStream path
  already sends as eventId/mountId/archId/recEvtType. Confirmed
  field-for-field against a live capture: the WS `id` matched one
  specific recording's own `id_on_RecServer` in a RecordingPicker/
  Recording.List-shaped API response (complete with that recording's
  own frameCount and start/stop unix timestamps), not a frame number
  or anything per-video-frame. `start`/`end` are seconds *into that
  file*, not wall-clock: `start = target_unix - rec.start_time`,
  `end = rec.stop_time - rec.start_time`. `stamp` is a per-connection
  command sequence counter, starting at 1 and incrementing with every
  later in-band command below.
- A later seek landing within the *same* recording's span reuses the
  connection instead of reconnecting -- a much shorter in-band message,
  `seekMs=<millisecondsIntoFile>&stamp=<n>`. Only a seek crossing into
  a different recording opens a new connection and repeats the
  `action=play` handshake above with that recording's own
  id/mountId/archId/start/end.

Not yet captured: pause/resume, speed changes (including reverse), and
the +-10s/event-jump transport controls -- expected to be either more
seekMs-style in-band messages or trivial client-side math, to be
confirmed the same way (live capture) before implementing them.
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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from surveillance.api.models import Recording

from surveillance.services.aac import AacDetector, adts_header, parse_audio_config

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

# Floor for how close to wall clock any History target may land --
# entering History mode, seeking, or resuming from pause, whichever is
# asking (see _set_history_delta, the single place this is enforced).
# A delta this small is inside the near-live window this bridge/DSM
# can't reliably serve yet.
MIN_HISTORY_DELTA_SECONDS = 10.0

# How long a muxed camera may deliver no audio at all before its audio
# stream is ended to stop it holding up the video (see _watch_audio_gap).
# Must fire before the write timeout above does: the mux stops draining
# video 0.7s into the gap, and _WRITE_TIMEOUT only starts once the 1MiB
# video pipe has filled on top of that, so 3s plus a check interval
# leaves room even on a camera that fills the pipe quickly. Well beyond
# any real inter-frame gap: audio arrives every 20-125ms.
_AUDIO_GAP_TIMEOUT = 3.0  # seconds
_AUDIO_GAP_CHECK_INTERVAL = 0.5  # seconds

# How long to let ffmpeg live before believing it started. An ffmpeg that
# doesn't like its arguments prints its complaint and exits in tens of
# milliseconds, so this only has to outlast that, not cover a slow start.
_FFMPEG_START_GRACE = 0.2  # seconds

# How long AacDetector may buffer video+audio before the bridge starts
# it anyway with whatever's been measured so far (or the default guess,
# if audio never arrived at all) -- AacDetector's own frame/interval
# counts (_AAC_DETECTION_VIDEO_FRAME_CAP/_AAC_DETECTION_INTERVALS in
# aac.py) bound it while frames keep arriving; this is what ends it if
# a camera sending video slowly, or holding the connection open with
# nothing but control frames, never fills either. Well past what a
# healthy camera needs: 11 intervals take about 1.4s even at the lowest
# AAC rate, and 60 video frames about 2.4s at 25fps.
_AAC_DETECTION_TIMEOUT = 10.0  # seconds

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
# Audio codecs that need per-frame transformation (the raw frame
# recovered from what DSM sent, a synthesized ADTS header prepended)
# rather than PCMU's raw passthrough. Two camera models are known, and
# they do not send the frame the same way -- see
# AacDetector.reconstruct_frame (aac.py).
_AAC_AUDIO_CODECS = frozenset({"MPEG4-GENERIC"})

# AAC detection can buffer up to AacDetector's own video-frame cap
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
    """Resize the pipe this descriptor is an end of. Either end will do:
    the buffer is a property of the pipe, not of the descriptor."""
    if _F_SETPIPE_SZ is None:
        return  # BSD pipe buffers are not tunable from userland
    with contextlib.suppress(OSError):
        fcntl.fcntl(fd, _F_SETPIPE_SZ, _PIPE_CAPACITY)


def _set_write_end_nonblocking(fd: int) -> None:
    """Make an end this process writes to non-blocking, once.

    _write_pipe needs O_NONBLOCK to time a wedged reader out instead of
    parking a worker thread on it forever. Setting it there, on the dup,
    would reach this descriptor anyway: status flags live on the open
    file description, which a dup shares. Only our own write ends are
    touched; the ends ffmpeg and mpv read stay as they were.
    """
    os.set_blocking(fd, False)


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

    def __init__(
        self,
        ws_url: str,
        verify_ssl: bool,
        sid: str,
        label: str = "",
        history_recording: Recording | None = None,
        history_target: int = 0,
        history_resolver: Callable[[int], Awaitable[Recording | None]] | None = None,
        history_speed: str = "1",
        history_reverse: bool = False,
    ) -> None:
        self._ws_url = ws_url
        self._verify_ssl = verify_ssl
        self._sid = sid
        # History (recorded) playback -- see the module docstring for the
        # wire protocol. None means an ordinary Live bridge; ws_url is
        # still built by the caller either way (get_live_view_path /
        # a future history equivalent), same as it always has been.
        self._history_recording = history_recording
        # Looks up whatever recording covers a given target_unix (a
        # thin wrapper around find_recording_at -- this class has no
        # API access of its own). Only needed for a target that has
        # drifted past self._history_recording's own span (see
        # _refresh_history_recording_if_stale); a Live bridge, or a
        # History one that's never outlived its original recording,
        # never calls it at all.
        self._history_resolver = history_resolver
        # Stored as "seconds behind the wall clock" rather than the
        # absolute target itself, so a reconnect long after the initial
        # seek -- this bridge's own History sessions tend to get
        # dropped after roughly two minutes even with nothing wrong
        # client-side, most likely because it hasn't yet found whatever
        # DSM's own web client does to keep one open indefinitely --
        # computes where playback should have reached by now instead of
        # rewinding to the original target every time (see
        # _current_history_target/_build_history_play_message). Set via
        # _set_history_delta, not assigned directly, even here in the
        # constructor -- entering History mode by clicking within
        # MIN_HISTORY_DELTA_SECONDS of live must clamp exactly like a
        # seek()/resume() landing there does.
        self._history_delta_seconds: float = 0.0
        self._set_history_delta(history_target)
        self._history_stamp = 0
        # True while pause()/resume() has this bridge paused -- Live and
        # History act on it differently (see both methods' docstrings),
        # so it means "don't write frames to the pipe" for one and "DSM
        # was asked to stop sending" for the other.
        self._paused = False
        # The position _current_history_target() freezes at while
        # self._paused, instead of continuing to advance -- None
        # whenever not paused. Set by pause(), cleared by resume().
        self._history_paused_position: int | None = None
        # History playback speed multiplier, as the literal string DSM
        # expects (see _history_play_params) -- history_speed lets a
        # slot freshly entering History mid-layout start at whatever
        # speed the rest of the layout is already at, matching the
        # scope every other timeline control shares, rather than
        # always resetting to 1x (meaningless for Live, which never
        # sets it). Persists across this bridge's own periodic
        # reconnects the same way self._paused does, until a later
        # set_speed() changes it.
        self._speed: str = history_speed
        # History playback direction -- see history_speed's own
        # comment, same reasoning applies (a slot freshly entering
        # History mid-layout starts in reverse if the rest of the
        # layout already is, and this persists across reconnects the
        # same way).
        self._reverse: bool = history_reverse
        # Ground truth for _current_history_target(), taken from the
        # most recent video frame's own msec header field (see
        # _dispatch_media_frame) rather than derived purely from
        # self._history_delta_seconds/wall clock -- accurate at any
        # speed, where the delta-based estimate is only ever right at
        # 1x. None whenever nothing has arrived yet for whatever is
        # currently loaded (a fresh connect/reconnect, a seek, a
        # recording swap, or a resume from pause), all of which reset
        # it so a stale value from before that point is never reused.
        self._last_video_msec: int | None = None
        # Highest real (frame-derived) absolute position seen since the
        # last consume_last_real_tick() call -- for LiveView's shared
        # timeline marker, which cares about "did any active camera
        # actually deliver real data in the last second" across every
        # slot in the layout, not just this bridge's own position (see
        # consume_last_real_tick's own docstring). Deliberately separate
        # from self._last_video_msec, which this bridge keeps resetting
        # to None on its own reconnects/seeks for its own purposes --
        # this one only ever moves forward, and only LiveView clears it.
        self._last_real_tick: int | None = None
        # The currently connected socket, for seek() to send on from
        # outside _pump's own scope -- None whenever no connection is up
        # (including between reconnect attempts), so seek() knows to fold
        # a cross-recording seek into the next reconnect instead of
        # sending on a dead socket.
        self._current_ws: Any = None
        # Purely for logging — lets a "dropped"/"stalled"/"gave up" line be
        # traced back to a specific camera after the fact, since the bridge
        # itself only ever sees a bare URL.
        self._label = label or ws_url
        self._read_fd: int = -1
        self._video_write_fd: int = -1
        self._audio_write_fd: int = -1
        self._ffmpeg_proc: asyncio.subprocess.Process | None = None
        self._ffmpeg_watch: asyncio.Task[None] | None = None
        self._ffmpeg_stderr: asyncio.Task[None] | None = None
        self._audio_gap_watch: asyncio.Task[None] | None = None
        # When each stream last reached its pipe. Both armed when the
        # muxer starts, so a camera that announces an audio codec and then
        # never sends one is caught by the same deadline as one that goes
        # quiet, and neither reads as stale before anything has arrived.
        self._last_audio_at = 0.0
        self._last_video_at = 0.0
        # Set while a video write is blocked, which freezes the stamp
        # above for as long as it lasts (see _write_pipe).
        self._video_write_in_flight = False
        self._audio_active = False
        self._audio_codec: str = ""
        self._ready_event = asyncio.Event()
        # ffmpeg's Matroska muxer needs a correct, stable rate from its
        # very first probe to write valid output -- a wrong initial guess
        # that self-corrects a few frames in still poisons the muxer's
        # extradata detection. Some cameras give the real rate up front,
        # in the codec-info payload (see AacDetector.set_config_from_header);
        # the rest need real inter-frame timing to reveal it. Either way
        # video+audio are buffered (see AacDetector) until framing
        # detection finishes, and only then does ffmpeg start: knowing
        # the rate early does not let a camera skip that wait. One
        # instance per bridge, reused for its whole lifetime -- see
        # aac.py's AacDetector.
        self._aac = AacDetector()
        self._pending_video_codec: str = ""
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
        _setup_pipes) — False before that, and False for a camera whose
        audio codec isn't muxable (see _FFMPEG_AUDIO_ARGS) or that has no
        audio at all. Can also go False mid-session, when a camera stops
        sending audio and the gap watchdog ends the stream, so a caller
        showing audio controls has to read it again rather than once.
        """
        return self._audio_active

    @property
    def is_history(self) -> bool:
        """Whether this bridge is playing recorded video (History mode)
        rather than a camera's live stream -- set for the bridge's whole
        lifetime by the history_recording constructor argument, not
        something a caller flips after the fact (see seek() to change
        what's playing within an existing History bridge)."""
        return self._history_recording is not None

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

    async def _setup_pipes(
        self, video_codec: str, audio_codec: str, audio_extra: str, payload: bytes
    ) -> None:
        """One-time setup on the first codec-info frame of the bridge's
        lifetime: decide whether DSM's audio track can be muxed in, and
        create whatever pipe(s) mpv will read from.

        If audio can't be muxed (an unrecognized codec, or no audio at
        all), falls back to the original raw-video-only passthrough —
        mpv auto-detects H.264/H.265 straight from the Annex B stream,
        no container needed — so a camera without usable audio sees no
        behavior change at all from before this feature existed.

        AAC is a third case: video+audio are buffered rather than piped
        anywhere yet, until the sample rate and channel count are known
        (see AacDetector.feed_audio) — ffmpeg only starts once that's
        known, so `start()` (and mpv) stay blocked a little longer for
        these cameras specifically. Detection itself is still needed
        regardless of whether parse_audio_config below holds: it decides
        how a raw frame is reconstructed from what DSM sent (see
        AacDetector.reconstruct_frame), which the payload here says
        nothing about. Bounded three ways: enough intervals,
        AacDetector's own video-frame cap, or _AAC_DETECTION_TIMEOUT.
        """
        self._audio_codec = audio_codec
        if video_codec in _FFMPEG_VIDEO_FORMAT and audio_codec in _AAC_AUDIO_CODECS:
            self._pending_video_codec = video_codec
            self._aac.start(_AAC_DETECTION_TIMEOUT)
            config = parse_audio_config(payload, audio_extra)
            if config is not None:
                declared_channels, sample_rate = config
                self._aac.set_config_from_header(declared_channels, sample_rate)
                log.debug(
                    "WebSocket bridge for %s: %dHz, %d channel(s) from codec-info payload",
                    self._label,
                    sample_rate,
                    declared_channels,
                )
            else:
                log.debug(
                    "WebSocket bridge for %s: detecting AAC sample rate before muxing",
                    self._label,
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
            # Grown like every other pipe here: mpv does not open the read
            # end until start() has returned and the caller has acted on
            # it, so the frames written in between have only the buffer to
            # sit in, and on the default 64KiB a couple of H.265 keyframes
            # fill it before any reader exists.
            _grow_pipe_buffer(self._read_fd)
            _set_write_end_nonblocking(self._video_write_fd)
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
            for fd in (video_r, audio_r, out_r):
                _grow_pipe_buffer(fd)
            _set_write_end_nonblocking(video_w)
            _set_write_end_nonblocking(audio_w)
            await self._spawn_ffmpeg(video_codec, audio_codec, video_r, audio_r, out_w)
        except BaseException:
            # Either a pipe, the spawn itself, or ffmpeg's own startup
            # failed. Whichever fds exist have no subprocess to inherit
            # them now, and this runs again on every reconnect attempt
            # until the bridge gives up, so leaking them here would
            # compound quickly.
            #
            # BaseException, not OSError: _spawn_ffmpeg waits out a start
            # grace, and this runs on the pump task, so a bridge stopped
            # in that window raises CancelledError right here. None of
            # these fds is on the object yet, so nothing else would ever
            # close them. The clause ends in a bare raise either way.
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
        self._last_audio_at = self._last_video_at = time.monotonic()
        self._audio_gap_watch = asyncio.create_task(self._watch_audio_gap())

    async def _spawn_ffmpeg(
        self, video_codec: str, audio_codec: str, video_r: int, audio_r: int, out_w: int
    ) -> None:
        """Build ffmpeg's argument list around the caller's pipe fds,
        start it, and confirm it is still running. Kept apart from
        _start_muxed only so the fd cleanup there covers the pipes and the
        spawn under one except OSError."""
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
            # The interleaver has no notion of a stream that ended part
            # way through a live mux: when the gap watchdog closes the
            # audio input, it goes on holding video back waiting for audio
            # that will never come, and only lets it through on this
            # valve, which defaults to 10s. That is a 10s freeze followed
            # by a backlog that never clears. 0 does not mean "no wait",
            # it disables the valve and makes the hold permanent.
            "-max_interleave_delta",
            "100000",  # microseconds
            "-f",
            "matroska",
            "-live",
            "1",
            "-flush_packets",
            "1",
            "pipe:1",
        ]
        # Let the muxer's own complaints through on a debug run. ffmpeg is
        # the prime suspect whenever a muxed slot stalls, and with its
        # stderr discarded it is the one component in the pipeline that
        # says nothing at any log level. Piped and drained by us rather
        # than inherited: inheriting hands ffmpeg whatever the app's stderr
        # is, which on the capture command the docs give is a pipe with
        # tee on the far end, so a reader that stops keeping up would block
        # ffmpeg in write(2) and stall the very stream being diagnosed.
        debugging = log.isEnabledFor(logging.DEBUG)
        self._ffmpeg_proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=subprocess.DEVNULL,
            stdout=out_w,
            stderr=subprocess.PIPE if debugging else subprocess.DEVNULL,
            pass_fds=[video_r, audio_r],
        )
        # An ffmpeg that starts but rejects these arguments (a build too
        # old, or one the user pointed us at) exits straight away, and
        # nothing downstream would ever notice: mpv reads ffmpeg's output
        # pipe, so the slot would sit on a frozen frame while our writes
        # land in a pipe with no reader. Report it as the OSError the
        # spawn itself would have raised, so the camera drops to
        # video-only like it does for any other unusable ffmpeg. A slow
        # start is not mistaken for a failed one: only a process that has
        # already exited counts.
        await asyncio.sleep(_FFMPEG_START_GRACE)
        if self._ffmpeg_proc.returncode is not None:
            raise OSError(f"ffmpeg exited at once with code {self._ffmpeg_proc.returncode}")
        self._ffmpeg_watch = asyncio.create_task(self._watch_ffmpeg(self._ffmpeg_proc))
        if self._ffmpeg_proc.stderr is not None:
            self._ffmpeg_stderr = asyncio.create_task(
                self._drain_ffmpeg_stderr(self._ffmpeg_proc.stderr)
            )

    async def _drain_ffmpeg_stderr(self, stream: asyncio.StreamReader) -> None:
        """Log what the muxer writes to stderr, and keep it unblocked.

        Only runs on a debug run, where the stderr pipe exists. It has to
        be drained by someone: ffmpeg blocks in write(2) once the pipe
        fills, and a muxer blocked there stops draining its input pipes,
        which is a stalled slot.
        """
        async for line in stream:
            text = line.decode(errors="replace").strip()
            if text:
                log.debug("ffmpeg for %s: %s", self._label, text)

    async def _watch_ffmpeg(self, proc: asyncio.subprocess.Process) -> None:
        """End the bridge if ffmpeg exits while the session is running.

        mpv plays ffmpeg's output, so a muxer that dies leaves the slot on
        its last frame, and nothing else reports it usefully. The next
        pipe write does fail with EPIPE, but the pump answers that by
        reconnecting onto the same dead pipes until the failure streak
        runs out, and a camera between frames may not write for a while.
        Ending the bridge with a real reason hands the slot to the
        caller's stream-lost path, which rebuilds the whole pipeline:
        fresh ffmpeg, fresh pipes, fresh play(). Nothing else can recover
        a session whose muxer is gone.
        """
        returncode = await proc.wait()
        if self._stopping or self._ffmpeg_proc is not proc:
            return  # our own teardown asked for this
        if self._pump_task is None or self._pump_task.done():
            # The pump has already finished and recorded why. Closing the
            # write ends on its way out is what ffmpeg is exiting from, so
            # reporting that exit here would replace the real reason (a
            # stalled pipe write, say) with its own consequence: wait_closed
            # reads _error after the pump returns, so the last writer wins.
            return
        self._error = f"ffmpeg exited with code {returncode}"
        log.warning("WebSocket bridge for %s: %s", self._label, self._error)
        self._pump_task.cancel()

    async def _handle_control_frame(
        self, fields: dict[str, str], header: bytes, payload: bytes
    ) -> None:
        """Handle a close notice or codec-info frame (anything that isn't
        a video/audio payload) — pipe/ffmpeg setup happens here, once,
        the first time codec info arrives for this bridge's lifetime.

        *payload* is the codec-info frame's own payload (SPS/PPS NALs
        and, on some cameras, an audio-config trailer sized by adoExtra
        — see parse_audio_config in aac.py); every other control frame
        (close notices) carries none, which _setup_pipes never looks at.
        """
        if "close" in fields:
            log.debug("WebSocket stream close: %s", header.decode(errors="replace"))
            return
        if self._read_fd < 0 and not self._aac.detecting:
            await self._setup_pipes(
                fields.get("vdoCodec", ""),
                fields.get("adoCodec", ""),
                fields.get("adoExtra", ""),
                payload,
            )

    async def _handle_pcmu_audio_frame(self, payload: bytes) -> None:
        """Write a real PCMU audio payload to ffmpeg's audio input."""
        await asyncio.to_thread(self._write_pipe, True, payload)

    async def _handle_aac_audio_frame(self, header_tail: bytes, payload: bytes) -> None:
        """Write a real AAC frame to ffmpeg's audio input, after
        reconstructing the raw frame (see AacDetector.reconstruct_frame)
        and prepending a synthesized ADTS header (see aac.py) —
        ffmpeg's plain "aac" demuxer needs ADTS framing, not what DSM
        sends.

        The sample rate, framing mode and channel count are already
        known by the time this ever runs (detection happens before
        ffmpeg starts at all — see AacDetector.feed_audio).
        """
        frame = self._aac.reconstruct_frame(header_tail, payload)
        header = adts_header(len(frame), self._aac.sample_rate, self._aac.channels)
        await asyncio.to_thread(self._write_pipe, True, header + frame)

    async def _fall_back_to_video_only(self) -> None:
        """Give up on muxing this camera's AAC in and use the original
        raw-video-only passthrough instead, flushing the video buffered
        during detection into it.

        Says nothing about why: the three callers give up for reasons
        that are not the same finding at all (a camera that sent no audio,
        a framing that would not decode, an ffmpeg that would not run),
        and each logs its own before calling this.

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
        if self._stopping:
            # Torn down while detection was still running. The pipe this
            # would build gets no reader: the caller stopped watching for
            # _ready_event, so nobody opens the read end, and the flush
            # below would fill the buffer and then park a worker thread
            # on it for the whole write timeout on the way out.
            self._aac.video_buffer.clear()
            self._aac.audio_buffer.clear()
            return
        self._read_fd, self._video_write_fd = os.pipe()
        _grow_pipe_buffer(self._read_fd)
        _set_write_end_nonblocking(self._video_write_fd)
        self._audio_active = False
        self._ready_event.set()
        for nal in self._aac.video_buffer:
            await asyncio.to_thread(self._write_pipe, False, nal)
        self._aac.video_buffer.clear()
        self._aac.audio_buffer.clear()

    async def _expire_aac_detection(self) -> None:
        """End detection on its deadline rather than on either counter,
        starting with whatever arrived (see _finish_aac_detection)."""
        log.info(
            "WebSocket bridge for %s: AAC detection did not complete in %.0fs, "
            "starting with what arrived",
            self._label,
            _AAC_DETECTION_TIMEOUT,
        )
        await self._finish_aac_detection()

    async def _finish_aac_detection(self) -> None:
        """Work out how to read this camera's AAC (see AacDetector.finish)
        and act on the answer: start the muxed ffmpeg pipeline, or fall
        back to video-only -- flushing everything buffered during
        detection either way."""
        valid, reason = await self._aac.finish(self._label)
        if valid:
            await self._start_aac_pipeline()
            return
        log.warning(
            "WebSocket bridge for %s: %s, streaming video without audio", self._label, reason
        )
        await self._fall_back_to_video_only()

    async def _start_aac_pipeline(self) -> None:
        """Start the muxed ffmpeg pipeline using the framing mode
        _finish_aac_detection just validated, or fall back to
        video-only if ffmpeg itself won't run."""
        log.debug(
            "WebSocket bridge for %s: AAC sample rate %dHz, %d channel(s), %s, "
            "starting muxed pipeline (%d buffered video, %d buffered audio frames)",
            self._label,
            self._aac.sample_rate,
            self._aac.channels,
            "header-embedded prefix"
            if self._aac.use_header_prepend
            else f"{self._aac.frame_prefix_len}-byte frame prefix",
            len(self._aac.video_buffer),
            len(self._aac.audio_buffer),
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
        for nal in self._aac.video_buffer:
            await asyncio.to_thread(self._write_pipe, False, nal)
            await asyncio.sleep(0.04)
        self._aac.video_buffer.clear()

    async def _flush_aac_audio(self) -> None:
        """Drain the audio buffered during detection, paced at the frame
        duration implied by the sample rate just detected."""
        audio_frame_duration = 1024 / self._aac.sample_rate
        for header_tail, payload in self._aac.audio_buffer:
            await self._handle_aac_audio_frame(header_tail, payload)
            await asyncio.sleep(audio_frame_duration)
        self._aac.audio_buffer.clear()

    async def _handle_video_frame(self, nal: bytes) -> None:
        """Route a video NAL to ffmpeg's input, or buffer it if still
        waiting on AAC sample-rate detection (see _setup_pipes)."""
        if self._aac.detecting:
            if self._aac.feed_video(nal):
                await self._finish_aac_detection()
        else:
            await asyncio.to_thread(self._write_pipe, False, nal)

    async def _dispatch_audio_frame(self, header_tail: bytes, payload: bytes) -> None:
        """Route a real audio payload to whichever handler matches the
        current codec/detection state. *header_tail* is only used by the
        AAC path (see AacDetector.reconstruct_frame)."""
        if self._aac.detecting:
            if self._aac.feed_audio(header_tail, payload):
                await self._finish_aac_detection()
        elif self._audio_codec in _AAC_AUDIO_CODECS:
            await self._handle_aac_audio_frame(header_tail, payload)
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

        Also where AAC detection's wall-clock deadline is checked (see
        _AAC_DETECTION_TIMEOUT). Any message will do: the point is to
        bound detection whenever the connection is alive at all, not only
        when media happens to be flowing. Doing it here rather than from
        a timer task also keeps every _finish_aac_detection call on the
        pump task, so nothing arrives mid-decision.

        Also where a paused History bridge's idle timeout is suspended
        (see the self._paused branch below): pause() asks DSM to stop
        sending entirely, so the silence this would otherwise treat as
        a stall is exactly what was asked for.
        """
        while True:
            timeout: float | None = _IDLE_TIMEOUT
            if self._aac.detecting:
                # Waiting the full idle timeout on a camera whose detection
                # deadline lands sooner would let the stall fire first, and
                # a stall only reconnects: detection would start over, and
                # over, with start() still waiting on it.
                timeout = min(_IDLE_TIMEOUT, max(0.0, self._aac.deadline - time.monotonic()))
            elif self._paused and self.is_history:
                timeout = None
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except TimeoutError:
                if self._aac.expired():
                    # Nothing is arriving and detection is out of time.
                    # Finishing beats treating it as a stall: the slot gets
                    # video rather than another reconnect it can't use.
                    await self._expire_aac_detection()
                    continue
                self._error = f"stalled: no data for {_IDLE_TIMEOUT:.0f}s"
                raise _StreamStalled(self._error) from None
            self._attempt_got_data = True
            if self._aac.expired():
                await self._expire_aac_detection()
            if not isinstance(message, bytes) or len(message) < 4:
                continue
            (hdr_len,) = struct.unpack(">I", message[:4])
            if 4 + hdr_len > len(message):
                continue
            header = message[4 : 4 + hdr_len]
            payload = message[4 + hdr_len :]
            fields = _parse_header(header)

            if "close" in fields or "vdoCodec" in fields or "adoCodec" in fields:
                await self._handle_control_frame(fields, header, payload)
                continue

            if (self._read_fd < 0 and not self._aac.detecting) or not payload:
                continue  # haven't seen codec info yet, or an empty frame

            await self._dispatch_media_frame(fields.get("mediaType"), header, payload)

    async def _dispatch_media_frame(
        self, media_type: str | None, header: bytes, payload: bytes
    ) -> None:
        """Route one video/audio frame from _read_messages -- pulled out
        of that loop just to keep its own branch count down, not
        because this is reused anywhere else."""
        if self._paused and not self.is_history:
            self._discard_paused_frame(media_type)
            return
        if media_type == "1":
            if self.is_history:
                # Ground truth for _current_history_target(): DSM's own
                # msec is where playback has actually reached, not a
                # theoretical wall-clock*speed estimate -- the two
                # diverge as soon as speed is anything but 1x, since
                # DSM needs real time to ramp delivery up (or down) to
                # a new rate rather than changing it instantly.
                msec = _parse_header(header).get("msec")
                if msec is not None:
                    self._last_video_msec = int(msec)
                    if self._history_recording is not None:
                        tick = self._history_recording.start_time + self._last_video_msec // 1000
                        if self._last_real_tick is None or tick > self._last_real_tick:
                            self._last_real_tick = tick
            # The payload arrives without the Annex B start code, so
            # prepend it and mpv/ffmpeg can find NAL boundaries. Where
            # DSM leaves it has never been checked here; the constant
            # is what the black screen needed.
            await self._handle_video_frame(b"\x00\x00\x00\x01" + payload)
        elif media_type == "2" and (self._audio_active or self._aac.detecting):
            # For some cameras the header ends in the leading bytes
            # the AAC payload is missing -- see
            # AacDetector.reconstruct_frame. Harmless to pass along
            # for PCMU too, since that path just ignores it.
            await self._dispatch_audio_frame(header[-4:], payload)

    async def _send_keepalive_loop(self, ws: Any) -> None:
        """Send a keepalive every _KEEPALIVE_INTERVAL for as long as the
        connection lives.

        A send that fails means the connection is already gone. Close it
        and let the read loop find out on its own, rather than cancelling
        that loop: a cancellation lands wherever the loop happens to be,
        and most of the time that is inside an asyncio.to_thread pipe
        write, which cancelling does not stop. The worker thread would
        carry on writing into a pipe the next connection is about to
        reuse, interleaving two frames into one stream. Closing makes
        recv() raise instead, so the loop unwinds between writes.
        """
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_INTERVAL)
                await ws.send("keepAlive")
        except Exception:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _read_messages_with_keepalive(self, ws: Any) -> None:
        """Read messages with a keepalive running alongside, so the NAS
        doesn't drop the session for client silence.

        The read loop stays inline and owns the connection. The keepalive
        never touches it, so the only cancellation that can reach a pipe
        write is stop()'s, after which nothing reconnects."""
        keepalive_task = asyncio.create_task(self._send_keepalive_loop(ws))
        try:
            await self._read_messages(ws)
        finally:
            keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await keepalive_task

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

    def _current_history_target(self) -> int:
        """Where History playback should be *right now* -- frozen at
        self._history_paused_position while self._paused; otherwise
        self._last_video_msec if a frame has actually arrived for
        whatever is currently loaded (accurate at any speed -- see
        that attribute's own comment for why); otherwise derived from
        self._history_delta_seconds and the current wall clock, which
        is only ever right at 1x, as a fallback for the moment before
        the first frame of a fresh connect/reconnect/seek arrives."""
        if self._history_paused_position is not None:
            return self._history_paused_position
        if self._last_video_msec is not None and self._history_recording is not None:
            return self._history_recording.start_time + self._last_video_msec // 1000
        return int(time.time() - self._history_delta_seconds)

    def _set_history_delta(self, target_unix: float) -> int:
        """Store *target_unix* as self._history_delta_seconds, clamped
        so the resulting delta is never less than
        MIN_HISTORY_DELTA_SECONDS behind wall clock -- a delta that
        small is inside the near-live window this bridge/DSM can't
        reliably serve yet.

        The single place that sets self._history_delta_seconds --
        __init__, seek(), and resume() all go through this rather than
        assigning it directly, so nothing can ask DSM to play within
        that window no matter which of the three is doing the asking.

        Returns the clamped target actually stored, for a caller that
        needs to reflect what's really playing (e.g. seek()'s own
        return value, or a UI syncing its on-screen position) rather
        than assuming the requested target_unix was used as-is.
        """
        now = time.time()
        clamped = min(target_unix, now - MIN_HISTORY_DELTA_SECONDS)
        self._history_delta_seconds = now - clamped
        return int(clamped)

    def request_pause(self) -> None:
        """Synchronously arm self._paused, ahead of the rest of what
        pause() does.

        A caller that's also about to pause mpv locally (Live View,
        for the local freeze -- see _write_pipe's own guard) must call
        this *before* touching mpv, not just await pause(): mpv's own
        pause takes effect immediately on the GTK thread, while
        pause() itself only runs once scheduled onto this bridge's own
        event loop/thread, which is not guaranteed to happen first. In
        that gap, a video write already in flight (or one that starts
        in it) sees mpv stop draining before self._paused is actually
        set, blocks for the full _WRITE_TIMEOUT, and gets mistaken for
        a stalled/dead stream -- more likely the larger a camera's own
        frames are, since that widens the window a write can still be
        in flight when the race is lost. The visible symptom is a
        flicker: the stream gets killed and reconnected purely from
        this race, not from anything actually wrong with it.

        A plain bool write -- idempotent, and safe to call from any
        thread.
        """
        self._paused = True

    async def pause(self) -> int | None:
        """Freeze playback in place until resume(). Callers pausing
        mpv locally too should call request_pause() first (see its own
        docstring for why) -- this covers what's left: nothing further
        for Live (the WS feed keeps flowing quietly in the background,
        so resume() needs no reconnect, just wall-clock live again),
        or, for History, asking DSM to actually stop sending and
        freezing the position _current_history_target() returns, so
        the real time behind wall clock grows for as long as this
        lasts, the same way a real DVR pause works.

        Returns the frozen position for a History bridge, for the
        caller to reflect on its own UI; None for Live, which has no
        such position.
        """
        self.request_pause()
        if not self.is_history:
            return None
        if self._history_paused_position is not None:
            return self._history_paused_position  # already paused
        self._history_paused_position = self._current_history_target()
        await self._send_history_update()
        return self._history_paused_position

    async def resume(self) -> int | None:
        """Undo pause(). History clamps the resume point to at least
        MIN_HISTORY_DELTA_SECONDS behind wall clock -- a pause
        shorter than that would otherwise resume closer to live than
        this bridge/DSM can reliably serve (see that constant). Live
        forces a fresh reconnect on the same pipe rather than just
        letting frames flow again from wherever DSM happens to be --
        pause() discarded every frame while frozen, I-frames included,
        so simply resuming mid-GOP fed the decoder P/B-frames with no
        recent keyframe behind them, producing corrupt/artifacted
        video until the next one arrived naturally, several seconds
        later. Reusing the same reconnect _pump already does for an
        ordinary drop -- DSM sends fresh codec-info and a clean start
        on the same pipe -- is what already makes that look like
        routine buffering to mpv instead (see _pump's own reconnect
        comment), rather than inventing a second recovery path here.

        Returns the resumed position for a History bridge, same as
        pause(); None for Live.
        """
        if not self._paused:
            return self._history_paused_position
        self._paused = False
        if not self.is_history:
            if self._current_ws is not None:
                await self._current_ws.close()
            return None
        if self._history_paused_position is None:
            return None
        resume_position = self._set_history_delta(self._history_paused_position)
        self._history_paused_position = None
        # The frozen position may have just been clamped forward (see
        # _set_history_delta) -- self._last_video_msec, last stamped
        # before the pause, would otherwise outrank that fresh delta
        # estimate in _current_history_target and undo the clamp.
        self._last_video_msec = None
        await self._send_history_update()
        return resume_position

    @property
    def is_paused(self) -> bool:
        """Whether pause() currently has this bridge paused -- read by
        Live View to stop ticking a History slot's on-screen position
        forward while frozen (see _tick_history_positions)."""
        return self._paused

    @property
    def current_history_position(self) -> int | None:
        """Where History playback currently is, including while frozen
        by pause() -- None for a Live bridge, which has no such
        position. A thin public wrapper around
        _current_history_target() for callers outside this class."""
        return self._current_history_target() if self.is_history else None

    def consume_last_real_tick(self) -> int | None:
        """Return the highest real (frame-derived) absolute position seen
        since the last call, clearing it back to None.

        Unlike current_history_position, this never falls back to a
        wall-clock estimate -- None means "no real data from this camera
        since the last check", which is the point: LiveView's shared
        timeline marker takes the max of this across every active slot
        once a second, and only estimates its own position from wall
        clock/speed when every active camera comes back None, whatever
        the reason (a real recording gap in the focused camera alone is
        masked for free by any other active camera still delivering
        real ticks; only a gap or outage affecting the whole layout at
        once ever reaches that fallback). No lock around the write in
        _dispatch_media_frame (a different thread, see the module's
        asyncio/GTK split) -- losing an update to a race here just means
        this reports one frame later than it could have, same tolerance
        already accepted for self._last_video_msec's own cross-thread read.
        """
        tick = self._last_real_tick
        self._last_real_tick = None
        return tick

    async def _refresh_history_recording_if_stale(self) -> None:
        """Swap in a fresh recording via self._history_resolver if the
        one currently loaded no longer covers _current_history_target().

        Without this, a recording that stops extending (its stop_time
        stays fixed once DSM finishes writing it) leaves every
        reconnect after playback catches up to that point clamped to
        its last offset (_build_history_play_message's own clamp) --
        DSM serving nothing new from there, so the bridge stalls and
        reconnects in a tight, unproductive loop instead of picking up
        wherever a newer recording has since covered the same camera.
        """
        rec = self._history_recording
        if rec is None or self._history_resolver is None:
            return
        target = self._current_history_target()
        if rec.start_time <= target <= rec.stop_time:
            return
        fresh = await self._history_resolver(target)
        if fresh is not None:
            self._history_recording = fresh
            # self._last_video_msec belongs to the recording just
            # swapped out -- interpreted against the new one's
            # start_time it would land somewhere meaningless, so
            # _current_history_target must fall back to the delta
            # estimate until a frame actually arrives for this one.
            self._last_video_msec = None

    def _history_play_params(self) -> dict[str, str]:
        """Shared field set for History's in-band `action=play` message
        (see the module docstring for the wire protocol) -- used both
        to open a connection (_build_history_play_message) and to
        update pause/speed/reverse in place on one already open
        (_send_history_update), which differ only in restart and
        (implicitly, via self._paused/self._speed/self._reverse)
        pause/speed/reverse.

        Every field below came from one live capture with one fixed
        value each (except mute, which we did see vary) -- nothing here
        has been tested against a different value, so "confirmed" only
        ever means "this literal string decodes real recorded video",
        not "this is the whole valid range". Grouped by how much of
        that is actually known:

        - start/end/stamp/id/mountId/archId/recEvtType: meaning
          confirmed, not just the value -- see the module docstring for
          start/end/stamp/id, and Recording.mount_id/.arch_id/
          .event_type's own docs for the rest.
        - mute: every capture had this "true" (Monitor Center's own
          player happened to be muted each time), which first read as
          "just the client's mute toggle, mirroring Live's separate
          out-of-band mute=true message" -- but fixing it at "true" here
          left History sessions with no audio to mute at all, live
          testing against this app found. Fixed at "false" instead, so
          DSM actually sends audio frames for _setup_pipes/ffmpeg to mux
          the same way Live gets them, and playback-side muting (the
          slot toolbar's mute button, same as Live) is what decides
          whether that audio is heard.
        - method/blAudio/action: name suggests the role (blAudio --
          include audio) and it lines up with Live's own same-named
          field, but no capture ever varied it.
        - blMux/browser/stmSrc/autoDrop: genuinely unconfirmed even by
          name -- included because DSM's own web client always sends
          them and omitting an unfamiliar field felt riskier than
          copying it verbatim. stmSrc=1 here vs the connect URL's
          stmSrc=2 (see module docstring) is the one hint that these
          matter somehow, not just boilerplate.
        - pause: confirmed both ways by a live capture -- pause=true
          does make DSM stop sending entirely (not just video), and
          pause=false resumes it (see pause()/resume()).
        - speed: also confirmed by a live capture across 0.5x-32x --
          DSM genuinely delivers frames that many times faster (frame
          rate and the msec header both scale by the same factor, in
          lockstep), not just a hint mpv is left to interpret on its
          own, so no client-side pacing is needed beyond relaying
          whatever arrives (see set_speed()). Untested below 0.5x or
          above 32x.
        - reverse: also confirmed by a live capture -- reverse=true
          delivers frames with genuinely decreasing msec (matching the
          current speed's own magnitude when combined, e.g.
          reverse=true&speed=4 ran backward at ~4x), and this bridge's
          existing frame-reconstruction pipeline decodes it with no
          changes needed (see set_reverse()). One asymmetry: frame
          delivery at reverse&speed=1 specifically arrived markedly
          slower than forward at the same nominal speed -- a DSM-side
          characteristic of reverse itself, not something client-side
          pacing could fix.
        """
        rec = self._history_recording
        if rec is None:
            raise RuntimeError("_history_play_params called with no recording set")
        end = max(0, rec.stop_time - rec.start_time)
        # Clamped to [0, end], not just floored at 0: find_recording_at
        # hands back the *nearest* recording, not necessarily one that
        # actually covers the target, for a target landing in a gap
        # between recordings. Unclamped, a target past this recording's
        # own end produced a start beyond its length -- DSM held on the
        # last decoded frame rather than erroring, which read as the
        # slot having frozen. self._current_history_target() (not the
        # original target) is what gets clamped: on a reconnect long
        # after the initial seek, that's what keeps this landing near
        # where playback should have reached by now instead of
        # rewinding to the original target every time.
        start = min(end, max(0, self._current_history_target() - rec.start_time))
        return {
            "action": "play",
            "method": "MixStream",
            "blMux": "true",
            "browser": "2",
            "stmSrc": "1",
            "blAudio": "true",
            "mute": "false",
            "speed": self._speed,
            "reverse": "true" if self._reverse else "false",
            "recEvtType": str(rec.event_type),
            "mountId": str(rec.mount_id),
            "archId": str(rec.arch_id),
            "start": str(start),
            "end": str(end),
            "autoDrop": "true",
            "restart": "true",
            "pause": "true" if self._paused else "false",
            "stamp": str(self._history_stamp),
            "id": str(rec.id),
        }

    def _build_history_play_message(self) -> str:
        """Build the in-band `action=play` string sent once per
        connection, right after connecting, whenever
        self._history_recording is set -- see _history_play_params for
        what each field means.

        Resets the stamp to 1 for a fresh connection, and always
        carries self._paused/_history_paused_position through (via
        _history_play_params/_current_history_target): a reconnect
        that happens to land mid-pause -- _pump's own, not just a
        caller's -- opens already paused at the frozen position rather
        than accidentally resuming.
        """
        self._history_stamp = 1
        params = self._history_play_params()
        return "&".join(f"{k}={v}" for k, v in params.items())

    async def _send_history_update(self) -> None:
        """Push whatever pause()/resume()/set_speed() last set (via
        self._paused/self._speed) to DSM, in place, on the connection
        already open -- restart=false so DSM doesn't also jump back to
        _history_play_params' start= the way a fresh action=play does;
        only the field(s) the caller just changed are meant to change
        here.

        A no-op while no connection is up: _build_history_play_message
        picks up self._paused/self._speed on the next connect/reconnect
        regardless (see its own docstring), so the caller still leaves
        the bridge in the right state either way.
        """
        if self._current_ws is None:
            return
        self._history_stamp += 1
        params = self._history_play_params()
        params["restart"] = "false"
        await self._current_ws.send("&".join(f"{k}={v}" for k, v in params.items()))

    async def set_speed(self, speed: str) -> None:
        """Change History playback speed in place on the connection
        already open. History-only: a no-op for Live, which has no
        speed concept -- matches the toolbar's own speed dropdown,
        disabled outside History mode, but callers need not gate on
        is_history themselves.

        *speed* is DSM's own literal multiplier string ("2", "0.5",
        ...) -- see _history_play_params' own "speed" bullet for what's
        confirmed about the range. Persists across this bridge's own
        periodic reconnects the same way self._speed's own comment
        describes, until a later set_speed() or a fresh History entry
        changes it again.
        """
        if not self.is_history:
            return
        self._speed = speed
        await self._send_history_update()

    async def set_reverse(self, reverse: bool) -> None:
        """Toggle History playback direction in place on the
        connection already open. History-only: a no-op for Live,
        which has no reverse concept -- matches the toolbar's own
        Fwd/Rev toggle, disabled outside History mode, but callers
        need not gate on is_history themselves.

        See _history_play_params' own "reverse" bullet for what's
        confirmed about combining this with speed. Persists across
        this bridge's own periodic reconnects the same way
        self._reverse's own comment describes, until a later
        set_reverse() or a fresh History entry changes it again.
        """
        if not self.is_history:
            return
        self._reverse = reverse
        await self._send_history_update()

    async def seek(self, recording: Recording, target_unix: int) -> int:
        """Seek to *target_unix* within *recording* (History mode only).

        Reuses the connection with an in-band `seekMs` command when
        *recording* is the one already selected -- matching what a real
        seek within the loaded recording does (see the module
        docstring). Otherwise updates the pending recording/target and
        closes the current connection: _pump's own reconnect loop then
        opens a fresh one and sends a new action=play for *recording*,
        the same machinery an ordinary connection drop already uses,
        rather than a second reconnect path living here too.

        Either way, self._history_delta_seconds is updated too, via
        _set_history_delta rather than directly -- target_unix within
        MIN_HISTORY_DELTA_SECONDS of wall clock (a click right near
        the live edge of the ruler) must clamp exactly like resume()
        landing there does, never asking DSM to play that close to
        live. A *later* reconnect -- caller-requested, or _pump's own
        after this bridge's own History session drops on its own after
        a couple of minutes regardless of activity -- resumes from
        where playback should have reached by then, derived from that
        delta, rather than rewinding to this seek's target again.

        Also clears pause() unconditionally: a seek is "go here and
        play", the same as clicking play on a paused video always
        implicitly resumes it, and leaving self._paused set would
        otherwise strand the bridge in a stale, wrong state -- either
        still asking DSM to hold at the *old* frozen position
        (_current_history_target ignoring this seek's target entirely
        while self._history_paused_position stays set), or, worse, a
        cross-recording reconnect opening the new connection already
        paused (_build_history_play_message picks up self._paused too).

        Returns the actual (possibly clamped) target now in effect,
        for the caller to reflect on its own UI rather than assuming
        target_unix was used as-is.
        """
        was_paused = self._paused
        self._paused = False
        self._history_paused_position = None
        # A seek moves the position outright, same or different
        # recording alike -- self._last_video_msec belongs to wherever
        # playback was *before* this, and _current_history_target must
        # fall back to the (freshly set, below) delta estimate until a
        # frame actually arrives for the new target.
        self._last_video_msec = None
        clamped_target = self._set_history_delta(target_unix)
        if self._history_recording is not None and recording.id == self._history_recording.id:
            if self._current_ws is not None:
                if was_paused:
                    # An explicit pause=false (which increments
                    # self._history_stamp itself) beats relying on an
                    # untested assumption that a bare seekMs also
                    # implicitly resumes a connection DSM still thinks
                    # is paused.
                    await self._send_history_update()
                else:
                    self._history_stamp += 1
                    # Same clamp as _build_history_play_message's
                    # start, and for the same reason -- clamped_target
                    # can still land past this recording's own end when
                    # it's the nearest one to a target that's actually
                    # in a gap.
                    duration_ms = max(0, (recording.stop_time - recording.start_time) * 1000)
                    offset_ms = min(
                        duration_ms, max(0, (clamped_target - recording.start_time) * 1000)
                    )
                    await self._current_ws.send(f"seekMs={offset_ms}&stamp={self._history_stamp}")
            return clamped_target
        self._history_recording = recording
        if self._current_ws is not None:
            await self._current_ws.close()
        return clamped_target

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
                        self._current_ws = ws
                        # A new session gets the full gap timeout to
                        # deliver its first audio frame. Left at whatever
                        # the dropped session ended on, the stamp is stale
                        # by the whole outage, and the first video frame
                        # back would make the gap watchdog read the
                        # reconnect itself as a camera that went quiet.
                        # The video stamp needs no such reset: a stale one
                        # can only delay a fire, never cause it.
                        self._last_audio_at = self._connected_at
                        delay = 0.0
                        if self._history_recording is not None:
                            # Belongs to whatever connection just ended
                            # -- _build_history_play_message's own
                            # start= must fall back to the delta
                            # estimate until a frame actually arrives
                            # on this one (see self._last_video_msec's
                            # own comment).
                            self._last_video_msec = None
                            await self._refresh_history_recording_if_stale()
                            await ws.send(self._build_history_play_message())
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

                # The `async with` block above has exited by now, one way
                # or another -- nothing left for seek() to send on until
                # the next connection (if any) sets this again.
                self._current_ws = None

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

    async def _watch_audio_gap(self) -> None:
        """End the audio stream if the camera stops delivering audio.

        A live mux holds its video input for as long as one of its inputs
        has nothing to deliver: measured on ffmpeg 7.1.5, video stops
        being drained 0.7s after audio goes quiet and never resumes, so
        the video pipe fills, _write_pipe times out and the whole slot is
        given up and rebuilt into the same wedge on the next poll. A
        camera whose audio simply stops (silence suppression, a dropped
        microphone, an audio codec DSM announces but never sends) would
        do that every poll interval, forever.

        Closing the write end releases it. ffmpeg ends that stream and
        goes back to muxing video alone, within about 130ms even when it
        has been wedged for some time, so this does not have to beat the
        0.7s window, only the write timeout that follows it. The audio is
        gone for the rest of the session: the stream cannot be reopened
        without restarting ffmpeg, and a camera silent this long has no
        audio worth muxing anyway.

        Only fires while video is still arriving, which is the whole of
        the fault: a mux with nothing coming in on either input is idle,
        not wedged. Without that condition a session the NAS drops
        silently would trip this every time, since recv() waits out
        _IDLE_TIMEOUT before reconnecting and neither stream arrives
        meanwhile, and a camera that recovers perfectly would come back
        mute.
        """
        while True:
            await asyncio.sleep(_AUDIO_GAP_CHECK_INTERVAL)
            if self._audio_write_fd < 0:
                return  # torn down, or already closed by an earlier gap
            now = time.monotonic()
            gap = now - self._last_audio_at
            video_arriving = (
                self._video_write_in_flight or now - self._last_video_at < _AUDIO_GAP_TIMEOUT
            )
            if gap < _AUDIO_GAP_TIMEOUT or not video_arriving:
                continue
            log.warning(
                "WebSocket bridge for %s: no audio for %.0fs, ending the audio stream "
                "so it stops holding up the video",
                self._label,
                gap,
            )
            self._close_audio_write_fd()
            return

    def _close_audio_write_fd(self) -> None:
        """Close just the audio write end. Thread-safe, idempotent.

        Takes the same lock _write_pipe reads the descriptor under, so a
        write already in flight keeps its own dup of it and this cannot
        close the number out from under a later one.
        """
        with self._fd_lock:
            fd, self._audio_write_fd = self._audio_write_fd, -1
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        # No audio can reach the player from here on, so the caller's
        # audio controls have something to go on besides a log line.
        self._audio_active = False

    def _discard_paused_frame(self, media_type: str | None) -> None:
        """Drop an incoming video/audio frame while a Live bridge is
        paused (see _write_pipe's own guard, which this pre-empts a
        layer earlier -- avoiding a to_thread dispatch, not just the
        write itself, for every frame a long pause across many cameras
        at once would otherwise submit for nothing).

        Still stamps the gap watchdog's timestamps directly, same as a
        real write would (see _write_pipe's own comment on why those
        must keep moving regardless of whether anything was written).
        """
        if media_type == "1":
            self._last_video_at = time.monotonic()
        elif media_type == "2":
            self._last_audio_at = time.monotonic()

    def _write_pipe(self, audio: bool, data: bytes) -> None:
        """Write to one of the pipes through a private copy of the fd.

        Runs in a worker thread. The descriptor is read and duplicated
        under the lock _close_write_fd() takes, so a teardown racing this
        cannot close it between the read and the write(2) -- the next
        bridge's os.pipe() gets the same numbers back, so a late write
        would land in another camera's stream, or in whatever else
        happened to claim the number. Only the number is private: the dup
        shares the original's file status flags, which is why O_NONBLOCK
        is set once at pipe creation rather than here.

        A duplicate rather than holding the lock across the write: a write
        to a pipe mpv has not drained blocks until it does, and
        close_write_end() is called from the GTK main thread.

        Non-blocking with its own timeout (_WRITE_TIMEOUT) rather than a
        plain blocking os.write(): a pipe whose reader has stopped
        draining it (ffmpeg or mpv wedged downstream) would otherwise
        block here forever, with no way back to ws.recv() and so no way
        to ever raise, reconnect, or hand off to the stream-lost recovery
        path that's built for exactly this.

        Also where a paused *Live* bridge discards frames instead of
        writing them (History pauses DSM itself instead -- see
        pause()): mpv's own pause alone only freezes rendering, its
        demuxer keeps reading ahead into its own cache regardless, so
        without this the WS feed's normal flow would keep writing into
        this pipe until that cache fills -- at which point the write
        above would block long enough to misread a deliberate, healthy
        pause as a stalled pipe and reconnect a stream that was never
        actually broken.
        """
        # Stamped here rather than at the call sites, so the gap watchdog
        # measures when media reached the pipe and cannot be kept alive by
        # frames that never got that far.
        if audio:
            self._last_audio_at = time.monotonic()
        else:
            self._last_video_at = time.monotonic()
        if self._paused and not self.is_history:
            return
        with self._fd_lock:
            fd = self._audio_write_fd if audio else self._video_write_fd
            if fd < 0:
                return
            dup = os.dup(fd)
        # A video write that has started and cannot finish is what a
        # wedged mux looks like from here, and it stops the stamp above
        # advancing for as long as it lasts. The gap watchdog has to read
        # that as video still arriving, or the very case it exists for
        # looks to it like a camera that went quiet altogether. One video
        # write is in flight at a time: the read loop awaits each.
        if not audio:
            self._video_write_in_flight = True
        try:
            view = memoryview(data)
            poller = select.poll()
            poller.register(dup, select.POLLOUT)
            deadline = time.monotonic() + _WRITE_TIMEOUT
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Video NALs go down this path in both modes, so a
                    # video pipe stall is either ffmpeg or mpv. An audio
                    # one can only be a muxed camera, since nothing else
                    # writes that pipe. The byte counts tell a reader that
                    # never started from one that stopped part way.
                    raise _PipeWriteStalled(
                        f"{'audio' if audio else 'video'} pipe write stalled for "
                        f"{_WRITE_TIMEOUT:.0f}s with {len(view)} of {len(data)} bytes "
                        "left, downstream reader stopped draining"
                    )
                poller.poll(remaining * 1000)
                try:
                    n = os.write(dup, view)
                except BlockingIOError:
                    continue
                view = view[n:]
                # Any progress means the reader is alive: the deadline is
                # for a stall, not a budget for how long a big frame may
                # take to hand over.
                deadline = time.monotonic() + _WRITE_TIMEOUT
        finally:
            if not audio:
                self._video_write_in_flight = False
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

        if self._ffmpeg_watch is not None:
            self._ffmpeg_watch.cancel()
            self._ffmpeg_watch = None

        if self._ffmpeg_stderr is not None:
            self._ffmpeg_stderr.cancel()
            self._ffmpeg_stderr = None

        if self._audio_gap_watch is not None:
            self._audio_gap_watch.cancel()
            self._audio_gap_watch = None

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
