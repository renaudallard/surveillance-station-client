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

"""AAC helpers for WebSocket audio muxing (see ws_bridge.py, which
drives AacDetector below).

DSM never sends a self-contained ADTS frame, which is what ffmpeg's
plain "aac" demuxer needs. AacDetector.reconstruct_frame recovers the
raw frame and adts_header() below synthesizes a header the demuxer can
find via the sync word. What has to be recovered is not the same for
every camera -- see reconstruct_frame for the two shapes seen so far.

On the camera whose frames were captured, the payload arrives behind a
short prefix, and that prefix is the tail of an ADTS header rather than
the RFC 3640 AU-header this code first assumed. adts_header() below
reproduces all three of its bytes exactly: they are aac_frame_length's
low 11 bits, adts_buffer_fullness 0x7FF, and one raw_data_block per
frame. Read as an RFC 3640 AU-header the same bytes give an AU size of
~1600 for a ~420-byte frame and an AU index of 7 where the RFC requires
0, so that reading is excluded.

Four bytes are therefore missing from the front of each payload, and
PR #17 reports ((p[0] << 3) | (p[1] >> 5)) == len(p) + 4 holding on
every frame of that camera. That is the reading above restated -- the
length field counts the ADTS header in -- so it confirms four bytes are
gone without saying where they went, and the captures in test_aac.py
are 12-byte prefixes, so nothing in the tree can evaluate it anyway.

The prefix length is therefore still measured per camera (see
detect_frame_prefix_len) from frames buffered during startup rather
than computed from the payload. That measurement cannot lean on ffmpeg
reporting a decode error: leaving one prefix byte unstripped makes its
decoder recover through an internal retry that drops the packet's own
timestamp without surfacing anything, which is what let WebSocket
reconnect gaps pass unnoticed until audio and video had drifted apart.
AacDetector.frames_look_valid only checks that ffmpeg stays quiet, so
it cannot catch that by itself.

A second camera model (reported in PR #17 as a Reolink RLC-823A) sends
the same adoCodec with no payload prefix at all: the payload is missing
the frame's own leading bytes, and the WS message's header ends in
exactly those. detect_frame_prefix_len does not report that -- it only
eliminates, so it hands back whichever length happens to survive -- so
it is the ffmpeg check that rules the payload-only model out, and
reconstruct_frame then rebuilds the frame from the header instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
from collections.abc import Sequence
from statistics import median

log = logging.getLogger(__name__)

# DSM does expose the negotiated sample rate and channel count, but not
# in adoExtra itself -- adoExtra is just the byte length of a trailer
# DSM appends to the codec-info frame's payload (see parse_audio_config
# below), and not every camera has been confirmed to send one. Timing
# stays the fallback: every AAC-LC frame carries a fixed 1024 samples,
# so it alone is enough to determine the rate live for a camera whose
# trailer doesn't parse, without a per-camera-model lookup table.
_SAMPLES_PER_FRAME = 1024
_STANDARD_SAMPLE_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)

# aac_frame_length is 13 bits and counts the header in.
_ADTS_MAX_FRAME_LEN = 0x1FFF

_ADTS_FREQ_INDEX = {
    96000: 0,
    88200: 1,
    64000: 2,
    48000: 3,
    44100: 4,
    32000: 5,
    24000: 6,
    22050: 7,
    16000: 8,
    12000: 9,
    11025: 10,
    8000: 11,
    7350: 12,
}


def strip_frame_prefix(frame: bytes, prefix_len: int) -> bytes:
    """Remove DSM's leading prefix, leaving the raw AAC frame."""
    return frame[prefix_len:]


# 2 is the shortest prefix seen in practice and 3 is the only other one,
# so the search starts at 2; a shorter candidate would be tested against
# real AAC payload bytes rather than prefix bytes, which proves nothing.
# The ceiling is arbitrary but generous, and matches the range already
# ruled out for the second camera model in the module docstring.
_PREFIX_MIN_LEN = 2
_PREFIX_MAX_LEN = 8

# AAC's raw_data_block starts with a 3-bit id_syn_ele naming the first
# syntax element. 0b111 is ID_END, meaning "no elements follow" -- a real,
# non-empty frame can never legitimately start with that.
_AAC_ELEMENT_ID_END = 0b111

# The two elements that name a channel layout: 0b000 is a
# single_channel_element, carrying one channel, and 0b001 a
# channel_pair_element, carrying two. Every other element (FIL padding,
# DSE data, ...) says nothing about the layout.
_AAC_ELEMENT_CHANNELS = {0b000: 1, 0b001: 2}


