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

"""Live view grid for displaying camera streams."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera, CameraStatus, PtzPatrol, PtzPreset, Recording
from surveillance.config import save_config, save_config_now
from surveillance.services import ptz
from surveillance.services.event import list_recording_presence, merge_intervals
from surveillance.services.live import (
    AUDIO_PROTOCOLS,
    OFFLINE_PLACEHOLDER_URL,
    get_history_view_path,
    get_live_view_path,
)
from surveillance.services.ptt import PttOccupiedError, PttSession
from surveillance.services.recording import fetch_camera_thumbnail_at, find_recording_at
from surveillance.services.snapshot import download_snapshot, take_and_save_snapshot
from surveillance.services.ws_bridge import WebSocketBridge
from surveillance.ui.layouts import LAYOUT_VISIBLE, valid_layout
from surveillance.ui.mpv_widget import MpvGLArea, attach_zoom_pan_controls
from surveillance.ui.rtsp_health import RtspHealthMonitor
from surveillance.ui.slot_toolbar import SlotToolbar
from surveillance.ui.timeline import Timeline
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)

# Internal grid is always 4x4 (16 slots).  Positions: idx = row*4 + col.
_GRID_COLS = 4
_MAX_SLOTS = 16

_TIMELINE_THUMBNAIL_WIDTH = 160
_TIMELINE_THUMBNAIL_HEIGHT = 90

# Gap between each slot's own WS-connect/ffmpeg-spawn burst when many
# slots change Live/History state at once (entering History via a
# timeline click, or returning via the Live button/layout switch) --
# without it, a full 4x4 grid opens 16 new WebSocket connections and
# spawns up to 16 new ffmpeg mux processes in the same GTK main-loop
# iteration, on top of whatever the slots being replaced were still
# using (their own teardown is async -- see CameraSlot.stop_stream --
# so there's a real window where both coexist). Confirmed live: this
# reliably OOM-killed the whole app (and took other running
# applications down with it) before staggering was added; 3x3 (9
# slots) survived unstaggered. Not scientifically tuned -- just small
# enough that a full 4x4 transition (16 * this) still feels immediate.
_HISTORY_TRANSITION_STAGGER_MS = 150

_TIMELINE_NUDGE_SECONDS = 10  # Back 10s / Forward 10s's step size

# Safety net for _flush_timeline_nudge's in-flight tracking: normally
# cleared the moment the focus slot's own recording lookup resolves,
# but that signal can go missing (e.g. the timeline focus slot changes
# mid-lookup) -- without this, a single missed signal would wedge
# Back/Forward 10s shut forever. Generous relative to DSM's observed
# few-second lookup latency, since firing early just means a
# still-accumulating burst of clicks gets flushed a little sooner
# rather than fully coalesced -- never incorrect, just less batched.
_NUDGE_RESOLVE_TIMEOUT_SECONDS = 10

# How long a pan/zoom must settle before the presence bar refetches --
# same technique and rough magnitude as the hover-thumbnail debounce
# (see timeline._THUMBNAIL_DEBOUNCE_MS), just slower since a presence
# fetch covers a whole camera list rather than one hover point.
_PRESENCE_DEBOUNCE_MS = 200

# While following "now" the view changes every tick by design (see
# TimelineCanvas._on_tick), which would otherwise mean a presence
# fetch every second; this caps it to an occasional trailing-edge
# refresh instead.
_PRESENCE_LIVE_REFRESH_SEC = 5.0


class CameraSlot(Gtk.Box):
    """Self-contained camera slot with a header label, video player, and
    hover-revealed toolbar (see slot_toolbar.SlotToolbar)."""

    def __init__(self, index: int, tls_verify: bool = True) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.index = index
        self._display_index = index
        self.camera: Camera | None = None

        # Header bar (outside GL rendering area)
        self._header = Gtk.Label(label=f"Slot {index + 1}")
        self._header.add_css_class("slot-header")
        self._header.add_css_class("dim-label")
        self._header.add_css_class("caption")
        self._header.set_xalign(0.5)
        self.append(self._header)

        # Video player, muted by default (see clear()/CameraSlot policy —
        # MpvGLArea itself defaults unmuted since PlayerDialog wants that).
        self.player = MpvGLArea(tls_verify=tls_verify)
        self.player.set_vexpand(True)
        self.player.set_hexpand(True)
        self.player.set_mute(True)

        # Overlay instead of appending the player directly so the hover
        # toolbar can float on top of it.
        self._player_overlay = Gtk.Overlay()
        self._player_overlay.set_child(self.player)
        self.append(self._player_overlay)

        self._toolbar = SlotToolbar(index, self.player)
        self._toolbar.set_snapshot_trigger(self._take_snapshot)
        self._player_overlay.add_overlay(self._toolbar)

        # On the player, not the overlay: the toolbar is an overlay child,
        # so a pointer crossing between the two never leaves the overlay
        # and it would emit nothing. Watching the video itself makes that
        # crossing a leave/enter pair the toolbar's own hover tracking
        # already knows how to handle.
        video_hover = Gtk.EventControllerMotion()
        video_hover.connect(
            "enter", lambda *_a: self._toolbar.notify_video_hover_enter(bool(self.camera))
        )
        video_hover.connect("leave", lambda *_a: self._toolbar.notify_video_hover_leave())
        self.player.add_controller(video_hover)

        # Click handlers — one on the header, one on the player.
        # GLArea consumes events so a CAPTURE gesture on the parent Box
        # only works for the first grid cell; direct gestures work for all.
        header_click = Gtk.GestureClick(button=1)
        header_click.connect("pressed", self._on_click)
        self._header.add_controller(header_click)

        # Scroll-to-zoom (centered on the cursor) and click-and-drag pan,
        # shared with the recording player dialog. The player uses a drag
        # gesture instead of a plain click, so a left-button drag can pan a
        # zoomed-in video — a small movement below the threshold is still
        # treated as a click (slot selection).
        attach_zoom_pan_controls(self.player, on_click=self._invoke_click_callback)

        # Right-click context menu — same header/player dual-gesture reason
        # as the left-click handlers above.
        self._menu_popover = Gtk.Popover()
        self._menu_popover.set_has_arrow(False)
        self._menu_popover.set_parent(self)
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self._snapshot_menu_btn = Gtk.Button(label="Take Snapshot")
        self._snapshot_menu_btn.add_css_class("flat")
        self._snapshot_menu_btn.connect("clicked", self._on_menu_take_snapshot)
        menu_box.append(self._snapshot_menu_btn)

        self._open_1x1_menu_btn = Gtk.Button(label="Open in 1x1 Layout")
        self._open_1x1_menu_btn.add_css_class("flat")
        self._open_1x1_menu_btn.connect("clicked", self._on_menu_open_1x1)
        menu_box.append(self._open_1x1_menu_btn)

        self._clear_menu_btn = Gtk.Button(label="Clear Slot")
        self._clear_menu_btn.add_css_class("flat")
        self._clear_menu_btn.connect("clicked", self._on_menu_clear_slot)
        menu_box.append(self._clear_menu_btn)

        self._menu_popover.set_child(menu_box)

        header_right_click = Gtk.GestureClick(button=3)
        header_right_click.connect("pressed", self._on_right_click)
        self._header.add_controller(header_right_click)

        player_right_click = Gtk.GestureClick(button=3)
        player_right_click.connect("pressed", self._on_right_click)
        self.player.add_controller(player_right_click)

        self._ws_bridge: WebSocketBridge | None = None
        self._rtsp_monitor: RtspHealthMonitor | None = None
        self._ptt_session: PttSession | None = None
        # This slot's current position within History playback, unix
        # time — None whenever it's on Live (or RTSP, which has no
        # History at all). Set on every seek and ticked forward by
        # LiveView's own history-position timer while playing (see
        # _tick_history_positions); read back when this slot becomes
        # the timeline's focus slot, to show its position rather than
        # whichever slot had focus last (see _set_timeline_focus_slot).
        self._history_position: float | None = None
        # Set when a stream gives up while the camera is still reported
        # ENABLED (a transport-level failure, not a status change) — there's
        # no future status transition to retry on, so sync_camera_statuses()
        # retries this slot on every poll instead, until it recovers.
        self._stream_lost = False
        self._click_callback: object = None
        self._status = ""  # stream state shown after the camera name
        self._snapshot_callback: object = None
        self._open_1x1_callback: object = None
        self._clear_slot_callback: object = None
        self._open_1x1_available_callback: object = None

    def set_click_callback(self, callback: object) -> None:
        self._click_callback = callback

    def set_snapshot_callback(self, callback: object) -> None:
        self._snapshot_callback = callback

    def set_open_1x1_callback(self, callback: object) -> None:
        self._open_1x1_callback = callback

    def set_clear_slot_callback(self, callback: object) -> None:
        self._clear_slot_callback = callback

    def set_open_1x1_available_callback(self, callback: object) -> None:
        """Callback returning bool: whether "Open in 1x1 Layout" makes
        sense right now (False when the grid is already showing just this
        one slot in 1x1 — there'd be nothing to do)."""
        self._open_1x1_available_callback = callback

    # -- Thin forwarders to the toolbar, keeping LiveView's own call sites
    # unaware of the CameraSlot/SlotToolbar split. --

    def set_volume_changed_callback(self, callback: object) -> None:
        self._toolbar.set_volume_changed_callback(callback)

    def set_saved_volume(self, volume: int) -> None:
        self._toolbar.set_saved_volume(volume)

    def set_mute_changed_callback(self, callback: object) -> None:
        self._toolbar.set_mute_changed_callback(callback)

    def set_saved_mute(self, muted: bool) -> None:
        self._toolbar.set_saved_mute(muted)

    def set_audio_playable(self, playable: bool) -> None:
        self._toolbar.set_audio_playable(playable)

    def set_history_mode(self, is_history: bool) -> None:
        self._toolbar.set_history_mode(is_history)

    def set_mic_callback(self, callback: object) -> None:
        self._toolbar.set_mic_callback(callback)

    def set_mic_active(self, active: bool) -> None:
        self._toolbar.set_mic_active(active)

    def _update_mute_icon(self) -> None:
        self._toolbar.update_mute_icon()

    def set_zoom_callback(self, callback: object) -> None:
        self._toolbar.set_zoom_callback(callback)

    def set_focus_callback(self, callback: object) -> None:
        self._toolbar.set_focus_callback(callback)

    def set_ptz_callback(self, callback: object) -> None:
        self._toolbar.set_ptz_callback(callback)

    def set_preset_callback(self, callback: object) -> None:
        self._toolbar.set_preset_callback(callback)

    def set_patrol_callback(self, callback: object) -> None:
        self._toolbar.set_patrol_callback(callback)

    def set_presets(self, presets: list[PtzPreset]) -> None:
        self._toolbar.set_presets(presets)

    def set_patrols(self, patrols: list[PtzPatrol]) -> None:
        self._toolbar.set_patrols(patrols)

    def _take_snapshot(self) -> None:
        if self._snapshot_callback and callable(self._snapshot_callback):
            self._snapshot_callback(self.index)

    def _on_right_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        if n_press != 1:
            return
        has_camera = self.camera is not None
        self._snapshot_menu_btn.set_sensitive(has_camera)
        self._clear_menu_btn.set_sensitive(has_camera)

        show_open_1x1 = True
        if self._open_1x1_available_callback and callable(self._open_1x1_available_callback):
            show_open_1x1 = self._open_1x1_available_callback()
        self._open_1x1_menu_btn.set_visible(show_open_1x1)
        self._open_1x1_menu_btn.set_sensitive(has_camera)

        widget = gesture.get_widget()
        if widget is not None:
            # Despite gi's stub claiming a (bool, x, y) triple, this
            # actually returns a plain (x, y) tuple at runtime (confirmed
            # directly) — unpacking a third "ok" value here raised
            # ValueError on every right-click, silently aborting before
            # the popover's popup() call below ever ran.
            coords = widget.translate_coordinates(self, x, y)
            if coords is not None:
                px, py = coords
                rect = Gdk.Rectangle()
                rect.x, rect.y, rect.width, rect.height = int(px), int(py), 1, 1
                self._menu_popover.set_pointing_to(rect)
        self._menu_popover.popup()

    def _on_menu_take_snapshot(self, btn: Gtk.Button) -> None:
        self._menu_popover.popdown()
        self._take_snapshot()

    def _on_menu_open_1x1(self, btn: Gtk.Button) -> None:
        self._menu_popover.popdown()
        if self._open_1x1_callback and callable(self._open_1x1_callback):
            self._open_1x1_callback(self.index)

    def _on_menu_clear_slot(self, btn: Gtk.Button) -> None:
        self._menu_popover.popdown()
        if self._clear_slot_callback and callable(self._clear_slot_callback):
            self._clear_slot_callback(self.index)

    def _on_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        if n_press == 1:
            self._invoke_click_callback()

    def _invoke_click_callback(self) -> None:
        if self._click_callback and callable(self._click_callback):
            self._click_callback(self.index)

    def set_display_index(self, display_idx: int) -> None:
        self._display_index = display_idx
        if not self.camera:
            self._header.set_label(f"Slot {display_idx + 1}")

    def _camera_label(self) -> str:
        """Header text for an assigned camera, with its stream state."""
        name = self.camera.name if self.camera else ""
        return f"{name} ({self._status})" if self._status else name

    def set_selected(self, selected: bool) -> None:
        if selected:
            self._header.remove_css_class("dim-label")
            self._header.add_css_class("slot-selected-label")
            self._header.set_label(f"▶ Slot {self._display_index + 1} — click a camera")
        elif self.camera:
            self._header.remove_css_class("slot-selected-label")
            self._header.add_css_class("dim-label")
            self._header.set_label(self._camera_label())
        else:
            self._header.remove_css_class("slot-selected-label")
            self._header.add_css_class("dim-label")
            self._header.set_label(f"Slot {self._display_index + 1}")

    def set_timeline_focus(self, focused: bool) -> None:
        """Toggle the border marking this as the timeline's reference
        camera — independent of set_selected(), which changes the
        header text for the unrelated camera-assignment click flow."""
        if focused:
            self.add_css_class("timeline-focus-frame")
        else:
            self.remove_css_class("timeline-focus-frame")

    def set_status(self, status: str) -> None:
        """Show the stream state next to the camera name, "" once playing."""
        self._status = status
        if self.camera:
            self._header.set_label(self._camera_label())

    def assign(self, camera: Camera) -> None:
        self.camera = camera
        self._status = ""
        self._header.set_label(camera.name)
        self._header.remove_css_class("slot-selected-label")
        self._header.add_css_class("dim-label")
        self._toolbar.assign(camera)

    def update_camera(self, camera: Camera) -> None:
        """Refresh this slot's camera data (e.g. a status change) without
        resetting stream state or selection UI, unlike assign()."""
        self.camera = camera
        self._header.set_label(self._camera_label())

    def stop_stream(self) -> None:
        """Stop playback, then tear down the WebSocket bridge / RTSP monitor.

        mpv has to let go of the pipe before the bridge closes it. The next
        bridge calls os.pipe() and gets the very same descriptor numbers
        back, so a demuxer still holding the old ones would read the new
        stream out from under it and never decode a frame.
        """
        self.player.stop()
        if self._rtsp_monitor is not None:
            self._rtsp_monitor.stop()
            self._rtsp_monitor = None
        if self._ws_bridge is not None:
            bridge = self._ws_bridge
            self._ws_bridge = None
            bridge.close_write_end()
            run_async(bridge.stop())

    def stop_ptt(self) -> None:
        """End any push-to-talk session on this slot.

        Deliberately separate from stop_stream(): push-to-talk is its own
        audio-out channel to the camera, so a routine video restart (a
        protocol change, a status flap, a new stream URL) must not cut a
        conversation short. Only the paths where the slot itself stops
        being used call this.
        """
        if self._ptt_session is not None:
            self._ptt_session.stop()
            self._ptt_session = None
            self.set_mic_active(False)

    def clear(self) -> None:
        self.stop_stream()
        self.stop_ptt()
        self.player.reset_zoom()
        self._toolbar.clear()
        self.set_history_mode(False)
        self._history_position = None
        self.camera = None
        self._status = ""
        self._stream_lost = False
        self._header.set_label(f"Slot {self._display_index + 1}")
        self._header.remove_css_class("slot-selected-label")
        self._header.add_css_class("dim-label")


class LiveView(Gtk.Box):
    """Live camera view with configurable grid layout."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.app = window.app
        self._selected_slot: int | None = None
        # Slot whose camera the timeline previews on hover — independent
        # of _selected_slot (that one's for the camera-assignment click
        # flow and can be unset; this one always points at a real slot).
        self._timeline_focus_slot: int = 0
        self._timeline_last_activity: float = 0.0
        # Back/Forward 10s coalescing -- see _flush_timeline_nudge.
        self._pending_nudge_seconds: float = 0.0
        self._nudge_seek_in_flight: bool = False
        # Bumped once per _seek_slot_to_time batch (see _on_timeline_seek)
        # so _on_recording_resolved can tell a lookup superseded by a
        # newer seek -- landing late, after that newer one already
        # applied -- from one still worth acting on.
        self._seek_generation: int = 0
        # Timeline Pause/Play's own state -- distinct from
        # _streams_paused below (that one's for navigating away from
        # Live View entirely, this one's a deliberate user action that
        # persists while the page stays open; see _pause_all_slots).
        self._timeline_paused: bool = False
        # Timeline speed dropdown's own state -- DSM's literal
        # multiplier string (see WebSocketBridge.set_speed), applied to
        # every active History slot the same way Pause is, and reset to
        # "1" wherever Pause's own state is (leaving History, switching
        # layout): see _return_all_to_live/_apply_layout.
        self._timeline_speed: str = "1"
        # Timeline Fwd/Rev toggle's own state -- same scope/reset as
        # _timeline_speed (see WebSocketBridge.set_reverse).
        self._timeline_reverse: bool = False
        self._active: list[int] = []  # physical indices of visible slots
        # Recording-presence cache: camera_id -> (covered_start,
        # covered_end, spans). A view fully within [covered_start,
        # covered_end] is served from cache; anything else triggers a
        # fetch of the view padded by one window's width on each side,
        # which replaces the entry (see _presence_covers/_refresh_presence)
        # -- not a general interval-coverage set, just enough to avoid
        # re-fetching on the small pans/zooms this bar is actually used for.
        # Deliberately NOT consulted at all while following "now" (see
        # _on_timeline_view_changed's force=True) -- a live fetch's own
        # padding reaches past "now" into a future with no recordings
        # yet, so treating that as durable coverage would freeze the
        # trailing edge at whatever was true the first time it was ever
        # fetched, for every camera sharing that cache entry.
        self._presence_cache: dict[int, tuple[float, float, list[tuple[int, int]]]] = {}
        self._presence_debounce_id = 0
        # Bumped per fetch batch so a slower-landing fetch superseded by
        # a newer view change never overwrites the cache with stale data
        # -- same technique as _seek_generation/_timeline_thumbnail_generation.
        self._presence_generation = 0
        self._presence_last_live_fetch = 0.0
        self._current_layout: str = valid_layout(self.app.config.grid_layout)
        self._cameras: list[Camera] = []  # last known camera list
        self._streams_paused = False  # true while another page is shown

        self.set_hexpand(True)
        self.set_vexpand(True)

        # Grid container
        self.grid = Gtk.Grid()
        self.grid.add_css_class("live-grid")
        self.grid.set_row_spacing(2)
        self.grid.set_column_spacing(2)
        self.grid.set_row_homogeneous(True)
        self.grid.set_column_homogeneous(True)
        self.grid.set_hexpand(True)
        self.grid.set_vexpand(True)
        self.grid.set_overflow(Gtk.Overflow.HIDDEN)

        # Pre-create all 16 slots (max for 4x4) and attach to the grid.
        # Slots are never removed — only shown/hidden on layout change.
        tls_verify = self.app.api.profile.verify_ssl if self.app.api else True
        self._slots: list[CameraSlot] = []
        for i in range(_MAX_SLOTS):
            r, c = divmod(i, _GRID_COLS)
            slot = CameraSlot(i, tls_verify=tls_verify)
            slot.set_click_callback(self._on_slot_clicked)
            slot.set_snapshot_callback(self._on_slot_take_snapshot)
            slot.set_open_1x1_callback(self._on_slot_open_1x1)
            slot.set_clear_slot_callback(self._on_slot_clear)
            slot.set_open_1x1_available_callback(lambda: self._current_layout != "1x1")
            slot.set_volume_changed_callback(self._on_slot_volume_changed)
            slot.set_mute_changed_callback(self._on_slot_mute_changed)
            slot.set_mic_callback(self._on_slot_mic_toggle)
            slot.set_zoom_callback(self._on_slot_zoom)
            slot.set_focus_callback(self._on_slot_focus)
            slot.set_ptz_callback(self._on_slot_ptz_move)
            slot.set_preset_callback(self._on_slot_preset)
            slot.set_patrol_callback(self._on_slot_patrol)
            self.grid.attach(slot, c, r, 1, 1)
            self._slots.append(slot)

        # Apply initial layout (show/hide slots)
        self._apply_layout()

        self.timeline = Timeline()
        self.timeline.set_visible(self.app.config.timeline_visible)
        self.timeline.canvas.set_hover_callback(self._on_timeline_hover)
        self.timeline.canvas.set_hover_leave_callback(self._on_timeline_hover_leave)
        self.timeline.canvas.set_seek_callback(self._on_timeline_seek)
        self.timeline.canvas.set_view_changed_callback(self._on_timeline_view_changed)
        self.timeline.live_btn.connect("clicked", self._on_timeline_live_clicked)
        self.timeline.back_10s_btn.connect("clicked", self._on_timeline_back_10s)
        self.timeline.forward_10s_btn.connect("clicked", self._on_timeline_forward_10s)
        self.timeline.pause_btn.connect("clicked", self._on_timeline_pause_play)
        self.timeline.set_speed_callback(self._on_timeline_speed_selected)
        self.timeline.set_reverse_callback(self._on_timeline_reverse_selected)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.grid)
        content.append(self.timeline)

        # A Gtk.Overlay rather than a Gtk.Popover for the hover-preview
        # thumbnail: a popover is a separate native surface, and one
        # positioned to appear over the grid never actually became
        # visible in testing — the grid's video slots are GL-rendered
        # (MpvGLArea) and paint over it regardless of its own reported
        # visible/mapped state. An overlay child is composited in the
        # same render tree as its base, same as each slot's own
        # hover toolbar already floats over its GL video correctly (see
        # CameraSlot._player_overlay) — the same fix applied one level
        # up, spanning the whole grid+timeline rather than one slot.
        self._thumbnail_picture = Gtk.Picture()
        self._thumbnail_picture.set_size_request(
            _TIMELINE_THUMBNAIL_WIDTH, _TIMELINE_THUMBNAIL_HEIGHT
        )
        self._thumbnail_picture.add_css_class("timeline-thumbnail")

        # Date/time overlaid on the thumbnail itself, matching DSM's own
        # hover preview. A second, inner Overlay (its size follows the
        # picture, the base child — the label is just floated on top,
        # same as the picture is floated on the grid+timeline below).
        self._thumbnail_time_label = Gtk.Label()
        self._thumbnail_time_label.add_css_class("timeline-thumbnail-time")
        self._thumbnail_time_label.set_justify(Gtk.Justification.CENTER)
        self._thumbnail_time_label.set_halign(Gtk.Align.CENTER)
        self._thumbnail_time_label.set_valign(Gtk.Align.END)
        self._thumbnail_time_label.set_margin_bottom(4)

        self._thumbnail_frame = Gtk.Overlay()
        self._thumbnail_frame.set_child(self._thumbnail_picture)
        self._thumbnail_frame.add_overlay(self._thumbnail_time_label)
        self._thumbnail_frame.set_halign(Gtk.Align.START)
        self._thumbnail_frame.set_valign(Gtk.Align.START)
        self._thumbnail_frame.set_visible(False)
        # Input-transparent: sitting over the ruler, it would otherwise
        # swallow the very motion events that keep it positioned.
        self._thumbnail_frame.set_can_target(False)
        self._timeline_thumbnail_generation = 0

        self._overlay = Gtk.Overlay()
        self._overlay.set_child(content)
        self._overlay.add_overlay(self._thumbnail_frame)
        # Without this, Gtk.Overlay measures the child within whatever
        # space is left after its margin, shrinking it near the right
        # edge instead of keeping its requested size — our own margin
        # clamp in _show_timeline_thumbnail is what actually keeps it
        # on-screen, so the overlay doesn't need to constrain it too.
        self._overlay.set_clip_overlay(self._thumbnail_frame, False)
        self.append(self._overlay)

        GLib.timeout_add(1000, self._check_timeline_focus_idle)
        GLib.timeout_add(1000, self._tick_history_positions)

    # ------------------------------------------------------------------
    # Layout management
    # ------------------------------------------------------------------

    def _apply_layout(self) -> None:
        """Show/hide slots to match the current layout."""
        # History mode is scoped to the layout it was entered in -- a
        # switch always leaves it, on every slot, not just ones becoming
        # hidden (see _return_all_to_live). Before self._active changes
        # below, since that's what it reads to know which slots to check.
        self._return_all_to_live()
        # Same scoping for a Pause left active. Every slot's own local
        # pause needs clearing explicitly here, not just the toolbar's
        # tracking flag/icon: slot objects are a fixed pool reused
        # across layout switches, not recreated, and mpv.play() never
        # resets mpv.pause on its own -- a slot paused in the old
        # layout would otherwise still show a frozen picture under
        # whatever camera the new layout puts on it, with the toolbar
        # wrongly reading "playing". Unconditionally, across the whole
        # pool rather than just self._active: a slot hidden by this
        # switch can still be reused by a later one.
        if self._timeline_paused:
            self._timeline_paused = False
            self.timeline.set_paused(False)
        for slot in self._slots:
            slot.player.set_paused(False)
            if slot._rtsp_monitor is not None:
                slot._rtsp_monitor.set_paused(False)
        new_active = list(LAYOUT_VISIBLE[self._current_layout])
        self._select_slot(None)

        # Stop streams on slots that are becoming hidden. Zoom is reset for
        # every slot on every layout switch, not just ones changing camera —
        # deliberately not "remembered per camera" across layouts (unlike
        # DSM's own Monitor Center) since a slot showing the same camera in
        # two layouts wouldn't otherwise get a fresh stream to reset it on.
        # Mute, by contrast, auto-mutes only the slots actually losing
        # visibility here — one staying visible with the same camera
        # shouldn't have its manual mute/volume choice clobbered by an
        # unrelated layout switch. A newly-shown slot gets its camera's
        # mute/volume restored separately, via _restore_saved_audio_state()
        # in _restore_layout_cameras().
        for i, slot in enumerate(self._slots):
            slot.player.reset_zoom()
            if i in new_active:
                slot.set_visible(True)
                display_idx = new_active.index(i)
                slot.set_display_index(display_idx)
            else:
                slot.set_visible(False)
                if slot.camera:
                    slot.player.set_mute(True)
                    slot._update_mute_icon()
                    slot.stop_stream()
                    slot.stop_ptt()

        self._active = new_active
        self._set_timeline_focus_slot(new_active[0])

    def register_timeline_activity(self) -> None:
        """Record mouse activity anywhere in the app window.

        Called from MainWindow's window-level motion controller. Keeps
        the current timeline-focus slot's frame lit as long as there's
        been activity within the last 10s, and revives it on the next
        activity even after it's already faded — so moving toward and
        hovering the timeline re-lights whichever slot it previews,
        with nothing timeline-specific needed here to make that happen.
        """
        self._timeline_last_activity = time.time()
        self._slots[self._timeline_focus_slot].set_timeline_focus(True)

    def _check_timeline_focus_idle(self) -> bool:
        if time.time() - self._timeline_last_activity >= 10.0:
            self._slots[self._timeline_focus_slot].set_timeline_focus(False)
        return True  # continue ticking

    def _set_timeline_focus_slot(self, slot_idx: int) -> None:
        if slot_idx != self._timeline_focus_slot:
            self._slots[self._timeline_focus_slot].set_timeline_focus(False)
            self._timeline_focus_slot = slot_idx
            # The shared marker follows whichever slot the timeline
            # focuses on (see _set_history_position) — switching focus
            # has to resync it to the new slot's own position, History
            # or not, rather than leaving the previous slot's marker up.
            self.timeline.canvas.set_history_position(self._slots[slot_idx]._history_position)
            # The presence bar's own focus row follows the same switch.
            self._request_presence_refresh()
        self.register_timeline_activity()

    def _tick_history_positions(self) -> bool:
        """Refresh every slot's History playback position from its own
        bridge's actual last-received frame (see
        WebSocketBridge.current_history_position) rather than assuming
        forward progress ourselves -- a theoretical wall-clock-based
        estimate drifts from real playback at any speed but 1x, since
        DSM needs real time to ramp delivery up (or down) to a new
        rate rather than changing it instantly, and stays wrong
        afterwards for as long as the estimate's own assumptions don't
        match what's actually playing. Skipped for a paused slot: its
        position is frozen by WebSocketBridge.pause() itself."""
        for slot_idx in self._active:
            slot = self._slots[slot_idx]
            if slot._ws_bridge is None or slot._ws_bridge.is_paused:
                continue
            position = slot._ws_bridge.current_history_position
            if position is not None:
                self._set_history_position(slot, position)
        return True  # continue ticking

    def _on_timeline_hover(self, local_x: float, timestamp: float) -> None:
        """TimelineCanvas.set_hover_callback target.

        Empty and offline slots both just mean "no thumbnail" — same as
        fetch_camera_thumbnail_at's own empty-result case for a camera
        with nothing recorded at that time, so nothing further needs to
        distinguish them here.
        """
        camera = self._slots[self._timeline_focus_slot].camera
        if camera is None or not self.app.api:
            self._thumbnail_frame.set_visible(False)
            return

        # PyGObject versions disagree on whether this returns (ok, x, y)
        # or just (x, y) on success / None on failure — handle both
        # rather than pin to whichever shape this system happens to use.
        translated = self.timeline.canvas.translate_coordinates(self._overlay, local_x, 0)
        if translated is None:
            return
        if len(translated) == 3:
            ok, overlay_x, overlay_y = translated
            if not ok:
                return
        else:
            overlay_x, overlay_y = translated

        self._timeline_thumbnail_generation += 1
        generation = self._timeline_thumbnail_generation

        run_async(
            fetch_camera_thumbnail_at(self.app.api, camera.id, int(timestamp)),
            callback=lambda data: self._show_timeline_thumbnail(
                generation, overlay_x, overlay_y, timestamp, data
            ),
            error_callback=lambda exc: log.debug("Timeline thumbnail fetch failed: %s", exc),
        )

    def _on_timeline_hover_leave(self) -> None:
        self._timeline_thumbnail_generation += 1  # orphan any fetch already in flight
        self._thumbnail_frame.set_visible(False)

    # ------------------------------------------------------------------
    # Recording presence
    # ------------------------------------------------------------------

    def _on_timeline_view_changed(self, start: float, end: float, following: bool) -> None:
        """TimelineCanvas.set_view_changed_callback target.

        Two very different cadences share this one callback: a settled
        pan/zoom (following=False) is debounced like the hover thumbnail,
        but following=True fires on every one-second tick by design (see
        TimelineCanvas._on_tick) and would mean a fetch a second if
        treated the same way -- throttled instead to an occasional
        trailing-edge refresh.
        """
        if following:
            if time.time() - self._presence_last_live_fetch < _PRESENCE_LIVE_REFRESH_SEC:
                return
            self._presence_last_live_fetch = time.time()
            # force=True: see _presence_cache's own comment for why the
            # live-follow path can't rely on the coverage cache.
            self._refresh_presence(start, end, force=True)
            return
        if self._presence_debounce_id:
            GLib.source_remove(self._presence_debounce_id)
        self._presence_debounce_id = GLib.timeout_add(
            _PRESENCE_DEBOUNCE_MS, self._on_presence_debounce_fire, start, end
        )

    def _on_presence_debounce_fire(self, start: float, end: float) -> bool:
        self._presence_debounce_id = 0
        self._refresh_presence(start, end)
        return False  # one-shot timeout, don't repeat

    def _request_presence_refresh(self) -> None:
        """Re-derive presence for whatever the timeline's current view
        covers, for a change to which cameras are active/focused rather
        than to the view itself -- see the call sites in
        _set_timeline_focus_slot, _save_session, and
        _restore_layout_cameras.
        """
        if not hasattr(self, "timeline"):
            return  # still constructing -- _apply_layout runs before self.timeline exists
        start, end = self.timeline.canvas.get_view_range()
        self._refresh_presence(start, end)

    def _active_presence_cameras(self) -> tuple[int | None, list[int]]:
        """(focus_camera_id, active_camera_ids) for the current layout --
        an empty slot contributes nothing to either."""
        active_ids = []
        seen: set[int] = set()
        for i in self._active:
            camera = self._slots[i].camera
            if camera is not None and camera.id not in seen:
                seen.add(camera.id)
                active_ids.append(camera.id)
        focus_camera = self._slots[self._timeline_focus_slot].camera
        return (focus_camera.id if focus_camera else None), active_ids

    def _presence_covers(self, camera_id: int, start: float, end: float) -> bool:
        cached = self._presence_cache.get(camera_id)
        return cached is not None and cached[0] <= start and cached[1] >= end

    def _refresh_presence(self, start: float, end: float, force: bool = False) -> None:
        """Fetch/update presence for whatever cameras are focused/active,
        for the [start, end] view range. *force* skips the cache-coverage
        check (see _presence_cache's own comment for why the live-follow
        path needs this) -- callers other than the live-follow path leave
        it False so a settled pan/zoom over already-covered territory
        stays a cache hit."""
        if not self.app.api:
            return
        focus_camera_id, active_camera_ids = self._active_presence_cameras()
        focus_id_list = [focus_camera_id] if focus_camera_id is not None else []
        all_ids = list(dict.fromkeys([*active_camera_ids, *focus_id_list]))
        if not all_ids:
            self.timeline.canvas.set_presence_data([], [])
            return

        if force:
            needed = all_ids
        else:
            needed = [cid for cid in all_ids if not self._presence_covers(cid, start, end)]
        if not needed:
            self._apply_presence_to_canvas(focus_camera_id, active_camera_ids)
            return

        # Padded by one window's width on each side so re-panning by up
        # to a screen's worth in either direction stays a cache hit
        # instead of a network round trip -- see _presence_cache's own
        # comment for what this caching scheme deliberately doesn't do.
        pad = end - start
        fetch_start, fetch_end = start - pad, end + pad

        self._presence_generation += 1
        generation = self._presence_generation
        run_async(
            list_recording_presence(self.app.api, needed, int(fetch_start), int(fetch_end)),
            callback=lambda result: self._on_presence_fetched(
                generation,
                needed,
                fetch_start,
                fetch_end,
                result,
                focus_camera_id,
                active_camera_ids,
            ),
            error_callback=lambda exc: log.debug("Presence fetch failed: %s", exc),
        )

    def _on_presence_fetched(
        self,
        generation: int,
        requested_ids: list[int],
        fetch_start: float,
        fetch_end: float,
        result: dict[int, list[tuple[int, int]]],
        focus_camera_id: int | None,
        active_camera_ids: list[int],
    ) -> None:
        if generation != self._presence_generation:
            return  # superseded by a newer view change
        for cid in requested_ids:
            self._presence_cache[cid] = (fetch_start, fetch_end, result.get(cid, []))
        self._apply_presence_to_canvas(focus_camera_id, active_camera_ids)

    def _apply_presence_to_canvas(
        self, focus_camera_id: int | None, active_camera_ids: list[int]
    ) -> None:
        focus_spans = (
            self._presence_cache[focus_camera_id][2]
            if focus_camera_id is not None and focus_camera_id in self._presence_cache
            else []
        )
        layout_spans = merge_intervals(
            [
                span
                for cid in active_camera_ids
                for span in self._presence_cache.get(cid, (0.0, 0.0, []))[2]
            ]
        )
        self.timeline.canvas.set_presence_data(focus_spans, layout_spans)

    def _on_timeline_seek(self, timestamp: float) -> None:
        """TimelineCanvas.set_seek_callback target — seeks every active
        slot with a camera into History mode at *timestamp*, each
        resolving its own camera's covering recording independently.
        The timeline is one shared display, not a shared stream: it
        tells each slot what to play, the same way Live already works.
        Staggered across slots (see _HISTORY_TRANSITION_STAGGER_MS)
        rather than all fired in the same instant.

        One generation (see _seek_generation) for the whole batch, not
        one per slot -- slots within the same batch must not supersede
        each other, only a *later* call to this method should.

        Also clears a pending Pause, the same as WebSocketBridge.seek()
        does at its own level: a seek is "go here and play", so leaving
        the toolbar showing Play (and every slot's own player still
        locally paused) after this would read as still paused when it
        isn't."""
        if self._timeline_paused:
            self._timeline_paused = False
            self.timeline.set_paused(False)
            for slot_idx in self._active:
                self._slots[slot_idx].player.set_paused(False)
        target_unix = int(timestamp)
        self._seek_generation += 1
        generation = self._seek_generation
        actions: list[Callable[[], None]] = [
            partial(self._seek_slot_to_time, self._slots[slot_idx], target_unix, generation)
            for slot_idx in self._active
        ]
        self._run_staggered(actions)

    def _run_staggered(self, actions: list[Callable[[], None]]) -> None:
        """Run each of *actions* in order, _HISTORY_TRANSITION_STAGGER_MS
        apart across GTK main-loop iterations instead of all in the same
        one -- see that constant for why. The first runs immediately."""
        for i, action in enumerate(actions):
            if i == 0:
                action()
            else:
                GLib.timeout_add(
                    i * _HISTORY_TRANSITION_STAGGER_MS, self._run_staggered_one, action
                )

    @staticmethod
    def _run_staggered_one(action: Callable[[], None]) -> bool:
        action()
        return False  # one-shot timeout, don't repeat

    def _seek_slot_to_time(self, slot: CameraSlot, target_unix: int, generation: int) -> None:
        """Resolve which recording covers *target_unix* for *slot*'s
        camera and enter (or continue) History mode there.

        Shared by _on_timeline_seek (once per active slot on a timeline
        click, all sharing one generation) and _assign_to_slot (a
        camera picked into a slot that was already in History mode
        keeps playing recorded video for the newly picked camera too,
        at the same point in time, rather than silently dropping back
        to live -- its own fresh generation, a batch of one)."""
        if not self.app.api or slot.camera is None:
            return
        api = self.app.api
        camera = slot.camera
        slot_idx = slot.index
        cam_id = camera.id
        run_async(
            find_recording_at(api, camera.id, target_unix),
            callback=lambda rec, i=slot_idx, c=cam_id: self._on_recording_resolved(
                generation, i, c, target_unix, rec
            ),
            error_callback=lambda exc, i=slot_idx, name=camera.name: self._on_history_lookup_failed(
                i, name, exc
            ),
        )

    def _on_history_lookup_failed(
        self, slot_idx: int, camera_name: str, exc: BaseException
    ) -> None:
        log.error("History lookup failed for %s: %s", camera_name, exc)
        self._finish_timeline_seek(slot_idx)

    def _on_recording_resolved(
        self,
        generation: int,
        slot_idx: int,
        cam_id: int,
        target_unix: int,
        recording: Recording | None,
    ) -> None:
        """find_recording_at's result for one slot's seek request.

        Discarded outright if a newer seek has been issued since this
        lookup started (see _seek_generation) -- DSM's own per-camera
        lookup latency varies enough, especially across a whole grid,
        that a burst of clicks/ruler drags can otherwise have a stale
        lookup land *after* a newer one already applied, silently
        snapping a slot back to an earlier position and, worse, doing
        it repeatedly as more stale lookups keep trickling in.

        Still calls _finish_timeline_seek even when discarded this
        way: a stale lookup's role as *an* in-flight one for this slot
        is over regardless of whether its own result gets used.
        Skipping that release here stranded a superseded Back/Forward
        10s click until only _NUDGE_RESOLVE_TIMEOUT_SECONDS' safety
        net eventually cleared it, several seconds later than a click
        should ever take to register.
        """
        if generation != self._seek_generation:
            self._finish_timeline_seek(slot_idx)
            return
        slot = self._slots[slot_idx]
        if not (slot.camera and slot.camera.id == cam_id):
            self._finish_timeline_seek(slot_idx)
            return
        if recording is None:
            when = time.strftime("%c", time.localtime(target_unix))
            log.info("No recording found near %s for %s", when, slot.camera.name)
            self._finish_timeline_seek(slot_idx)
        elif slot._ws_bridge is not None and slot._ws_bridge.is_history:
            # Already playing recorded video: let the bridge itself decide
            # whether this is a same-recording reseek (reuses the
            # connection) or a jump to a different one (reconnects) — see
            # WebSocketBridge.seek()'s own docstring.
            run_async(
                slot._ws_bridge.seek(recording, target_unix),
                callback=lambda pos, s=slot, i=slot_idx: self._on_history_seek_applied(s, i, pos),
                error_callback=lambda exc, i=slot_idx: self._on_history_seek_failed(slot, i, exc),
            )
        else:
            position = self._enter_history_mode(slot, recording, target_unix)
            self._on_history_seek_applied(slot, slot_idx, position)

    def _on_history_seek_applied(self, slot: CameraSlot, slot_idx: int, position: int) -> None:
        """Common tail for _on_recording_resolved's two seek-performing
        branches. *position* may differ from what was requested (see
        WebSocketBridge.seek's/_enter_history_mode's own near-live
        clamp) -- reflecting it here, not the raw click/nudge target,
        is what keeps the on-screen marker and a follow-up Forward 10s
        (whose own reference point is this slot's _history_position)
        from drifting past wall clock click by click."""
        self._set_history_position(slot, position)
        self.timeline.set_history_active(True)
        self._finish_timeline_seek(slot_idx)

    def _on_history_seek_failed(self, slot: CameraSlot, slot_idx: int, exc: BaseException) -> None:
        log.error("History seek failed for %s: %s", slot.camera.name if slot.camera else "?", exc)
        self._finish_timeline_seek(slot_idx)

    def _finish_timeline_seek(self, slot_idx: int) -> None:
        """Whatever a seek's outcome, the focus slot's part in it is
        done -- let any Back/Forward 10s clicks that piled up meanwhile
        fire as one flush (see _flush_timeline_nudge), now that
        _set_history_position (if it ran) has already landed rather
        than still being about to."""
        if slot_idx == self._timeline_focus_slot:
            self._nudge_seek_in_flight = False
            self._flush_timeline_nudge()

    def _set_history_position(self, slot: CameraSlot, position: float | None) -> None:
        """Record *slot*'s own current History playback position (see
        CameraSlot._history_position) and, if *slot* is the timeline's
        current focus slot, push it to the canvas marker too — the two
        can disagree, e.g. a non-focus slot mid-seek, and only the
        focus slot's position is ever what the shared marker shows."""
        slot._history_position = position
        if slot.index == self._timeline_focus_slot:
            self.timeline.canvas.set_history_position(position)

    def _on_timeline_live_clicked(self, _btn: Gtk.Button) -> None:
        """Timeline's Live button — return every slot currently playing
        recorded video back to its normal live stream."""
        self._return_all_to_live()

    def _on_timeline_back_10s(self, _btn: Gtk.Button) -> None:
        """Timeline's Back 10s button — see _nudge_timeline."""
        self._nudge_timeline(-_TIMELINE_NUDGE_SECONDS)

    def _on_timeline_forward_10s(self, _btn: Gtk.Button) -> None:
        """Timeline's Forward 10s button — see _nudge_timeline. Only
        reachable in History mode (see Timeline.set_history_active), so
        there's always a position on the timeline to jump forward from."""
        self._nudge_timeline(_TIMELINE_NUDGE_SECONDS)

    def _nudge_timeline(self, delta_seconds: float) -> None:
        """Accumulate a Back/Forward 10s click and flush it if nothing
        is already in flight. DSM's per-camera recording lookup
        (_seek_slot_to_time) takes long enough that a burst of taps
        would otherwise each fire their own already-stale lookup;
        instead they collapse into whatever the accumulated delta is
        once the previous one lands (_flush_timeline_nudge, triggered
        from _on_recording_resolved)."""
        self._pending_nudge_seconds += delta_seconds
        if not self._nudge_seek_in_flight:
            self._flush_timeline_nudge()

    def _flush_timeline_nudge(self) -> None:
        """Fire one seek for whatever Back/Forward 10s delta has
        accumulated since the last one landed. Marked in flight, via
        the same path _on_timeline_seek already uses (which is also
        what takes every slot into History mode, so clicking Back 10s
        while live drops straight into it), until the focus slot's own
        lookup resolves and calls back in here for anything that
        accumulated meanwhile.

        A target landing within WebSocketBridge's own near-live floor
        is left for it to clamp (see _set_history_delta there) rather
        than caught here and redirected to Live -- Forward 10s stays
        historical navigation like every other seek; the Live button
        right next to it is the deliberate way back to real time."""
        delta, self._pending_nudge_seconds = self._pending_nudge_seconds, 0.0
        if delta == 0 or not self._active:
            return
        focus = self._slots[self._timeline_focus_slot]
        reference = focus._history_position if focus._history_position is not None else time.time()
        target = reference + delta
        self._nudge_seek_in_flight = True
        GLib.timeout_add_seconds(_NUDGE_RESOLVE_TIMEOUT_SECONDS, self._on_nudge_resolve_timeout)
        self._on_timeline_seek(target)

    def _on_nudge_resolve_timeout(self) -> bool:
        """Safety net for _flush_timeline_nudge's in-flight tracking —
        see _NUDGE_RESOLVE_TIMEOUT_SECONDS."""
        if self._nudge_seek_in_flight:
            self._nudge_seek_in_flight = False
            self._flush_timeline_nudge()
        return False  # one-shot

    def _on_timeline_pause_play(self, _btn: Gtk.Button) -> None:
        """Timeline's Pause/Play button — a single shared toggle for
        every active slot, the same scope as Back/Forward 10s and
        Live."""
        if self._timeline_paused:
            self._resume_all_slots()
        else:
            self._pause_all_slots()

    def _pause_all_slots(self) -> None:
        """Freeze every active slot in place, each per its own current
        mode rather than forcing a shared one -- see
        WebSocketBridge.pause's own docstring for what "freeze" means
        per mode. A Live slot never leaves Live mode just because it's
        paused (see timeline.py's transport-cluster comment for why
        that differs from Back 10s/Previous event)."""
        self._timeline_paused = True
        self.timeline.set_paused(True)
        for slot_idx in self._active:
            slot = self._slots[slot_idx]
            if slot.camera is None:
                continue
            if slot._ws_bridge is not None:
                # Synchronously, before mpv -- see request_pause's own
                # docstring for the write-stall race this closes.
                slot._ws_bridge.request_pause()
            if slot._rtsp_monitor is not None:
                # Also before mpv -- same idea as request_pause: its
                # own stall detection must already know a pause is
                # deliberate before mpv.pause stops time_pos advancing,
                # or it reads the frozen clock as the stream having
                # died (see RtspHealthMonitor.set_paused's docstring).
                slot._rtsp_monitor.set_paused(True)
            # The local freeze applies regardless of protocol -- a
            # camera on RTSP/mjpeg/etc. has no _ws_bridge at all, but
            # mpv is still what's rendering it either way.
            slot.player.set_paused(True)
            if slot._ws_bridge is None:
                continue
            camera_name = slot.camera.name
            run_async(
                slot._ws_bridge.pause(),
                callback=lambda pos, s=slot: (
                    self._set_history_position(s, pos) if pos is not None else None
                ),
                error_callback=lambda exc, name=camera_name: log.error(
                    "Pause failed for %s: %s", name, exc
                ),
            )

    def _resume_all_slots(self) -> None:
        """Undo _pause_all_slots for every active slot. A History
        slot's resumed position can land later than where it was
        paused (WebSocketBridge.resume's own wall-clock floor), hence
        still updating _set_history_position here rather than assuming
        the frozen marker was already correct."""
        self._timeline_paused = False
        self.timeline.set_paused(False)
        for slot_idx in self._active:
            slot = self._slots[slot_idx]
            if slot.camera is None:
                continue
            # Same reasoning as _pause_all_slots: the local unfreeze
            # applies regardless of protocol.
            slot.player.set_paused(False)
            if slot._rtsp_monitor is not None:
                slot._rtsp_monitor.set_paused(False)
            if slot._ws_bridge is None:
                continue
            camera_name = slot.camera.name
            run_async(
                slot._ws_bridge.resume(),
                callback=lambda pos, s=slot: (
                    self._set_history_position(s, pos) if pos is not None else None
                ),
                error_callback=lambda exc, name=camera_name: log.error(
                    "Resume failed for %s: %s", name, exc
                ),
            )

    def _on_timeline_speed_selected(self, value: str) -> None:
        """Timeline's speed dropdown — applies to every active History
        slot at once, the same scope as Pause/Back/Forward 10s. A Live
        slot has no speed concept (WebSocketBridge.set_speed is a
        no-op for one), so this only actually does anything for slots
        already in History mode."""
        self._timeline_speed = value
        for slot_idx in self._active:
            slot = self._slots[slot_idx]
            if slot.camera is None or slot._ws_bridge is None:
                continue
            camera_name = slot.camera.name
            run_async(
                slot._ws_bridge.set_speed(value),
                error_callback=lambda exc, name=camera_name: log.error(
                    "Speed change failed for %s: %s", name, exc
                ),
            )

    def _on_timeline_reverse_selected(self, reverse: bool) -> None:
        """Timeline's Fwd/Rev toggle — same scope/reasoning as
        _on_timeline_speed_selected."""
        self._timeline_reverse = reverse
        for slot_idx in self._active:
            slot = self._slots[slot_idx]
            if slot.camera is None or slot._ws_bridge is None:
                continue
            camera_name = slot.camera.name
            run_async(
                slot._ws_bridge.set_reverse(reverse),
                error_callback=lambda exc, name=camera_name: log.error(
                    "Direction change failed for %s: %s", name, exc
                ),
            )

    def _return_all_to_live(self) -> None:
        """Return every active slot currently playing recorded video
        back to its normal live stream — shared by the Live button and
        _apply_layout, since History state is scoped to the current
        layout and doesn't carry across a switch to a different one.
        Restarted staggered (see _HISTORY_TRANSITION_STAGGER_MS), not
        all in the same instant, for the same reason _on_timeline_seek
        is.

        __init__ calls _apply_layout() once before self.timeline exists
        (self._active is still empty at that point too, so nothing
        below finds anything to act on either way) — guard rather than
        reorder __init__, since nothing else here depends on
        construction order.
        """
        actions: list[Callable[[], None]] = []
        for slot_idx in self._active:
            slot = self._slots[slot_idx]
            if slot.camera and slot._ws_bridge is not None and slot._ws_bridge.is_history:
                self._set_history_position(slot, None)
                actions.append(partial(self._start_stream, slot_idx, slot.camera))
        self._run_staggered(actions)
        if hasattr(self, "timeline"):
            self.timeline.set_history_active(False)
            self._timeline_speed = "1"
            self.timeline.set_speed("1")
            self._timeline_reverse = False
            self.timeline.set_reverse(False)
            if actions:
                # Only when something actually left History -- a layout
                # switch while every slot was already Live shouldn't
                # clobber a view the user may have deliberately panned/
                # zoomed while still watching live.
                self.timeline.canvas.reset_view()

    def _show_timeline_thumbnail(
        self, generation: int, overlay_x: float, overlay_y: float, timestamp: float, data: bytes
    ) -> None:
        # The cursor may have moved on (or left) while the fetch was in
        # flight — a stale image popping up over wherever it's pointing
        # now would be worse than just not showing one.
        if generation != self._timeline_thumbnail_generation:
            log.debug("Timeline thumbnail: dropped stale response")
            return
        if not data:
            log.debug("Timeline thumbnail: empty response")
            return
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()
        except Exception as exc:
            log.debug("Timeline thumbnail decode failed: %s", exc)
            return
        if pixbuf is None:
            log.debug("Timeline thumbnail: decoded to no pixbuf")
            return

        # Gtk.Picture's own natural size follows its paintable's native
        # pixel size, not set_size_request()'s minimum — DSM returns a
        # much bigger image (seen: 320x180) than the small preview this
        # is meant to be, and without capping it here the widget renders
        # at that full native size regardless of what's requested, and
        # the margin math below (which assumes the requested size) would
        # then be positioning a box far taller than it actually draws.
        scaled = pixbuf.scale_simple(
            _TIMELINE_THUMBNAIL_WIDTH, _TIMELINE_THUMBNAIL_HEIGHT, GdkPixbuf.InterpType.BILINEAR
        )
        if scaled is None:
            return
        self._thumbnail_picture.set_paintable(Gdk.Texture.new_for_pixbuf(scaled))
        self._thumbnail_time_label.set_label(
            datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d\n%H:%M:%S")
        )
        # Centered horizontally on the cursor, bottom edge resting on
        # the ruler's top edge, clamped to stay within the overlay.
        overlay_width = self._overlay.get_width()
        max_x = overlay_width - _TIMELINE_THUMBNAIL_WIDTH
        x = max(0.0, min(overlay_x - _TIMELINE_THUMBNAIL_WIDTH / 2, max_x))
        y = max(0.0, overlay_y - _TIMELINE_THUMBNAIL_HEIGHT)
        self._thumbnail_frame.set_margin_start(int(x))
        self._thumbnail_frame.set_margin_top(int(y))
        self._thumbnail_frame.set_visible(True)

    def set_layout(self, layout: str) -> None:
        """Switch to *layout*, keeping each layout's camera assignments."""
        if layout == self._current_layout or layout not in LAYOUT_VISIBLE:
            return
        # Save current layout's cameras before switching
        self._save_layout_cameras()
        self._current_layout = layout
        self._apply_layout()
        # Restore the new layout's saved cameras
        self._restore_layout_cameras()
        self._save_session()

    def _save_layout_cameras(self) -> None:
        """Save camera assignments for the current layout to config."""
        cam_ids: list[int] = []
        for i in self._active:
            cam = self._slots[i].camera
            cam_ids.append(cam.id if cam else 0)
        log.debug("layout_cameras save: [%s] = %s", self._current_layout, cam_ids)
        self.app.config.layout_cameras[self._current_layout] = cam_ids

    def _restore_layout_cameras(self) -> None:
        """Restore saved camera assignments for the current layout.

        Layouts are independent: one with no saved assignment starts empty
        rather than inheriting whatever another layout had shown, since the
        16 physical slots are shared behind the scenes across layouts.
        """
        cam_ids = self.app.config.layout_cameras.get(self._current_layout, [])
        log.debug("layout_cameras restore: [%s] = %s", self._current_layout, cam_ids)
        # Prefer fresh camera list from sidebar; fall back to locally cached list.
        cameras = self.window.sidebar.cameras or self._cameras
        if not cameras:
            return

        cam_map = {c.id: c for c in cameras}
        seen: set[int] = set()
        for i, phys in enumerate(self._active):
            cam_id = cam_ids[i] if i < len(cam_ids) else 0
            if cam_id and cam_id in cam_map and cam_id not in seen:
                seen.add(cam_id)
                cam = cam_map[cam_id]
                self._slots[phys].assign(cam)
                self._restore_saved_audio_state(self._slots[phys], cam)
                self._update_slot_audio(self._slots[phys], cam)
                self._load_slot_ptz_extras(self._slots[phys], cam)
                # While another page is shown the streams are paused; keep
                # the assignment current but do not start playback behind
                # the user's back — resume_streams() starts it on return.
                if self._streams_paused:
                    continue
                self._start_stream(phys, cam)
            else:
                # Saved state says this slot is empty (or a stale duplicate),
                # so clear it explicitly: hidden slots from other layouts keep
                # their camera in memory rather than resetting it.
                self._slots[phys].clear()
        # The layout-accumulated row's own camera set just changed.
        self._request_presence_refresh()

    # ------------------------------------------------------------------
    # User interactions
    # ------------------------------------------------------------------

    def confirm_clear_layout(self) -> None:
        """Confirm, then clear all streams and camera assignments in this layout.

        Called from the header bar's grid-layout menu.
        """
        dialog = Gtk.AlertDialog()
        dialog.set_message("Clear all streams in this layout?")
        dialog.set_detail(
            "Every camera assignment in the current grid layout will be removed. "
            "This cannot be undone."
        )
        dialog.set_buttons(["Cancel", "Clear All"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def _on_response(d: Gtk.AlertDialog, result: object) -> None:
            try:
                idx = d.choose_finish(result)
            except Exception:
                return
            if idx == 1:
                self._do_clear_all()

        dialog.choose(self.window, None, _on_response)

    def _do_clear_all(self) -> None:
        """Actually clear all streams and camera assignments."""
        for slot in self._slots:
            slot.clear()
        self._select_slot(None)
        self._save_session()

    def _on_slot_clicked(self, slot_idx: int) -> None:
        """Select a grid slot, or deselect it if already selected."""
        if slot_idx not in self._active:
            return
        self._set_timeline_focus_slot(slot_idx)
        if self._selected_slot == slot_idx:
            self._select_slot(None)
        else:
            self._select_slot(slot_idx)

    def _select_slot(self, slot_idx: int | None) -> None:
        """Update the selected slot and its visual indicator."""
        if self._selected_slot is not None and self._selected_slot < len(self._slots):
            self._slots[self._selected_slot].set_selected(False)
        self._selected_slot = slot_idx
        if slot_idx is not None and slot_idx < len(self._slots):
            self._slots[slot_idx].set_selected(True)

    def on_camera_selected(self, camera: Camera) -> None:
        """Handle camera selection.

        With a slot selected: assign the camera to that slot.
        Without a slot selected: switch to 1x1 and show only this camera.
        """
        if self._selected_slot is not None:
            self._assign_to_slot(self._selected_slot, camera)
            self._select_slot(None)
        else:
            # Save current layout before switching
            self._save_layout_cameras()
            # Clear visible slots and switch to 1x1
            for i in self._active:
                self._slots[i].clear()
            self._current_layout = "1x1"
            self.window.sync_grid_layout("1x1")
            self._apply_layout()
            self._slots[0].assign(camera)
            self._restore_saved_audio_state(self._slots[0], camera)
            self._update_slot_audio(self._slots[0], camera)
            self._load_slot_ptz_extras(self._slots[0], camera)
            self._start_stream(0, camera)
        self._save_session()

    def clear_selected_slot(self) -> None:
        """Clear the camera assigned to the currently selected slot, if any."""
        if self._selected_slot is None:
            return
        self._slots[self._selected_slot].clear()
        self._select_slot(None)
        self._save_session()

    def _on_slot_take_snapshot(self, slot_idx: int) -> None:
        """Right-click menu action: take a snapshot of this slot's camera.

        Matches DSM's own "Take Snapshot" behavior: the snapshot is saved
        to the server's snapshot database immediately (so it shows up on
        the Snapshots page) regardless of what happens next, and a Save
        dialog is then offered so the user can optionally also keep a
        local copy — cancelling that dialog does not undo the server-side
        save.
        """
        camera = self._slots[slot_idx].camera
        if not camera or not self.app.api:
            return

        run_async(
            take_and_save_snapshot(self.app.api, camera.id),
            callback=lambda snapshot_id: self._on_snapshot_taken(camera, snapshot_id),
            error_callback=lambda e: log.error("Snapshot failed: %s", e),
        )

    def _on_snapshot_taken(self, camera: Camera, snapshot_id: int) -> None:
        log.info("Snapshot saved to server (id=%d) for %s", snapshot_id, camera.name)
        if not self.app.api:
            return

        dialog = Gtk.FileDialog()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r'[/\\<>:"|?*]', "_", camera.name)
        dialog.set_initial_name(f"{safe_name}_{timestamp}.jpg")
        snapshot_dir = self.app.config.snapshot_dir
        if snapshot_dir:
            dialog.set_initial_folder(Gio.File.new_for_path(snapshot_dir))

        def _on_save(d: Gtk.FileDialog, result: object) -> None:
            try:
                gfile = d.save_finish(result)
            except Exception:
                return  # Cancelled — snapshot is already saved server-side
            if gfile is None:
                return
            path = gfile.get_path()
            if not path or self.app.api is None:
                return
            run_async(
                download_snapshot(self.app.api, snapshot_id, Path(path)),
                callback=lambda p: log.info("Snapshot also saved locally to %s", p),
                error_callback=lambda e: log.error("Local snapshot save failed: %s", e),
            )

        dialog.save(self.window, None, _on_save)

    def _restore_saved_audio_state(self, slot: CameraSlot, camera: Camera) -> None:
        """Restore *camera*'s persisted volume and mute state into *slot*.

        Mute still auto-mutes whenever a camera loses visibility (see
        clear(), _apply_layout(), pause_streams()) — this is the other
        half: called wherever a camera *gains* visibility, restoring
        whatever it was set to rather than a fixed default.
        """
        volume = self.app.config.camera_volume.get(camera.id, 50)
        slot.set_saved_volume(volume)
        muted = self.app.config.camera_muted.get(camera.id, True)
        slot.set_saved_mute(muted)

    def _update_slot_audio(self, slot: CameraSlot, camera: Camera) -> None:
        """Tell *slot* whether audio can reach the player for *camera*
        right now.

        Having an audio track is not enough: it also has to arrive over a
        protocol that carries one. The RTSP-family protocols (see
        AUDIO_PROTOCOLS) settle this immediately. The WebSocket-family
        protocols ("auto", "websocket") get an optimistic has_audio-based
        guess instead: real audio muxing (see ws_bridge.py) only kicks in
        once DSM's codec-info frame confirms a codec we can actually mux
        (PCMU or AAC), so _start_ws_bridge's _on_ready corrects this down
        for a camera whose audio codec turns out not to be one of those,
        once that's actually known. mjpeg never carries audio.
        """
        protocol = self.app.config.camera_protocols.get(camera.id, "auto")
        playable = camera.has_audio and (
            protocol in AUDIO_PROTOCOLS or protocol in ("auto", "websocket")
        )
        slot.set_audio_playable(playable)

    def _load_slot_ptz_extras(self, slot: CameraSlot, camera: Camera) -> None:
        """Populate *slot*'s Preset/Patrol dropdowns for *camera* — only
        PTZ cameras have either.

        Both lists are dropped if the slot has moved on to another camera
        by the time they arrive: picking an entry acts on whichever camera
        the slot holds now, so a stale list would aim one camera's presets
        at another. Same check as _on_stream_url().
        """
        if not camera.is_ptz or not self.app.api:
            return
        cam_id = camera.id

        def _still_current() -> bool:
            return bool(slot.camera and slot.camera.id == cam_id)

        def _apply_presets(presets: list[PtzPreset]) -> None:
            if _still_current():
                slot.set_presets(presets)

        def _apply_patrols(patrols: list[PtzPatrol]) -> None:
            if _still_current():
                slot.set_patrols(patrols)

        run_async(
            ptz.list_presets(self.app.api, camera.id),
            callback=_apply_presets,
            error_callback=lambda e: log.error("PTZ list_presets failed: %s", e),
        )
        run_async(
            ptz.list_patrols(self.app.api, camera.id),
            callback=_apply_patrols,
            error_callback=lambda e: log.error("PTZ list_patrols failed: %s", e),
        )

    def _on_slot_mute_changed(self, slot_idx: int, muted: bool) -> None:
        camera = self._slots[slot_idx].camera
        if not camera:
            return
        self.app.config.camera_muted[camera.id] = muted
        save_config(self.app.config)

    def _on_slot_volume_changed(self, slot_idx: int, volume: int) -> None:
        camera = self._slots[slot_idx].camera
        if not camera:
            return
        self.app.config.camera_volume[camera.id] = volume
        save_config(self.app.config)

    def _on_slot_mic_toggle(self, slot_idx: int, active: bool) -> None:
        slot = self._slots[slot_idx]
        if not active:
            if slot._ptt_session is not None:
                slot._ptt_session.stop()
                slot._ptt_session = None
            return

        camera = slot.camera
        if not camera or not self.app.api:
            slot.set_mic_active(False)
            return

        session = PttSession(camera.id)
        slot._ptt_session = session
        run_async(
            session.run(self.app.api),
            error_callback=lambda e, s=slot, sess=session: self._on_ptt_ended(s, sess, e),
        )

    def _on_ptt_ended(self, slot: CameraSlot, session: PttSession, exc: BaseException) -> None:
        """A push-to-talk session ended on its own (occupied camera, dropped
        connection, ...) rather than the user tapping to stop.

        stop() only sets an event the send loop polls, so a session still in
        the handshake keeps running and can raise long after the user tapped
        off: check_occupied() reports an occupied camera without rechecking
        the flag, and connect() has its own timeout. By then the slot may
        already own a newer session, so the failure has to be matched against
        the session that produced it before anything is cleared.
        """
        if slot._ptt_session is not session:
            return  # a newer session owns the mic now
        if isinstance(exc, PttOccupiedError):
            log.info("Push-to-talk: %s", exc)
        else:
            log.error("Push-to-talk session ended: %s", exc)
        slot._ptt_session = None
        slot.set_mic_active(False)

    def _on_slot_ptz_move(self, slot_idx: int, direction: str, move_type: str) -> None:
        camera = self._slots[slot_idx].camera
        if not camera or not self.app.api:
            return
        run_async(
            ptz.move(self.app.api, camera.id, f"{direction}{move_type}"),
            error_callback=lambda e: log.error("PTZ move failed: %s", e),
        )

    def _on_slot_zoom(self, slot_idx: int, direction: str, move_type: str) -> None:
        camera = self._slots[slot_idx].camera
        if not camera or not self.app.api:
            return
        run_async(
            ptz.zoom(self.app.api, camera.id, f"{direction}{move_type}"),
            error_callback=lambda e: log.error("PTZ zoom failed: %s", e),
        )

    def _on_slot_focus(self, slot_idx: int, control: str, move_type: str) -> None:
        camera = self._slots[slot_idx].camera
        if not camera or not self.app.api:
            return
        run_async(
            ptz.focus(self.app.api, camera.id, control, move_type),
            error_callback=lambda e: log.error("PTZ focus failed: %s", e),
        )

    def _on_slot_preset(self, slot_idx: int, preset_id: int) -> None:
        camera = self._slots[slot_idx].camera
        if not camera or not self.app.api:
            return
        run_async(
            ptz.go_preset(self.app.api, camera.id, preset_id),
            error_callback=lambda e: log.error("PTZ go_preset failed: %s", e),
        )

    def _on_slot_patrol(self, slot_idx: int, patrol_id: int) -> None:
        camera = self._slots[slot_idx].camera
        if not camera or not self.app.api:
            return
        run_async(
            ptz.run_patrol(self.app.api, camera.id, patrol_id),
            error_callback=lambda e: log.error("PTZ run_patrol failed: %s", e),
        )

    def _on_slot_open_1x1(self, slot_idx: int) -> None:
        """Right-click menu action: switch to 1x1 layout showing just this
        slot's camera."""
        camera = self._slots[slot_idx].camera
        if not camera:
            return
        # Ensure on_camera_selected takes its "switch to 1x1" branch rather
        # than "assign to the selected slot".
        self._select_slot(None)
        self.on_camera_selected(camera)

    def _on_slot_clear(self, slot_idx: int) -> None:
        """Right-click menu action: clear this specific slot's camera
        assignment, regardless of which slot (if any) is currently
        selected."""
        self._slots[slot_idx].clear()
        if self._selected_slot == slot_idx:
            self._select_slot(None)
        self._save_session()

    def _assign_to_slot(self, slot_idx: int, camera: Camera) -> None:
        """Assign a camera to a specific slot, moving it if already displayed."""
        # Remove camera from its current slot if displayed elsewhere
        for slot in self._slots:
            if slot.camera and slot.camera.id == camera.id and slot.index != slot_idx:
                slot.clear()
                break

        target = self._slots[slot_idx]
        # A slot playing recorded video keeps doing so for the newly
        # picked camera too, at the same point in time it was already
        # showing -- picking a camera isn't implicitly "return to live"
        # the way the Live button explicitly is (see _return_all_to_live).
        # Read before clear() below, which drops it.
        history_target = (
            target._history_position
            if target._ws_bridge is not None and target._ws_bridge.is_history
            else None
        )

        # Clear the target slot and assign
        target.clear()
        target.assign(camera)
        self._restore_saved_audio_state(target, camera)
        self._update_slot_audio(target, camera)
        self._load_slot_ptz_extras(target, camera)
        if history_target is not None:
            self._seek_generation += 1
            self._seek_slot_to_time(target, int(history_target), self._seek_generation)
        else:
            self._start_stream(slot_idx, camera)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _start_stream(self, slot_idx: int, camera: Camera) -> None:
        """Start streaming a camera in a slot.

        A camera the server reports as not ENABLED (disabled, or
        disconnected/offline) never gets a real RTSP/WebSocket URL handed
        to mpv — playing a placeholder instead avoids ever calling play()
        on a stream that may never resolve, which is what wedges a slot's
        render context with no way back (see git history for the
        investigation). sync_camera_statuses() swaps back to the real
        stream automatically once the camera is ENABLED again.
        """
        slot = self._slots[slot_idx]
        was_lost = slot._stream_lost
        if camera.status != CameraStatus.ENABLED:
            slot._stream_lost = False  # showing "offline", not a lost stream
            slot.stop_stream()
            slot.stop_ptt()  # the camera is not reachable to talk to either
            slot.player.reset_zoom()  # the placeholder card is never zoomed
            slot.set_status("offline")
            slot.player.play(OFFLINE_PLACEHOLDER_URL)
            return

        if was_lost:
            # Retrying after a previous give-up, not a fresh selection —
            # show that something is happening rather than a silent black
            # screen while it reconnects (cleared once actually confirmed:
            # _on_ready for WebSocket, _on_stream_recovered for RTSP).
            slot.set_status("attempting reconnect")

        if not self.app.api:
            return

        api = self.app.api
        protocol = self.app.config.camera_protocols.get(camera.id, "auto")
        override = self.app.config.camera_overrides.get(camera.id, "")

        cam_id = camera.id

        async def _get_url() -> tuple[int, int, str]:
            url = await get_live_view_path(api, camera.id, protocol=protocol, override_url=override)
            return slot_idx, cam_id, url

        run_async(
            _get_url(),
            callback=self._on_stream_url,
            error_callback=lambda e: log.error(
                "Failed to get stream URL for %s: %s", camera.name, e
            ),
        )

    def _on_stream_url(self, result: tuple[int, int, str]) -> None:
        slot_idx, cam_id, url = result
        slot = self._slots[slot_idx]
        if self._streams_paused:
            # The user left the Live View page while this URL was being
            # fetched. A slot keeps its own visible flag when the page is
            # unmapped, so that alone would not stop us starting a stream
            # nobody is watching and pause_streams() has already been past.
            # resume_streams() starts it again on return.
            return
        if slot.get_visible() and slot.camera and slot.camera.id == cam_id:
            log.info("Starting stream in slot %d: %s", slot_idx, url)
            slot.stop_stream()
            # Only now is a stream really starting. Holding the flag until
            # here keeps the retry armed when the URL fetch itself fails,
            # which is the likeliest outcome when the NAS being unreachable
            # is what killed the stream in the first place.
            slot._stream_lost = False
            if url.startswith(("ws://", "wss://")):
                self._start_ws_bridge(slot, url)
            else:
                self._start_rtsp_monitor(slot, url)

    def _start_ws_bridge(self, slot: CameraSlot, url: str) -> None:
        """Start a Live WebSocket bridge and play the resulting pipe in
        mpv (see _enter_history_mode for the History equivalent —
        _start_bridge is the plumbing they share)."""
        slot.stop_stream()
        verify_ssl = self.app.api.profile.verify_ssl if self.app.api else True
        sid = self.app.api.sid if self.app.api else ""
        label = slot.camera.name if slot.camera else ""
        bridge = WebSocketBridge(url, verify_ssl, sid, label=label)
        self._start_bridge(slot, bridge)

    def _enter_history_mode(self, slot: CameraSlot, recording: Recording, target_unix: int) -> int:
        """Switch *slot* into History mode, playing *recording* from
        *target_unix* (see ws_bridge.py's module docstring for the wire
        protocol). Only for a fresh seek -- a slot already in History
        mode reuses its existing bridge's seek() instead, which reuses
        the connection when it can rather than tearing down and
        rebuilding a whole pipeline for every click (see
        _on_recording_resolved).

        Returns the actual (possibly clamped, see
        WebSocketBridge._set_history_delta) position now playing --
        available synchronously right off the freshly constructed
        bridge, no await needed, since the clamp runs in its own
        __init__ -- for the caller to reflect on its own UI rather than
        assuming target_unix was used as-is. Falls back to target_unix
        itself if there's no API to build a bridge at all."""
        if not self.app.api:
            return target_unix
        slot.stop_stream()
        verify_ssl = self.app.api.profile.verify_ssl
        sid = self.app.api.sid
        label = slot.camera.name if slot.camera else ""
        url = get_history_view_path(self.app.api)
        camera_id = recording.camera_id

        async def resolve(target: int) -> Recording | None:
            """Bound to camera_id, not to slot.camera -- this outlives
            whatever the slot is showing by the time a stale reconnect
            calls it (see WebSocketBridge's own history_resolver)."""
            api = self.app.api
            return await find_recording_at(api, camera_id, target) if api else None

        bridge = WebSocketBridge(
            url,
            verify_ssl,
            sid,
            label=label,
            history_recording=recording,
            history_target=target_unix,
            history_resolver=resolve,
            history_speed=self._timeline_speed,
            history_reverse=self._timeline_reverse,
        )
        self._start_bridge(slot, bridge)
        position = bridge.current_history_position
        return position if position is not None else target_unix

    def _start_bridge(self, slot: CameraSlot, bridge: WebSocketBridge) -> None:
        """Plumbing shared by Live and History bridges alike: assign
        *bridge* to *slot*, play the pipe once ready, and report a
        permanent give-up the same way regardless of which mode started
        it (see _start_ws_bridge / _enter_history_mode)."""
        slot._ws_bridge = bridge
        # Ghosts/restores the camera-motor controls immediately, before
        # the pipe is even ready -- a slot mid-History-connect has no
        # live camera under it any more than one already playing does.
        slot.set_history_mode(bridge.is_history)
        if not bridge.is_history:
            # Defensive: covers every path back to Live, not just the
            # Live button (already clears this itself) -- e.g. a slot
            # hidden by a layout switch while in History, then reshown
            # later on its normal live stream, would otherwise keep a
            # stale position that resurfaces if it becomes the timeline
            # focus slot again.
            self._set_history_position(slot, None)
        cam_id = slot.camera.id if slot.camera else -1
        slot_idx = slot.index

        def _on_ready(pipe_url: str) -> None:
            s = self._slots[slot_idx]
            if s._ws_bridge is not bridge:
                # The slot tore this bridge down (or replaced it) while
                # start() was still resolving. Its read fd is closed by now
                # and the next bridge's os.pipe() hands the same numbers
                # back, so playing this pipe_url would point mpv at another
                # camera's stream.
                return
            if s.get_visible() and s.camera and s.camera.id == cam_id:
                log.info(
                    "WebSocket bridge ready, playing pipe: %s (audio_active=%s, history=%s)",
                    pipe_url,
                    bridge.audio_active,
                    bridge.is_history,
                )
                s.set_status("")
                s.player.play(
                    pipe_url, low_latency=not bridge.audio_active, muxed_audio=bridge.audio_active
                )
                # Corrects the optimistic has_audio-based guess from
                # _update_slot_audio() now that whether DSM's audio codec
                # was actually mixable (PCMU or AAC) is known for certain.
                s.set_audio_playable(bridge.audio_active)

        def _on_start_failed(exc: Exception) -> None:
            if self._slots[slot_idx]._ws_bridge is not bridge:
                # Same case _on_ready guards, arriving down the other
                # branch: a bridge stopped while start() was still pending
                # ends its pump without ever becoming ready, so start()
                # raises. Switching camera or leaving the page during the
                # second or two a stream takes to come up is ordinary, and
                # was being reported as a failure to start it.
                log.debug("WebSocket bridge stopped before it was ready: %s", exc)
                return
            log.error("WebSocket bridge failed: %s", exc)

        run_async(
            bridge.start(),
            callback=_on_ready,
            error_callback=_on_start_failed,
        )
        # WebSocketBridge reconnects on the same pipe internally and never
        # surfaces a routine drop as a "closed" event, so mpv never sees a
        # real EOF from one. This only fires once the bridge has genuinely
        # given up (a run of attempts that never even connect) or on a
        # deliberate stop (empty reason, ignored below).
        run_async(
            bridge.wait_closed(),
            callback=lambda reason: self._on_stream_gave_up(slot_idx, cam_id, bridge, reason),
        )

    def _start_rtsp_monitor(self, slot: CameraSlot, url: str) -> None:
        """Play a plain RTSP URL and watch it with an RtspHealthMonitor.

        Unlike WebSocket streams (bridged through WebSocketBridge, which
        already detects and recovers from a dead connection), mpv talks to
        an RTSP camera directly with nothing watching for the demuxer
        dying silently mid-stream — this is what fills that gap.
        """
        slot.set_status("")  # clear any leftover "offline"/"reconnect" label
        # RTSP has no History mode at all (see ws_bridge.py's module
        # docstring) -- a defensive reset for a slot that switches
        # protocol away from a History WS session straight to RTSP,
        # which would otherwise leave its camera-motor controls ghosted
        # with nothing to ever un-ghost them.
        slot.set_history_mode(False)
        self._set_history_position(slot, None)
        slot.player.play(url)
        cam_id = slot.camera.id if slot.camera else -1
        slot_idx = slot.index
        label = slot.camera.name if slot.camera else ""

        monitor = RtspHealthMonitor(
            slot.player,
            url,
            label,
            on_gave_up=lambda reason: self._on_stream_gave_up(slot_idx, cam_id, monitor, reason),
            on_recovered=lambda: self._on_stream_recovered(slot_idx, cam_id, monitor),
        )
        slot._rtsp_monitor = monitor

    def _on_stream_recovered(self, slot_idx: int, cam_id: int, monitor: RtspHealthMonitor) -> None:
        """Clear the "attempting reconnect" status once a retried RTSP
        stream is confirmed advancing again."""
        slot = self._slots[slot_idx]
        if slot._rtsp_monitor is not monitor:
            return  # slot moved on to something else
        if not slot.get_visible() or not slot.camera or slot.camera.id != cam_id:
            return
        slot.set_status("")

    def _on_stream_gave_up(
        self, slot_idx: int, cam_id: int, source: WebSocketBridge | RtspHealthMonitor, reason: str
    ) -> None:
        """Show a slot whose stream gave up after repeated failures.

        *source* is whichever object reported the failure — compared
        against the slot's current bridge/monitor so a stale notification
        from one the slot has since moved on from is ignored.
        """
        slot = self._slots[slot_idx]
        if not reason or (slot._ws_bridge is not source and slot._rtsp_monitor is not source):
            return  # we stopped it ourselves, or the slot moved on
        if not slot.get_visible() or not slot.camera or slot.camera.id != cam_id:
            return
        log.error("Stream for %s gave up (%s)", slot.camera.name, reason)
        # The camera may still be reported ENABLED (this is a transport-
        # level failure, not necessarily a status change) — mark it so
        # sync_camera_statuses() keeps retrying on the next poll even
        # without seeing a status transition to react to.
        slot._stream_lost = True
        # Swap to the placeholder rather than leaving the wedged mpv state
        # on screen: this is a normal stop()/play() cycle (same as any
        # camera-to-camera switch), just targeting a local synthetic
        # stream instead of the dead network one, so it can't wedge.
        slot.stop_stream()
        slot.player.reset_zoom()  # the placeholder card is never zoomed
        slot.set_status("stream lost")
        slot.player.play(OFFLINE_PLACEHOLDER_URL)

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def _save_session(self) -> None:
        """Persist grid layout and per-layout camera assignments to config."""
        cam_ids: list[int] = []
        for i in self._active:
            cam = self._slots[i].camera
            cam_ids.append(cam.id if cam else 0)
        self.app.config.grid_layout = self._current_layout
        self.app.config.layout_cameras[self._current_layout] = cam_ids
        log.debug("layout_cameras session save: [%s] = %s", self._current_layout, cam_ids)
        save_config_now(self.app.config)
        # Session changes (assign/clear a slot's camera) can change the
        # layout-accumulated row's own camera set.
        self._request_presence_refresh()

    def restore_session(self, cameras: list[Camera]) -> None:
        """Restore camera assignments from config."""
        self._cameras = cameras
        self._restore_layout_cameras()

    def sync_camera_statuses(self, cameras: list[Camera]) -> None:
        """Swap a visible slot between its real stream and the offline
        placeholder as its camera's reported status changes.

        Called after every sidebar camera-list refresh (including the
        periodic poll), so a camera that comes back online has its real
        feed restored automatically, without the user re-selecting it.

        Also retries any slot whose stream gave up (slot._stream_lost) while
        its camera is still ENABLED, even without a status transition — a
        transport-level RTSP failure has no status change to react to
        otherwise, so it would be stuck on the placeholder forever.
        """
        self._cameras = cameras
        cam_map = {c.id: c for c in cameras}
        for i in self._active:
            slot = self._slots[i]
            if not slot.camera:
                continue
            fresh = cam_map.get(slot.camera.id)
            if fresh is None:
                continue
            # A bridge that was muxing audio can stop mid-session, when
            # the camera goes quiet and the gap watchdog ends the stream
            # to keep the video moving. Nothing else revisits the audio
            # control after the stream started, so it would go on
            # offering a volume slider that reaches nothing.
            if slot._ws_bridge is not None and not slot._ws_bridge.audio_active:
                slot.set_audio_playable(False)
            status_changed = fresh.status != slot.camera.status
            retry_lost_stream = slot._stream_lost and fresh.status == CameraStatus.ENABLED
            if not status_changed and not retry_lost_stream:
                continue
            slot.update_camera(fresh)
            # While another page is shown the streams are paused; keep the
            # camera status current but do not start a stream behind the
            # user's back — resume_streams restores them on return.
            if self._streams_paused:
                continue
            self._start_stream(i, fresh)

    def restart_camera(self, camera_id: int) -> None:
        """Restart the stream for a camera if it is currently displayed.

        Called after a protocol override change, among other things — the
        mute button's playability needs re-resolving too, since that's
        exactly the kind of change that flips it (WebSocket <-> RTSP).
        """
        for slot in self._slots:
            if slot.get_visible() and slot.camera and slot.camera.id == camera_id:
                slot.stop_stream()
                self._update_slot_audio(slot, slot.camera)
                self._start_stream(slot.index, slot.camera)

    def pause_streams(self) -> None:
        """Stop all mpv playback but keep camera assignments.

        Also resets zoom and auto-mutes — leaving the Live View page loses
        visibility for every slot, same as any other slot that stops being
        shown (see _apply_layout()). resume_streams() restores each
        camera's actual mute/volume choice, not just leaving it muted.
        """
        self._streams_paused = True
        for slot in self._slots:
            slot.player.reset_zoom()
            slot.player.set_mute(True)
            slot._update_mute_icon()
            if slot.camera:
                slot.stop_stream()
                slot.stop_ptt()

    def resume_streams(self) -> None:
        """Restart streams for all visible slots that have a camera assigned."""
        self._streams_paused = False
        for i in self._active:
            slot = self._slots[i]
            if slot.camera:
                self._restore_saved_audio_state(slot, slot.camera)
                self._start_stream(i, slot.camera)

    def stop_all(self) -> None:
        """Stop all streams."""
        for slot in self._slots:
            slot.clear()
