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

"""Recording search dialog with camera and time range filters."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from surveillance.api.models import Camera

log = logging.getLogger(__name__)


class RecordingSearchDialog(Gtk.Window):
    """Dialog for configuring recording search filters."""

    def __init__(
        self,
        parent: Gtk.Window,
        cameras: list[Camera],
        on_search: Callable[[list[int] | None, datetime | None, datetime | None], None],
        on_reset: Callable[[], None],
        selected_ids: list[int] | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> None:
        super().__init__(
            title="Search Recordings",
            transient_for=parent,
            modal=True,
        )
        self._cameras = cameras
        self._camera_checks: dict[int, Gtk.CheckButton] = {}
        self._time_preset_used = from_time is not None or to_time is not None
        self._on_search = on_search
        self._on_reset = on_reset

        self.set_default_size(450, 400)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_vexpand(True)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        time_frame = Gtk.Frame(label="Time Range")
        time_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        time_box.set_margin_top(8)
        time_box.set_margin_bottom(8)
        time_box.set_margin_start(8)
        time_box.set_margin_end(8)

        preset_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        preset_box.set_halign(Gtk.Align.START)

        self.today_btn = Gtk.Button(label="Today")
        self.today_btn.connect("clicked", self._on_preset_today)
        preset_box.append(self.today_btn)

        self.week_btn = Gtk.Button(label="Last 7 days")
        self.week_btn.connect("clicked", self._on_preset_week)
        preset_box.append(self.week_btn)

        self.month_btn = Gtk.Button(label="Last 30 days")
        self.month_btn.connect("clicked", self._on_preset_month)
        preset_box.append(self.month_btn)

        time_box.append(preset_box)

        range_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        from_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        from_label = Gtk.Label(label="From:")
        from_label.set_xalign(0)
        from_box.append(from_label)

        self.from_date = Gtk.Calendar()
        from_box.append(self.from_date)

        self.from_time_entry = Gtk.Entry()
        self.from_time_entry.set_placeholder_text("00:00:00")
        self.from_time_entry.set_max_length(8)
        from_box.append(self.from_time_entry)

        range_box.append(from_box)

        to_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        to_label = Gtk.Label(label="To:")
        to_label.set_xalign(0)
        to_box.append(to_label)

        self.to_date = Gtk.Calendar()
        to_box.append(self.to_date)

        self.to_time_entry = Gtk.Entry()
        self.to_time_entry.set_placeholder_text("23:59:59")
        self.to_time_entry.set_max_length(8)
        to_box.append(self.to_time_entry)

        range_box.append(to_box)

        time_box.append(range_box)
        time_frame.set_child(time_box)
        content.append(time_frame)

        cam_frame = Gtk.Frame(label="Cameras")
        cam_scroll = Gtk.ScrolledWindow()
        cam_scroll.set_min_content_height(150)
        cam_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.cam_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.cam_box.set_margin_top(8)
        self.cam_box.set_margin_bottom(8)
        self.cam_box.set_margin_start(8)
        self.cam_box.set_margin_end(8)

        self.all_cam_btn = Gtk.CheckButton(label="All Cameras")
        self.all_cam_btn.set_active(True)
        self.all_cam_btn.connect("toggled", self._on_all_cameras_toggled)
        self.cam_box.append(self.all_cam_btn)

        for cam in cameras:
            check = Gtk.CheckButton(label=cam.name)
            check.set_active(cam.id in (selected_ids or []))
            check.connect("toggled", self._on_camera_toggled)
            self._camera_checks[cam.id] = check
            self.cam_box.append(check)

        cam_scroll.set_child(self.cam_box)
        cam_frame.set_child(cam_scroll)
        content.append(cam_frame)

        outer.append(content)

        # Button bar
        btn_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_bar.set_margin_top(8)
        btn_bar.set_margin_bottom(12)
        btn_bar.set_margin_start(12)
        btn_bar.set_margin_end(12)

        reset_btn = Gtk.Button(label="Reset")
        reset_btn.connect("clicked", self._on_reset_clicked)
        btn_bar.append(reset_btn)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        btn_bar.append(spacer)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        btn_bar.append(cancel_btn)

        search_btn = Gtk.Button(label="Search")
        search_btn.add_css_class("suggested-action")
        search_btn.connect("clicked", self._on_search_clicked)
        btn_bar.append(search_btn)

        outer.append(Gtk.Separator())
        outer.append(btn_bar)

        if from_time:
            self._set_datetime(self.from_date, self.from_time_entry, from_time)
        if to_time:
            self._set_datetime(self.to_date, self.to_time_entry, to_time)

        self._update_all_cameras_state()
        self._on_all_cameras_toggled(self.all_cam_btn)

    def _on_all_cameras_toggled(self, btn: Gtk.CheckButton) -> None:
        active = btn.get_active()
        for check in self._camera_checks.values():
            check.set_sensitive(not active)

    def _on_camera_toggled(self, btn: Gtk.CheckButton) -> None:
        self._update_all_cameras_state()

    def _update_all_cameras_state(self) -> None:
        any_selected = any(c.get_active() for c in self._camera_checks.values())
        self.all_cam_btn.set_active(not any_selected)

    def _on_preset_today(self, btn: Gtk.Button) -> None:
        self._time_preset_used = True
        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self._set_datetime(self.from_date, self.from_time_entry, start)
        self._set_datetime(self.to_date, self.to_time_entry, now)

    def _on_preset_week(self, btn: Gtk.Button) -> None:
        self._time_preset_used = True
        now = datetime.now()
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        self._set_datetime(self.from_date, self.from_time_entry, start)
        self._set_datetime(self.to_date, self.to_time_entry, now)

    def _on_preset_month(self, btn: Gtk.Button) -> None:
        self._time_preset_used = True
        now = datetime.now()
        start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        self._set_datetime(self.from_date, self.from_time_entry, start)
        self._set_datetime(self.to_date, self.to_time_entry, now)

    def _set_datetime(self, calendar: Gtk.Calendar, time_entry: Gtk.Entry, dt: datetime) -> None:
        gdt = GLib.DateTime.new_local(
            dt.year, dt.month, dt.day, dt.hour, dt.minute, float(dt.second)
        )
        calendar.select_day(gdt)
        time_entry.set_text(dt.strftime("%H:%M:%S"))

    def _get_datetime(
        self, calendar: Gtk.Calendar, time_entry: Gtk.Entry, default_time: str = "00:00:00"
    ) -> datetime:
        gdt = calendar.get_date()
        year = gdt.get_year()
        month = gdt.get_month()
        day = gdt.get_day_of_month()
        time_str = time_entry.get_text().strip() or default_time
        try:
            hour, minute, second = map(int, time_str.split(":"))
        except ValueError:
            hour, minute, second = 0, 0, 0
        return datetime(year, month, day, hour, minute, second)

    def _get_selected_camera_ids(self) -> list[int] | None:
        """Return selected camera IDs, or None for all cameras."""
        if self.all_cam_btn.get_active():
            return None
        return [cam_id for cam_id, check in self._camera_checks.items() if check.get_active()]

    def _get_from_time(self) -> datetime | None:
        """Return the start of the time range, or None if not set."""
        if not self.from_time_entry.get_text().strip() and not self._time_preset_used:
            return None
        return self._get_datetime(self.from_date, self.from_time_entry, "00:00:00")

    def _get_to_time(self) -> datetime | None:
        """Return the end of the time range, or None if not set."""
        if not self.to_time_entry.get_text().strip() and not self._time_preset_used:
            return None
        return self._get_datetime(self.to_date, self.to_time_entry, "23:59:59")

    def _on_search_clicked(self, btn: Gtk.Button) -> None:
        self._on_search(
            self._get_selected_camera_ids(),
            self._get_from_time(),
            self._get_to_time(),
        )
        self.close()

    def _on_reset_clicked(self, btn: Gtk.Button) -> None:
        self._on_reset()
        self.close()
