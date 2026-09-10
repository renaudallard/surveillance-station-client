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


"""Tests for the mpv playback profiles (no GTK or libmpv required)."""

from __future__ import annotations

from typing import Any

import pytest

from surveillance.ui import mpv_widget
from surveillance.ui.mpv_widget import (
    _CACHE_CONTROL_SPEED_DOWN,
    _CACHE_CONTROL_SPEED_UP,
    _CACHE_HIGH_SPEED_ENTER,
    _CACHE_HIGH_SPEED_MAX_SECONDS,
    _CACHE_SECONDS_DEFAULT,
    _DEMUXER_MAX_BYTES_MIB,
    _DEMUXER_MAX_BYTES_UNCACHED,
    MpvGLArea,
    _cache_control_speed,
    _cache_target_seconds,
    _high_speed_cache_seconds,
    _start_option,
)


class _Recorder:
    """Stands in for the mpv handle, recording every option written.

    Both ways of writing one: python-mpv accepts item and attribute
    assignment alike, so recording only the first would let an option
    added the other way slip past the checks below unnoticed.
    """

    options: dict[str, Any]

    def __init__(self) -> None:
        object.__setattr__(self, "options", {})

    def __setitem__(self, name: str, value: Any) -> None:
        self.options[name] = value

    def __getitem__(self, name: str) -> Any:
        return self.options[name]

    def __setattr__(self, name: str, value: Any) -> None:
        self.options[name.replace("_", "-")] = value


def _applied(*, low_latency: bool, muxed_audio: bool, history_speed: float = 1.0) -> dict[str, Any]:
    """The options one profile writes.

    _apply_playback_options only reads four attributes, so it runs
    against a stand-in rather than a real widget, which would need a GL
    context and libmpv.
    """

    class _Widget:
        def __init__(self) -> None:
            self._mpv = _Recorder()
            self._low_latency = low_latency
            self._muxed_audio = muxed_audio
            self._history_speed = history_speed

    widget = _Widget()
    MpvGLArea._apply_playback_options(widget)  # type: ignore[arg-type]
    return widget._mpv.options


