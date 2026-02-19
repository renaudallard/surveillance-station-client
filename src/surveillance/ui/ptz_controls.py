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

"""PTZ direction pad, zoom, and preset controls."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera, PtzPatrol, PtzPreset
from surveillance.services import ptz
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)


class PtzControls(Gtk.Box):
    """PTZ control panel with direction pad, zoom, and presets."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.window = window
        self.app = window.app
        self.camera: Camera | None = None
        self._presets: list[PtzPreset] = []
        self._patrols: list[PtzPatrol] = []

        self.set_margin_top(4)
        self.set_margin_bottom(4)
        self.set_margin_start(8)
        self.set_margin_end(8)
        self.set_halign(Gtk.Align.CENTER)

        # -- Left column: Direction pad (3x3 grid) --
        pad = Gtk.Grid()
        pad.set_row_homogeneous(True)
        pad.set_column_homogeneous(True)
        pad.set_row_spacing(2)
        pad.set_column_spacing(2)
        pad.set_valign(Gtk.Align.CENTER)
        pad.add_css_class("ptz-pad")

        # Continuous directions use press/release (Start/Stop)
        move_dirs = [
            (0, 1, "up", "\u2191"),
            (1, 0, "left", "\u2190"),
            (1, 2, "right", "\u2192"),
            (2, 1, "down", "\u2193"),
        ]

        for row, col, direction, symbol in move_dirs:
            btn = Gtk.Button(label=symbol)
            gesture = Gtk.GestureClick()
            gesture.connect("pressed", self._on_move_press, direction)
            gesture.connect("released", self._on_move_release, direction)
            btn.add_controller(gesture)
            pad.attach(btn, col, row, 1, 1)

        # Home is a single click
        home_btn = Gtk.Button(label="\u2302")
        home_btn.connect("clicked", self._on_home)
        pad.attach(home_btn, 1, 1, 1, 1)

        self.append(pad)

        # -- Center column: Zoom --
        zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        zoom_box.set_valign(Gtk.Align.CENTER)

        zoom_out_btn = Gtk.Button(label="\u2212")  # minus
        zoom_out_btn.set_tooltip_text("Zoom Out")
        zoom_out_gesture = Gtk.GestureClick()
        zoom_out_gesture.connect("pressed", self._on_zoom_press, "out")
        zoom_out_gesture.connect("released", self._on_zoom_release, "out")
        zoom_out_btn.add_controller(zoom_out_gesture)
        zoom_box.append(zoom_out_btn)

        zoom_label = Gtk.Label(label="Zoom")
        zoom_box.append(zoom_label)

        zoom_in_btn = Gtk.Button(label="+")
        zoom_in_btn.set_tooltip_text("Zoom In")
        zoom_in_gesture = Gtk.GestureClick()
        zoom_in_gesture.connect("pressed", self._on_zoom_press, "in")
        zoom_in_gesture.connect("released", self._on_zoom_release, "in")
        zoom_in_btn.add_controller(zoom_in_gesture)
        zoom_box.append(zoom_in_btn)

        self.append(zoom_box)

        # -- Right column: Presets + Patrols --
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        right.set_valign(Gtk.Align.CENTER)

        preset_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        preset_label = Gtk.Label(label="Preset:")
        preset_box.append(preset_label)
        self.preset_combo = Gtk.ComboBoxText()
        self.preset_combo.connect("changed", self._on_preset_changed)
        preset_box.append(self.preset_combo)
        right.append(preset_box)

        patrol_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        patrol_label = Gtk.Label(label="Patrol:")
        patrol_box.append(patrol_label)
        self.patrol_combo = Gtk.ComboBoxText()
        patrol_box.append(self.patrol_combo)
        patrol_btn = Gtk.Button(label="Start")
        patrol_btn.connect("clicked", self._on_start_patrol)
        patrol_box.append(patrol_btn)
        right.append(patrol_box)

        self.append(right)

    def set_camera(self, camera: Camera) -> None:
        """Set the camera to control."""
        self.camera = camera
        if camera.is_ptz:
            self.set_sensitive(True)
            self._load_presets()
            self._load_patrols()
        else:
            self.set_sensitive(False)

    def _on_move_press(
        self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, direction: str
    ) -> None:
        if not self.camera or not self.app.api:
            return
        run_async(
            ptz.move(self.app.api, self.camera.id, f"{direction}Start"),
            error_callback=lambda e: log.error("PTZ move failed: %s", e),
        )

    def _on_move_release(
        self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, direction: str
    ) -> None:
        if not self.camera or not self.app.api:
            return
        run_async(
            ptz.move(self.app.api, self.camera.id, f"{direction}Stop"),
            error_callback=lambda e: log.error("PTZ move failed: %s", e),
        )

    def _on_home(self, btn: Gtk.Button) -> None:
        if not self.camera or not self.app.api:
            return
        run_async(
            ptz.move(self.app.api, self.camera.id, "home"),
            error_callback=lambda e: log.error("PTZ move failed: %s", e),
        )

    def _on_zoom_press(
        self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, direction: str
    ) -> None:
        if not self.camera or not self.app.api:
            return
        run_async(
            ptz.zoom(self.app.api, self.camera.id, f"{direction}Start"),
            error_callback=lambda e: log.error("PTZ zoom failed: %s", e),
        )

    def _on_zoom_release(
        self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, direction: str
    ) -> None:
        if not self.camera or not self.app.api:
            return
        run_async(
            ptz.zoom(self.app.api, self.camera.id, f"{direction}Stop"),
            error_callback=lambda e: log.error("PTZ zoom failed: %s", e),
        )

    def _load_presets(self) -> None:
        if not self.camera or not self.app.api:
            return
        run_async(
            ptz.list_presets(self.app.api, self.camera.id),
            callback=self._update_presets,
        )

    def _update_presets(self, presets: list[PtzPreset]) -> None:
        self._presets = presets
        self.preset_combo.remove_all()
        for p in presets:
            self.preset_combo.append(str(p.id), p.name)

    def _on_preset_changed(self, combo: Gtk.ComboBoxText) -> None:
        preset_id_str = combo.get_active_id()
        if not preset_id_str or not self.camera or not self.app.api:
            return
        run_async(
            ptz.go_preset(self.app.api, self.camera.id, int(preset_id_str)),
            error_callback=lambda e: log.error("PTZ preset failed: %s", e),
        )

    def _load_patrols(self) -> None:
        if not self.camera or not self.app.api:
            return
        run_async(
            ptz.list_patrols(self.app.api, self.camera.id),
            callback=self._update_patrols,
        )

    def _update_patrols(self, patrols: list[PtzPatrol]) -> None:
        self._patrols = patrols
        self.patrol_combo.remove_all()
        for p in patrols:
            self.patrol_combo.append(str(p.id), p.name)

    def _on_start_patrol(self, btn: Gtk.Button) -> None:
        patrol_id_str = self.patrol_combo.get_active_id()
        if not patrol_id_str or not self.camera or not self.app.api:
            return
        run_async(
            ptz.run_patrol(self.app.api, self.camera.id, int(patrol_id_str)),
            error_callback=lambda e: log.error("PTZ patrol failed: %s", e),
        )
