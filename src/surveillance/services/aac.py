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

"""AAC helpers for WebSocket audio muxing (see ws_bridge.py).

DSM never sends a self-contained ADTS frame, which is what ffmpeg's
plain "aac" demuxer needs. The bridge recovers the raw frame and
prepends a synthesized ADTS header the demuxer can find via the sync
word. What has to be recovered is not the same for every camera -- see
_reconstruct_aac_frame in ws_bridge.py for the two shapes seen so far.

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
_aac_frames_look_valid in ws_bridge.py only checks that ffmpeg stays
quiet, so it cannot catch that by itself.

A second camera model (reported in PR #17 as a Reolink RLC-823A) sends
the same adoCodec with no payload prefix at all: the payload is missing
the frame's own leading bytes, and the WS message's header ends in
exactly those. detect_frame_prefix_len does not report that -- it only
eliminates, so it hands back whichever length happens to survive -- so
it is the ffmpeg check that rules the payload-only model out, and
ws_bridge.py's _reconstruct_aac_frame then rebuilds the frame from the
header instead.
"""

from __future__ import annotations

from collections.abc import Sequence

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
