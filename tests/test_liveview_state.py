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

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from surveillance.api.models import CameraStatus
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
        _history_target=LiveView._history_target,
        _slot_seek_generation={},
        _event_nav_generation=0,
        _event_search_running=False,
        _pending_nudge_seconds=0.0,
    )
    page._forget_pending_lookups = lambda: LiveView._forget_pending_lookups(page)  # type: ignore[arg-type]
    page._set_event_search_busy = lambda f: LiveView._set_event_search_busy(page, f)  # type: ignore[arg-type]
    page._end_timeline_pause = lambda: LiveView._end_timeline_pause(page)  # type: ignore[arg-type]
    page._reset_playback_speed = lambda: LiveView._reset_playback_speed(page)  # type: ignore[arg-type]
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
        page._slot_seek_generation = {}
        page._event_nav_generation = 0
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
            _stream_lost=False,
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

    def test_a_lost_history_slot_reloads_where_it_was(self) -> None:
        """_on_stream_gave_up has already torn the bridge down, but the
        slot is still in History: the camera poll's retry treats it so,
        and Reload has to agree."""
        events: list[object] = []
        page = self._page(events)
        page._slots[0]._stream_lost = True
        LiveView._on_slot_reload(page, 0)  # type: ignore[arg-type]
        assert events == [("stop",), ("seek", 1_700_000_000, 5)]

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


class TestReloadAllStreams:
    """The header bar's Reload restarts the layout a slot at a time.

    All at once is the burst _HISTORY_TRANSITION_STAGGER_MS exists to
    spread out, and a reload is not worth bringing it back."""

    @staticmethod
    def _page(held: dict[int, int], active: list[int]) -> SimpleNamespace:
        slots = []
        for i in range(16):
            cam = SimpleNamespace(id=held[i], name=f"cam{held[i]}") if i in held else None
            slots.append(
                _Calls(
                    index=i,
                    camera=cam,
                    player=_Calls(),
                    _ws_bridge=None,
                    _rtsp_monitor=None,
                    _history_position=None,
                )
            )
        page = SimpleNamespace(_slots=slots, _active=active, staggered=[], reloaded=[])
        page._run_staggered = page.staggered.extend
        page._on_slot_reload = page.reloaded.append
        return page

    def test_only_visible_slots_holding_a_camera_are_reloaded(self) -> None:
        # Slot 2 is hidden under this layout and 5 is empty.
        page = self._page(held={0: 1, 1: 2, 2: 3, 4: 4}, active=[0, 1, 4, 5])
        LiveView.reload_all_streams(page)  # type: ignore[arg-type]
        for action in page.staggered:
            action()
        assert page.reloaded == [0, 1, 4]

    def test_the_restarts_go_through_the_stagger(self) -> None:
        page = self._page(held={0: 1, 1: 2, 4: 3, 5: 4}, active=[0, 1, 4, 5])
        LiveView.reload_all_streams(page)  # type: ignore[arg-type]
        # Handed over whole, not run here: nothing has reloaded yet.
        assert len(page.staggered) == 4
        assert page.reloaded == []

    def test_an_empty_layout_reloads_nothing(self) -> None:
        page = self._page(held={}, active=[0, 1, 4, 5])
        LiveView.reload_all_streams(page)  # type: ignore[arg-type]
        assert page.staggered == []


