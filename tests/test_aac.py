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

"""Tests for the AAC-hbr (RFC 3640) helpers used by WebSocket audio muxing."""

from __future__ import annotations

import pytest

from surveillance.services.aac import (
    adts_header,
    detect_au_header_len,
    nearest_sample_rate,
    strip_au_header,
)


def _parse_adts(header: bytes) -> tuple[int, int, int]:
    """Minimal local ADTS parser -- reads back what adts_header() wrote,
    to verify our own bit-packing round-trips correctly without needing
    ffmpeg or embedded real audio data."""
    freq_idx = (header[2] >> 2) & 0xF
    channels = ((header[2] & 0x1) << 2) | ((header[3] >> 6) & 0x3)
    frame_len = ((header[3] & 0x3) << 11) | (header[4] << 3) | ((header[5] >> 5) & 0x7)
    return freq_idx, channels, frame_len


class TestAdtsHeader:
    def test_round_trips_rate_and_length(self) -> None:
        freq_table = {8000: 11, 16000: 8, 44100: 4, 48000: 3}
        for rate, expected_idx in freq_table.items():
            header = adts_header(413, sample_rate=rate)
            freq_idx, ch, frame_len = _parse_adts(header)
            assert freq_idx == expected_idx
            assert ch == 2
            assert frame_len == 413 + 7

    def test_sync_word_and_length(self) -> None:
        header = adts_header(100, sample_rate=8000)
        assert len(header) == 7
        assert header[0] == 0xFF
        assert (header[1] & 0xF0) == 0xF0

    def test_rejects_a_frame_longer_than_the_length_field(self) -> None:
        """13 bits, header included. Past that the excess is dropped and the
        header describes a much shorter frame, which no reader can recover."""
        assert _parse_adts(adts_header(8184, sample_rate=16000))[2] == 8191
        with pytest.raises(ValueError, match="exceeds what an ADTS header"):
            adts_header(8185, sample_rate=16000)


class TestNearestSampleRate:
    def test_snaps_to_known_rates(self) -> None:
        assert nearest_sample_rate(1024 / 8000) == 8000
        assert nearest_sample_rate(1024 / 16000) == 16000
        assert nearest_sample_rate(1024 / 44100) == 44100

    def test_tolerates_jitter(self) -> None:
        # +-15ms jitter around the real ~128ms/8kHz interval, as observed
        # against a real camera -- must not snap to a neighboring rate.
        assert nearest_sample_rate(0.113) == 8000
        assert nearest_sample_rate(0.145) == 8000

    def test_non_positive_interval_has_a_safe_default(self) -> None:
        assert nearest_sample_rate(0) > 0
        assert nearest_sample_rate(-1) > 0


class TestStripAuHeader:
    def test_removes_leading_n_bytes(self) -> None:
        frame = b"\x00\x01\x02\x03\x04"
        assert strip_au_header(frame, 3) == b"\x03\x04"
        assert strip_au_header(frame, 2) == b"\x02\x03\x04"


def _element(id_syn_ele: int, filler: int = 0x00) -> int:
    """A byte whose top 3 bits are the given AAC id_syn_ele value."""
    return ((id_syn_ele & 0x7) << 5) | (filler & 0x1F)


_CPE = 1  # channel_pair_element -- what a real stereo frame starts with
_END = 0b111  # id_syn_ele meaning "no elements follow"


def _frame_with_header(header: bytes, payload_first_byte: int, length: int = 20) -> bytes:
    """A synthetic AU-header-prefixed AAC frame: `header` bytes, then a
    raw_data_block starting with `payload_first_byte`, padded out with
    arbitrary content."""
    payload = bytes([payload_first_byte]) + b"\xaa" * (length - 1)
    return header + payload


