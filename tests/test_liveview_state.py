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
from surveillance.ui.liveview import CameraSlot, LiveView


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
    _history_position: float | None
    _stream_lost: bool

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
    page._resume_all_slots = lambda **kw: LiveView._resume_all_slots(page, **kw)  # type: ignore[arg-type]
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


class TestStreamStartedUnderPause:
    """A stream that starts while the layout is paused joins the Pause:
    its bridge or monitor is paused along with the player, so neither
    reads the paused mpv as a stream that died."""

    @pytest.fixture
    def launched(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        launched: list[object] = []
        monkeypatch.setattr(
            liveview,
            "run_async",
            lambda coro, callback=None, error_callback=None: launched.append(coro),
        )
        return launched

    @staticmethod
    def _bridge_page(paused: bool) -> SimpleNamespace:
        return SimpleNamespace(
            _timeline_paused=paused,
            _leaving_history_slots=set(),
            _timeline_speed="1",
            _set_history_position=lambda slot, pos: None,
            _on_stream_gave_up=lambda *args: None,
        )

    def test_new_bridge_is_paused_first(self, launched: list[object]) -> None:
        bridge = _Calls(
            is_history=False,
            pause=lambda: "pause",
            start=lambda: "start",
            wait_closed=lambda: "wait_closed",
        )
        slot = _slot()
        page = self._bridge_page(paused=True)
        page._pause_bridge = lambda s: LiveView._pause_bridge(page, s)  # type: ignore[arg-type]
        LiveView._start_bridge(page, slot, bridge)  # type: ignore[arg-type]
        assert bridge.called("request_pause") == [()]
        assert launched == ["pause", "start", "wait_closed"]

    def test_new_bridge_is_left_alone_without_a_pause(self, launched: list[object]) -> None:
        bridge = _Calls(
            is_history=False,
            pause=lambda: "pause",
            start=lambda: "start",
            wait_closed=lambda: "wait_closed",
        )
        page = self._bridge_page(paused=False)
        LiveView._start_bridge(page, _slot(), bridge)  # type: ignore[arg-type]
        assert bridge.called("request_pause") == []
        assert launched == ["start", "wait_closed"]

    @pytest.mark.parametrize("paused", [True, False])
    def test_new_rtsp_monitor_follows_the_pause(self, paused: bool) -> None:
        slot = _slot()
        page = self._bridge_page(paused)
        LiveView._start_rtsp_monitor(page, slot, "rtsp://cam/stream")  # type: ignore[arg-type]
        monitor = slot._rtsp_monitor
        assert isinstance(monitor, liveview.RtspHealthMonitor)
        try:
            assert monitor._paused is paused
            assert slot.player.called("play") == [("rtsp://cam/stream",)]
        finally:
            monitor.stop()


class TestLayoutRestore:
    """A layout switch leaves a slot alone when the incoming layout keeps
    the camera it already streams; everything else starts afresh."""

    @staticmethod
    def _page(active: list[int], saved: list[int], held: dict[int, int]) -> SimpleNamespace:
        cameras = [SimpleNamespace(id=i, name=f"cam{i}") for i in range(1, 10)]
        slots = [_slot() for _ in range(16)]
        for i, slot in enumerate(slots):
            slot.index = i
            slot.camera = SimpleNamespace(id=held[i], name=f"cam{held[i]}") if i in held else None
        page = SimpleNamespace(
            _active=active,
            _slots=slots,
            _current_layout="2x2",
            _streams_paused=False,
            _cameras=cameras,
            app=SimpleNamespace(config=SimpleNamespace(layout_cameras={"2x2": saved})),
            window=SimpleNamespace(sidebar=SimpleNamespace(cameras=cameras)),
            started=[],
            cleared=[],
        )
        page._restore_saved_audio_state = lambda slot, cam: None
        page._update_slot_audio = lambda slot, cam: None
        page._load_slot_ptz_extras = lambda slot, cam: None
        page._request_presence_refresh = lambda: None
        page._start_stream = lambda idx, cam: page.started.append((idx, cam.id))
        page._clear_slot = lambda slot: page.cleared.append(slot.index)
        return page

    def test_unchanged_streaming_slots_are_left_alone(self) -> None:
        page = self._page(active=[0, 1, 4, 5], saved=[1, 2, 3, 4], held={0: 1, 1: 2, 2: 3})
        LiveView._restore_layout_cameras(page, frozenset({0, 1, 2}))  # type: ignore[arg-type]
        assert page.started == [(4, 3), (5, 4)]
        assert page._slots[0].called("assign") == []
        assert page._slots[4].called("assign") != []

    def test_a_hidden_slot_holding_the_camera_starts_afresh(self) -> None:
        page = self._page(active=[0, 1, 4, 5], saved=[1, 2, 3, 4], held={0: 1, 1: 2})
        LiveView._restore_layout_cameras(page, frozenset({4, 5}))  # type: ignore[arg-type]
        assert [idx for idx, _ in page.started] == [0, 1, 4, 5]

    def test_a_slot_changing_camera_is_restarted(self) -> None:
        page = self._page(active=[0, 1, 4, 5], saved=[2, 1, 3, 4], held={0: 1, 1: 2})
        LiveView._restore_layout_cameras(page, frozenset({0, 1}))  # type: ignore[arg-type]
        assert page.started == [(0, 2), (1, 1), (4, 3), (5, 4)]

    def test_set_layout_hands_over_the_outgoing_active_slots(self) -> None:
        page = SimpleNamespace(_current_layout="2x2", _active=[0, 1, 4, 5], handed=None)
        page._save_layout_cameras = lambda: None
        page._apply_layout = lambda: setattr(page, "_active", [0])
        page._restore_layout_cameras = lambda streaming: setattr(page, "handed", streaming)
        page._save_session = lambda: None
        LiveView.set_layout(page, "1x1")  # type: ignore[arg-type]
        assert page.handed == frozenset({0, 1, 4, 5})
        assert page._current_layout == "1x1"


class TestSeekSupersession:
    """A seek waiting its turn in the stagger is dropped once a newer
    seek or a layout switch has moved its slot's generation on."""

    @pytest.fixture
    def launched(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        launched: list[object] = []
        monkeypatch.setattr(
            liveview,
            "run_async",
            lambda coro, callback=None, error_callback=None: launched.append(coro),
        )
        monkeypatch.setattr(liveview, "find_recording_at", lambda api, cam, t: "lookup")
        return launched

    @staticmethod
    def _page(generations: dict[int, int]) -> SimpleNamespace:
        page = SimpleNamespace(
            app=SimpleNamespace(api=object()),
            _slot_seek_generation=generations,
            finished=[],
        )
        page._finish_timeline_seek = page.finished.append
        return page

    def test_a_current_generation_looks_the_recording_up(self, launched: list[object]) -> None:
        page = self._page({3: 7})
        slot = _slot()
        slot.index = 3
        LiveView._seek_slot_to_time(page, slot, 1_700_000_000, 7)  # type: ignore[arg-type]
        assert launched == ["lookup"]
        assert page.finished == []

    def test_a_stale_generation_is_dropped_and_released(self, launched: list[object]) -> None:
        page = self._page({3: 8})
        slot = _slot()
        slot.index = 3
        LiveView._seek_slot_to_time(page, slot, 1_700_000_000, 7)  # type: ignore[arg-type]
        assert launched == []
        assert page.finished == [3]

    def test_a_layout_switch_forgets_every_generation(self, launched: list[object]) -> None:
        page = self._page({3: 8})
        slot = _slot()
        slot.index = 3
        # The part of _apply_layout that matters here, run against the
        # same dict the seek reads.
        page._slot_seek_generation.clear()
        LiveView._seek_slot_to_time(page, slot, 1_700_000_000, 8)  # type: ignore[arg-type]
        assert launched == []
        assert page.finished == [3]


class TestOfflineCard:
    """A slot dropped to the offline card loses its History position,
    and the toolbar's History-active state is re-derived."""

    def test_offline_clears_the_history_position(self) -> None:
        slot = _slot()
        slot._history_position = 1_700_000_000.0
        slot._stream_lost = True
        page = SimpleNamespace(_slots=[slot], positions=[], synced=0)
        page._set_history_position = lambda s, pos: page.positions.append((s, pos))
        page._sync_history_active = lambda: setattr(page, "synced", page.synced + 1)
        camera = SimpleNamespace(id=1, name="cam", status=liveview.CameraStatus.DISCONNECTED)
        LiveView._start_stream(page, 0, camera)  # type: ignore[arg-type]
        assert page.positions == [(slot, None)]
        assert page.synced == 1
        assert slot.called("set_history_mode") == [(False,)]
        assert slot.player.called("play") == [(liveview.OFFLINE_PLACEHOLDER_URL,)]
        assert slot._stream_lost is False

    def test_history_active_ignores_a_slot_on_its_way_back_to_live(self) -> None:
        leaving = _slot(bridge=_bridge(history=True))
        staying = _slot(bridge=_bridge(history=False))
        page = SimpleNamespace(
            timeline=_Calls(),
            _slots=[leaving, staying],
            _active=[0, 1],
            _leaving_history_slots={0},
        )
        LiveView._sync_history_active(page)  # type: ignore[arg-type]
        assert page.timeline.called("set_history_active") == [(False,)]
        page._leaving_history_slots = set()
        LiveView._sync_history_active(page)  # type: ignore[arg-type]
        assert page.timeline.called("set_history_active") == [(False,), (True,)]


class TestResumedPosition:
    """Only the Play button puts a bridge's resumed position on the
    timeline. The other paths settle the position themselves, and a
    resume answers a main-loop turn later, so applying it there would
    overwrite what they just decided."""

    @pytest.fixture
    def callbacks(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        callbacks: list[object] = []
        monkeypatch.setattr(
            liveview,
            "run_async",
            lambda coro, callback=None, error_callback=None: callbacks.append(callback),
        )
        return callbacks

    @staticmethod
    def _paused_page() -> tuple[SimpleNamespace, _Calls]:
        slot = _slot(bridge=_bridge(history=True))
        page = _page(True, [slot], active=[0])
        page.positions = []
        page._set_history_position = lambda s, pos: page.positions.append((s, pos))
        page._apply_resumed_position = lambda s, pos: LiveView._apply_resumed_position(page, s, pos)  # type: ignore[arg-type]
        return page, slot

    def test_play_applies_the_resumed_position(self, callbacks: list[object]) -> None:
        page, slot = self._paused_page()
        page._pause_all_slots = lambda: None
        LiveView._on_timeline_pause_play(page, None)  # type: ignore[arg-type]
        assert callbacks and callbacks[0] is not None
        callbacks[0](1_700_000_042)  # type: ignore[operator]
        assert page.positions == [(slot, 1_700_000_042)]

    def test_a_live_bridge_reports_no_position_to_apply(self, callbacks: list[object]) -> None:
        page, _slot_obj = self._paused_page()
        page._pause_all_slots = lambda: None
        LiveView._on_timeline_pause_play(page, None)  # type: ignore[arg-type]
        callbacks[0](None)  # type: ignore[operator]
        assert page.positions == []

    def test_the_other_paths_ask_for_no_position_callback(self, callbacks: list[object]) -> None:
        page, _slot_obj = self._paused_page()
        LiveView._resume_all_slots(page)  # type: ignore[arg-type]
        assert callbacks == [None]
        assert page.positions == []


class TestSlotReload:
    """The right-click Reload restarts one slot and nothing else.

    A layout switch leaves a slot alone when it keeps its camera (see
    TestLayoutRestore), so this is what resets a single stream that has
    gone bad."""

    @staticmethod
    def _page(events: list[object], bridge: object = None) -> SimpleNamespace:
        slot = _Calls(
            index=0,
            camera=SimpleNamespace(id=7, name="cam7"),
            player=_Calls(),
            _ws_bridge=bridge,
            _rtsp_monitor=None,
            _history_position=1_700_000_000.0,
            stop_stream=lambda: events.append(("stop",)),
        )
        page = SimpleNamespace(
            _slots=[slot],
            _seek_generation=4,
            _slot_seek_generation={},
        )
        page._history_target = LiveView._history_target
        page._restart_slot_stream = lambda *args: LiveView._restart_slot_stream(page, *args)  # type: ignore[arg-type]
        page._start_stream = lambda idx, cam: events.append(("start", idx, cam.id))
        page._seek_slot_to_time = lambda slot, when, gen: events.append(("seek", when, gen))
        return page

    def test_a_live_slot_is_stopped_then_started_again(self) -> None:
        events: list[object] = []
        page = self._page(events)
        LiveView._on_slot_reload(page, 0)  # type: ignore[arg-type]
        assert events == [("stop",), ("start", 0, 7)]
        assert page._slot_seek_generation == {}

    def test_a_history_slot_reloads_where_it_was(self) -> None:
        events: list[object] = []
        page = self._page(events, bridge=SimpleNamespace(is_history=True))
        LiveView._on_slot_reload(page, 0)  # type: ignore[arg-type]
        # Stopped first, or the seek would reuse the bridge it found
        # rather than building the fresh one the reload is for.
        assert events == [("stop",), ("seek", 1_700_000_000, 5)]
        assert page._slot_seek_generation == {0: 5}

    def test_a_live_bridge_does_not_count_as_a_position(self) -> None:
        events: list[object] = []
        page = self._page(events, bridge=SimpleNamespace(is_history=False))
        LiveView._on_slot_reload(page, 0)  # type: ignore[arg-type]
        assert events == [("stop",), ("start", 0, 7)]

    def test_an_empty_slot_reloads_nothing(self) -> None:
        events: list[object] = []
        page = self._page(events)
        page._slots[0].camera = None
        LiveView._on_slot_reload(page, 0)  # type: ignore[arg-type]
        assert events == []

    def test_the_menu_item_hands_over_the_slot_index(self) -> None:
        reloaded: list[int] = []
        slot = SimpleNamespace(
            index=3,
            _menu_popover=_Calls(),
            _reload_callback=reloaded.append,
        )
        CameraSlot._on_menu_reload(slot, None)  # type: ignore[arg-type]
        assert reloaded == [3]
        assert slot._menu_popover.called("popdown") == [()]
