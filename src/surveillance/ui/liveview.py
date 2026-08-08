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
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gio, Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera, CameraStatus, PtzPatrol, PtzPreset
from surveillance.config import save_config, save_config_now
from surveillance.services import ptz
from surveillance.services.live import (
    AUDIO_PROTOCOLS,
    OFFLINE_PLACEHOLDER_URL,
    get_live_view_path,
)
from surveillance.services.ptt import PttOccupiedError, PttSession
from surveillance.services.snapshot import download_snapshot, take_and_save_snapshot
from surveillance.services.ws_bridge import WebSocketBridge
from surveillance.ui.layouts import LAYOUT_VISIBLE, valid_layout
from surveillance.ui.mpv_widget import MpvGLArea, attach_zoom_pan_controls
from surveillance.ui.rtsp_health import RtspHealthMonitor
from surveillance.ui.slot_toolbar import SlotToolbar
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)

# Internal grid is always 4x4 (16 slots).  Positions: idx = row*4 + col.
_GRID_COLS = 4
_MAX_SLOTS = 16


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
        self._active: list[int] = []  # physical indices of visible slots
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
        self.append(self.grid)

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

    # ------------------------------------------------------------------
    # Layout management
    # ------------------------------------------------------------------

    def _apply_layout(self) -> None:
        """Show/hide slots to match the current layout."""
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
        """Select a grid slot, or switch to 1x1 if clicking a selected slot with a camera."""
        if slot_idx not in self._active:
            return
        if self._selected_slot == slot_idx:
            cam = self._slots[slot_idx].camera
            if cam and self._current_layout != "1x1":
                # Second click on selected slot with a camera: zoom to 1x1
                self._select_slot(None)
                self.on_camera_selected(cam)
                return
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
        slot's camera — the same "zoom in" behavior as clicking an
        already-selected slot with a camera (see _on_slot_clicked)."""
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

        # Clear the target slot and assign
        self._slots[slot_idx].clear()
        self._slots[slot_idx].assign(camera)
        self._restore_saved_audio_state(self._slots[slot_idx], camera)
        self._update_slot_audio(self._slots[slot_idx], camera)
        self._load_slot_ptz_extras(self._slots[slot_idx], camera)
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
        """Start a WebSocket bridge and play the resulting pipe in mpv."""
        slot.stop_stream()
        verify_ssl = self.app.api.profile.verify_ssl if self.app.api else True
        sid = self.app.api.sid if self.app.api else ""
        label = slot.camera.name if slot.camera else ""
        bridge = WebSocketBridge(url, verify_ssl, sid, label=label)
        slot._ws_bridge = bridge
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
                    "WebSocket bridge ready, playing pipe: %s (audio_active=%s)",
                    pipe_url,
                    bridge.audio_active,
                )
                s.set_status("")
                s.player.play(
                    pipe_url, low_latency=not bridge.audio_active, muxed_audio=bridge.audio_active
                )
                # Corrects the optimistic has_audio-based guess from
                # _update_slot_audio() now that whether DSM's audio codec
                # was actually mixable (PCMU or AAC) is known for certain.
                s.set_audio_playable(bridge.audio_active)

        run_async(
            bridge.start(),
            callback=_on_ready,
            error_callback=lambda e: log.error("WebSocket bridge failed: %s", e),
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
