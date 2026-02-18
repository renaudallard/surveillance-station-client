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

"""Recording browser with filters and playback."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GdkPixbuf, Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera, Recording
from surveillance.services.recording import fetch_camera_snapshot, list_recordings
from surveillance.util.async_bridge import run_async

_THUMB_WIDTH = 120
_THUMB_HEIGHT = 68

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)


class RecordingsView(Gtk.Box):
    """Recording browser with camera filter and recording list."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.app = window.app
        self._recordings: list[Recording] = []
        self._total = 0
        self._offset = 0
        self._camera_id: int | None = None

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)

        label = Gtk.Label(label="Recordings")
        label.add_css_class("title-4")
        label.set_hexpand(True)
        label.set_xalign(0)
        toolbar.append(label)

        # Filter: all or selected camera
        filter_label = Gtk.Label(label="Camera:")
        toolbar.append(filter_label)

        self.camera_combo = Gtk.ComboBoxText()
        self.camera_combo.append("all", "All cameras")
        self.camera_combo.set_active_id("all")
        self.camera_combo.connect("changed", self._on_filter_changed)
        toolbar.append(self.camera_combo)

        refresh_btn = Gtk.Button()
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.connect("clicked", lambda _: self._load_recordings())
        toolbar.append(refresh_btn)

        self.append(toolbar)
        self.append(Gtk.Separator())

        # Recording list
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-activated", self._on_row_activated)
        scroll.set_child(self.listbox)
        self.append(scroll)

        # Pagination
        page_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        page_box.set_halign(Gtk.Align.CENTER)
        page_box.set_margin_top(4)
        page_box.set_margin_bottom(4)

        self.prev_btn = Gtk.Button(label="Previous")
        self.prev_btn.connect("clicked", self._on_prev)
        self.prev_btn.set_sensitive(False)
        page_box.append(self.prev_btn)

        self.page_label = Gtk.Label(label="")
        page_box.append(self.page_label)

        self.next_btn = Gtk.Button(label="Next")
        self.next_btn.connect("clicked", self._on_next)
        self.next_btn.set_sensitive(False)
        page_box.append(self.next_btn)

        self.append(page_box)

        # Update camera filter from sidebar and load initial data
        self._update_camera_filter()
        self._load_recordings()

    def _update_camera_filter(self) -> None:
        """Populate camera filter from sidebar camera list."""
        for cam in self.window.sidebar.cameras:
            self.camera_combo.append(str(cam.id), cam.name)

    def _on_filter_changed(self, combo: Gtk.ComboBoxText) -> None:
        active = combo.get_active_id()
        if active == "all":
            self._camera_id = None
        else:
            try:
                self._camera_id = int(active) if active else None
            except ValueError:
                self._camera_id = None
        self._offset = 0
        self._load_recordings()

    def _load_recordings(self) -> None:
        if not self.app.api:
            return
        run_async(
            list_recordings(self.app.api, self._camera_id, self._offset),
            callback=self._on_recordings_loaded,
            error_callback=lambda e: log.error("Failed to load recordings: %s", e),
        )

    def _on_recordings_loaded(self, result: tuple[list[Recording], int]) -> None:
        recordings, total = result
        self._recordings = recordings
        self._total = total

        # Clear list
        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)

        # Add rows and queue thumbnail loads
        for rec in recordings:
            row = self._create_recording_row(rec)
            self.listbox.append(row)
            self._load_thumbnail(row, rec)

        # Update pagination
        self.prev_btn.set_sensitive(self._offset > 0)
        self.next_btn.set_sensitive(self._offset + 50 < total)
        page = (self._offset // 50) + 1
        total_pages = max(1, (total + 49) // 50)
        self.page_label.set_text(f"Page {page} of {total_pages} ({total} total)")

    def _load_thumbnail(self, row: Gtk.ListBoxRow, rec: Recording) -> None:
        """Async-load a thumbnail for a recording row."""
        if not self.app.api:
            return
        picture: Gtk.Picture = row._thumbnail  # type: ignore[attr-defined]

        def _on_thumb(data: bytes) -> None:
            if not data:
                return
            try:
                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pixbuf = loader.get_pixbuf()
                if pixbuf:
                    texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                    picture.set_paintable(texture)
            except Exception as exc:
                log.warning("Thumbnail decode failed for recording %d: %s", rec.id, exc)

        run_async(
            fetch_camera_snapshot(self.app.api, rec.camera_id),
            callback=_on_thumb,
            error_callback=lambda e: log.warning("Thumbnail fetch failed: %s", e),
        )

    def _create_recording_row(self, rec: Recording) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.add_css_class("recording-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        # Thumbnail placeholder
        picture = Gtk.Picture()
        picture.set_size_request(_THUMB_WIDTH, _THUMB_HEIGHT)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.add_css_class("recording-thumbnail")
        box.append(picture)

        # Info column
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.set_valign(Gtk.Align.CENTER)

        cam_label = Gtk.Label(label=rec.camera_name)
        cam_label.add_css_class("camera-label")
        cam_label.set_xalign(0)
        info_box.append(cam_label)

        # Time range
        start = datetime.fromtimestamp(rec.start_time)
        stop = datetime.fromtimestamp(rec.stop_time)
        time_str = f"{start:%Y-%m-%d %H:%M:%S} - {stop:%H:%M:%S}"
        time_label = Gtk.Label(label=time_str)
        time_label.set_xalign(0)
        time_label.add_css_class("dim-label")
        time_label.add_css_class("caption")
        info_box.append(time_label)

        # Duration
        duration = rec.stop_time - rec.start_time
        mins, secs = divmod(duration, 60)
        dur_label = Gtk.Label(label=f"{mins}m {secs}s")
        dur_label.set_xalign(0)
        dur_label.add_css_class("dim-label")
        dur_label.add_css_class("caption")
        info_box.append(dur_label)

        box.append(info_box)

        # Play button
        play_btn = Gtk.Button()
        play_btn.set_icon_name("media-playback-start-symbolic")
        play_btn.set_tooltip_text("Play")
        play_btn.connect("clicked", self._on_play, rec)
        box.append(play_btn)

        # Download button
        dl_btn = Gtk.Button()
        dl_btn.set_icon_name("document-save-symbolic")
        dl_btn.set_tooltip_text("Download")
        dl_btn.connect("clicked", self._on_download, rec)
        box.append(dl_btn)

        row.set_child(box)
        row.recording = rec  # type: ignore[attr-defined]
        row._thumbnail = picture  # type: ignore[attr-defined]
        return row

    def _on_row_activated(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        rec = row.recording  # type: ignore[attr-defined]
        self._play_recording(rec)

    def _on_play(self, btn: Gtk.Button, rec: Recording) -> None:
        self._play_recording(rec)

    def _play_recording(self, rec: Recording) -> None:
        """Open recording in the player."""
        from surveillance.ui.player import PlayerDialog

        dialog = PlayerDialog(self.window, self.app, rec)
        dialog.present()

    def _on_download(self, btn: Gtk.Button, rec: Recording) -> None:
        """Download recording to disk."""
        dialog = Gtk.FileDialog()
        start = datetime.fromtimestamp(rec.start_time)
        dialog.set_initial_name(f"{rec.camera_name}_{start:%Y%m%d_%H%M%S}.mp4")

        def _on_save(d: Gtk.FileDialog, result: object) -> None:
            try:
                gfile = d.save_finish(result)
                if gfile:
                    path = gfile.get_path()
                    if path:
                        from pathlib import Path

                        from surveillance.services.recording import download_recording

                        if self.app.api is None:
                            return
                        run_async(
                            download_recording(self.app.api, rec.id, Path(path)),
                            callback=lambda p: log.info("Downloaded to %s", p),
                            error_callback=lambda e: log.error("Download failed: %s", e),
                        )
            except Exception as e:
                log.error("Save dialog error: %s", e)

        dialog.save(self.window, None, _on_save)

    def _on_prev(self, btn: Gtk.Button) -> None:
        self._offset = max(0, self._offset - 50)
        self._load_recordings()

    def _on_next(self, btn: Gtk.Button) -> None:
        self._offset += 50
        self._load_recordings()

    def on_camera_selected(self, camera: Camera) -> None:
        """Handle camera selection from sidebar."""
        self.camera_combo.set_active_id(str(camera.id))
