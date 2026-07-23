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

"""Snapshot browser with filters and picture viewing — same layout as
Recordings, with "Play" mapped to viewing the still image instead of
starting video playback."""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GdkPixbuf, Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera, Snapshot
from surveillance.config import load_search_filters, save_search_filters
from surveillance.services.recording import (
    PRESET_LAST7D,
    PRESET_LAST24H,
    PRESET_TODAY,
    PRESET_YESTERDAY,
    preset_range,
)
from surveillance.services.snapshot import (
    delete_snapshot,
    download_snapshot,
    fetch_snapshot_image,
    list_snapshots,
)
from surveillance.ui.advanced_search import AdvancedSearchDialog
from surveillance.util.async_bridge import run_async

_THUMB_WIDTH = 120
_THUMB_HEIGHT = 68

# Server-side filtering + pagination is used whenever at most one camera
# is selected (see services.snapshot.list_snapshots — camId/from/to/start
# are all real, working params, confirmed against a real NAS). camId only
# accepts a single value though, so the one case that can't be done
# server-side is a multi-camera Advanced Search selection: that falls back
# to fetching everything in the time range (a large limit rather than
# genuinely unbounded — snapshot counts are normally far smaller than
# recordings or events) and filtering/paginating client-side instead. See
# _load_snapshots().
_PAGE_SIZE = 50
_FETCH_ALL_LIMIT = 5000

if TYPE_CHECKING:
    from surveillance.app import SurveillanceApp
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)

_COMBO_LABEL_MAX_LEN = 22


