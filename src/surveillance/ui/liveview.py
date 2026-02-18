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
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera
from surveillance.config import save_config
from surveillance.services.live import get_live_view_path
from surveillance.ui.mpv_widget import MpvGLArea
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)

LAYOUTS = {
    "1x1": (1, 1),
    "2x2": (2, 2),
    "3x3": (3, 3),
    "4x4": (4, 4),
}

# Internal grid is always 4x4 (16 slots).  Positions: idx = row*4 + col.
_GRID_COLS = 4
_MAX_SLOTS = 16

# Physical slot indices that are visible for each layout.
_LAYOUT_VISIBLE: dict[str, list[int]] = {
    "1x1": [0],
    "2x2": [0, 1, 4, 5],
    "3x3": [0, 1, 2, 4, 5, 6, 8, 9, 10],
    "4x4": list(range(16)),
}


class CameraSlot(Gtk.Box):
    """Self-contained camera slot with a header label and video player."""

    def __init__(self, index: int) -> None:
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
        self.player = MpvGLArea()
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

        self._click_callback: object = None

    def set_click_callback(self, callback: object) -> None:
        self._click_callback = callback

    def _on_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        if n_press == 1 and self._click_callback and callable(self._click_callback):
            self._click_callback(self.index)

    def set_display_index(self, display_idx: int) -> None:
        self._display_index = display_idx
        if not self.camera:
            self._header.set_label(f"Slot {display_idx + 1}")

    def set_selected(self, selected: bool) -> None:
        if selected:
            self._header.remove_css_class("dim-label")
            self._header.add_css_class("slot-selected-label")
            self._header.set_label(f"\u25b6 Slot {self._display_index + 1} \u2014 click a camera")
        elif self.camera:
            self._header.remove_css_class("slot-selected-label")
            self._header.add_css_class("dim-label")
            self._header.set_label(self.camera.name)
        else:
            self._header.remove_css_class("slot-selected-label")
            self._header.add_css_class("dim-label")
            self._header.set_label(f"Slot {self._display_index + 1}")

    def assign(self, camera: Camera) -> None:
        self.camera = camera
        self._header.set_label(camera.name)
        self._header.remove_css_class("slot-selected-label")
        self._header.add_css_class("dim-label")

    def clear(self) -> None:
        self.camera = None
        self.player.stop()
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
        self._current_layout: str = self.app.config.grid_layout
        self._inhibit_save = False
        self._cameras: list[Camera] = []  # last known camera list

        self.set_hexpand(True)
        self.set_vexpand(True)

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(4)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)

        label = Gtk.Label(label="Live View")
        label.add_css_class("title-4")
        label.set_hexpand(True)
        label.set_xalign(0)
        toolbar.append(label)

        # Layout selector
        layout_label = Gtk.Label(label="Layout:")
        toolbar.append(layout_label)

        self.layout_combo = Gtk.ComboBoxText()
        for layout_name in LAYOUTS:
            self.layout_combo.append(layout_name, layout_name)
        self.layout_combo.set_active_id(self.app.config.grid_layout)
        self.layout_combo.connect("changed", self._on_layout_changed)
        toolbar.append(self.layout_combo)

        clear_btn = Gtk.Button()
        clear_btn.set_icon_name("edit-clear-all-symbolic")
        clear_btn.set_tooltip_text("Clear all streams")
        clear_btn.connect("clicked", self._on_clear_clicked)
        toolbar.append(clear_btn)

        self.append(toolbar)
        self.append(Gtk.Separator())

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
        self._slots: list[CameraSlot] = []
        for i in range(_MAX_SLOTS):
            r, c = divmod(i, _GRID_COLS)
            slot = CameraSlot(i)
            slot.set_click_callback(self._on_slot_clicked)
            self.grid.attach(slot, c, r, 1, 1)
            self._slots.append(slot)

        # Apply initial layout (show/hide slots)
        self._apply_layout()

    # ------------------------------------------------------------------
    # Layout management
    # ------------------------------------------------------------------

    def _apply_layout(self) -> None:
        """Show/hide slots to match the current layout."""
        layout_name = self.layout_combo.get_active_id() or "2x2"
        new_active = list(_LAYOUT_VISIBLE.get(layout_name, _LAYOUT_VISIBLE["2x2"]))
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
                    slot.player.stop()

        self._active = new_active

    def _on_layout_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self._inhibit_save:
            return
        # Save current layout's cameras before switching
        self._save_layout_cameras()
        # Update current layout and apply
        self._current_layout = combo.get_active_id() or "2x2"
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
        self.app.config.layout_cameras[self._current_layout] = cam_ids

    def _restore_layout_cameras(self) -> None:
        """Restore saved camera assignments for the current layout."""
        layout = self.layout_combo.get_active_id() or "2x2"
        cam_ids = self.app.config.layout_cameras.get(layout, [])
        if not cam_ids or not self._cameras:
            return

        cam_map = {c.id: c for c in self._cameras}
        seen: set[int] = set()
        for i, cam_id in enumerate(cam_ids):
            if i >= len(self._active):
                break
            phys = self._active[i]
            if cam_id and cam_id in cam_map:
                if cam_id in seen:
                    continue
                seen.add(cam_id)
                cam = cam_map[cam_id]
                self._slots[phys].assign(cam)
                self._start_stream(phys, cam)

    # ------------------------------------------------------------------
    # User interactions
    # ------------------------------------------------------------------

    def _on_clear_clicked(self, btn: Gtk.Button) -> None:
        """Clear all streams and camera assignments."""
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
            else:
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
            self._inhibit_save = True
            self.layout_combo.set_active_id("1x1")
            self._inhibit_save = False
            self._apply_layout()
            self._slots[0].assign(camera)
            self._start_stream(0, camera)
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
            slot.player.play(url)

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def _save_session(self) -> None:
        """Persist grid layout and per-layout camera assignments to config."""
        layout = self.layout_combo.get_active_id() or "2x2"
        cam_ids: list[int] = []
        for i in self._active:
            cam = self._slots[i].camera
            cam_ids.append(cam.id if cam else 0)
        self.app.config.grid_layout = layout
        self.app.config.layout_cameras[layout] = cam_ids
        save_config(self.app.config)

    def restore_session(self, cameras: list[Camera]) -> None:
        """Restore camera assignments from config."""
        self._cameras = cameras
        layout = self.layout_combo.get_active_id() or "2x2"
        cam_ids = self.app.config.layout_cameras.get(layout, [])
        if not cam_ids:
            return

        cam_map = {c.id: c for c in cameras}
        seen: set[int] = set()
        for i, cam_id in enumerate(cam_ids):
            if i >= len(self._active):
                break
            phys = self._active[i]
            if cam_id and cam_id in cam_map:
                if cam_id in seen:
                    continue
                seen.add(cam_id)
                cam = cam_map[cam_id]
                self._slots[phys].assign(cam)
                self._start_stream(phys, cam)

    def restart_camera(self, camera_id: int) -> None:
        """Restart the stream for a camera if it is currently displayed."""
        for slot in self._slots:
            if slot.get_visible() and slot.camera and slot.camera.id == camera_id:
                self._start_stream(slot.index, slot.camera)

    def stop_all(self) -> None:
        """Stop all streams."""
        for slot in self._slots:
            slot.clear()

    def stop_slot(self, slot_idx: int) -> None:
        """Stop stream in a specific slot."""
        if slot_idx < len(self._slots):
            self._slots[slot_idx].clear()
