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

"""Tests for LiveView's own state handling, driven on stand-ins: a real
LiveView needs a display, and these methods read only what the stand-in
carries (the same technique as test_mpv_profiles' _applied)."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from surveillance.ui import liveview
from surveillance.ui.liveview import LiveView


class _FakeGLib:
    """Records timeout sources the way GLib hands out ids for them."""

    def __init__(self) -> None:
        self.added: list[tuple[int, Callable[[], bool]]] = []
        self.removed: list[int] = []

    def timeout_add(self, interval: int, callback: Callable[[], bool]) -> int:
        self.added.append((interval, callback))
        return len(self.added)

    def source_remove(self, source_id: int) -> None:
        self.removed.append(source_id)


class TestPageTickers:
    """The two one-second tickers follow the page on and off screen.

    Started on map and stopped on unmap: a page the stack covers stays
    realized, and a login replaces the whole LiveView, so a ticker bound
    to neither would keep an old page alive and ticking forever."""

    @pytest.fixture
    def glib(self, monkeypatch: pytest.MonkeyPatch) -> _FakeGLib:
        fake = _FakeGLib()
        monkeypatch.setattr(liveview, "GLib", fake)
        return fake

    @staticmethod
    def _ticking_page() -> SimpleNamespace:
        return SimpleNamespace(
            _focus_idle_id=0,
            _history_tick_id=0,
            _check_timeline_focus_idle=lambda: True,
            _tick_history_positions=lambda: True,
        )

    def test_map_starts_both_once(self, glib: _FakeGLib) -> None:
        page = self._ticking_page()
        LiveView._on_map(page, None)  # type: ignore[arg-type]
        LiveView._on_map(page, None)  # type: ignore[arg-type]
        assert [interval for interval, _ in glib.added] == [1000, 1000]
        assert {callback for _, callback in glib.added} == {
            page._check_timeline_focus_idle,
            page._tick_history_positions,
        }

    def test_unmap_removes_both_and_only_once(self, glib: _FakeGLib) -> None:
        page = self._ticking_page()
        LiveView._on_map(page, None)  # type: ignore[arg-type]
        LiveView._on_unmap(page, None)  # type: ignore[arg-type]
        LiveView._on_unmap(page, None)  # type: ignore[arg-type]
        assert sorted(glib.removed) == [1, 2]
        assert (page._focus_idle_id, page._history_tick_id) == (0, 0)

    def test_remap_starts_again(self, glib: _FakeGLib) -> None:
        page = self._ticking_page()
        LiveView._on_map(page, None)  # type: ignore[arg-type]
        LiveView._on_unmap(page, None)  # type: ignore[arg-type]
        LiveView._on_map(page, None)  # type: ignore[arg-type]
        assert len(glib.added) == 4
        assert page._focus_idle_id and page._history_tick_id


class _Calls:
    """A stand-in that records every method call by name."""

    # What the code under test reads off a slot, a timeline or a page
    # stand-in; set per instance through the constructor.
    index: int
    camera: object
    player: _Calls
    canvas: _Calls
    _ws_bridge: object
    _rtsp_monitor: object

    def __init__(self, **attrs: object) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.__dict__.update(attrs)

    def __getattr__(self, name: str) -> Callable[..., None]:
        def record(*args: object) -> None:
            self.calls.append((name, args))

        return record

    def called(self, name: str) -> list[tuple[object, ...]]:
        return [args for called, args in self.calls if called == name]


def _slot(camera: object = None, bridge: object = None, monitor: object = None) -> _Calls:
    if camera is None:
        camera = SimpleNamespace(id=1, name="cam")
    return _Calls(index=0, camera=camera, player=_Calls(), _ws_bridge=bridge, _rtsp_monitor=monitor)


def _page(paused: bool, slots: list[_Calls], active: list[int]) -> SimpleNamespace:
    """A LiveView stand-in carrying the state the Pause paths read, with
    the two helpers they call bound to it as the real methods."""
    for i, slot in enumerate(slots):
        slot.index = i
    page = SimpleNamespace(
        _timeline_paused=paused,
        timeline=_Calls(canvas=_Calls()),
        _slots=slots,
        _active=active,
        _set_history_position=lambda slot, pos: None,
    )
    page._end_timeline_pause = lambda: LiveView._end_timeline_pause(page)  # type: ignore[arg-type]
    page._resume_all_slots = lambda: LiveView._resume_all_slots(page)  # type: ignore[arg-type]
    return page


def _bridge(history: bool = False) -> SimpleNamespace:
    # resume() hands back a marker rather than a coroutine: run_async is
    # faked below, so a real coroutine would never be awaited.
    return SimpleNamespace(is_history=history, resume=lambda: "resume")


class TestTimelinePause:
    """Ending a Pause has to reach the bridges, not only the players.

    mpv keeps its pause across stop() and play(), a paused Live bridge
    discards every frame, and a bridge or monitor left unpaused behind a
    paused mpv reads the stall as a dead stream and gives up. So every
    path that leaves a Pause behind resumes what it keeps, and every
    path that restarts everything clears the local pause on the whole
    slot pool first."""

    @pytest.fixture
    def launched(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        launched: list[object] = []
        monkeypatch.setattr(
            liveview,
            "run_async",
            lambda coro, callback=None, error_callback=None: launched.append(coro),
        )
        return launched

    def test_resume_reaches_bridges_monitors_and_every_player(self, launched: list[object]) -> None:
        live = _slot(bridge=_bridge())
        monitor = _Calls()
        rtsp = _slot(monitor=monitor)
        hidden = _slot()
        hidden.camera = None
        page = _page(True, [live, rtsp, hidden], active=[0, 1])
        LiveView._resume_all_slots(page)  # type: ignore[arg-type]
        assert page._timeline_paused is False
        assert page.timeline.called("set_paused") == [(False,)]
        assert launched == ["resume"]
        assert monitor.called("set_paused") == [(False,)]
        for slot in (live, rtsp, hidden):
            assert slot.player.called("set_paused") == [(False,)]

    def test_resume_without_a_pause_touches_no_bridge(self, launched: list[object]) -> None:
        page = _page(False, [_slot(bridge=_bridge())], active=[0])
        LiveView._resume_all_slots(page)  # type: ignore[arg-type]
        assert launched == []
        assert page.timeline.calls == []

    def test_leaving_the_page_ends_the_pause(self, launched: list[object]) -> None:
        slot = _slot(bridge=_bridge())
        page = _page(True, [slot], active=[0])
        page._streams_paused = False
        LiveView.pause_streams(page)  # type: ignore[arg-type]
        assert page._timeline_paused is False
        assert page.timeline.called("set_paused") == [(False,)]
        assert slot.player.called("set_paused") == [(False,)]
        assert slot.called("stop_stream") == [()]

    def test_return_to_live_resumes_the_live_bridge_it_keeps(self, launched: list[object]) -> None:
        slot = _slot(bridge=_bridge(history=False))
        page = _page(True, [slot], active=[0])
        page._leaving_history_slots = set()
        page._timeline_speed = "4"
        page._timeline_reverse = True
        page._run_staggered = lambda actions: [action() for action in actions]
        page._start_stream = lambda *args: None
        LiveView._return_all_to_live(page)  # type: ignore[arg-type]
        assert page._timeline_paused is False
        assert launched == ["resume"]
        assert page.timeline.called("set_speed") == [("1",)]

    def test_a_seek_resumes_before_looking_anything_up(self, launched: list[object]) -> None:
        slot = _slot(bridge=_bridge(history=False))
        page = _page(True, [slot], active=[0])
        page._seek_generation = 0
        page._slot_seek_generation = {}
        page._run_staggered = lambda actions: [action() for action in actions]
        page._seek_slot_to_time = lambda *args: None
        LiveView._on_timeline_seek(page, 1_700_000_000.0)  # type: ignore[arg-type]
        assert page._timeline_paused is False
        assert launched == ["resume"]
        assert page.timeline.canvas.called("ensure_visible") == [(1_700_000_000.0,)]
