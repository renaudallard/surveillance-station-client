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

"""Events and alerts list view."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera, Event, Recording
from surveillance.services.event import list_events
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)

EVENT_TYPES = {
    0: "Continuous",
    1: "Motion Detection",
    2: "Alarm",
    3: "Manual",
    4: "External",
    5: "Action Rule",
    6: "Edge",
    7: "Custom Event",
    8: "Action Rule",
    9: "Continuous",
}


class EventsView(Gtk.Box):
    """Events and alerts list with filters."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.app = window.app
        self._events: list[Event] = []
        self._offset = 0
        self._total = 0
        self._camera_id: int | None = None

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)

        label = Gtk.Label(label="Events")
        label.add_css_class("title-4")
        label.set_hexpand(True)
        label.set_xalign(0)
        toolbar.append(label)

        # Camera filter
        filter_label = Gtk.Label(label="Camera:")
        toolbar.append(filter_label)

        self.camera_combo = Gtk.ComboBoxText()
        self.camera_combo.append("all", "All cameras")
        self.camera_combo.set_active_id("all")
        self.camera_combo.connect("changed", self._on_filter_changed)
        toolbar.append(self.camera_combo)

        refresh_btn = Gtk.Button()
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.connect("clicked", lambda _: self._load_events())
        toolbar.append(refresh_btn)

        self.append(toolbar)
        self.append(Gtk.Separator())

        # Events list
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

        # Populate camera filter and load initial data
        for cam in self.window.sidebar.cameras:
            self.camera_combo.append(str(cam.id), cam.name)
        self._load_events()

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
        self._load_events()

    def _load_events(self) -> None:
        if not self.app.api:
            return
        run_async(
            list_events(self.app.api, self._camera_id, self._offset),
            callback=self._on_events_loaded,
            error_callback=lambda e: log.error("Failed to load events: %s", e),
        )

    def _on_events_loaded(self, result: tuple[list[Event], int]) -> None:
        events, total = result
        self._events = events
        self._total = total

        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)

        for event in events:
            row = self._create_event_row(event)
            self.listbox.append(row)

        self.prev_btn.set_sensitive(self._offset > 0)
        self.next_btn.set_sensitive(self._offset + 50 < total)
        page = (self._offset // 50) + 1
        total_pages = max(1, (total + 49) // 50)
        self.page_label.set_text(f"Page {page} of {total_pages} ({total} total)")

    def _create_event_row(self, event: Event) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.add_css_class("event-row")
        row._event = event  # type: ignore[attr-defined]

        if event.event_type == 1:
            row.add_css_class("motion")
        elif event.event_type == 2:
            row.add_css_class("alarm")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        # Info column
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)

        # Top line: type badge + camera name
        top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        type_name = EVENT_TYPES.get(event.event_type, f"Type {event.event_type}")
        type_label = Gtk.Label(label=type_name)
        type_label.add_css_class("caption")
        if event.event_type == 1:
            type_label.add_css_class("accent")
        elif event.event_type == 2:
            type_label.add_css_class("error")
        top_box.append(type_label)

        cam_label = Gtk.Label(label=event.camera_name)
        cam_label.add_css_class("camera-label")
        top_box.append(cam_label)
        info_box.append(top_box)

        # Time range
        start = datetime.fromtimestamp(event.start_time)
        if event.stop_time:
            stop = datetime.fromtimestamp(event.stop_time)
            time_str = f"{start:%Y-%m-%d %H:%M:%S} - {stop:%H:%M:%S}"
        else:
            time_str = f"{start:%Y-%m-%d %H:%M:%S} (ongoing)"
        time_label = Gtk.Label(label=time_str)
        time_label.set_xalign(0)
        time_label.add_css_class("dim-label")
        time_label.add_css_class("caption")
        info_box.append(time_label)

        # Duration
        if event.stop_time:
            duration = event.stop_time - event.start_time
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
        play_btn.set_tooltip_text("Play recording")
        play_btn.set_valign(Gtk.Align.CENTER)
        play_btn.connect("clicked", self._on_play, event)
        box.append(play_btn)

        row.set_child(box)
        return row

    def _on_row_activated(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        event = row._event  # type: ignore[attr-defined]
        self._play_event(event)

    def _on_play(self, btn: Gtk.Button, event: Event) -> None:
        self._play_event(event)

    def _play_event(self, event: Event) -> None:
        """Open the recording for this event in the player."""
        from surveillance.ui.player import PlayerDialog

        rec = Recording(
            id=event.id,
            camera_id=event.camera_id,
            camera_name=event.camera_name,
            start_time=event.start_time,
            stop_time=event.stop_time,
            event_type=event.event_type,
            mount_id=event.mount_id,
            arch_id=event.arch_id,
        )
        dialog = PlayerDialog(self.window, self.app, rec)
        dialog.present()

    def _on_prev(self, btn: Gtk.Button) -> None:
        self._offset = max(0, self._offset - 50)
        self._load_events()

    def _on_next(self, btn: Gtk.Button) -> None:
        self._offset += 50
        self._load_events()

    def on_camera_selected(self, camera: Camera) -> None:
        """Handle camera selection from sidebar."""
        self.camera_combo.set_active_id(str(camera.id))
