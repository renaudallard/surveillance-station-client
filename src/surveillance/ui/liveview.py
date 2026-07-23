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

from gi.repository import Gdk, Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera
from surveillance.config import save_config_now
from surveillance.services.live import get_live_view_path
from surveillance.services.snapshot import download_snapshot, take_and_save_snapshot
from surveillance.services.ws_bridge import WebSocketBridge
from surveillance.ui.layouts import LAYOUT_VISIBLE, valid_layout
from surveillance.ui.mpv_widget import MpvGLArea
from surveillance.ui.ptz_controls import PtzControls
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)

# Internal grid is always 4x4 (16 slots).  Positions: idx = row*4 + col.
_GRID_COLS = 4
_MAX_SLOTS = 16


class CameraSlot(Gtk.Box):
    """Self-contained camera slot with a header label and video player."""

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

        # Video player
        self.player = MpvGLArea(tls_verify=tls_verify)
        self.player.set_vexpand(True)
        self.player.set_hexpand(True)
        self.append(self.player)

        # Click handlers — one on the header, one on the player.
        # GLArea consumes events so a CAPTURE gesture on the parent Box
        # only works for the first grid cell; direct gestures work for all.
        header_click = Gtk.GestureClick(button=1)
        header_click.connect("pressed", self._on_click)
        self._header.add_controller(header_click)

        player_click = Gtk.GestureClick(button=1)
        player_click.connect("pressed", self._on_click)
        self.player.add_controller(player_click)

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
        if self._snapshot_callback and callable(self._snapshot_callback):
            self._snapshot_callback(self.index)

    def _on_menu_open_1x1(self, btn: Gtk.Button) -> None:
        self._menu_popover.popdown()
        if self._open_1x1_callback and callable(self._open_1x1_callback):
            self._open_1x1_callback(self.index)

    def _on_menu_clear_slot(self, btn: Gtk.Button) -> None:
        self._menu_popover.popdown()
        if self._clear_slot_callback and callable(self._clear_slot_callback):
            self._clear_slot_callback(self.index)

    def _on_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        if n_press == 1 and self._click_callback and callable(self._click_callback):
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

    def stop_stream(self) -> None:
        """Stop playback, then tear down the WebSocket bridge.

        mpv has to let go of the pipe before the bridge closes it. The next
        bridge calls os.pipe() and gets the very same descriptor numbers
        back, so a demuxer still holding the old ones would read the new
        stream out from under it and never decode a frame.
        """
        self.player.stop()
        if self._ws_bridge is not None:
            bridge = self._ws_bridge
            self._ws_bridge = None
            bridge.close_write_end()
            run_async(bridge.stop())

    def clear(self) -> None:
        self.stop_stream()
        self.camera = None
        self._status = ""
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
            self.grid.attach(slot, c, r, 1, 1)
            self._slots.append(slot)

        # PTZ controls (shown below grid when a PTZ camera is active)
        self._ptz_sep = Gtk.Separator()
        self._ptz_sep.set_visible(False)
        self.append(self._ptz_sep)

        self._ptz_controls = PtzControls(window)
        self._ptz_controls.set_visible(False)
        self.append(self._ptz_controls)

        # Apply initial layout (show/hide slots)
        self._apply_layout()

    # ------------------------------------------------------------------
    # Layout management
    # ------------------------------------------------------------------

    def _apply_layout(self) -> None:
        """Show/hide slots to match the current layout."""
        new_active = list(LAYOUT_VISIBLE[self._current_layout])
        self._select_slot(None)

        # Stop streams on slots that are becoming hidden
        for i, slot in enumerate(self._slots):
            if i in new_active:
                slot.set_visible(True)
                display_idx = new_active.index(i)
                slot.set_display_index(display_idx)
            else:
                slot.set_visible(False)
                if slot.camera:
                    slot.stop_stream()

        self._active = new_active
        self._update_ptz_controls()

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
                self._start_stream(phys, cam)
            else:
                # Saved state says this slot is empty (or a stale duplicate),
                # so clear it explicitly: hidden slots from other layouts keep
                # their camera in memory rather than resetting it.
                self._slots[phys].clear()
        self._update_ptz_controls()

    # ------------------------------------------------------------------
    # PTZ controls
    # ------------------------------------------------------------------

    def _update_ptz_controls(self) -> None:
        """Show or hide PTZ controls based on the active camera."""
        camera: Camera | None = None
        if len(self._active) == 1:
            camera = self._slots[self._active[0]].camera
        elif self._selected_slot is not None:
            camera = self._slots[self._selected_slot].camera

        if camera and camera.is_ptz:
            self._ptz_controls.set_camera(camera)
            self._ptz_controls.set_visible(True)
            self._ptz_sep.set_visible(True)
        else:
            self._ptz_controls.set_visible(False)
            self._ptz_sep.set_visible(False)

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
        self._update_ptz_controls()
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
        self._update_ptz_controls()

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
            self._start_stream(0, camera)
        self._update_ptz_controls()
        self._save_session()

    def clear_selected_slot(self) -> None:
        """Clear the camera assigned to the currently selected slot, if any."""
        if self._selected_slot is None:
            return
        self._slots[self._selected_slot].clear()
        self._select_slot(None)
        self._update_ptz_controls()
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
        self._update_ptz_controls()
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
        self._start_stream(slot_idx, camera)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _start_stream(self, slot_idx: int, camera: Camera) -> None:
        """Start streaming a camera in a slot."""
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
        if slot.get_visible() and slot.camera and slot.camera.id == cam_id:
            log.info("Starting stream in slot %d: %s", slot_idx, url)
            slot.stop_stream()
            if url.startswith(("ws://", "wss://")):
                self._start_ws_bridge(slot, url)
            else:
                slot.set_status("")
                slot.player.play(url)

    def _start_ws_bridge(self, slot: CameraSlot, url: str) -> None:
        """Start a WebSocket bridge and play the resulting pipe in mpv."""
        slot.stop_stream()
        verify_ssl = self.app.api.profile.verify_ssl if self.app.api else True
        sid = self.app.api.sid if self.app.api else ""
        bridge = WebSocketBridge(url, verify_ssl, sid)
        slot._ws_bridge = bridge
        cam_id = slot.camera.id if slot.camera else -1
        slot_idx = slot.index

        def _on_ready(pipe_url: str) -> None:
            s = self._slots[slot_idx]
            if s.get_visible() and s.camera and s.camera.id == cam_id:
                log.info("WebSocket bridge ready, playing pipe: %s", pipe_url)
                s.set_status("")
                s.player.play(pipe_url, low_latency=True)

        run_async(
            bridge.start(),
            callback=_on_ready,
            error_callback=lambda e: log.error("WebSocket bridge failed: %s", e),
        )
        # The NAS drops this WebSocket session routinely, every ~15-25s, as
        # normal behavior — WebSocketBridge reconnects on the same pipe
        # internally and never surfaces those as a "closed" event, so mpv
        # never sees a real EOF and just keeps playing through them. This
        # only fires once the bridge has genuinely given up (a run of
        # attempts that never even connect) or on a deliberate stop (empty
        # reason, ignored below).
        run_async(
            bridge.wait_closed(),
            callback=lambda reason: self._on_stream_gave_up(slot_idx, cam_id, bridge, reason),
        )

    def _on_stream_gave_up(
        self, slot_idx: int, cam_id: int, bridge: WebSocketBridge, reason: str
    ) -> None:
        """Show a slot whose WebSocket bridge gave up after repeated failures."""
        slot = self._slots[slot_idx]
        if not reason or slot._ws_bridge is not bridge:
            return  # we stopped it ourselves, or the slot moved on
        if not slot.get_visible() or not slot.camera or slot.camera.id != cam_id:
            return
        log.error("Stream for %s gave up (%s)", slot.camera.name, reason)
        slot.set_status("stream lost")

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

    def restart_camera(self, camera_id: int) -> None:
        """Restart the stream for a camera if it is currently displayed."""
        for slot in self._slots:
            if slot.get_visible() and slot.camera and slot.camera.id == camera_id:
                slot.stop_stream()
                self._start_stream(slot.index, slot.camera)

    def pause_streams(self) -> None:
        """Stop all mpv playback but keep camera assignments."""
        for slot in self._slots:
            if slot.camera:
                slot.stop_stream()

    def resume_streams(self) -> None:
        """Restart streams for all visible slots that have a camera assigned."""
        for i in self._active:
            slot = self._slots[i]
            if slot.camera:
                self._start_stream(i, slot.camera)

    def stop_all(self) -> None:
        """Stop all streams."""
        for slot in self._slots:
            slot.clear()