def detect_frame_prefix_len(frames: Sequence[bytes]) -> int | None:
    """Work out how many leading bytes DSM puts in front of the raw AAC
    frame, from a handful of real (still prefixed) frames.

    This eliminates rather than confirms. A candidate whose first
    post-strip byte reads as AAC's "immediate end, zero elements"
    marker on any sample frame is provably wrong, since a real
    raw_data_block cannot start with it; every other candidate is
    merely not disproved, and the shortest survivor wins. That
    preference is what makes the answer right on the captured camera,
    not the test itself, so a wrong prefix length is still possible in
    principle and _aac_frames_look_valid stays the only real check.

    Frames too short to carry any candidate are dropped rather than
    allowed to veto one: a runt payload says nothing about the framing,
    and letting it rule every length out would cost the camera its
    audio for the whole session. Returns None if nothing usable is left
    or no candidate holds, so callers can fall back instead of guessing.
    """
    usable = [frame for frame in frames if len(frame) > _PREFIX_MAX_LEN]
    if not usable:
        return None
    for length in range(_PREFIX_MIN_LEN, _PREFIX_MAX_LEN + 1):
        if all((frame[length] >> 5) != _AAC_ELEMENT_ID_END for frame in usable):
            return length
    return None


def detect_channel_count(frames: Sequence[bytes], declared: int = 2) -> int:
    """Work out how many channels the camera's AAC carries, from a
    handful of already-reconstructed frames.

    Reading it off each frame separately does not work: an encoder is
    free to open a frame with an element that names no layout at all,
    and libavcodec emits a FIL first on the first frame of every stream
    it writes. Labelling that one frame differently from the rest gives
    a channel_configuration that changes mid-stream, which no valid
    ADTS stream has, and demuxers take the layout from the first frame
    anyway -- so the whole stream would end up labelled by the one frame
    that says the least. Settling it once, from the first frame that
    does name a layout, avoids both.

    Falls back to *declared* when no frame names one: the count DSM put
    in the codec-info trailer where it sent one (see parse_audio_config),
    stereo where it did not, which is what this code assumed
    unconditionally before either source existed. Reading the frame in
    preference to the declaration is Synology's own order, not a guess:
    its decoder takes the declared count and then overrides it from this
    same id_syn_ele (AACHelper::ParseChannelCount in libplayerlib.so,
    NativeAACDecoder.getChannelCount in the DS cam APK).
    """
    for frame in frames:
        if frame:
            channels = _AAC_ELEMENT_CHANNELS.get(frame[0] >> 5)
            if channels is not None:
                return channels
    return declared


def nearest_sample_rate(interval_seconds: float) -> int:
    """Snap a measured inter-frame interval to the nearest standard AAC
    sample rate, assuming AAC-LC's fixed 1024 samples per frame."""
    if interval_seconds <= 0:
        return 16000
    measured = _SAMPLES_PER_FRAME / interval_seconds
    return min(_STANDARD_SAMPLE_RATES, key=lambda r: abs(r - measured))


def parse_audio_config(payload: bytes, extra_len: str) -> tuple[int, int] | None:
    """Decode the audio trailer DSM appends to the codec-info frame's
    payload, after the video config (confirmed on three real cameras,
    two AAC and one PCMU): the last *extra_len* bytes -- adoExtra,
    still a string here since it comes straight from the parsed header
    -- hold ASCII "<channels>|<sampleRate>|", optionally followed by a
    codec-specific blob (a raw 2-byte AAC AudioSpecificConfig; PCMU's
    trailer ends at the second "|" with nothing after it).

    Returns (channels, sample_rate), or None if adoExtra is missing or
    not a plain number, the payload is shorter than it claims, or the
    trailer isn't in this shape at all -- a camera that has not been
    confirmed to send one. Callers fall back to runtime detection in
    every such case, so this only ever adds information, never removes
    a camera's audio.
    """
    if not extra_len.isdigit():
        return None
    n = int(extra_len)
    if not 0 < n <= len(payload):
        return None
    parts = payload[-n:].split(b"|", 2)
    if len(parts) < 2:
        return None
    try:
        channels = int(parts[0])
        sample_rate = int(parts[1])
    except ValueError:
        return None
    if channels <= 0 or sample_rate not in _ADTS_FREQ_INDEX:
        return None
    return channels, sample_rate


