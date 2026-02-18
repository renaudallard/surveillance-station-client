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
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.window = window
        self.app = window.app
        self.camera: Camera | None = None
        self._presets: list[PtzPreset] = []
        self._patrols: list[PtzPatrol] = []

        self.set_margin_top(8)
        self.set_margin_bottom(8)
        self.set_margin_start(8)
        self.set_margin_end(8)

        # Title
        label = Gtk.Label(label="PTZ Controls")
        label.add_css_class("title-4")
        self.append(label)

        # Direction pad (3x3 grid)
        pad = Gtk.Grid()
        pad.set_row_homogeneous(True)
        pad.set_column_homogeneous(True)
        pad.set_row_spacing(2)
        pad.set_column_spacing(2)
        pad.set_halign(Gtk.Align.CENTER)
        pad.add_css_class("ptz-pad")

        directions = [
            (0, 0, "upleft", "\u2196"),
            (0, 1, "up", "\u2191"),
            (0, 2, "upright", "\u2197"),
            (1, 0, "left", "\u2190"),
            (1, 1, "home", "\u2302"),
            (1, 2, "right", "\u2192"),
            (2, 0, "downleft", "\u2199"),
            (2, 1, "down", "\u2193"),
            (2, 2, "downright", "\u2198"),
        ]

        for row, col, direction, symbol in directions:
            btn = Gtk.Button(label=symbol)
            btn.connect("clicked", self._on_direction, direction)
            pad.attach(btn, col, row, 1, 1)

        self.append(pad)

        # Zoom controls
        zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        zoom_box.set_halign(Gtk.Align.CENTER)

        zoom_out_btn = Gtk.Button(label="\u2212")  # minus
        zoom_out_btn.set_tooltip_text("Zoom Out")
        zoom_out_btn.connect("clicked", self._on_zoom, "out")
        zoom_box.append(zoom_out_btn)

        zoom_label = Gtk.Label(label="Zoom")
        zoom_box.append(zoom_label)

        zoom_in_btn = Gtk.Button(label="+")
        zoom_in_btn.set_tooltip_text("Zoom In")
        zoom_in_btn.connect("clicked", self._on_zoom, "in")
        zoom_box.append(zoom_in_btn)

        self.append(zoom_box)

        # Speed control
        speed_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        speed_box.set_halign(Gtk.Align.CENTER)
        speed_label = Gtk.Label(label="Speed:")
        speed_box.append(speed_label)

        self.speed_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 5, 1)
        self.speed_scale.set_value(3)
        self.speed_scale.set_size_request(100, -1)
        speed_box.append(self.speed_scale)
        self.append(speed_box)

        # Presets dropdown
        self.append(Gtk.Separator())
        preset_label = Gtk.Label(label="Presets")
        preset_label.add_css_class("title-4")
        self.append(preset_label)

        self.preset_combo = Gtk.ComboBoxText()
        self.preset_combo.connect("changed", self._on_preset_changed)
        self.append(self.preset_combo)

        # Patrols dropdown
        patrol_label = Gtk.Label(label="Patrols")
        patrol_label.add_css_class("title-4")
        self.append(patrol_label)

        self.patrol_combo = Gtk.ComboBoxText()
        self.append(self.patrol_combo)

        patrol_btn = Gtk.Button(label="Start Patrol")
        patrol_btn.connect("clicked", self._on_start_patrol)
        self.append(patrol_btn)

    def set_camera(self, camera: Camera) -> None:
        """Set the camera to control."""
        self.camera = camera
        if camera.is_ptz:
            self.set_sensitive(True)
            self._load_presets()
            self._load_patrols()
        else:
            self.set_sensitive(False)

    def _get_speed(self) -> int:
        return int(self.speed_scale.get_value())

    def _on_direction(self, btn: Gtk.Button, direction: str) -> None:
        if not self.camera or not self.app.api:
            return
        run_async(
            ptz.move(self.app.api, self.camera.id, direction, self._get_speed()),
            error_callback=lambda e: log.error("PTZ move failed: %s", e),
        )

    def _on_zoom(self, btn: Gtk.Button, direction: str) -> None:
        if not self.camera or not self.app.api:
            return
        run_async(
            ptz.zoom(self.app.api, self.camera.id, direction, self._get_speed()),
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
