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
}


class CameraSlot(Gtk.Box):
    """Self-contained camera slot with a header label and video player."""

    def __init__(self, index: int) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.index = index
        self.camera: Camera | None = None

        # Header bar (outside GL rendering area)
        self._header = Gtk.Label(label=f"Slot {index + 1}")
        self._header.add_css_class("slot-header")
        self._header.add_css_class("dim-label")
        self._header.add_css_class("caption")
        self._header.set_xalign(0)
        self._header.set_margin_start(4)
        self._header.set_margin_end(4)
        self.append(self._header)

        # Video player
        self.player = MpvGLArea()
        self.player.set_vexpand(True)
        self.player.set_hexpand(True)
        self.append(self.player)

        # Click handler on the whole slot
        click = Gtk.GestureClick(button=1)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_click)
        self.add_controller(click)

        self._click_callback: object = None

    def set_click_callback(self, callback: object) -> None:
        self._click_callback = callback

    def _on_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        if self._click_callback and callable(self._click_callback):
            self._click_callback(self.index)

    def set_selected(self, selected: bool) -> None:
        if selected:
            self._header.remove_css_class("dim-label")
            self._header.add_css_class("slot-selected-label")
            self._header.set_label(f"\u25b6 Slot {self.index + 1} — click a camera")
        elif self.camera:
            self._header.remove_css_class("slot-selected-label")
            self._header.add_css_class("dim-label")
            self._header.set_label(self.camera.name)
        else:
            self._header.remove_css_class("slot-selected-label")
            self._header.add_css_class("dim-label")
            self._header.set_label(f"Slot {self.index + 1}")

    def assign(self, camera: Camera) -> None:
        self.camera = camera
        self._header.set_label(camera.name)
        self._header.remove_css_class("slot-selected-label")
        self._header.add_css_class("dim-label")

    def clear(self) -> None:
        self.camera = None
        self.player.stop()
        self._header.set_label(f"Slot {self.index + 1}")
        self._header.remove_css_class("slot-selected-label")
        self._header.add_css_class("dim-label")


class LiveView(Gtk.Box):
    """Live camera view with configurable grid layout."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.app = window.app
        self._slots: list[CameraSlot] = []
        self._selected_slot: int | None = None

        self.add_css_class("live-grid")
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
        self.grid.set_row_homogeneous(True)
        self.grid.set_column_homogeneous(True)
        self.grid.set_hexpand(True)
        self.grid.set_vexpand(True)
        self.append(self.grid)

        # Build initial grid
        self._build_grid()

    def _build_grid(self) -> None:
        """Build the video player grid."""
        # Stop existing slots
        for slot in self._slots:
            slot.player.stop()

        # Remove old grid children
        child = self.grid.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.grid.remove(child)
            child = next_child

        self._slots.clear()
        self._selected_slot = None

        layout_name = self.layout_combo.get_active_id() or "2x2"
        rows, cols = LAYOUTS.get(layout_name, (2, 2))

        for r in range(rows):
            for c in range(cols):
                idx = r * cols + c
                slot = CameraSlot(idx)
                slot.set_click_callback(self._on_slot_clicked)
                self.grid.attach(slot, c, r, 1, 1)
                self._slots.append(slot)

    def _on_layout_changed(self, combo: Gtk.ComboBoxText) -> None:
        # Preserve current assignments
        old: dict[int, Camera] = {}
        for slot in self._slots:
            if slot.camera:
                old[slot.index] = slot.camera

        self._build_grid()

        # Re-assign cameras that still fit
        for idx, cam in old.items():
            if idx < len(self._slots):
                self._slots[idx].assign(cam)
                self._start_stream(idx, cam)

        self._save_session()

    def _on_clear_clicked(self, btn: Gtk.Button) -> None:
        """Clear all streams and camera assignments."""
        for slot in self._slots:
            slot.clear()
        self._select_slot(None)
        self._save_session()

    def _on_slot_clicked(self, slot_idx: int) -> None:
        """Select a grid slot for the next camera assignment."""
        log.info("Slot %d clicked (was: %s)", slot_idx, self._selected_slot)
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
            # Switch to 1x1 and show this camera full-screen
            for slot in self._slots:
                slot.clear()
            # set_active_id triggers _on_layout_changed which rebuilds the grid
            self.layout_combo.set_active_id("1x1")
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

        # Clear the target slot
        if slot_idx < len(self._slots):
            self._slots[slot_idx].clear()
            self._slots[slot_idx].assign(camera)
            self._start_stream(slot_idx, camera)

    def _start_stream(self, slot_idx: int, camera: Camera) -> None:
        """Start streaming a camera in a slot."""
        if not self.app.api:
            return

        api = self.app.api
        protocol = self.app.config.camera_protocols.get(camera.id, "auto")
        override = self.app.config.camera_overrides.get(camera.id, "")

        async def _get_url() -> tuple[int, str]:
            url = await get_live_view_path(api, camera.id, protocol=protocol, override_url=override)
            return slot_idx, url

        run_async(
            _get_url(),
            callback=self._on_stream_url,
            error_callback=lambda e: log.error(
                "Failed to get stream URL for %s: %s", camera.name, e
            ),
        )

    def _on_stream_url(self, result: tuple[int, str]) -> None:
        slot_idx, url = result
        if slot_idx < len(self._slots):
            log.info("Starting stream in slot %d: %s", slot_idx, url)
            self._slots[slot_idx].player.play(url)

    def _save_session(self) -> None:
        """Persist grid layout and camera assignments to config."""
        layout = self.layout_combo.get_active_id() or "2x2"
        cam_ids: list[int] = []
        for slot in self._slots:
            cam_ids.append(slot.camera.id if slot.camera else 0)
        self.app.config.grid_layout = layout
        self.app.config.last_cameras = cam_ids
        save_config(self.app.config)

    def restore_session(self, cameras: list[Camera]) -> None:
        """Restore camera assignments from config."""
        cam_map = {c.id: c for c in cameras}
        seen: set[int] = set()
        for i, cam_id in enumerate(self.app.config.last_cameras):
            if cam_id and cam_id in cam_map and i < len(self._slots):
                if cam_id in seen:
                    continue
                seen.add(cam_id)
                cam = cam_map[cam_id]
                self._slots[i].assign(cam)
                self._start_stream(i, cam)

    def stop_all(self) -> None:
        """Stop all streams."""
        for slot in self._slots:
            slot.clear()

    def stop_slot(self, slot_idx: int) -> None:
        """Stop stream in a specific slot."""
        if slot_idx < len(self._slots):
            self._slots[slot_idx].clear()