class TestLostStreamRetryKeepsHistory:
    """sync_camera_statuses retries a slot whose stream gave up. That went
    through _start_stream, the live starter, which has no History
    awareness at all: a slot playing recorded video came back on the live
    edge instead, with no way back to where it was.
    """

    @staticmethod
    def _camera(status: CameraStatus = CameraStatus.ENABLED) -> SimpleNamespace:
        return SimpleNamespace(id=7, name="CAM 7", status=status)

    @staticmethod
    def _slot(history_position: float | None, status: CameraStatus) -> SimpleNamespace:
        return SimpleNamespace(
            index=0,
            camera=SimpleNamespace(id=7, name="CAM 7", status=status),
            # Already torn down by _on_stream_gave_up, which is why the
            # position has to come off the slot rather than the bridge.
            _ws_bridge=None,
            _stream_lost=True,
            _history_position=history_position,
            update_camera=lambda camera: None,
            set_audio_playable=lambda playable: None,
        )

    def _restarts(
        self, slot: SimpleNamespace, cameras: list[SimpleNamespace]
    ) -> list[tuple[int, int, float | None]]:
        restarted: list[tuple[int, int, float | None]] = []
        page = SimpleNamespace(
            _cameras=[],
            _active=[0],
            _slots=[slot],
            _streams_paused=False,
            _restart_slot_stream=lambda idx, camera, target: restarted.append(
                (idx, camera.id, target)
            ),
        )
        LiveView.sync_camera_statuses(page, cameras)  # type: ignore[arg-type]
        return restarted

    def test_a_lost_history_slot_is_retried_where_it_left_off(self) -> None:
        slot = self._slot(1_700_000_500.0, CameraStatus.ENABLED)
        restarted = self._restarts(slot, [self._camera()])
        assert restarted == [(0, 7, 1_700_000_500.0)]

    def test_a_lost_live_slot_is_retried_live(self) -> None:
        slot = self._slot(None, CameraStatus.ENABLED)
        restarted = self._restarts(slot, [self._camera()])
        assert restarted == [(0, 7, None)]

    def test_a_camera_reported_offline_is_not_retried_into_history(self) -> None:
        """_start_stream shows the offline card and drops the position;
        asking it to seek into History for an unreachable camera would
        leave the slot waiting on a recording it cannot fetch."""
        slot = self._slot(1_700_000_500.0, CameraStatus.ENABLED)
        restarted = self._restarts(slot, [self._camera(CameraStatus.DISCONNECTED)])
        assert restarted == [(0, 7, None)]


class TestPushToTalkOnReassign:
    """Assigning a camera turns the slot's mic indicator off, so it has
    to end the session too: a layout switch can hand a still-visible
    slot another camera while someone is talking through the first."""

    def test_assign_ends_a_running_session(self) -> None:
        session = _Calls()
        slot = _slot()
        slot._ptt_session = session
        slot._header = _Calls()
        slot._toolbar = _Calls()
        slot.stop_ptt = lambda: CameraSlot.stop_ptt(slot)  # type: ignore[arg-type]

        CameraSlot.assign(slot, SimpleNamespace(id=2, name="cam2"))  # type: ignore[arg-type]

        assert session.called("stop") == [()]
        assert slot._ptt_session is None
        assert slot.camera.id == 2  # type: ignore[attr-defined]