def adts_header(payload_length: int, sample_rate: int, channels: int) -> bytes:
    """Build a 7-byte ADTS header (no CRC, AAC-LC) for an AAC frame of
    *payload_length* bytes -- lets ffmpeg's plain "aac" demuxer read an
    otherwise-bare AAC stream via ADTS sync-word auto-detection.

    *channels* comes from detect_channel_count rather than being assumed,
    the way Synology's own decoder resolves it (NativeAACDecoder.
    getChannelCount in the DS cam APK). Getting it wrong is not loud --
    ffmpeg decodes a mono frame labelled stereo without one complaint and
    leaves whatever was in the buffer in the right channel.

    Raises ValueError past what the header can describe. A 1024-sample
    AAC-LC frame never comes anywhere near that, so this catches a caller
    feeding in something that is not one frame rather than a real camera,
    which matters because the alternative is a silently wrong length: the
    field is 13 bits and the excess would just be dropped.
    """
    freq_idx = _ADTS_FREQ_INDEX[sample_rate]
    profile_id = 1  # AAC-LC (object type 2) -> ADTS profile field = object_type - 1
    frame_len = payload_length + 7
    if frame_len > _ADTS_MAX_FRAME_LEN:
        raise ValueError(
            f"AAC frame of {payload_length} bytes exceeds what an ADTS header can describe"
        )
    h = bytearray(7)
    h[0] = 0xFF
    h[1] = 0xF1
    h[2] = ((profile_id & 0x3) << 6) | ((freq_idx & 0xF) << 2) | ((channels >> 2) & 0x1)
    h[3] = ((channels & 0x3) << 6) | ((frame_len >> 11) & 0x3)
    h[4] = (frame_len >> 3) & 0xFF
    h[5] = ((frame_len & 0x7) << 5) | 0x1F
    h[6] = 0xFC
    return bytes(h)


# Cap on how much video AacDetector buffers before giving up on
# collecting _AAC_DETECTION_INTERVALS real audio intervals and starting
# anyway with whatever has been measured so far (or the default guess,
# if audio never arrived at all), so a camera that claims AAC but
# doesn't actually deliver a steady audio stream still starts.
_AAC_DETECTION_VIDEO_FRAME_CAP = 60

# Inter-frame intervals to collect before locking the AAC sample rate.
# Odd, because the rate comes from their median: these are wall-clock
# arrival times measured on the one event loop that also serves every
# other camera, so a single scheduling hiccup is normal. The rates sit
# close together (48000 vs 44100 is 8.8% apart, about 0.9ms of interval
# at 48kHz), so a mean would let one late frame pick the wrong one and
# play the whole session at the wrong pitch.
_AAC_DETECTION_INTERVALS = 11

# How long the throwaway ffmpeg that checks a reconstructed AAC framing
# may take. It decodes a handful of buffered frames from a pipe and
# exits, so this only has to cover a loaded machine, not real work.
_AAC_PROBE_TIMEOUT = 3.0  # seconds


