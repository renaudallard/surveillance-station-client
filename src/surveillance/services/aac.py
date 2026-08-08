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

"""AAC (MPEG4-GENERIC over RTP, RFC 3640 "AAC-hbr" mode) helpers for
WebSocket audio muxing (see ws_bridge.py).

DSM delivers each AAC access unit prefixed with an RFC 3640 AU-header
rather than a self-contained ADTS frame. ffmpeg can't read the bare
AU-header framing directly, so each frame needs that header stripped
and a synthesized ADTS header prepended instead, which ffmpeg's plain
"aac" demuxer auto-detects via the ADTS sync word.

The AU-header's length isn't a fixed protocol constant -- RFC 3640
negotiates it per stream (sizeLength/indexLength/etc.), so different
camera models can legitimately use different lengths ("2 bytes", 13-bit
size + 3-bit index, is the widely-documented "AAC-hbr" default, but not
the only one seen in practice). Hardcoding a single length risks
silently breaking whichever cameras don't use it, so the length is
detected per camera instead (see detect_au_header_len) from a handful
of real frames buffered during startup.

That detection can't rely on ffmpeg reporting a decode error: its
decoder silently recovers from a wrong AU-header length via an
internal retry, discarding that packet's own timestamp in the process
without ever surfacing an error -- so ``_aac_frames_look_valid`` (in
ws_bridge.py, which only checks that ffmpeg doesn't error) can't catch
a wrong length on its own.

A second camera model tested alongside this one reported the same
adoCodec but used different framing entirely -- no length in
detect_au_header_len's search range decoded cleanly for it, so audio
for a camera like that still falls back to video-only.
"""

from __future__ import annotations

from collections.abc import Sequence

# DSM doesn't expose the negotiated sample rate directly (adoExtra's
# encoding isn't known), but AAC-hbr's "constantDuration" default means
# every frame carries a fixed 1024 samples -- so timing alone, measured
# from real frame arrivals, is enough to determine the rate live,
# without a per-camera-model lookup table.
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


def strip_au_header(frame: bytes, header_len: int) -> bytes:
    """Remove the leading RFC 3640 AU-header, leaving the raw AAC frame."""
    return frame[header_len:]


# RFC 3640 AAC-hbr's documented default (13-bit size + 3-bit index) is the
# shortest AU-header actually seen in practice -- shorter candidates would
# only ever be tested against real payload bytes, not header bytes, which
# defeats the point (see detect_au_header_len). The upper bound is a
# generous ceiling, matching the range already ruled out for the second,
# unsupported camera model mentioned in the module docstring.
_AU_HEADER_MIN_LEN = 2
_AU_HEADER_MAX_LEN = 8

# AAC's raw_data_block starts with a 3-bit id_syn_ele naming the first
# syntax element; 0b111 is ID_END, meaning "no elements follow". A real,
# non-empty frame can never legitimately start with that.
_AAC_ELEMENT_ID_END = 0b111


def detect_au_header_len(frames: Sequence[bytes]) -> int | None:
    """Determine how many leading bytes of RFC 3640 AU-header framing to
    strip, from a handful of real (still AU-header-prefixed) frames.

    Tries each candidate length in ascending order and returns the first
    (shortest) one whose first post-strip byte never reads as AAC's
    "immediate end, zero elements" marker across every sample frame --
    a real frame's raw_data_block can never legitimately start with
    that, so any candidate that does is provably wrong. Preferring the
    shortest passing candidate matters: a too-long strip reads into real
    (effectively random) payload bytes, which will eventually also
    produce that same marker by chance given enough frames, just less
    reliably than a header byte that hasn't even reached real content
    yet.

    Returns None if no candidate holds across every frame, e.g. a
    camera using an entirely different framing scheme, so callers can
    fall back accordingly instead of guessing.
    """
    if not frames:
        return None
    for length in range(_AU_HEADER_MIN_LEN, _AU_HEADER_MAX_LEN + 1):
        if all(
            len(frame) > length and (frame[length] >> 5) != _AAC_ELEMENT_ID_END for frame in frames
        ):
            return length
    return None


def nearest_sample_rate(interval_seconds: float) -> int:
    """Snap a measured inter-frame interval to the nearest standard AAC
    sample rate, assuming the standard 1024-sample AAC-hbr frame size."""
    if interval_seconds <= 0:
        return 16000
    measured = _SAMPLES_PER_FRAME / interval_seconds
    return min(_STANDARD_SAMPLE_RATES, key=lambda r: abs(r - measured))


def adts_header(payload_length: int, sample_rate: int) -> bytes:
    """Build a 7-byte ADTS header (no CRC, AAC-LC, stereo) for an AAC frame
    of *payload_length* bytes -- lets ffmpeg's plain "aac" demuxer read an
    otherwise-bare AAC stream via ADTS sync-word auto-detection.

    Raises ValueError past what the header can describe. A 1024-sample
    AAC-LC frame never comes anywhere near that, so this catches a caller
    feeding in something that is not one frame rather than a real camera,
    which matters because the alternative is a silently wrong length: the
    field is 13 bits and the excess would just be dropped.
    """
    freq_idx = _ADTS_FREQ_INDEX[sample_rate]
    profile_id = 1  # AAC-LC (object type 2) -> ADTS profile field = object_type - 1
    channels = 2
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