class TestLostHistorySlot:
    """A History slot whose stream gave up has no bridge, only its saved
    position. Every path that decides between History and Live has to
    read it the same way the camera poll's retry does."""

    @staticmethod
    def _lost_slot() -> _Calls:
        slot = _slot()
        slot._stream_lost = True
        slot._history_position = 1_700_000_500.0
        return slot

    def test_the_live_button_drops_its_position(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(liveview, "run_async", lambda *args, **kwargs: None)
        slot = self._lost_slot()
        page = _page(False, [slot], active=[0])
        page._leaving_history_slots = set()
        page._set_history_position = lambda slot, pos: setattr(slot, "_history_position", pos)
        page._run_staggered = lambda actions: [action() for action in actions]
        page._start_stream = lambda *args: None
        page._timeline_speed = "1"
        page._timeline_reverse = False
        LiveView._return_all_to_live(page)  # type: ignore[arg-type]
        assert slot._history_position is None
        assert LiveView._history_target(slot) is None  # type: ignore[arg-type]

    def test_a_protocol_change_keeps_it_in_history(self) -> None:
        slot = self._lost_slot()
        slot.get_visible = lambda: True  # type: ignore[method-assign]
        restarted: list[tuple[int, int, float | None]] = []
        page = SimpleNamespace(_slots=[slot], _streams_paused=False)
        page._history_target = LiveView._history_target
        page._update_slot_audio = lambda slot, camera: None
        page._restart_slot_stream = lambda idx, camera, target: restarted.append(
            (idx, camera.id, target)
        )
        LiveView.restart_camera(page, 1)  # type: ignore[arg-type]
        assert restarted == [(0, 1, 1_700_000_500.0)]

        # Not while another page is shown: nothing would see the stream,
        # and resume_streams restarts every slot on return anyway.
        restarted.clear()
        page._streams_paused = True
        LiveView.restart_camera(page, 1)  # type: ignore[arg-type]
        assert restarted == []

    def test_starting_its_history_stream_ends_the_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Left marked lost, every camera poll retried it again and
        re-seeked a bridge that was playing fine."""

        class _Bridge:
            current_history_position = 1_700_000_500

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

        monkeypatch.setattr(liveview, "WebSocketBridge", _Bridge)
        monkeypatch.setattr(liveview, "get_history_view_path", lambda api: "wss://nas/history")
        slot = self._lost_slot()
        page = SimpleNamespace(
            app=SimpleNamespace(
                api=SimpleNamespace(profile=SimpleNamespace(verify_ssl=False), sid="sid")
            ),
            _timeline_speed="1",
            _timeline_reverse=False,
            _start_bridge=lambda slot, bridge: None,
        )
        recording = SimpleNamespace(camera_id=1)
        LiveView._enter_history_mode(page, slot, recording, 1_700_000_500)  # type: ignore[arg-type]
        assert slot._stream_lost is False


class TestLeavingThePageDropsLookups:
    """A seek or event lookup that lands after leaving Live View must
    not open History streams on the hidden page."""

    def test_in_flight_results_go_stale(self) -> None:
        slot = _slot(bridge=_bridge())
        page = _page(False, [slot], active=[0])
        page._streams_paused = False
        page._slot_seek_generation = {0: 7}
        page._event_nav_generation = 3
        page._pending_nudge_seconds = 10.0
        LiveView.pause_streams(page)  # type: ignore[arg-type]
        assert page._slot_seek_generation == {}
        assert page._event_nav_generation == 4
        assert page._pending_nudge_seconds == 0.0

        finished: list[int] = []
        page._finish_timeline_seek = finished.append
        LiveView._on_recording_resolved(page, 7, 0, 1, 1_700_000_000, object())  # type: ignore[arg-type]
        assert finished == [0], "the lookup must be dropped, not applied"


class TestSelectedSlotPrompt:
    """A selected slot's header asks for a camera. A status change, such
    as a lost stream's retry saying "attempting reconnect", or a camera
    update used to write over that prompt while it was still selected."""

    class _Header:
        def __init__(self) -> None:
            self.label = ""

        def set_label(self, text: str) -> None:
            self.label = text

        def add_css_class(self, name: str) -> None:
            pass

        def remove_css_class(self, name: str) -> None:
            pass

    def _slot(self) -> SimpleNamespace:
        slot = SimpleNamespace(
            camera=SimpleNamespace(id=1, name="cam"),
            _header=self._Header(),
            _display_index=2,
            _status="",
            _selected=False,
        )
        slot._camera_label = lambda: CameraSlot._camera_label(slot)  # type: ignore[arg-type]
        return slot

    def test_status_and_camera_updates_keep_the_prompt(self) -> None:
        slot = self._slot()
        CameraSlot.set_selected(slot, True)  # type: ignore[arg-type]
        prompt = slot._header.label
        CameraSlot.set_status(slot, "attempting reconnect")  # type: ignore[arg-type]
        CameraSlot.update_camera(slot, SimpleNamespace(id=1, name="cam renamed"))  # type: ignore[arg-type]
        assert slot._header.label == prompt

        CameraSlot.set_selected(slot, False)  # type: ignore[arg-type]
        assert slot._header.label == "cam renamed (attempting reconnect)"


class TestCalendarMonthWithoutAnswer:
    """The picker's month check has to be answered even when no
    availability is coming, or it says "Checking..." forever."""

    def test_a_layout_without_cameras_says_so(self) -> None:
        told: list[tuple[int, int, str]] = []
        page = SimpleNamespace(
            app=SimpleNamespace(api=object()),
            timeline=SimpleNamespace(set_calendar_month_unknown=lambda *a: told.append(a)),
            _active_timeline_cameras=lambda: ([], []),
        )
        LiveView._on_calendar_month_changed(page, 2026, 9)  # type: ignore[arg-type]
        assert told == [(2026, 9, "No cameras in this layout")]

    def test_a_failed_lookup_says_so_unless_superseded(self) -> None:
        told: list[tuple[int, int, str]] = []
        page = SimpleNamespace(
            _calendar_generation=3,
            timeline=SimpleNamespace(set_calendar_month_unknown=lambda *a: told.append(a)),
        )
        LiveView._on_calendar_availability_failed(page, 2, 2026, 8, OSError())  # type: ignore[arg-type]
        assert told == []
        LiveView._on_calendar_availability_failed(page, 3, 2026, 9, OSError())  # type: ignore[arg-type]
        assert told == [(2026, 9, "Could not check recordings for this month")]


class TestReturningToThePageResetsSpeed:
    """Coming back to Live View restarts every slot on Live, and a return
    to Live resets the History speed and direction to 1x forward."""

    def test_speed_and_direction_reset(self) -> None:
        page = SimpleNamespace(
            _streams_paused=True,
            _active=[],
            _slots=[],
            _timeline_speed="16",
            _timeline_reverse=True,
            timeline=_Calls(),
            _sync_history_active=lambda: None,
        )
        page._reset_playback_speed = lambda: LiveView._reset_playback_speed(page)  # type: ignore[arg-type]
        LiveView.resume_streams(page)  # type: ignore[arg-type]
        assert (page._timeline_speed, page._timeline_reverse) == ("1", False)
        assert page.timeline.called("set_speed") == [("1",)]
        assert page.timeline.called("set_reverse") == [(False,)]


class TestLiveDropsLookupsInFlight:
    """A seek or event lookup sent before the Live button, or a layout
    switch, used to land after it and put the slots back into History."""

    def test_live_makes_them_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(liveview, "run_async", lambda *args, **kwargs: None)
        page = _page(False, [], active=[])
        page._slot_seek_generation = {0: 7}
        page._event_nav_generation = 3
        page._leaving_history_slots = set()
        page._run_staggered = lambda actions: None
        page._timeline_speed = "1"
        page._timeline_reverse = False
        page._pending_nudge_seconds = -20.0  # two Back 10s clicks queued
        LiveView._return_all_to_live(page)  # type: ignore[arg-type]
        assert page._slot_seek_generation == {}
        assert page._event_nav_generation == 4
        assert page._pending_nudge_seconds == 0.0

    def test_a_late_bridge_seek_answer_is_dropped(self) -> None:
        bridge = object()
        slot = _slot(bridge=object())  # Live replaced the bridge meanwhile
        finished: list[int] = []
        applied: list[int] = []
        page = SimpleNamespace(_slot_seek_generation={0: 5})
        page._finish_timeline_seek = finished.append
        page._on_history_seek_applied = lambda s, i, pos: applied.append(pos)
        LiveView._on_bridge_seek_done(page, 5, slot, 0, bridge, 1_700_000_000)  # type: ignore[arg-type]
        assert applied == [] and finished == [0]

        slot._ws_bridge = bridge
        LiveView._on_bridge_seek_done(page, 4, slot, 0, bridge, 1_700_000_000)  # type: ignore[arg-type]
        assert applied == [], "a superseded seek must not apply either"
        LiveView._on_bridge_seek_done(page, 5, slot, 0, bridge, 1_700_000_000)  # type: ignore[arg-type]
        assert applied == [1_700_000_000]


class TestPtzCommandOrder:
    """A PTZ Start and its Stop used to be independent tasks. After an
    expired session each logs in again on its own, and a Stop whose login
    came back first reached the NAS before its Start, leaving the motor
    running."""

    async def test_a_slow_start_still_reaches_the_nas_before_its_stop(self) -> None:
        reached: list[str] = []

        async def command(name: str, delay: float) -> None:
            await asyncio.sleep(delay)  # the re-login this one waits on
            reached.append(name)

        page = SimpleNamespace(_ptz_locks={})
        start = asyncio.ensure_future(
            LiveView._ptz_in_order(page, 1, command("upStart", 0.2))  # type: ignore[arg-type]
        )
        stop = asyncio.ensure_future(
            LiveView._ptz_in_order(page, 1, command("upStop", 0.0))  # type: ignore[arg-type]
        )
        await asyncio.gather(start, stop)
        assert reached == ["upStart", "upStop"]

    async def test_other_cameras_are_not_held_up(self) -> None:
        reached: list[str] = []

        async def command(name: str, delay: float) -> None:
            await asyncio.sleep(delay)
            reached.append(name)

        page = SimpleNamespace(_ptz_locks={})
        await asyncio.gather(
            LiveView._ptz_in_order(page, 1, command("cam1", 0.2)),  # type: ignore[arg-type]
            LiveView._ptz_in_order(page, 2, command("cam2", 0.0)),  # type: ignore[arg-type]
        )
        assert reached == ["cam2", "cam1"]


def _nav_event(start: int, camera_id: int = 1, stop: int = 0) -> object:
    from surveillance.api.models import Event

    return Event(
        id=1,
        camera_id=camera_id,
        camera_name="Cam",
        event_type=0,
        start_time=start,
        stop_time=stop,
    )


class TestEventNavGrace:
    """How long after an event's start Previous event goes on to the one
    before it: a share of the visible timeline, held to 10-30s, then
    scaled by max(1, 1 + ln(speed))."""

    @pytest.mark.parametrize(("view_span", "expected"), [(60, 10.0), (500, 20.0), (3600, 30.0)])
    def test_follows_the_visible_span_within_bounds(
        self, view_span: float, expected: float
    ) -> None:
        assert liveview.event_nav_grace_seconds(1.0, view_span) == pytest.approx(expected)

    def test_slow_motion_keeps_the_1x_grace_period(self) -> None:
        assert liveview.event_nav_grace_seconds(0.125, 60) == pytest.approx(10.0)

    def test_grows_with_speed(self) -> None:
        import math

        assert liveview.event_nav_grace_seconds(16.0, 60) == pytest.approx(
            10.0 * (1 + math.log(16))
        )


class TestPickNavTarget:
    # Latest start that still counts: wall clock minus the near-live floor.
    LATEST = 10_000.0

    def _pick(
        self,
        starts: list[int],
        reference: float,
        forward: bool,
        grace: float = 10,
        reverse: bool = False,
        length: int = 20,
    ) -> float | None:
        events = [_nav_event(s, stop=s + length) for s in starts]
        return liveview.pick_nav_target(events, reference, forward, grace, self.LATEST, reverse)

    def test_previous_just_after_an_event_goes_to_the_one_before(self) -> None:
        assert self._pick([500, 1000], reference=1006, forward=False) == 500

    def test_previous_well_into_an_event_returns_to_its_start(self) -> None:
        assert self._pick([500, 1000], reference=1030, forward=False) == 1000

    def test_previous_skips_a_cluster_starting_within_the_grace_period(self) -> None:
        # Motion at 1000 and an object detection a second later: one stop.
        assert self._pick([500, 1000, 1001], reference=1003, forward=False) == 500

    def test_next_skips_what_a_seek_just_landed_on(self) -> None:
        assert self._pick([1000, 1001, 1500], reference=1000, forward=True) == 1500

    def test_events_near_wall_clock_never_count(self) -> None:
        assert self._pick([1000, 10_001], reference=2000, forward=True) is None

    # Playing in reverse, events are entered at their end (start + 20 here).

    def test_reverse_lands_on_the_end(self) -> None:
        assert self._pick([500, 1000], reference=1015, forward=False, reverse=True) == 520

    def test_reverse_next_just_after_landing_goes_further_on(self) -> None:
        # Landed on 1020 (the end of 1000) and played back 4s.
        assert self._pick([1000, 1500], reference=1016, forward=True, reverse=True) == 1520

    def test_reverse_next_well_into_an_event_returns_to_its_end(self) -> None:
        assert self._pick([1000, 1500], reference=1005, forward=True, reverse=True) == 1020

    def test_reverse_previous_skips_what_a_seek_just_landed_on(self) -> None:
        assert self._pick([500, 1000], reference=1020, forward=False, reverse=True) == 520


class TestEventNavSearchWindows:
    def test_previous_steps_back_without_overlap(self) -> None:
        windows = liveview.event_nav_search_windows(100_000, False, None, 200_000)
        assert windows == [(96_400, 100_000), (13_600, 96_400), (-504_800, 13_600)]

    def test_previous_starts_past_what_the_timeline_already_has(self) -> None:
        windows = liveview.event_nav_search_windows(100_000, False, (98_000, 101_000), 200_000)
        assert windows[0] == (96_400, 98_000)

    def test_a_range_not_containing_the_position_is_ignored(self) -> None:
        windows = liveview.event_nav_search_windows(100_000, False, (0, 50_000), 200_000)
        assert windows[0] == (96_400, 100_000)

    def test_next_never_searches_past_now(self) -> None:
        windows = liveview.event_nav_search_windows(100_000, True, None, 102_000)
        assert windows == [(100_000, 102_000)]


class TestEventNavSearch:
    """Previous/Next event picks from the events the timeline already
    has when it can, and only searches past them otherwise."""

    def _page(self, monkeypatch: pytest.MonkeyPatch, cached: dict[int, tuple]) -> SimpleNamespace:
        requested: list[tuple[int, int]] = []
        responses: dict[tuple[int, int], list[object]] = {}

        def list_events(_api: object, _ids: object, _names: object, start: int, end: int) -> object:
            requested.append((start, end))
            return (start, end)

        def run_async(window: tuple[int, int], callback: Callable, error_callback: object) -> None:
            callback(responses.get(window, []))

        monkeypatch.setattr(liveview, "run_async", run_async)
        monkeypatch.setattr(liveview.time, "time", lambda: 1_000_000.0)
        busy: list[object] = []
        seeks: list[float] = []
        canvas = SimpleNamespace(get_view_range=lambda: (0.0, 60.0))
        page = SimpleNamespace(
            app=SimpleNamespace(
                api=object(), event_backend=SimpleNamespace(list_events=list_events)
            ),
            timeline=SimpleNamespace(canvas=canvas, set_event_search_busy=busy.append),
            _active=[0],
            _active_timeline_cameras=lambda: (0, [1]),
            _focus_reference_time=lambda: 500_000.0,
            _timeline_speed="1",
            _timeline_reverse=False,
            _event_nav_generation=0,
            _event_search_running=False,
            _event_cache=cached,
            _event_passes_filter=lambda _ev: True,
            _camera_name=str,
            _on_timeline_seek=seeks.append,
            requested=requested,
            responses=responses,
            busy=busy,
            seeks=seeks,
        )
        page._cached_event_coverage = lambda ids: LiveView._cached_event_coverage(page, ids)
        page._set_event_search_busy = lambda f: LiveView._set_event_search_busy(page, f)
        page._search_event_window = lambda *args: LiveView._search_event_window(page, *args)
        return page

    def test_a_cached_event_needs_no_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = self._page(monkeypatch, {1: (499_000.0, 501_000.0, [_nav_event(499_500)])})
        LiveView._seek_to_nearest_event(page, forward=False)
        assert page.requested == []
        assert page.seeks == [499_500]
        assert page.busy == []  # never shown busy

    def test_searches_on_past_the_cache_until_something_turns_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = self._page(monkeypatch, {1: (499_000.0, 501_000.0, [])})
        page.responses[(413_600, 496_400)] = [_nav_event(420_000)]
        LiveView._seek_to_nearest_event(page, forward=False)
        assert page.requested == [(496_400, 499_000), (413_600, 496_400)]
        assert page.seeks == [420_000]
        # Busy while searching, cleared once found.
        assert page.busy == [False, None]

    def test_nothing_anywhere_clears_the_busy_indicator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = self._page(monkeypatch, {})
        LiveView._seek_to_nearest_event(page, forward=False)
        assert len(page.requested) == 3
        assert page.seeks == []
        assert page.busy == [False, None]


class TestEventSearchBusyBeforeTheTimeline:
    """_forget_pending_lookups runs while Live View is still being built,
    before the timeline exists: clearing the busy indicator then must not
    touch it, or building the page fails and login never completes."""

    def test_nothing_running_leaves_the_timeline_alone(self) -> None:
        page = SimpleNamespace(
            _slot_seek_generation={},
            _event_nav_generation=0,
            _event_search_running=False,
            _pending_nudge_seconds=0.0,
        )
        page._set_event_search_busy = lambda f: LiveView._set_event_search_busy(page, f)
        LiveView._forget_pending_lookups(page)  # no timeline attribute at all
        assert page._event_nav_generation == 1


class TestMarkerWithoutAFocusStream:
    """The shared marker follows whichever slots still play, even once
    the focus slot's own stream has given up and left it bridgeless."""

    def _page(self, paused: bool) -> SimpleNamespace:
        class _Bridge:
            def __init__(self, tick: int) -> None:
                self.tick = tick

            def consume_last_real_tick(self) -> int:
                return self.tick

        focus = SimpleNamespace(index=0, _ws_bridge=None, _history_position=1000.0, player=None)
        other = SimpleNamespace(index=1, _ws_bridge=_Bridge(1016), _history_position=1016.0)
        marker: list[float | None] = []
        page = SimpleNamespace(
            _timeline_paused=paused,
            _timeline_focus_slot=0,
            _slots=[focus, other],
            _active=[0, 1],
            _leaving_history_slots=set(),
            _history_gap_last_set_position=1000.0,
            _history_gap_started_at=None,
            _history_gap_reference_position=0.0,
            _timeline_speed="16",
            _timeline_reverse=False,
            timeline=SimpleNamespace(canvas=SimpleNamespace(set_history_position=marker.append)),
            marker=marker,
        )
        page._set_history_position = lambda slot, pos: LiveView._set_history_position(
            page, slot, pos
        )
        return page

    def test_the_marker_keeps_moving(self) -> None:
        page = self._page(paused=False)
        LiveView._advance_focus_history_position(page)
        assert page.marker == [1016.0]

    def test_a_paused_timeline_keeps_it_still(self) -> None:
        page = self._page(paused=True)
        LiveView._advance_focus_history_position(page)
        assert page.marker == []
