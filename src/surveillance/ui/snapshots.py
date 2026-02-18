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

"""Snapshot browser with grid view."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera, Snapshot
from surveillance.services.snapshot import delete_snapshot, list_snapshots, save_snapshot
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)


class SnapshotsView(Gtk.Box):
    """Snapshot browser with take/browse/download/delete."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.app = window.app
        self._snapshots: list[Snapshot] = []
        self._camera_id: int | None = None

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)

        label = Gtk.Label(label="Snapshots")
        label.add_css_class("title-4")
        label.set_hexpand(True)
        label.set_xalign(0)
        toolbar.append(label)

        # Take snapshot button
        self.take_btn = Gtk.Button()
        self.take_btn.set_icon_name("camera-photo-symbolic")
        self.take_btn.set_tooltip_text("Take Snapshot")
        self.take_btn.connect("clicked", self._on_take_snapshot)
        self.take_btn.set_sensitive(False)
        toolbar.append(self.take_btn)

        refresh_btn = Gtk.Button()
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.connect("clicked", lambda _: self._load_snapshots())
        toolbar.append(refresh_btn)

        self.append(toolbar)
        self.append(Gtk.Separator())

        # Snapshot list
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scroll.set_child(self.listbox)
        self.append(scroll)

    def _load_snapshots(self) -> None:
        if not self.app.api:
            return
        run_async(
            list_snapshots(self.app.api, self._camera_id),
            callback=self._on_snapshots_loaded,
            error_callback=lambda e: log.error("Failed to load snapshots: %s", e),
        )

    def _on_snapshots_loaded(self, result: tuple[list[Snapshot], int]) -> None:
        snapshots, total = result
        self._snapshots = snapshots

        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)

        for snap in snapshots:
            row = self._create_snapshot_row(snap)
            self.listbox.append(row)

    def _create_snapshot_row(self, snap: Snapshot) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        cam_label = Gtk.Label(label=snap.camera_name)
        cam_label.add_css_class("camera-label")
        cam_label.set_xalign(0)
        cam_label.set_size_request(150, -1)
        box.append(cam_label)

        time_str = datetime.fromtimestamp(snap.create_time).strftime("%Y-%m-%d %H:%M:%S")
        time_label = Gtk.Label(label=time_str)
        time_label.set_hexpand(True)
        time_label.set_xalign(0)
        box.append(time_label)

        # Download button
        dl_btn = Gtk.Button()
        dl_btn.set_icon_name("document-save-symbolic")
        dl_btn.set_tooltip_text("Download")
        dl_btn.connect("clicked", self._on_download, snap)
        box.append(dl_btn)

        # Delete button
        del_btn = Gtk.Button()
        del_btn.set_icon_name("user-trash-symbolic")
        del_btn.set_tooltip_text("Delete")
        del_btn.connect("clicked", self._on_delete, snap)
        box.append(del_btn)

        row.set_child(box)
        return row

    def _on_take_snapshot(self, btn: Gtk.Button) -> None:
        camera = self.window.selected_camera
        if not camera or not self.app.api:
            return

        snap_dir = Path(self.app.config.snapshot_dir)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = snap_dir / f"{camera.name}_{timestamp}.jpg"

        run_async(
            save_snapshot(self.app.api, camera.id, output),
            callback=lambda p: self._on_snapshot_saved(p),
            error_callback=lambda e: log.error("Snapshot failed: %s", e),
        )

    def _on_snapshot_saved(self, path: object) -> None:
        log.info("Snapshot saved: %s", path)
        self._load_snapshots()

    def _on_download(self, btn: Gtk.Button, snap: Snapshot) -> None:
        dialog = Gtk.FileDialog()
        time_str = datetime.fromtimestamp(snap.create_time).strftime("%Y%m%d_%H%M%S")
        dialog.set_initial_name(f"{snap.camera_name}_{time_str}.jpg")

        def _on_save(d: Gtk.FileDialog, result: object) -> None:
            try:
                gfile = d.save_finish(result)
                if gfile:
                    path = gfile.get_path()
                    if path:
                        from surveillance.services.snapshot import download_snapshot

                        if self.app.api is None:
                            return
                        run_async(
                            download_snapshot(self.app.api, snap.id, Path(path)),
                            callback=lambda p: log.info("Downloaded to %s", p),
                            error_callback=lambda e: log.error("Download failed: %s", e),
                        )
            except Exception as e:
                log.error("Save dialog error: %s", e)

        dialog.save(self.window, None, _on_save)

    def _on_delete(self, btn: Gtk.Button, snap: Snapshot) -> None:
        if not self.app.api:
            return
        run_async(
            delete_snapshot(self.app.api, snap.id),
            callback=lambda _: self._load_snapshots(),
            error_callback=lambda e: log.error("Delete failed: %s", e),
        )

    def on_camera_selected(self, camera: Camera) -> None:
        """Handle camera selection from sidebar."""
        self._camera_id = camera.id
        self.take_btn.set_sensitive(True)
        self._load_snapshots()