def _truncate_label(text: str, max_len: int = _COMBO_LABEL_MAX_LEN) -> str:
    """Cap a combo entry's display text so a long camera name can't keep
    growing the dropdown's width — GTK sizes a closed ComboBoxText from the
    longest entry ever appended, and GTK CSS has no max-width to cap that."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _format_size(num_bytes: int) -> str:
    """Human-readable file size, matching DSM's own KB/MB display."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class SnapshotsView(Gtk.Box):
    """Snapshot browser with camera filter and snapshot list."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.app = window.app
        self._snapshots: list[Snapshot] = []
        self._total = 0
        self._page = 0
        # Set only while a multi-camera Advanced Search selection is
        # active (see _load_snapshots) — the client-side-filtering
        # fallback for the one thing SnapShot::List's camId can't do.
        self._multi_camera_filter: set[int] | None = None
        self._camera_id: int | None = None
        self._loading = False
        self._search_camera_ids: list[int] | None = None
        self._search_from_time: int | None = None
        self._search_to_time: int | None = None
        self._search_time_preset: str = ""

        self._load_search_from_config()

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_halign(Gtk.Align.END)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)

        filter_label = Gtk.Label(label="Camera:")
        toolbar.append(filter_label)

        self.camera_combo = Gtk.ComboBoxText()
        self.camera_combo.append("all", "All cameras")
        self.camera_combo.set_active_id("all")
        self.camera_combo.connect("changed", self._on_filter_changed)
        # Caps width via CSS (see .filter-combo in style.css) — set_size_request
        # alone only sets a floor, not a ceiling: GTK still requests width for
        # the longest camera name ever appended, so it can keep growing as
        # more cameras are added, eventually squeezing neighboring widgets.
        self.camera_combo.add_css_class("filter-combo")
        # KNOWN COSMETIC QUIRK: see the canonical comment on camera_combo in events.py.
        toolbar.append(self.camera_combo)

        refresh_btn = Gtk.Button()
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh")
        refresh_btn.connect("clicked", lambda _: self._load_snapshots())
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

        # Quick date presets
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
            (PRESET_TODAY, "Today"),
            (PRESET_YESTERDAY, "Yesterday"),
            (PRESET_LAST24H, "Last 24 h"),
            (PRESET_LAST7D, "Last 7 days"),
        ]:
            btn = Gtk.ToggleButton(label=text)
            btn.add_css_class("flat")
            btn.add_css_class("caption")
            btn.connect("toggled", self._on_preset_toggled, key)
            preset_bar.append(btn)
            self._preset_buttons[key] = btn

        self.append(preset_bar)

        # Filter summary
        self.filter_summary = Gtk.Label(label="")
        self.filter_summary.set_margin_start(8)
        self.filter_summary.set_margin_bottom(4)
        self.filter_summary.add_css_class("dim-label")
        self.filter_summary.add_css_class("caption")
        self.filter_summary.set_xalign(0)
        self.append(self.filter_summary)

        self.append(Gtk.Separator())

        # Snapshot list
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroll.set_child(self.row_box)
        self.append(scroll)

        # Download status bar
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

        # Restore preset button state, populate cameras, load
        self._sync_preset_buttons()
        self.refresh_camera_filter()
        self._load_snapshots()

    def on_page_shown(self) -> None:
        """Refresh whenever this page becomes visible (called from
        MainWindow.show_page) — snapshots taken elsewhere, e.g. via Live
        View's right-click menu, wouldn't otherwise show up here until an
        explicit Refresh click."""
        self._load_snapshots()

    def refresh_camera_filter(self) -> None:
        """Populate camera filter from sidebar camera list."""
        self._known_camera_ids: set[int] = set()
        for cam in self.window.sidebar.cameras:
            if cam.id not in self._known_camera_ids:
                self.camera_combo.append(str(cam.id), _truncate_label(cam.name))
                self._known_camera_ids.add(cam.id)

    def _ensure_camera_in_combo(self, camera_id: int, name: str) -> None:
        """Add a camera to the combo if not already present."""
        if not hasattr(self, "_known_camera_ids"):
            self._known_camera_ids = set()
        if camera_id not in self._known_camera_ids:
            self.camera_combo.append(str(camera_id), _truncate_label(name))
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
        self._page = 0
        self._load_snapshots()

    def _on_camera_filter_clicked(self, btn: Gtk.Button, camera_id: int) -> None:
        self._ensure_camera_in_combo(camera_id, btn.get_label() or "")
        self._camera_id = camera_id
        self._page = 0
        self.camera_combo.handler_block_by_func(self._on_filter_changed)
        self.camera_combo.set_active_id(str(camera_id))
        self.camera_combo.handler_unblock_by_func(self._on_filter_changed)
        self._load_snapshots()

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
                self._page = 0
                self._save_search_to_config()
                self._load_snapshots()
            return

        # Activating this preset — deactivate all others
        self._search_time_preset = key
        from_ts, to_ts = preset_range(key)
        self._search_from_time = from_ts
        self._search_to_time = to_ts
        self._page = 0

        for other_key, other_btn in self._preset_buttons.items():
            if other_key != key:
                other_btn.handler_block_by_func(self._on_preset_toggled)
                other_btn.set_active(False)
                other_btn.handler_unblock_by_func(self._on_preset_toggled)

        self._save_search_to_config()
        self._load_snapshots()

    def _on_reset_clicked(self, btn: Gtk.Button) -> None:
        self._search_camera_ids = None
        self._search_from_time = None
        self._search_to_time = None
        self._search_time_preset = ""
        self._camera_id = None
        self._page = 0
        self.camera_combo.handler_block_by_func(self._on_filter_changed)
        self.camera_combo.set_active_id("all")
        self.camera_combo.handler_unblock_by_func(self._on_filter_changed)
        self._sync_preset_buttons()
        self._save_search_to_config()
        self._load_snapshots()

    def _on_search_clicked(self, btn: Gtk.Button) -> None:
        """Open the advanced search dialog."""
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
            _event_type_ids: list[int] | None,
        ) -> None:
            # Snapshots has no event-type filter (Events-only feature) — the
            # dialog always passes this 4th argument regardless of page.
            self._search_camera_ids = camera_ids
            self._search_from_time = int(from_dt.timestamp()) if from_dt else None
            self._search_to_time = int(to_dt.timestamp()) if to_dt else None
            # Custom range clears preset
            self._search_time_preset = ""
            self._sync_preset_buttons()
            self._page = 0
            self._save_search_to_config()
            self._load_snapshots()

        def _on_reset() -> None:
            self._on_reset_clicked(btn)

        dialog = AdvancedSearchDialog(
            self.window,
            self.window.sidebar.cameras,
            on_search=_on_search,
            on_reset=_on_reset,
            selected_ids=self._search_camera_ids,
            from_time=from_time,
            to_time=to_time,
            title="Search Snapshots",
        )
        dialog.present()

    def _load_snapshots(self) -> None:
        """Query the server for the current page.

        Server-side filtering (camId/from/to/start — see
        services.snapshot.list_snapshots) is used whenever at most one
        camera is selected. A multi-camera Advanced Search selection falls
        back to fetching everything in the time range once and
        filtering/paginating client-side (see _on_multi_camera_loaded),
        since camId only accepts a single value.
        """
        if not self.app.api or self._loading:
            return
        self._loading = True
        self.prev_btn.set_sensitive(False)
        self.next_btn.set_sensitive(False)

        # Recompute time range so presets like "Today" stay current after midnight
        if self._search_time_preset:
            self._search_from_time, self._search_to_time = preset_range(self._search_time_preset)

        self._update_filter_summary()

        camera_ids = self._search_camera_ids
        if camera_ids is None and self._camera_id is not None:
            camera_ids = [self._camera_id]

        if camera_ids is not None and len(camera_ids) > 1:
            self._multi_camera_filter = set(camera_ids)
            run_async(
                list_snapshots(
                    self.app.api,
                    from_time=self._search_from_time,
                    to_time=self._search_to_time,
                    offset=0,
                    limit=_FETCH_ALL_LIMIT,
                ),
                callback=self._on_multi_camera_loaded,
                error_callback=self._on_load_error,
            )
        else:
            self._multi_camera_filter = None
            single_camera_id = camera_ids[0] if camera_ids else None
            run_async(
                list_snapshots(
                    self.app.api,
                    camera_id=single_camera_id,
                    from_time=self._search_from_time,
                    to_time=self._search_to_time,
                    offset=self._page * _PAGE_SIZE,
                    limit=_PAGE_SIZE,
                ),
                callback=self._on_snapshots_loaded,
                error_callback=self._on_load_error,
            )

    def _on_multi_camera_loaded(self, result: tuple[list[Snapshot], int]) -> None:
        """Client-side fallback for a multi-camera selection: cache the
        full (time-range-filtered) fetch and render the current page from
        it — see _render_multi_camera_page(), also used by _on_prev/_on_next
        so paging doesn't re-fetch."""
        self._loading = False
        snapshots, _total = result
        self._snapshots = snapshots
        self._render_multi_camera_page()

    def _render_multi_camera_page(self) -> None:
        camera_filter = self._multi_camera_filter
        if camera_filter is None:
            return
        filtered = [s for s in self._snapshots if s.camera_id in camera_filter]
        total_pages = max(1, (len(filtered) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self._page = max(0, min(self._page, total_pages - 1))
        start = self._page * _PAGE_SIZE
        self._render_page(filtered[start : start + _PAGE_SIZE], len(filtered))

    def _render_page(self, page_snapshots: list[Snapshot], total: int) -> None:
        """Rebuild the visible rows and pagination controls for one page —
        shared by the server-paginated (single camera or none) and
        client-paginated (multi-camera fallback) load paths."""
        self._total = total
        while child := self.row_box.get_first_child():
            self.row_box.remove(child)
        for snap in page_snapshots:
            row_box = self._create_snapshot_row(snap)
            self.row_box.append(row_box)

        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self.prev_btn.set_sensitive(self._page > 0)
        self.next_btn.set_sensitive(self._page < total_pages - 1)
        self.page_label.set_text(f"Page {self._page + 1} of {total_pages} ({total} total)")

    def _update_filter_summary(self) -> None:
        """Always show active filter state above the list."""
        parts: list[str] = []
        parts += self._camera_filter_parts()
        parts += self._time_filter_parts()
        if parts:
            self.filter_summary.set_text("Active filters: " + " | ".join(parts))
        else:
            self.filter_summary.set_text("No filters active — showing all snapshots")

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
            PRESET_TODAY: "Today",
            PRESET_YESTERDAY: "Yesterday",
            PRESET_LAST24H: "Last 24 h",
            PRESET_LAST7D: "Last 7 days",
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
        self.prev_btn.set_sensitive(self._page > 0)
        self.next_btn.set_sensitive(False)
        log.error("Failed to load snapshots: %s", error)

    def _on_snapshots_loaded(self, result: tuple[list[Snapshot], int]) -> None:
        """Server already filtered and paginated this page — just render it."""
        self._loading = False
        snapshots, total = result
        self._snapshots = snapshots
        self._render_page(snapshots, total)

    def _set_thumbnail(self, picture: Gtk.Picture, snap: Snapshot) -> None:
        """Decode the thumbnail already included inline in SnapShot::List's
        response — no per-row fetch needed, unlike Recording thumbnails."""
        if not snap.image_data:
            return
        try:
            raw = base64.b64decode(snap.image_data)
            loader = GdkPixbuf.PixbufLoader()
            loader.write(raw)
            loader.close()
            pixbuf = loader.get_pixbuf()
            if pixbuf:
                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                picture.set_paintable(texture)
        except Exception as exc:
            log.warning("Thumbnail decode failed for snapshot %d: %s", snap.id, exc)

    def _create_snapshot_row(self, snap: Snapshot) -> Gtk.Box:
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
        self._set_thumbnail(picture, snap)
        box.append(picture)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.set_valign(Gtk.Align.CENTER)

        cam_btn = Gtk.Button(label=snap.camera_name or "(no name)")
        cam_btn.add_css_class("camera-label")
        cam_btn.add_css_class("flat")
        cam_btn.set_halign(Gtk.Align.START)
        cam_btn.set_tooltip_text(f"Filter snapshots by {snap.camera_name}")
        cam_btn.connect("clicked", self._on_camera_filter_clicked, snap.camera_id)
        info_box.append(cam_btn)

        time_str = datetime.fromtimestamp(snap.create_time).strftime("%Y-%m-%d %H:%M:%S")
        time_label = Gtk.Label(label=time_str)
        time_label.set_xalign(0)
        time_label.add_css_class("dim-label")
        time_label.add_css_class("caption")
        info_box.append(time_label)

        if snap.file_size:
            size_label = Gtk.Label(label=_format_size(snap.file_size))
            size_label.set_xalign(0)
            size_label.add_css_class("dim-label")
            size_label.add_css_class("caption")
            info_box.append(size_label)

        box.append(info_box)

        # "Play" on Recordings opens video playback; here it opens the still
        # image instead, since a snapshot has no video to play.
        view_btn = Gtk.Button()
        view_btn.set_icon_name("media-playback-start-symbolic")
        view_btn.set_tooltip_text("View")
        view_btn.connect("clicked", self._on_view, snap)
        box.append(view_btn)

        dl_btn = Gtk.Button()
        dl_btn.set_icon_name("document-save-symbolic")
        dl_btn.set_tooltip_text("Download")
        dl_btn.connect("clicked", self._on_download, snap)
        box.append(dl_btn)

        del_btn = Gtk.Button()
        del_btn.set_icon_name("user-trash-symbolic")
        del_btn.set_tooltip_text("Delete")
        del_btn.connect("clicked", self._on_delete, snap)
        box.append(del_btn)

        return box

    def _on_view(self, btn: Gtk.Button, snap: Snapshot) -> None:
        dialog = SnapshotViewerDialog(self.window, self.app, snap)
        dialog.present()

    def _on_download(self, btn: Gtk.Button, snap: Snapshot) -> None:
        """Download snapshot to disk with button feedback and error dialog."""
        dialog = Gtk.FileDialog()
        ts = datetime.fromtimestamp(snap.create_time)
        safe_name = re.sub(r'[/\\<>:"|?*]', "_", snap.camera_name)
        dialog.set_initial_name(f"{safe_name}_{ts:%Y%m%d_%H%M%S}.jpg")

        def _on_save(d: Gtk.FileDialog, result: object) -> None:
            try:
                gfile = d.save_finish(result)
            except Exception:
                return
            if gfile is None:
                return
            path = gfile.get_path()
            if not path or self.app.api is None:
                return

            btn.set_sensitive(False)
            btn.set_icon_name("content-loading-symbolic")
            self._show_status("Downloading…", download_dir=None)

            def _restore() -> None:
                btn.set_sensitive(True)
                btn.set_icon_name("document-save-symbolic")

            def _on_success(p: Path) -> None:
                _restore()
                self._show_status(f"Saved to {p}", download_dir=str(p.parent))
                log.info("Snapshot %d downloaded to %s", snap.id, p)

            def _on_error(exc: Exception) -> None:
                _restore()
                log.error("Download failed for snapshot %d: %s", snap.id, exc)
                err = Gtk.AlertDialog()
                err.set_message("Download failed")
                err.set_detail(f"Could not download from '{snap.camera_name}'.\n\n{exc}")
                err.set_buttons(["OK"])
                err.show(self.window)

            run_async(
                download_snapshot(self.app.api, snap.id, Path(path)),
                callback=_on_success,
                error_callback=_on_error,
            )

        dialog.save(self.window, None, _on_save)

    def _on_delete(self, btn: Gtk.Button, snap: Snapshot) -> None:
        api = self.app.api
        if not api:
            return

        dialog = Gtk.AlertDialog()
        dialog.set_message("Delete snapshot?")
        dialog.set_detail("This cannot be undone.")
        dialog.set_buttons(["Cancel", "Delete"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def _on_response(d: Gtk.AlertDialog, result: object) -> None:
            try:
                idx = d.choose_finish(result)
            except Exception:
                return
            if idx == 1:
                run_async(
                    delete_snapshot(api, snap.id),
                    callback=lambda _: self._load_snapshots(),
                    error_callback=lambda e: log.error("Delete failed: %s", e),
                )

        dialog.choose(self.window, None, _on_response)

    def _show_status(self, message: str, download_dir: str | None) -> None:
        self._status_label.set_text(message)
        self._status_bar.set_visible(True)
        if download_dir:
            self._last_download_dir = download_dir
            self._open_folder_btn.set_visible(True)
        else:
            self._open_folder_btn.set_visible(False)

    def _on_open_folder(self, btn: Gtk.Button) -> None:
        if self._last_download_dir:
            from gi.repository import Gio

            uri = f"file://{self._last_download_dir}"
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except Exception as exc:
                log.warning("Could not open folder %s: %s", self._last_download_dir, exc)

    def _on_prev(self, btn: Gtk.Button) -> None:
        self._page = max(0, self._page - 1)
        if self._multi_camera_filter is not None:
            self._render_multi_camera_page()
        else:
            self._load_snapshots()

    def _on_next(self, btn: Gtk.Button) -> None:
        self._page += 1
        if self._multi_camera_filter is not None:
            self._render_multi_camera_page()
        else:
            self._load_snapshots()

    def on_camera_selected(self, camera: Camera) -> None:
        """Handle camera selection from sidebar."""
        self._ensure_camera_in_combo(camera.id, camera.name)
        self._camera_id = camera.id
        self._page = 0
        self.camera_combo.handler_block_by_func(self._on_filter_changed)
        self.camera_combo.set_active_id(str(camera.id))
        self.camera_combo.handler_unblock_by_func(self._on_filter_changed)
        self._load_snapshots()

    def _load_search_from_config(self) -> None:
        """Load search filters from config."""
        camera_ids, from_time, to_time, time_preset = load_search_filters(
            self.app.config, "snapshots_search"
        )
        if camera_ids:
            self._search_camera_ids = camera_ids
        if from_time:
            self._search_from_time = from_time
        if to_time:
            self._search_to_time = to_time
        self._search_time_preset = time_preset

    def _save_search_to_config(self) -> None:
        """Save search filters to config."""
        save_search_filters(
            self.app.config,
            "snapshots_search",
            self._search_camera_ids,
            self._search_from_time,
            self._search_to_time,
            self._search_time_preset,
        )


class SnapshotViewerDialog(Gtk.Window):
    """Simple full-size picture viewer for a single snapshot."""

    def __init__(self, parent: Gtk.Window, app: SurveillanceApp, snap: Snapshot) -> None:
        super().__init__()
        self.app = app
        ts = datetime.fromtimestamp(snap.create_time)
        self.set_title(f"{snap.camera_name} - {ts:%Y-%m-%d %H:%M:%S}")
        self.set_default_size(800, 600)
        self.set_transient_for(parent)

        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.set_child(self.picture)

        if app.api is not None:
            run_async(
                fetch_snapshot_image(app.api, snap.id),
                callback=self._on_image_loaded,
                error_callback=lambda e: log.error(
                    "Failed to load snapshot %d image: %s", snap.id, e
                ),
            )

    def _on_image_loaded(self, data: bytes) -> None:
        if not data:
            return
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()
            if pixbuf:
                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                self.picture.set_paintable(texture)
        except Exception as exc:
            log.warning("Failed to decode snapshot image: %s", exc)
