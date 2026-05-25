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

import concurrent.futures
import contextlib
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera, Recording, decode_detection_labels
from surveillance.config import save_config
from surveillance.services.recording import (
    PRESET_LAST7D as _PRESET_LAST7D,
)
from surveillance.services.recording import (
    PRESET_LAST24H as _PRESET_LAST24H,
)
from surveillance.services.recording import (
    PRESET_TODAY as _PRESET_TODAY,
)
from surveillance.services.recording import (
    PRESET_YESTERDAY as _PRESET_YESTERDAY,
)
from surveillance.services.recording import (
    download_recording,
    fetch_recording_thumbnail,
    list_recordings,
)
from surveillance.services.recording import (
    preset_range as _preset_range,
)
from surveillance.ui.recording_search import RecordingSearchDialog
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
        self._loading = False
        self._thumb_futures: list[concurrent.futures.Future[bytes]] = []
        self._thumb_generation = 0
        self._search_camera_ids: list[int] | None = None
        self._search_from_time: int | None = None
        self._search_to_time: int | None = None
        self._search_time_preset: str = ""

        self._load_search_from_config()

        # ── Toolbar row 1 ────────────────────────────────────────────────
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

        filter_label = Gtk.Label(label="Camera:")
        toolbar.append(filter_label)

        self.camera_combo = Gtk.ComboBoxText()
        self.camera_combo.append("all", "All cameras")
        self.camera_combo.set_active_id("all")
        self.camera_combo.connect("changed", self._on_filter_changed)
        toolbar.append(self.camera_combo)

        refresh_btn = Gtk.Button()
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh")
        refresh_btn.connect("clicked", lambda _: self._load_recordings())
        toolbar.append(refresh_btn)

        search_btn = Gtk.Button()
        search_btn.set_icon_name("system-search-symbolic")
        search_btn.set_tooltip_text("Advanced search (custom range, multiple cameras)")
        search_btn.connect("clicked", self._on_search_clicked)
        toolbar.append(search_btn)

        self._reset_btn = Gtk.Button(label="Reset")
        self._reset_btn.set_icon_name("edit-clear-symbolic")
        self._reset_btn.set_tooltip_text("Clear all filters")
        self._reset_btn.connect("clicked", self._on_reset_clicked)
        toolbar.append(self._reset_btn)

        self.append(toolbar)

        # ── Toolbar row 2: quick date presets ────────────────────────────
        preset_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        preset_bar.set_margin_start(8)
        preset_bar.set_margin_end(8)
        preset_bar.set_margin_bottom(6)

        preset_label = Gtk.Label(label="Quick filter:")
        preset_label.add_css_class("dim-label")
        preset_label.add_css_class("caption")
        preset_bar.append(preset_label)

        self._preset_buttons: dict[str, Gtk.ToggleButton] = {}
        for key, text in [
            (_PRESET_TODAY, "Today"),
            (_PRESET_YESTERDAY, "Yesterday"),
            (_PRESET_LAST24H, "Last 24 h"),
            (_PRESET_LAST7D, "Last 7 days"),
        ]:
            btn = Gtk.ToggleButton(label=text)
            btn.add_css_class("flat")
            btn.add_css_class("caption")
            btn.connect("toggled", self._on_preset_toggled, key)
            preset_bar.append(btn)
            self._preset_buttons[key] = btn

        self.append(preset_bar)

        # ── Active filter summary ─────────────────────────────────────────
        self.filter_summary = Gtk.Label(label="")
        self.filter_summary.set_margin_start(8)
        self.filter_summary.set_margin_bottom(4)
        self.filter_summary.add_css_class("dim-label")
        self.filter_summary.add_css_class("caption")
        self.filter_summary.set_xalign(0)
        self.filter_summary.set_visible(True)
        self.append(self.filter_summary)

        self.append(Gtk.Separator())

        # ── Recording list ────────────────────────────────────────────────
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        log.debug("RECORDINGS INIT: plain Gtk.Box")
        scroll.set_child(self.row_box)
        self.append(scroll)

        # ── Download status bar ───────────────────────────────────────────
        self._status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._status_bar.set_margin_start(8)
        self._status_bar.set_margin_end(8)
        self._status_bar.set_margin_top(4)
        self._status_bar.set_margin_bottom(4)
        self._status_bar.set_visible(False)

        self._status_label = Gtk.Label(label="")
        self._status_label.set_hexpand(True)
        self._status_label.set_xalign(0)
        self._status_bar.append(self._status_label)

        self._open_folder_btn = Gtk.Button(label="Open Folder")
        self._open_folder_btn.set_visible(False)
        self._open_folder_btn.connect("clicked", self._on_open_folder)
        self._status_bar.append(self._open_folder_btn)

        self.append(self._status_bar)
        self._last_download_dir: str = ""

        # ── Pagination ────────────────────────────────────────────────────
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

        # Restore preset button state, populate cameras, load
        self._sync_preset_buttons()
        self._update_camera_filter()
        self._load_recordings()

    # ------------------------------------------------------------------
    # Camera filter helpers
    # ------------------------------------------------------------------

    def _update_camera_filter(self) -> None:
        """Populate camera filter from sidebar camera list."""
        self._known_camera_ids: set[int] = set()
        for cam in self.window.sidebar.cameras:
            if cam.id not in self._known_camera_ids:
                self.camera_combo.append(str(cam.id), cam.name)
                self._known_camera_ids.add(cam.id)

    def _ensure_camera_in_combo(self, camera_id: int, name: str) -> None:
        if not hasattr(self, "_known_camera_ids"):
            self._known_camera_ids = set()
        if camera_id not in self._known_camera_ids:
            self.camera_combo.append(str(camera_id), name)
            self._known_camera_ids.add(camera_id)

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

    def _on_camera_filter_clicked(self, btn: Gtk.Button, camera_id: int) -> None:
        log.debug("FILTER CLICKED: camera_id=%s", camera_id)
        self._ensure_camera_in_combo(camera_id, btn.get_label() or "")
        self._camera_id = camera_id
        self._offset = 0
        self.camera_combo.handler_block_by_func(self._on_filter_changed)
        self.camera_combo.set_active_id(str(camera_id))
        self.camera_combo.handler_unblock_by_func(self._on_filter_changed)
        self._load_recordings()

    # ------------------------------------------------------------------
    # Date preset helpers
    # ------------------------------------------------------------------

    def _sync_preset_buttons(self) -> None:
        """Update toggle state of preset buttons to match current preset."""
        for key, btn in self._preset_buttons.items():
            btn.handler_block_by_func(self._on_preset_toggled)
            btn.set_active(key == self._search_time_preset)
            btn.handler_unblock_by_func(self._on_preset_toggled)

    def _on_preset_toggled(self, btn: Gtk.ToggleButton, key: str) -> None:
        if not btn.get_active():
            # Deactivating — only clear if this was the active preset
            if self._search_time_preset == key:
                self._search_time_preset = ""
                self._search_from_time = None
                self._search_to_time = None
                self._offset = 0
                self._save_search_to_config()
                self._load_recordings()
            return

        # Activating this preset — deactivate all others
        self._search_time_preset = key
        from_ts, to_ts = _preset_range(key)
        self._search_from_time = from_ts
        self._search_to_time = to_ts
        self._offset = 0

        for other_key, other_btn in self._preset_buttons.items():
            if other_key != key:
                other_btn.handler_block_by_func(self._on_preset_toggled)
                other_btn.set_active(False)
                other_btn.handler_unblock_by_func(self._on_preset_toggled)

        self._save_search_to_config()
        self._load_recordings()

    # ------------------------------------------------------------------
    # Reset / advanced search
    # ------------------------------------------------------------------

    def _on_reset_clicked(self, btn: Gtk.Button) -> None:
        self._search_camera_ids = None
        self._search_from_time = None
        self._search_to_time = None
        self._search_time_preset = ""
        self._camera_id = None
        self._offset = 0
        self.camera_combo.handler_block_by_func(self._on_filter_changed)
        self.camera_combo.set_active_id("all")
        self.camera_combo.handler_unblock_by_func(self._on_filter_changed)
        self._sync_preset_buttons()
        self._save_search_to_config()
        self._load_recordings()

    def _on_search_clicked(self, btn: Gtk.Button) -> None:
        from_time = None
        to_time = None
        if self._search_from_time:
            from_time = datetime.fromtimestamp(self._search_from_time)
        if self._search_to_time:
            to_time = datetime.fromtimestamp(self._search_to_time)

        def _on_search(
            camera_ids: list[int] | None,
            from_dt: datetime | None,
            to_dt: datetime | None,
        ) -> None:
            self._search_camera_ids = camera_ids
            self._search_from_time = int(from_dt.timestamp()) if from_dt else None
            self._search_to_time = int(to_dt.timestamp()) if to_dt else None
            # Custom range clears preset
            self._search_time_preset = ""
            self._sync_preset_buttons()
            self._offset = 0
            self._save_search_to_config()
            self._load_recordings()

        def _on_reset() -> None:
            self._on_reset_clicked(btn)

        dialog = RecordingSearchDialog(
            self.window,
            self.window.sidebar.cameras,
            on_search=_on_search,
            on_reset=_on_reset,
            selected_ids=self._search_camera_ids,
            from_time=from_time,
            to_time=to_time,
        )
        dialog.present()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_recordings(self) -> None:
        log.debug(
            "_load_recordings: cam=%s offset=%d loading=%s search=%s preset=%s",
            self._camera_id,
            self._offset,
            self._loading,
            self._search_camera_ids,
            self._search_time_preset,
        )
        if not self.app.api or self._loading:
            return
        self._loading = True
        self.prev_btn.set_sensitive(False)
        self.next_btn.set_sensitive(False)
        self._update_filter_summary()

        camera_ids = self._search_camera_ids
        if camera_ids is None and self._camera_id is not None:
            camera_ids = [self._camera_id]

        run_async(
            list_recordings(
                self.app.api,
                camera_ids=camera_ids,
                from_time=self._search_from_time,
                to_time=self._search_to_time,
                offset=self._offset,
            ),
            callback=self._on_recordings_loaded,
            error_callback=self._on_load_error,
        )

    def _update_filter_summary(self) -> None:
        """Always show active filter state above the list."""
        parts: list[str] = []
        parts += self._camera_filter_parts()
        parts += self._time_filter_parts()
        if parts:
            self.filter_summary.set_text("Active filters: " + " | ".join(parts))
        else:
            self.filter_summary.set_text("No filters active — showing all recordings")

    def _camera_filter_parts(self) -> list[str]:
        cam_map = {c.id: c.name for c in self.window.sidebar.cameras}
        if self._search_camera_ids:
            names = [cam_map.get(cid, str(cid)) for cid in self._search_camera_ids]
            return [f"Cameras: {', '.join(names)}"]
        if self._camera_id is not None:
            return [f"Camera: {cam_map.get(self._camera_id, str(self._camera_id))}"]
        return []

    def _time_filter_parts(self) -> list[str]:
        _PRESET_LABELS = {
            _PRESET_TODAY: "Today",
            _PRESET_YESTERDAY: "Yesterday",
            _PRESET_LAST24H: "Last 24 h",
            _PRESET_LAST7D: "Last 7 days",
        }
        if self._search_time_preset:
            label = _PRESET_LABELS.get(self._search_time_preset, self._search_time_preset)
            return [f"Time: {label}"]
        parts: list[str] = []
        if self._search_from_time:
            ts = datetime.fromtimestamp(self._search_from_time)
            parts.append(f"From: {ts:%Y-%m-%d %H:%M}")
        if self._search_to_time:
            ts = datetime.fromtimestamp(self._search_to_time)
            parts.append(f"To: {ts:%Y-%m-%d %H:%M}")
        return parts

    def _on_load_error(self, error: Exception) -> None:
        self._loading = False
        self.prev_btn.set_sensitive(self._offset > 0)
        self.next_btn.set_sensitive(self._offset + 50 < self._total)
        log.error("Failed to load recordings: %s", error)

    def _on_recordings_loaded(self, result: tuple[list[Recording], int]) -> None:
        self._loading = False
        recordings, total = result
        self._recordings = recordings
        self._total = total
        log.debug("Loaded %d recordings (total=%d)", len(recordings), total)
        if recordings:
            r = recordings[0]
            log.debug("First rec: id=%d cam='%s' cam_id=%d", r.id, r.camera_name, r.camera_id)

        for f in self._thumb_futures:
            f.cancel()
        self._thumb_futures.clear()
        self._thumb_generation += 1

        while child := self.row_box.get_first_child():
            self.row_box.remove(child)

        generation = self._thumb_generation
        deferred: list[tuple[Gtk.Picture, Recording]] = []
        for i, rec in enumerate(recordings):
            row_box, picture = self._create_recording_row(rec)
            self.row_box.append(row_box)
            if i < 10:
                self._load_thumbnail(picture, rec)
            else:
                deferred.append((picture, rec))

        if deferred:
            def _load_rest() -> bool:
                if self._thumb_generation != generation:
                    return False
                for pic, r in deferred:
                    self._load_thumbnail(pic, r)
                return False

            GLib.idle_add(_load_rest)

        self.prev_btn.set_sensitive(self._offset > 0)
        self.next_btn.set_sensitive(self._offset + 50 < total)
        page = (self._offset // 50) + 1
        total_pages = max(1, (total + 49) // 50)
        self.page_label.set_text(f"Page {page} of {total_pages} ({total} total)")

    def _load_thumbnail(self, picture: Gtk.Picture, rec: Recording) -> None:
        if not self.app.api:
            return

        def _on_thumb(data: bytes) -> None:
            if not data:
                log.debug("Thumb rec %d: empty data", rec.id)
                return
            try:
                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pixbuf = loader.get_pixbuf()
                if pixbuf:
                    texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                    picture.set_paintable(texture)
                    log.debug(
                        "Thumb rec %d: set %dx%d", rec.id, pixbuf.get_width(), pixbuf.get_height()
                    )
                else:
                    log.debug("Thumb rec %d: no pixbuf", rec.id)
            except Exception as exc:
                log.warning("Thumbnail decode failed for recording %d: %s", rec.id, exc)

        future = run_async(
            fetch_recording_thumbnail(self.app.api, rec),
            callback=_on_thumb,
            error_callback=lambda e: log.warning("Thumbnail fetch failed: %s", e),
        )
        self._thumb_futures.append(future)

    # ------------------------------------------------------------------
    # Recording row
    # ------------------------------------------------------------------

    def _create_recording_row(self, rec: Recording) -> tuple[Gtk.Box, Gtk.Picture]:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.add_css_class("recording-row")
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        picture = Gtk.Picture()
        picture.set_size_request(_THUMB_WIDTH, _THUMB_HEIGHT)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.add_css_class("recording-thumbnail")
        box.append(picture)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.set_valign(Gtk.Align.CENTER)

        cam_btn = Gtk.Button(label=rec.camera_name or "(no name)")
        cam_btn.add_css_class("camera-label")
        cam_btn.add_css_class("flat")
        cam_btn.set_halign(Gtk.Align.START)
        cam_btn.set_tooltip_text(f"Filter recordings by {rec.camera_name}")
        cam_btn.connect("clicked", self._on_camera_filter_clicked, rec.camera_id)
        info_box.append(cam_btn)
        log.debug("ROW cam='%s' id=%d", rec.camera_name, rec.camera_id)

        start = datetime.fromtimestamp(rec.start_time)
        if rec.stop_time:
            stop = datetime.fromtimestamp(rec.stop_time)
            time_str = f"{start:%Y-%m-%d %H:%M:%S} - {stop:%H:%M:%S}"
        else:
            time_str = f"{start:%Y-%m-%d %H:%M:%S} (ongoing)"
        time_label = Gtk.Label(label=time_str)
        time_label.set_xalign(0)
        time_label.add_css_class("dim-label")
        time_label.add_css_class("caption")
        info_box.append(time_label)

        if rec.stop_time:
            duration = rec.stop_time - rec.start_time
            mins, secs = divmod(duration, 60)
            dur_label = Gtk.Label(label=f"{mins}m {secs}s")
            dur_label.set_xalign(0)
            dur_label.add_css_class("dim-label")
            dur_label.add_css_class("caption")
            info_box.append(dur_label)

        det_labels = decode_detection_labels(rec.detection_label)
        if det_labels:
            det_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            for tag in det_labels:
                tag_label = Gtk.Label(label=tag)
                tag_label.add_css_class("caption")
                tag_label.add_css_class("accent")
                det_box.append(tag_label)
            info_box.append(det_box)

        box.append(info_box)

        play_btn = Gtk.Button()
        play_btn.set_icon_name("media-playback-start-symbolic")
        play_btn.set_tooltip_text("Play")
        play_btn.connect("clicked", self._on_play, rec)
        box.append(play_btn)

        dl_btn = Gtk.Button()
        dl_btn.set_icon_name("document-save-symbolic")
        dl_btn.set_tooltip_text("Download")
        dl_btn.connect("clicked", self._on_download, rec, dl_btn)
        box.append(dl_btn)

        return box, picture

    # ------------------------------------------------------------------
    # Playback / download
    # ------------------------------------------------------------------

    def _on_play(self, btn: Gtk.Button, rec: Recording) -> None:
        log.debug("PLAY CLICKED: rec_id=%s", rec.id)
        from surveillance.ui.player import PlayerDialog

        dialog = PlayerDialog(self.window, self.app, rec)
        dialog.present()

    def _on_download(self, btn: Gtk.Button, rec: Recording, dl_btn: Gtk.Button) -> None:
        dialog = Gtk.FileDialog()
        start = datetime.fromtimestamp(rec.start_time)
        safe_name = re.sub(r'[/\\<>:"|?*]', "_", rec.camera_name)
        dialog.set_initial_name(f"{safe_name}_{start:%Y%m%d_%H%M%S}.mp4")

        def _on_save(d: Gtk.FileDialog, result: object) -> None:
            try:
                gfile = d.save_finish(result)
                if not gfile:
                    return
                path = gfile.get_path()
                if not path:
                    return
            except Exception:
                log.exception("Save dialog error")
                return

            if self.app.api is None:
                return

            dl_btn.set_sensitive(False)
            dl_btn.set_icon_name("emblem-synchronizing-symbolic")
            dl_btn.set_tooltip_text("Downloading…")
            self._show_status("Downloading…", download_dir=None)

            def _on_done(p: Path) -> None:
                dl_btn.set_sensitive(True)
                dl_btn.set_icon_name("emblem-ok-symbolic")
                dl_btn.set_tooltip_text(f"Downloaded: {p}")
                self._show_status(f"Saved to {p}", download_dir=str(p.parent))
                log.info("Downloaded recording to %s", p)

            def _on_err(e: Exception) -> None:
                dl_btn.set_sensitive(True)
                dl_btn.set_icon_name("document-save-symbolic")
                dl_btn.set_tooltip_text("Download")
                self._show_download_error(str(e))
                log.error("Download failed: %s", e)

            run_async(
                download_recording(self.app.api, rec.id, Path(path)),
                callback=_on_done,
                error_callback=_on_err,
            )

        dialog.save(self.window, None, _on_save)

    def _show_status(self, message: str, download_dir: str | None) -> None:
        self._status_label.set_text(message)
        self._status_bar.set_visible(True)
        if download_dir:
            self._last_download_dir = download_dir
            self._open_folder_btn.set_visible(True)
        else:
            self._open_folder_btn.set_visible(False)

    def _show_download_error(self, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Download failed",
            secondary_text=message,
        )
        dialog.connect("response", lambda d, _: d.close())
        dialog.present()

    def _on_open_folder(self, btn: Gtk.Button) -> None:
        if self._last_download_dir:
            from gi.repository import Gio

            uri = f"file://{self._last_download_dir}"
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except Exception as exc:
                log.warning("Could not open folder %s: %s", self._last_download_dir, exc)

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _on_prev(self, btn: Gtk.Button) -> None:
        self._offset = max(0, self._offset - 50)
        self._load_recordings()

    def _on_next(self, btn: Gtk.Button) -> None:
        self._offset += 50
        self._load_recordings()

    # ------------------------------------------------------------------
    # Sidebar camera click → filter
    # ------------------------------------------------------------------

    def on_camera_selected(self, camera: Camera) -> None:
        """Handle camera selection from sidebar — filter recordings by this camera."""
        log.debug("RecordingsView.on_camera_selected: cam=%s id=%d", camera.name, camera.id)
        self._ensure_camera_in_combo(camera.id, camera.name)
        self._camera_id = camera.id
        self._offset = 0
        self.camera_combo.handler_block_by_func(self._on_filter_changed)
        self.camera_combo.set_active_id(str(camera.id))
        self.camera_combo.handler_unblock_by_func(self._on_filter_changed)
        self._load_recordings()

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------

    def _load_search_from_config(self) -> None:
        cfg = self.app.config
        if cfg.search_camera_ids:
            self._search_camera_ids = cfg.search_camera_ids
        if cfg.search_from_time:
            with contextlib.suppress(ValueError):
                self._search_from_time = int(
                    datetime.fromisoformat(cfg.search_from_time).timestamp()
                )
        if cfg.search_to_time:
            with contextlib.suppress(ValueError):
                self._search_to_time = int(datetime.fromisoformat(cfg.search_to_time).timestamp())
        self._search_time_preset = cfg.search_time_preset

    def _save_search_to_config(self) -> None:
        cfg = self.app.config
        cfg.search_camera_ids = self._search_camera_ids or []
        cfg.search_from_time = ""
        if self._search_from_time:
            cfg.search_from_time = datetime.fromtimestamp(self._search_from_time).isoformat()
        cfg.search_to_time = ""
        if self._search_to_time:
            cfg.search_to_time = datetime.fromtimestamp(self._search_to_time).isoformat()
        cfg.search_time_preset = self._search_time_preset
        save_config(cfg)
