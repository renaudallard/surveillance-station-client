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

"""Tests for the SURVEILLANCE_MPV_OPTS pass-through (no GTK or libmpv required)."""

from __future__ import annotations

import sys
import types
from typing import Any, ClassVar

import pytest

from surveillance.ui.mpv_widget import MpvGLArea, _mpv_options_from_env


class TestMpvOptionsFromEnv:
    def test_unset_means_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SURVEILLANCE_MPV_OPTS", raising=False)
        assert _mpv_options_from_env() == ([], {})

    def test_entries_split_on_whitespace_and_keep_their_commas(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """hwdec=nvdec,vaapi is mpv's own spelling of a decoder list."""
        monkeypatch.setenv("SURVEILLANCE_MPV_OPTS", "hwdec=nvdec,vaapi  hwdec-extra-frames=12")
        assert _mpv_options_from_env() == ([], {"hwdec": "nvdec,vaapi", "hwdec-extra-frames": "12"})

    def test_bare_entry_is_a_flag_whatever_it_looks_like(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is dropped here: a switch reaches mpv as one, and a
        mistyped separator reaches it as a name it will refuse."""
        monkeypatch.setenv("SURVEILLANCE_MPV_OPTS", "vd-lavc-fast hwdec-extra-frames:12")
        assert _mpv_options_from_env() == (["vd-lavc-fast", "hwdec-extra-frames:12"], {})

    def test_value_keeps_everything_after_the_first_equals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SURVEILLANCE_MPV_OPTS", "vd-lavc-o=strict=1")
        assert _mpv_options_from_env() == ([], {"vd-lavc-o": "strict=1"})

    def test_shell_quoting_protects_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SURVEILLANCE_MPV_OPTS", "title='Front door' hwdec=no")
        assert _mpv_options_from_env() == ([], {"title": "Front door", "hwdec": "no"})


class _FakeMPV:
    """python-mpv's constructor, recording what it was handed, plus the
    option writes and the play() that follow it on the same handle."""

    calls: ClassVar[list[tuple[tuple[str, ...], dict[str, Any]]]] = []

    def __init__(self, *flags: str, **options: Any) -> None:
        self.calls.append((flags, options))
        self.written: dict[str, Any] = {}
        self.played: list[str] = []

    def __setitem__(self, name: str, value: Any) -> None:
        self.written[name] = value

    def __getitem__(self, name: str) -> Any:
        return self.written[name]

    def play(self, url: str) -> None:
        self.played.append(url)


def _constructed(
    monkeypatch: pytest.MonkeyPatch, env: str | None, url: str | None = None
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """The flags and options _on_realize hands mpv.MPV with
    SURVEILLANCE_MPV_OPTS set to *env* (unset for None).

    Runs against a stand-in for the widget, since a real one needs a GL
    context, and one for python-mpv.
    """
    fake_mpv = types.ModuleType("mpv")
    vars(fake_mpv).update(
        MPV=_FakeMPV,
        MpvGlGetProcAddressFn=lambda fn: fn,
        MpvRenderContext=lambda *args, **kwargs: types.SimpleNamespace(),
    )
    monkeypatch.setitem(sys.modules, "mpv", fake_mpv)
    monkeypatch.delenv("SURVEILLANCE_AO", raising=False)
    if env is None:
        monkeypatch.delenv("SURVEILLANCE_MPV_OPTS", raising=False)
    else:
        monkeypatch.setenv("SURVEILLANCE_MPV_OPTS", env)

    class _Widget:
        _apply_playback_options = MpvGLArea._apply_playback_options
        _restart_cache_control = MpvGLArea._restart_cache_control
        _stop_cache_control = MpvGLArea._stop_cache_control
        _tick_cache_control = MpvGLArea._tick_cache_control
        _mpv: Any = None
        _tls_verify = True
        _muted = False
        _volume = 100
        _url = url
        _ctx = None
        _initialized = False
        _low_latency = False
        _muxed_audio = False
        _history_speed = 1.0
        _start_offset = 0.0
        _cache_control_enabled = False
        _cache_control_source = None

        def make_current(self) -> None:
            pass

        def get_error(self) -> None:
            return None

        def _mpv_log(self, *args: Any) -> None:
            pass

        def _mpv_update_cb(self) -> None:
            pass

    widget = _Widget()
    _FakeMPV.calls.clear()
    MpvGLArea._on_realize(widget, widget)  # type: ignore[arg-type]
    assert widget._initialized, "mpv was never constructed"
    assert len(_FakeMPV.calls) == 1
    _last_handle.append(widget._mpv)
    return _FakeMPV.calls[0]


_last_handle: list[Any] = []


class TestOnRealize:
    def test_unset_leaves_the_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        flags, options = _constructed(monkeypatch, None)
        assert flags == ()
        assert options["vo"] == "libmpv"
        assert options["hwdec"] == "auto"

    def test_an_entry_replaces_the_clients_own_option(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """hwdec and loglevel are both set by the client; an entry naming
        one must override it rather than duplicate the keyword."""
        _flags, options = _constructed(monkeypatch, "hwdec=nvdec loglevel=trace")
        assert options["hwdec"] == "nvdec"
        assert options["loglevel"] == "trace"

    def test_flags_and_options_reach_the_constructor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        flags, options = _constructed(monkeypatch, "vd-lavc-fast hwdec-extra-frames=12")
        assert flags == ("vd-lavc-fast",)
        assert options["hwdec-extra-frames"] == "12"

    def test_a_url_set_before_realization_asks_for_no_seek(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other place the start option is written: a slot given a
        URL before its widget was realized plays it from here instead of
        from play(), and a pipe cannot seek to the beginning either."""
        _constructed(monkeypatch, None, url="fd://7")
        handle = _last_handle[-1]
        assert handle.played == ["fd://7"]
        assert handle.written["start"] == "none"