class AacDetector:
    """Works out how to turn one camera's raw AAC into something
    ffmpeg's ADTS demuxer can read, and buffers video+audio while doing
    it -- see the module docstring for what's actually being determined
    and why it can't be known upfront.

    One instance per WebSocketBridge connection attempt, created once
    and reused for the bridge's whole lifetime (detection only ever
    runs on the first codec-info frame -- see ws_bridge.py's
    _handle_control_frame). The bridge owns starting it, feeding it
    frames, calling finish() once enough have arrived, and acting on
    the result (start the muxed pipeline, or fall back to video-only);
    this class only ever decides *how* to read this camera's AAC, never
    what to do with that answer.
    """

    def __init__(self) -> None:
        # Defaults match what every camera got before either of these
        # was detected -- see nearest_sample_rate/detect_channel_count.
        # sample_rate may be overwritten immediately by
        # set_config_from_header, before any frame has arrived at all;
        # channels never is -- see declared_channels below.
        self.sample_rate = 16000
        self.channels = 2
        # What the codec-info payload declared, if set_config_from_header
        # was ever called. Kept apart from channels because
        # frames_look_valid runs once per framing model: reading the
        # fallback back out of the settled channels would feed the
        # rejected model's answer to the second attempt in place of
        # DSM's own declaration.
        self.declared_channels = 2
        # Set by set_config_from_header when parse_audio_config (see
        # above) reads the real sample rate straight off the wire, so
        # finish() knows not to overwrite it with a timing guess. Only
        # the rate: the channel count that arrives with it is a starting
        # value, not a verdict -- see frames_look_valid.
        self.config_from_header = False
        # Some cameras don't put the whole frame, prefixed, in the
        # payload -- the payload is missing its own leading bytes, and
        # those are what DSM's per-message header ends in instead (see
        # reconstruct_frame). finish() flips this once the payload-only
        # prefix model fails to validate.
        self.use_header_prepend = False
        # Overwritten by detect_frame_prefix_len() in finish() before
        # any real frame is ever stripped.
        self.frame_prefix_len = 2
        self.detecting = False
        # Whether the *last* framing check ran out of time rather than
        # reaching a verdict, so the two are never reported as one.
        self.probe_timed_out = False
        self.deadline = 0.0
        self._intervals: list[float] = []
        self._last_audio_at: float | None = None
        self.video_buffer: list[bytes] = []
        self.audio_buffer: list[tuple[bytes, bytes]] = []

    def start(self, timeout_seconds: float) -> None:
        """Arm detection -- the caller feeds video/audio via feed_video/
        feed_audio from here on, until one of them reports the buffer
        full, or *timeout_seconds* passes with the caller checking
        expired() itself (there is no timer here to cancel)."""
        self.detecting = True
        self.deadline = time.monotonic() + timeout_seconds

    def expired(self) -> bool:
        """Is detection running, and out of time?"""
        return self.detecting and time.monotonic() >= self.deadline

    def set_config_from_header(self, channels: int, sample_rate: int) -> None:
        """Record what the caller decoded from the codec-info payload's
        own audio trailer (see parse_audio_config), available before a
        single audio frame has arrived -- unlike everything runtime
        detection produces. Frame reconstruction (frame_prefix_len /
        use_header_prepend) is untouched: the trailer says nothing
        about that half of the problem, so it still has to be detected
        regardless of whether this was ever called."""
        self.declared_channels = channels
        self.sample_rate = sample_rate
        self.config_from_header = True

    def feed_video(self, nal: bytes) -> bool:
        """Buffer one video NAL. Returns True once the video-frame cap
        is hit, meaning the caller should call finish() now."""
        self.video_buffer.append(nal)
        return len(self.video_buffer) >= _AAC_DETECTION_VIDEO_FRAME_CAP

    def feed_audio(self, header_tail: bytes, payload: bytes) -> bool:
        """Buffer one audio payload and the header tail it arrived
        with -- which of the two framings it's actually in is not
        decided until finish(), so both halves are kept exactly as
        they arrived (see reconstruct_frame). Returns True once enough
        real inter-frame intervals have been measured, meaning the
        caller should call finish() now."""
        now = time.monotonic()
        if self._last_audio_at is not None:
            interval = now - self._last_audio_at
            if 0 < interval < 0.5:  # skip anything spanning a reconnect gap
                self._intervals.append(interval)
        self._last_audio_at = now
        self.audio_buffer.append((header_tail, payload))
        return len(self._intervals) >= _AAC_DETECTION_INTERVALS

    def reconstruct_frame(self, header_tail: bytes, payload: bytes) -> bytes:
        """Recover one raw AAC frame from what DSM actually sent for it.

        Some cameras put the whole frame, prefixed, in the payload --
        strip_frame_prefix handles that. At least one model instead
        splits the frame across the WS message itself: the payload is
        missing its own leading bytes, and those are exactly what the
        per-message header ends in. finish() decides which of the two
        actually decodes for this camera and sets use_header_prepend
        accordingly.
        """
        if self.use_header_prepend:
            return header_tail + payload
        return strip_frame_prefix(payload, self.frame_prefix_len)

    async def frames_look_valid(self, label: str) -> bool:
        """Quick sanity check: does the frame reconstruction currently
        selected (frame_prefix_len / use_header_prepend -- see
        reconstruct_frame) actually produce decodable AAC for this
        camera?

        detect_frame_prefix_len (see finish()) has already eliminated
        the prefix lengths that are provably wrong when not using
        header-prepend mode, but it cannot confirm the one it returns,
        so this stays the only real check either way. It also covers
        cameras using neither framing at all: feeding ffmpeg the
        wrongly-transformed result doesn't just produce bad audio, it
        stalls the whole muxed pipeline outright. This catches that
        with a throwaway decode attempt before ever committing to a
        real session, so an unsupported camera falls back to
        video-only instead of a broken one.

        Settles channels on the way through, since the channel count
        can only be read off a reconstructed frame and this is where
        the frames get reconstructed. What set_config_from_header
        recorded (declared_channels) is passed in as the fallback, so a
        declaration only decides the layout where no frame names one:
        DSM declares what the camera negotiated, the frames carry what
        it actually sends, and ffmpeg cannot tell the two apart (see
        adts_header). That also means the stream being validated is
        exactly the one the session will go on to send, rather than one
        labelling being checked and another sent.

        Raises ValueError if the reconstruction produces something too
        long to be one frame, which is its own kind of "not ours" --
        see finish(), which decides what to do about it. *label*
        identifies the camera in the log lines below, same as
        WebSocketBridge's own label.
        """
        frames = [
            self.reconstruct_frame(header_tail, raw) for header_tail, raw in self.audio_buffer
        ]
        self.channels = detect_channel_count(frames, self.declared_channels)
        buf = bytearray()
        for frame in frames:
            buf += adts_header(len(frame), self.sample_rate, self.channels) + frame
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
            # Can't validate without ffmpeg; say yes and let the
            # bridge's own OSError handling drop us to video-only.
            return True
        # Which framing was under test, on every line: this runs twice
        # per camera (payload-prefix first, then header-prepend), so an
        # unqualified verdict says nothing about which one produced it.
        mode = "header-embedded prefix" if self.use_header_prepend else "frame prefix"
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(bytes(buf)), timeout=_AAC_PROBE_TIMEOUT
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            self.probe_timed_out = True
            log.debug(
                "WebSocket bridge for %s: %s framing check did not finish in %.0fs",
                label,
                mode,
                _AAC_PROBE_TIMEOUT,
            )
            return False
        # The verdict is "ffmpeg printed nothing", so the text it
        # printed is the whole of the reason a camera loses its audio.
        # Logging it is the only way to tell a real framing error from,
        # say, a build that dislikes an argument.
        complaint = stderr.decode(errors="replace").strip()
        log.debug(
            "WebSocket bridge for %s: %s framing %s",
            label,
            mode,
            f"rejected by ffmpeg: {complaint}" if complaint else "decodes",
        )
        return not complaint

    async def finish(self, label: str) -> tuple[bool, str]:
        """Lock in the AAC sample rate, taken from set_config_from_header
        where the caller found one and from the measured intervals
        otherwise, falling back to the default when neither produced
        one. Work out how to reconstruct a real frame from what DSM
        actually sent (see reconstruct_frame), and verify that
        reconstruction actually decodes for this camera -- everything
        buffered during detection is left in video_buffer/audio_buffer
        either way, for the caller to flush.

        Returns (valid, reason): *reason* explains a False verdict
        (video-only was the right call) and is meaningless otherwise.
        *label* identifies the camera in frames_look_valid's log lines.
        """
        if self._intervals and not self.config_from_header:
            self.sample_rate = nearest_sample_rate(median(self._intervals))
        self.detecting = False
        # Detection is over either way. Left populated, these would make
        # a reconnect that re-enters detection finish instantly off the
        # previous session's measurements.
        self._intervals.clear()
        self._last_audio_at = None

        if not self.audio_buffer:
            # Detection also ends on the video-frame cap and on its own
            # deadline, so it can finish having seen no audio at all.
            # There is nothing to work out from an empty buffer, and
            # nothing to hand ffmpeg either.
            return False, "no AAC audio arrived during detection"

        payloads = [payload for _header_tail, payload in self.audio_buffer]
        prefix_len = detect_frame_prefix_len(payloads)
        # Sticky across both framings tried below, not cleared per probe:
        # "not in a recognized framing" claims every framing was tried
        # and rejected, so one that ran out of time instead has to
        # disqualify that sentence even when the other reached a real
        # verdict.
        self.probe_timed_out = False
        try:
            valid = False
            if prefix_len is not None:
                self.frame_prefix_len = prefix_len
                valid = await self.frames_look_valid(label)
            if not valid:
                # The payload-only prefix model didn't hold for this
                # camera, so try reconstructing frames from the header
                # instead (see reconstruct_frame) before giving up on
                # it. This runs once per bridge, so the flag is still
                # off from __init__ and the attempt above really was
                # the payload-only one.
                self.use_header_prepend = True
                valid = await self.frames_look_valid(label)
        except ValueError:
            # A frame too long for an ADTS header to describe is not
            # one frame, so this camera is using neither framing.
            # Retrying is pointless rather than merely unlikely:
            # header-prepend makes the frame strictly longer than the
            # payload-only strip does, so it can only overflow the
            # same way.
            valid = False
            reason = "AAC frames are longer than an ADTS header can describe"
        else:
            # A probe that ran out of time established nothing about
            # the camera, so it must not be reported as a framing that
            # was tried and rejected.
            reason = (
                f"the AAC framing check did not finish in {_AAC_PROBE_TIMEOUT:.0f}s"
                if self.probe_timed_out
                else "AAC frames are not in a recognized framing"
            )
        return valid, reason
