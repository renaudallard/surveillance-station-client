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

"""Camera list sidebar."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera, CameraStatus
from surveillance.services.camera import list_cameras
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.app import SurveillanceApp
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)


class CameraSidebar(Gtk.Box):
    """Sidebar showing the list of cameras."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.app: SurveillanceApp = window.get_application()  # type: ignore[assignment]
        self.cameras: list[Camera] = []
        self._poll_id: int = 0

        self.add_css_class("sidebar")
        self.set_size_request(220, -1)

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_top(8)
        header.set_margin_bottom(8)
        header.set_margin_start(8)
        header.set_margin_end(8)
        label = Gtk.Label(label="Cameras")
        label.add_css_class("title-4")
        label.set_hexpand(True)
        label.set_xalign(0)
        header.append(label)

        refresh_btn = Gtk.Button()
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh")
        refresh_btn.connect("clicked", lambda _: self.refresh())
        header.append(refresh_btn)
        self.append(header)

        # Scrollable camera list
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-selected", self._on_row_selected)
        scroll.set_child(self.listbox)
        self.append(scroll)

        # View switcher at the bottom
        nav_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        nav_box.set_margin_top(8)
        nav_box.set_margin_bottom(8)
        nav_box.set_margin_start(8)
        nav_box.set_margin_end(8)

        for icon, label_text, page_name in [
            ("camera-video-symbolic", "Live View", "live"),
            ("media-playback-start-symbolic", "Recordings", "recordings"),
            ("camera-photo-symbolic", "Snapshots", "snapshots"),
            ("dialog-warning-symbolic", "Events", "events"),
        ]:
            btn = Gtk.Button()
            btn_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn_icon = Gtk.Image.new_from_icon_name(icon)
            btn_label = Gtk.Label(label=label_text)
            btn_label.set_xalign(0)
            btn_label.set_hexpand(True)
            btn_content.append(btn_icon)
            btn_content.append(btn_label)
            btn.set_child(btn_content)
            btn.connect("clicked", self._on_nav_clicked, page_name)
            nav_box.append(btn)

        self.append(Gtk.Separator())
        self.append(nav_box)

    def refresh(self, on_complete: Any = None) -> None:
        """Refresh the camera list."""
        if not self.app.api:
            return

        def _on_loaded(cameras: list[Camera]) -> None:
            self._update_camera_list(cameras)
            if on_complete:
                on_complete(cameras)

        run_async(
            list_cameras(self.app.api),
            callback=_on_loaded,
            error_callback=lambda e: log.error("Failed to refresh cameras: %s", e),
        )

    def _update_camera_list(self, cameras: list[Camera]) -> None:
        self.cameras = cameras

        # Remove old rows
        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)

        # Add camera rows
        for cam in cameras:
            row = self._create_camera_row(cam)
            self.listbox.append(row)

    def _create_camera_row(self, cam: Camera) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        # Status indicator
        status_icon = Gtk.Image()
        if cam.status == CameraStatus.ENABLED:
            status_icon.set_from_icon_name("emblem-ok-symbolic")
            status_icon.add_css_class("camera-status-online")
        elif cam.status == CameraStatus.DISCONNECTED:
            status_icon.set_from_icon_name("network-offline-symbolic")
            status_icon.add_css_class("camera-status-offline")
        else:
            status_icon.set_from_icon_name("action-unavailable-symbolic")
            status_icon.add_css_class("camera-status-offline")
        box.append(status_icon)

        # Camera info
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name_label = Gtk.Label(label=cam.name)
        name_label.set_xalign(0)
        name_label.add_css_class("camera-label")
        info_box.append(name_label)

        detail = f"{cam.vendor} {cam.model}" if cam.vendor else cam.model
        if detail:
            detail_label = Gtk.Label(label=detail)
            detail_label.set_xalign(0)
            detail_label.add_css_class("dim-label")
            detail_label.add_css_class("caption")
            info_box.append(detail_label)

        info_box.set_hexpand(True)
        box.append(info_box)

        # PTZ badge
        if cam.is_ptz:
            ptz_label = Gtk.Label(label="PTZ")
            ptz_label.add_css_class("caption")
            box.append(ptz_label)

        row.set_child(box)
        row.camera = cam  # type: ignore[attr-defined]
        return row

    def _on_row_selected(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        cam = row.camera  # type: ignore[attr-defined]
        self.window.on_camera_selected(cam)

    def _on_nav_clicked(self, btn: Gtk.Button, page_name: str) -> None:
        self.window.show_page(page_name)

    def start_polling(self) -> None:
        """Start periodic camera status polling."""
        interval = self.app.config.poll_interval_cameras
        self._poll_id = GLib.timeout_add_seconds(interval, self._poll_tick)

    def stop_polling(self) -> None:
        """Stop polling."""
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0

    def _poll_tick(self) -> bool:
        self.refresh()
        return True  # continue polling