class TestPlaybackProfiles:
    def test_every_profile_writes_the_same_options(self) -> None:
        """A slot's widget is reused across protocol switches, so a
        profile that leaves an option unwritten inherits whatever the
        previous stream set. Each profile must state all of them."""
        muxed = _applied(low_latency=False, muxed_audio=True)
        low_latency = _applied(low_latency=True, muxed_audio=False)
        default = _applied(low_latency=False, muxed_audio=False)
        assert set(muxed) == set(low_latency) == set(default)

    def test_muxed_audio_wins_over_low_latency(self) -> None:
        """Documented precedence: a muxed-audio stream is a container,
        not raw NALs, so it must not get the raw-NAL profile."""
        both = _applied(low_latency=True, muxed_audio=True)
        assert both == _applied(low_latency=False, muxed_audio=True)

    def test_high_history_speed_raises_the_default_profiles_cache(self) -> None:
        """Live (1x) keeps the profile's own small baseline; a
        high-speed History stream gets real margin on top of it."""
        live = _applied(low_latency=False, muxed_audio=False, history_speed=1.0)
        history = _applied(low_latency=False, muxed_audio=False, history_speed=100.0)
        assert live["cache-secs"] == pytest.approx(_CACHE_SECONDS_DEFAULT)
        assert history["cache-secs"] > live["cache-secs"]

    def test_low_latency_cache_stays_off_in_live(self) -> None:
        """A silent camera's Live stream keeps the lowest possible
        latency: no cache at all."""
        live = _applied(low_latency=True, muxed_audio=False, history_speed=1.0)
        assert live["cache"] == "no"
        assert live["cache-secs"] == 0.0

    def test_low_latency_cache_turns_on_at_high_history_speed(self) -> None:
        """Once a fast History rewind needs a real buffer (cache-secs > 0
        via the high-speed addition), the cache actually turns on;
        otherwise cache-secs/demuxer-readahead-secs stay inert and the
        setting does nothing, which is the bug this profile branch used
        to have."""
        history = _applied(low_latency=True, muxed_audio=False, history_speed=100.0)
        assert history["cache"] == "yes"
        assert history["cache-secs"] > 0.0

    def test_demuxer_max_bytes_is_shared_by_every_cached_profile(self) -> None:
        expected = f"{_DEMUXER_MAX_BYTES_MIB:g}MiB"
        muxed = _applied(low_latency=False, muxed_audio=True)
        low_latency = _applied(low_latency=True, muxed_audio=False, history_speed=100.0)
        default = _applied(low_latency=False, muxed_audio=False)
        assert muxed["demuxer-max-bytes"] == expected
        assert low_latency["demuxer-max-bytes"] == expected
        assert default["demuxer-max-bytes"] == expected

    def test_an_uncached_profile_keeps_the_tight_byte_cap(self) -> None:
        """demuxer-max-bytes is the ceiling the demuxer buffers up to
        while playback is stalled, so a cacheless Live stream keeps its
        own small bound rather than the shared one sized for a cache."""
        off = _applied(low_latency=True, muxed_audio=False, history_speed=1.0)
        assert off["cache"] == "no"
        assert off["demuxer-max-bytes"] == _DEMUXER_MAX_BYTES_UNCACHED

    def test_only_low_latency_is_ever_untimed(self) -> None:
        """correct-pts/untimed/container-fps-override/probesize describe
        what the source is, not how deep the buffer is: only the raw
        H.264/H.265 pipe is played untimed off a fixed frame rate, and
        only while it has no cache to keep timing for."""
        for kwargs in (
            {"low_latency": False, "muxed_audio": True},
            {"low_latency": True, "muxed_audio": False, "history_speed": 100.0},
            {"low_latency": False, "muxed_audio": False},
        ):
            options = _applied(**kwargs)
            assert options["correct-pts"] is True
            assert options["untimed"] is False
            assert options["container-fps-override"] == 0
            assert options["demuxer-lavf-probesize"] == 32768

        off = _applied(low_latency=True, muxed_audio=False, history_speed=1.0)
        assert off["correct-pts"] is False
        assert off["untimed"] is True
        assert off["container-fps-override"] == 25
        assert off["demuxer-lavf-probesize"] == 32

    def test_a_zero_cache_does_not_make_a_container_stream_raw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Settings page accepts 0 for either container profile's
        cache size. That turns the cache off, and must do nothing else:
        probesize=32 and a fixed 25fps would wreck an MKV or an RTSP
        stream."""
        monkeypatch.setattr(mpv_widget, "_CACHE_SECONDS_MUXED_AUDIO", 0.0)
        monkeypatch.setattr(mpv_widget, "_CACHE_SECONDS_DEFAULT", 0.0)
        for kwargs in (
            {"low_latency": False, "muxed_audio": True},
            {"low_latency": False, "muxed_audio": False},
        ):
            options = _applied(**kwargs)
            assert options["cache"] == "no"
            assert options["correct-pts"] is True
            assert options["untimed"] is False
            assert options["container-fps-override"] == 0
            assert options["demuxer-lavf-probesize"] == 32768


class TestCacheControlSpeed:
    def test_on_target_is_1x(self) -> None:
        assert _cache_control_speed(2.0, 2.0) == 1.0

    def test_ramps_up_toward_speed_up(self) -> None:
        """Halfway from target to _SPEED_UP_ENTER (1.25x target) should
        land halfway between 1.0x and _CACHE_CONTROL_SPEED_UP."""
        speed = _cache_control_speed(2.5, 2.0)
        assert speed == pytest.approx(1.0 + (_CACHE_CONTROL_SPEED_UP - 1.0) / 2)

    def test_ramps_down_toward_speed_down(self) -> None:
        """Halfway from target to _SPEED_DOWN_ENTER (0.75x target) should
        land halfway between 1.0x and _CACHE_CONTROL_SPEED_DOWN."""
        speed = _cache_control_speed(1.5, 2.0)
        assert speed == pytest.approx(1.0 - (1.0 - _CACHE_CONTROL_SPEED_DOWN) / 2)

    def test_clamps_at_speed_up(self) -> None:
        assert _cache_control_speed(100.0, 2.0) == _CACHE_CONTROL_SPEED_UP

    def test_clamps_at_speed_down(self) -> None:
        assert _cache_control_speed(0.0, 2.0) == _CACHE_CONTROL_SPEED_DOWN

    def test_stays_a_correction_at_every_depth(self) -> None:
        """Both ends are hard bounds: nothing this returns is a
        fast-forward, whatever the cache is doing."""
        for cache_seconds in (0.0, 0.1, 1.0, 2.0, 3.0, 20.0, 1000.0):
            speed = _cache_control_speed(cache_seconds, 2.0)
            assert _CACHE_CONTROL_SPEED_DOWN <= speed <= _CACHE_CONTROL_SPEED_UP


class TestHighSpeedCacheSeconds:
    def test_below_enter_threshold_adds_nothing(self) -> None:
        assert _high_speed_cache_seconds(1.0) == 0.0
        assert _high_speed_cache_seconds(_CACHE_HIGH_SPEED_ENTER - 0.01) == 0.0

    def test_at_enter_threshold_adds_about_one_second(self) -> None:
        assert _high_speed_cache_seconds(_CACHE_HIGH_SPEED_ENTER) == pytest.approx(1.0)

    def test_at_100x_hits_the_configured_max(self) -> None:
        assert _high_speed_cache_seconds(100.0) == pytest.approx(_CACHE_HIGH_SPEED_MAX_SECONDS)

    def test_grows_monotonically_between_the_endpoints(self) -> None:
        low = _high_speed_cache_seconds(4.0)
        mid = _high_speed_cache_seconds(32.0)
        high = _high_speed_cache_seconds(64.0)
        assert 0.0 < low < mid < high < _CACHE_HIGH_SPEED_MAX_SECONDS


class TestCacheTargetSeconds:
    def test_live_speed_keeps_the_bare_baseline(self) -> None:
        assert _cache_target_seconds(0.5, 1.0) == 0.5

    def test_high_history_speed_adds_the_high_speed_term(self) -> None:
        target = _cache_target_seconds(0.5, 100.0)
        assert target == pytest.approx(0.5 + _CACHE_HIGH_SPEED_MAX_SECONDS)


class _PlayerRecorder(_Recorder):
    """_Recorder plus the calls play() makes on the handle itself."""

    played: list[str]

    def __init__(self) -> None:
        super().__init__()
        object.__setattr__(self, "played", [])

    def play(self, url: str) -> None:
        self.played.append(url)


def _widget(recorder: _PlayerRecorder) -> Any:
    """A stand-in for a realized widget over *recorder*.

    play() calls three of its own methods, so those are carried over
    from the real class rather than stubbed: the point is what the whole
    path writes to the handle.
    """

    class _Widget:
        _apply_playback_options = MpvGLArea._apply_playback_options
        _restart_cache_control = MpvGLArea._restart_cache_control
        _stop_cache_control = MpvGLArea._stop_cache_control
        _tick_cache_control = MpvGLArea._tick_cache_control

        def __init__(self) -> None:
            self._mpv = recorder
            self._initialized = True
            self._low_latency = False
            self._muxed_audio = False
            self._history_speed = 1.0
            self._cache_control_enabled = False
            self._cache_control_source = None

    return _Widget()


def _play(url: str = "fd://7", **kwargs: Any) -> _PlayerRecorder:
    """The handle state left by one play(), on the same kind of stand-in
    _applied uses: play() reads only these attributes."""

    recorder = _PlayerRecorder()
    MpvGLArea.play(_widget(recorder), url, **kwargs)  # type: ignore[arg-type]
    return recorder


class TestStartOption:
    """mpv reads "none" as no start position and every other value as one,
    so "0" asked it to seek to the beginning of a stream that cannot
    seek, which it reported at error level on every stream this app
    opened."""

    def test_no_offset_leaves_the_start_unset(self) -> None:
        assert _start_option(0) == "none"
        assert _start_option(0.0) == "none"

    def test_an_offset_is_a_position(self) -> None:
        assert _start_option(12.5) == "12.5"
        assert _start_option(1) == "1"

    def test_play_without_an_offset_asks_for_no_seek(self) -> None:
        recorder = _play()
        assert recorder.options["start"] == "none"
        assert recorder.played == ["fd://7"]

    def test_play_with_an_offset_still_seeks_there(self) -> None:
        """A recording opened at a moment within it: the offset is the
        whole point, and that stream is seekable."""
        recorder = _play(start_offset=42.0)
        assert recorder.options["start"] == "42.0"

    def test_a_reused_widget_drops_the_previous_offset(self) -> None:
        """The option sticks on a handle, so a slot that played a
        recording and then a live stream has to clear it."""
        recorder = _play(start_offset=42.0)
        assert recorder.options["start"] == "42.0"
        MpvGLArea.play(_widget(recorder), "fd://9")  # type: ignore[arg-type]
        assert recorder.options["start"] == "none"