class TestDetectAuHeaderLen:
    def test_detects_the_documented_2_byte_default(self) -> None:
        # A 2-byte AU-header (arbitrary size+index bits), then a CPE start.
        frames = [_frame_with_header(bytes([0x34, i]), _element(_CPE)) for i in range(6)]
        assert detect_au_header_len(frames) == 2

    def test_detects_a_3_byte_header_like_the_real_camera_that_needed_this(self) -> None:
        # Mirrors the real capture that motivated this: a 2-byte AU-header
        # followed by one extra byte whose top 3 bits happen to read as
        # AAC's END marker, then the real CPE-starting payload one byte
        # later. A naive "stop at the first non-END offset" check must
        # not stop at length 2 here.
        header = bytes([0x34, 0x1F, _element(_END)])
        frames = [_frame_with_header(header, _element(_CPE)) for _ in range(6)]
        assert detect_au_header_len(frames) == 3

    def test_real_capture_bytes_regression(self) -> None:
        """The actual byte prefixes captured from the camera that exposed
        this bug (see aac.py's module docstring) -- a concrete guard
        against ever regressing back to 2."""
        real_frame_prefixes = [
            bytes.fromhex("341ffc211a1483320ccf8320"),
            bytes.fromhex("34fffc211a14830e06c681b0"),
            bytes.fromhex("339ffc211a148b5606c684b0"),
            bytes.fromhex("31bffc211a148b7c50660b59"),
            bytes.fromhex("345ffc211a14832606d284b0"),
        ]
        assert detect_au_header_len(real_frame_prefixes) == 3

    def test_prefers_the_shortest_passing_candidate(self) -> None:
        # Lengths 2 and 3 both hit END, so they're ruled out. From length
        # 4 onward every byte is 0xAA (non-END either way), so 4, 5, 6...
        # all technically "pass" -- the shortest of those must win, since
        # it's the one that hasn't read into real payload bytes yet.
        header = bytes([0x00, 0x00, _element(_END), _element(_END)])
        frames = [_frame_with_header(header, _element(_CPE)) for _ in range(6)]
        assert detect_au_header_len(frames) == 4

    def test_a_wrong_shorter_candidate_is_ruled_out_by_a_single_bad_frame(self) -> None:
        # Length 2 looks fine (non-END) on five frames but hits END on the
        # sixth -- one counterexample is enough to rule it out, even
        # though length 3 (the real answer here) passes on all six.
        header_ok = bytes([0x34, 0x1F, 0x00])
        header_bad = bytes([0x34, 0x1F, _element(_END)])
        frames = [_frame_with_header(header_ok, _element(_CPE)) for _ in range(5)]
        frames.append(_frame_with_header(header_bad, _element(_CPE)))
        assert detect_au_header_len(frames) == 3

    def test_never_returns_below_the_documented_minimum(self) -> None:
        # Every byte from offset 0 onward looks like a plausible non-END
        # element -- without a floor, a naive scan would stop at 0. The
        # expected floor (2) is hardcoded rather than imported: this
        # guards against the floor itself silently regressing, not just
        # against the algorithm failing to respect whatever it's set to.
        frames = [bytes([_element(_CPE)] * 10) for _ in range(6)]
        assert detect_au_header_len(frames) == 2

    def test_returns_none_for_unrecognized_framing(self) -> None:
        # Every candidate in range hits END somewhere -- no length works,
        # matching the real "second camera model" case in the docstring.
        frames = [bytes([_element(_END)] * 10) for _ in range(6)]
        assert detect_au_header_len(frames) is None

    def test_returns_none_for_frames_too_short_to_test_any_candidate(self) -> None:
        assert detect_au_header_len([b"\x00", b"\x01"]) is None

    def test_returns_none_for_no_frames(self) -> None:
        assert detect_au_header_len([]) is None


class TestSampleRateDetectionRobustness:
    """The rate is taken from the median of real WebSocket arrival
    intervals, measured on the event loop that also serves every other
    camera, so one late frame must not decide the whole session's pitch."""

    @pytest.mark.parametrize("rate", [16000, 22050, 32000, 44100, 48000])
    def test_median_survives_a_scheduling_hiccup(self, rate: int) -> None:
        from statistics import median

        from surveillance.services.aac import nearest_sample_rate
        from surveillance.services.ws_bridge import _AAC_DETECTION_INTERVALS

        nominal = 1024 / rate
        intervals = [nominal] * (_AAC_DETECTION_INTERVALS - 1) + [nominal + 0.050]
        assert nearest_sample_rate(median(intervals)) == rate

    def test_enough_intervals_for_a_median(self) -> None:
        from surveillance.services.ws_bridge import _AAC_DETECTION_INTERVALS

        assert _AAC_DETECTION_INTERVALS >= 3
        assert _AAC_DETECTION_INTERVALS % 2 == 1  # a true middle sample
