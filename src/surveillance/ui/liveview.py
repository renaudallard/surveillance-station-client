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


class LiveView(Gtk.Box):
    """Live camera view with configurable grid layout."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.app = window.app
        self._players: list[MpvGLArea] = []
        self._frames: list[Gtk.Frame] = []
        self._assigned: dict[int, Camera] = {}  # slot_index -> Camera
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
        # Stop and remove existing players
        for player in self._players:
            player.stop()
        self._players.clear()
        self._frames.clear()
        self._selected_slot = None

        # Remove old grid children
        while True:
            child = self.grid.get_child_at(0, 0)
            if child is None:
                break
            self.grid.remove(child)
        # Clear all cells
        for r in range(3):
            for c in range(3):
                child = self.grid.get_child_at(c, r)
                if child:
                    self.grid.remove(child)

        layout_name = self.layout_combo.get_active_id() or "2x2"
        rows, cols = LAYOUTS.get(layout_name, (2, 2))

        for r in range(rows):
            for c in range(cols):
                idx = r * cols + c
                frame = Gtk.Frame()
                overlay = Gtk.Overlay()
                player = MpvGLArea()
                overlay.set_child(player)
                # Invisible click target on top of the player
                click_target = Gtk.Box()
                click_target.set_hexpand(True)
                click_target.set_vexpand(True)
                click_target.set_can_target(True)
                click_gesture = Gtk.GestureClick(button=1)
                click_gesture.connect("pressed", self._on_slot_clicked, idx)
                click_target.add_controller(click_gesture)
                overlay.add_overlay(click_target)
                frame.set_child(overlay)
                self.grid.attach(frame, c, r, 1, 1)
                self._players.append(player)
                self._frames.append(frame)

        # Re-assign cameras to slots
        old_assigned = dict(self._assigned)
        self._assigned.clear()
        for idx, cam in old_assigned.items():
            if idx < len(self._players):
                self._assigned[idx] = cam
                self._start_stream(idx, cam)

    def _on_layout_changed(self, combo: Gtk.ComboBoxText) -> None:
        self._build_grid()
        self._save_session()

    def _on_clear_clicked(self, btn: Gtk.Button) -> None:
        """Clear all streams and camera assignments."""
        self.stop_all()
        self._select_slot(None)
        self._save_session()

    def _on_slot_clicked(
        self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, slot: int
    ) -> None:
        """Select a grid slot for the next camera assignment."""
        if self._selected_slot == slot:
            self._select_slot(None)
        else:
            self._select_slot(slot)

    def _select_slot(self, slot: int | None) -> None:
        """Update the selected slot and its visual indicator."""
        # Remove highlight from previous selection
        if self._selected_slot is not None and self._selected_slot < len(self._frames):
            self._frames[self._selected_slot].remove_css_class("slot-selected")
        self._selected_slot = slot
        # Add highlight to new selection
        if slot is not None and slot < len(self._frames):
            self._frames[slot].add_css_class("slot-selected")

    def on_camera_selected(self, camera: Camera) -> None:
        """Handle camera selection - assign to selected or next available slot."""
        # If camera is already in a slot, remove it first (allows moving)
        old_slot: int | None = None
        for idx, cam in self._assigned.items():
            if cam.id == camera.id:
                old_slot = idx
                break

        # Determine target slot
        if self._selected_slot is not None:
            slot = self._selected_slot
        elif old_slot is not None:
            # Already displayed and no slot selected — do nothing
            return
        else:
            # Find an empty slot
            slot = None
            for i in range(len(self._players)):
                if i not in self._assigned:
                    slot = i
                    break
            if slot is None:
                return

        assert slot is not None  # ensured by early returns above

        # Remove from old slot if moving
        if old_slot is not None and old_slot != slot:
            self._players[old_slot].stop()
            del self._assigned[old_slot]

        # Stop whatever was in the target slot
        if slot in self._assigned:
            self._players[slot].stop()

        self._assigned[slot] = camera
        self._start_stream(slot, camera)
        self._select_slot(None)
        self._save_session()

    def _start_stream(self, slot: int, camera: Camera) -> None:
        """Start streaming a camera in a slot."""
        if not self.app.api:
            return

        api = self.app.api

        protocol = self.app.config.camera_protocols.get(camera.id, "auto")
        override = self.app.config.camera_overrides.get(camera.id, "")

        async def _get_url() -> tuple[int, str]:
            url = await get_live_view_path(api, camera.id, protocol=protocol, override_url=override)
            return slot, url

        run_async(
            _get_url(),
            callback=self._on_stream_url,
            error_callback=lambda e: log.error(
                "Failed to get stream URL for %s: %s", camera.name, e
            ),
        )

    def _on_stream_url(self, result: tuple[int, str]) -> None:
        slot, url = result
        if slot < len(self._players):
            log.info("Starting stream in slot %d: %s", slot, url)
            self._players[slot].play(url)

    def _save_session(self) -> None:
        """Persist grid layout and camera assignments to config."""
        layout = self.layout_combo.get_active_id() or "2x2"
        cam_ids: list[int] = []
        for i in range(len(self._players)):
            cam = self._assigned.get(i)
            cam_ids.append(cam.id if cam else 0)
        self.app.config.grid_layout = layout
        self.app.config.last_cameras = cam_ids
        save_config(self.app.config)

    def restore_session(self, cameras: list[Camera]) -> None:
        """Restore camera assignments from config."""
        cam_map = {c.id: c for c in cameras}
        for i, cam_id in enumerate(self.app.config.last_cameras):
            if cam_id and cam_id in cam_map and i < len(self._players):
                cam = cam_map[cam_id]
                self._assigned[i] = cam
                self._start_stream(i, cam)

    def stop_all(self) -> None:
        """Stop all streams."""
        for player in self._players:
            player.stop()
        self._assigned.clear()

    def stop_slot(self, slot: int) -> None:
        """Stop stream in a specific slot."""
        if slot < len(self._players):
            self._players[slot].stop()
            self._assigned.pop(slot, None)
