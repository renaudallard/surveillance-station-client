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

"""Time lapse recording browser."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.api.models import TimeLapseRecording, TimeLapseTask
from surveillance.services.timelapse import (
    delete_recordings,
    list_recordings,
    list_tasks,
    lock_recordings,
    to_recording,
    unlock_recordings,
)
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)

_PAGE_SIZE = 50


def _format_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


class TimeLapseView(Gtk.Box):
    """Time lapse recording browser with task filter."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.app = window.app
        self._recordings: list[TimeLapseRecording] = []
        self._tasks: list[TimeLapseTask] = []
        self._total = 0
        self._offset = 0
        self._task_id: int = -1  # -1 = all tasks
        self._loading = False

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)

        label = Gtk.Label(label="Time Lapse")
        label.add_css_class("title-4")
        label.set_hexpand(True)
        label.set_xalign(0)
        toolbar.append(label)

        # Task filter
        filter_label = Gtk.Label(label="Task:")
        toolbar.append(filter_label)

        self.task_combo = Gtk.ComboBoxText()
        self.task_combo.append("all", "All Tasks")
        self.task_combo.set_active_id("all")
        self.task_combo.connect("changed", self._on_filter_changed)
        toolbar.append(self.task_combo)

        refresh_btn = Gtk.Button()
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh")
        refresh_btn.connect("clicked", lambda _: self._refresh())
        toolbar.append(refresh_btn)

        self.append(toolbar)
        self.append(Gtk.Separator())

        # Recording list — plain Gtk.Box to avoid ListBox click event issues
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroll.set_child(self.row_box)
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

        # Load tasks and recordings
        self._load_tasks()
        self._load_recordings()

    def _refresh(self) -> None:
        """Reload tasks and recordings."""
        self._load_tasks()
        self._load_recordings()

    def _load_tasks(self) -> None:
        """Load time lapse tasks for the filter combo."""
        if not self.app.api:
            return
        run_async(
            list_tasks(self.app.api),
            callback=self._on_tasks_loaded,
            error_callback=lambda e: log.error("Failed to load time lapse tasks: %s", e),
        )

    def _on_tasks_loaded(self, tasks: list[TimeLapseTask]) -> None:
        self._tasks = tasks
        # Rebuild combo, keeping current selection
        current = self.task_combo.get_active_id()
        self.task_combo.handler_block_by_func(self._on_filter_changed)
        self.task_combo.remove_all()
        self.task_combo.append("all", "All Tasks")
        for task in tasks:
            self.task_combo.append(str(task.id), task.name)
        if current and self.task_combo.get_active_id() != current:
            self.task_combo.set_active_id(current)
        if self.task_combo.get_active_id() is None:
            self.task_combo.set_active_id("all")
        self.task_combo.handler_unblock_by_func(self._on_filter_changed)

    def _on_filter_changed(self, combo: Gtk.ComboBoxText) -> None:
        active = combo.get_active_id()
        if active == "all":
            self._task_id = -1
        else:
            try:
                self._task_id = int(active) if active else -1
            except ValueError:
                self._task_id = -1
        self._offset = 0
        self._load_recordings()

    def _load_recordings(self) -> None:
        if not self.app.api or self._loading:
            return
        self._loading = True
        self.prev_btn.set_sensitive(False)
        self.next_btn.set_sensitive(False)
        log.debug(
            "_load_timelapse_recordings: task=%s offset=%d",
            self._task_id,
            self._offset,
        )
        run_async(
            list_recordings(
                self.app.api,
                task_id=self._task_id,
                offset=self._offset,
                limit=_PAGE_SIZE,
            ),
            callback=self._on_recordings_loaded,
            error_callback=self._on_load_error,
        )

    def _on_load_error(self, error: Exception) -> None:
        self._loading = False
        self.prev_btn.set_sensitive(self._offset > 0)
        self.next_btn.set_sensitive(self._offset + _PAGE_SIZE < self._total)
        log.error("Failed to load time lapse recordings: %s", error)

    def _on_recordings_loaded(self, result: tuple[list[TimeLapseRecording], int]) -> None:
        self._loading = False
        recordings, total = result
        self._recordings = recordings
        self._total = total
        log.debug("Loaded %d time lapse recordings (total=%d)", len(recordings), total)

        # Clear list
        while child := self.row_box.get_first_child():
            self.row_box.remove(child)

        for rec in recordings:
            row = self._create_recording_row(rec)
            self.row_box.append(row)

        # Update pagination
        self.prev_btn.set_sensitive(self._offset > 0)
        self.next_btn.set_sensitive(self._offset + _PAGE_SIZE < total)
        page = (self._offset // _PAGE_SIZE) + 1
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self.page_label.set_text(f"Page {page} of {total_pages} ({total} total)")

    def _create_recording_row(self, rec: TimeLapseRecording) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        # Info column
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.set_valign(Gtk.Align.CENTER)

        cam_label = Gtk.Label(label=rec.camera_name or "(no name)")
        cam_label.add_css_class("camera-label")
        cam_label.set_halign(Gtk.Align.START)
        info_box.append(cam_label)

        # Time range
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

        # Duration
        if rec.stop_time:
            duration = rec.stop_time - rec.start_time
            mins, secs = divmod(duration, 60)
            hours, mins = divmod(mins, 60)
            if hours:
                dur_str = f"{hours}h {mins}m {secs}s"
            else:
                dur_str = f"{mins}m {secs}s"
            dur_label = Gtk.Label(label=dur_str)
            dur_label.set_xalign(0)
            dur_label.add_css_class("dim-label")
            dur_label.add_css_class("caption")
            info_box.append(dur_label)

        # Status tags
        status_parts: list[str] = []
        if rec.recording:
            status_parts.append("Recording")
        if rec.is_locked:
            status_parts.append("Locked")
        if status_parts:
            status_label = Gtk.Label(label=" | ".join(status_parts))
            status_label.set_xalign(0)
            status_label.add_css_class("caption")
            status_label.add_css_class("accent")
            info_box.append(status_label)

        box.append(info_box)

        # File size
        if rec.file_size:
            size_label = Gtk.Label(label=_format_size(rec.file_size))
            size_label.add_css_class("dim-label")
            size_label.add_css_class("caption")
            size_label.set_valign(Gtk.Align.CENTER)
            box.append(size_label)

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

        # Lock/unlock toggle
        lock_btn = Gtk.Button()
        if rec.is_locked:
            lock_btn.set_icon_name("changes-prevent-symbolic")
            lock_btn.set_tooltip_text("Unlock")
        else:
            lock_btn.set_icon_name("changes-allow-symbolic")
            lock_btn.set_tooltip_text("Lock")
        lock_btn.connect("clicked", self._on_lock_toggle, rec)
        box.append(lock_btn)

        # Delete button
        del_btn = Gtk.Button()
        del_btn.set_icon_name("user-trash-symbolic")
        del_btn.set_tooltip_text("Delete")
        del_btn.connect("clicked", self._on_delete, rec)
        box.append(del_btn)

        return box

    def _on_play(self, btn: Gtk.Button, rec: TimeLapseRecording) -> None:
        log.debug("PLAY TIMELAPSE: rec_id=%s", rec.id)
        from surveillance.ui.player import PlayerDialog

        recording = to_recording(rec)
        dialog = PlayerDialog(self.window, self.app, recording)
        dialog.present()

    def _on_download(self, btn: Gtk.Button, rec: TimeLapseRecording) -> None:
        dialog = Gtk.FileDialog()
        start = datetime.fromtimestamp(rec.start_time)
        dialog.set_initial_name(f"timelapse_{rec.camera_name}_{start:%Y%m%d_%H%M%S}.mp4")

        def _on_save(d: Gtk.FileDialog, result: object) -> None:
            try:
                gfile = d.save_finish(result)
                if gfile:
                    path = gfile.get_path()
                    if path:
                        from pathlib import Path

                        from surveillance.services.timelapse import download_recording

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

    def _on_lock_toggle(self, btn: Gtk.Button, rec: TimeLapseRecording) -> None:
        if not self.app.api:
            return
        if rec.is_locked:
            run_async(
                unlock_recordings(self.app.api, [rec.id]),
                callback=lambda _: self._load_recordings(),
                error_callback=lambda e: log.error("Unlock failed: %s", e),
            )
        else:
            run_async(
                lock_recordings(self.app.api, [rec.id]),
                callback=lambda _: self._load_recordings(),
                error_callback=lambda e: log.error("Lock failed: %s", e),
            )

    def _on_delete(self, btn: Gtk.Button, rec: TimeLapseRecording) -> None:
        if not self.app.api:
            return
        run_async(
            delete_recordings(self.app.api, [rec.id]),
            callback=lambda _: self._load_recordings(),
            error_callback=lambda e: log.error("Delete failed: %s", e),
        )

    def _on_prev(self, btn: Gtk.Button) -> None:
        self._offset = max(0, self._offset - _PAGE_SIZE)
        self._load_recordings()

    def _on_next(self, btn: Gtk.Button) -> None:
        self._offset += _PAGE_SIZE
        self._load_recordings()
