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

"""Advanced search dialog with camera and time range filters, shared by
Recordings, Snapshots, and Events."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # type: ignore[import-untyped]

from surveillance.services.recording import (
    PRESET_LAST7D,
    PRESET_LAST24H,
    PRESET_LAST30D,
    PRESET_TODAY,
    PRESET_YESTERDAY,
    preset_range,
)

if TYPE_CHECKING:
    from surveillance.api.models import Camera

log = logging.getLogger(__name__)


class AdvancedSearchDialog(Gtk.Window):
    """Dialog for configuring advanced search filters — camera(s), time
    range, and (Events only) event types."""

    def __init__(
        self,
        parent: Gtk.Window,
        cameras: list[Camera],
        on_search: Callable[
            [
                list[int] | None,
                datetime | None,
                datetime | None,
                list[str] | None,
                bool,
                str | None,
            ],
            None,
        ],
        on_reset: Callable[[], None],
        selected_ids: list[int] | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        selected_preset: str | None = None,
        title: str = "Advanced Search",
        event_types: list[tuple[str, str]] | None = None,
        selected_event_type_ids: list[str] | None = None,
        selected_event_types_match_all: bool = False,
        show_extended_presets: bool = True,
    ) -> None:
        super().__init__(
            title=title,
            transient_for=parent,
            modal=True,
        )
        self._cameras = cameras
        self._camera_checks: dict[int, Gtk.CheckButton] = {}
        self._event_type_checks: dict[str, Gtk.CheckButton] = {}
        self._preset_buttons: dict[str, Gtk.ToggleButton] = {}
        self._time_range_set = from_time is not None or to_time is not None
        self._selected_preset = selected_preset
        self._syncing_fields = False
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

        preset_defs = [
            (PRESET_TODAY, "Today"),
            (PRESET_YESTERDAY, "Yesterday"),
            (PRESET_LAST24H, "Last 24 hrs"),
        ]
        # Hidden for Events (see show_extended_presets) to match its
        # quick-filter toolbar, which only offers Today/Yesterday/Last 24 hrs.
        if show_extended_presets:
            preset_defs += [
                (PRESET_LAST7D, "Last 7 days"),
                (PRESET_LAST30D, "Last 30 days"),
            ]
        for key, label in preset_defs:
            btn = Gtk.ToggleButton(label=label)
            btn.set_active(key == self._selected_preset)
            btn.connect("toggled", self._on_preset_toggled, key)
            preset_box.append(btn)
            self._preset_buttons[key] = btn

        time_box.append(preset_box)

        range_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        from_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        from_label = Gtk.Label(label="From:")
        from_label.set_xalign(0)
        from_box.append(from_label)

        self.from_date = Gtk.Calendar()
        for prop in ("notify::day", "notify::month", "notify::year"):
            self.from_date.connect(prop, self._on_calendar_date_changed)
        from_box.append(self.from_date)

        self.from_time_entry = Gtk.Entry()
        self.from_time_entry.set_placeholder_text("00:00:00")
        self.from_time_entry.set_max_length(8)
        self.from_time_entry.connect("changed", self._on_time_field_edited)
        from_box.append(self.from_time_entry)

        range_box.append(from_box)

        to_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        to_label = Gtk.Label(label="To:")
        to_label.set_xalign(0)
        to_box.append(to_label)

        self.to_date = Gtk.Calendar()
        for prop in ("notify::day", "notify::month", "notify::year"):
            self.to_date.connect(prop, self._on_calendar_date_changed)
        to_box.append(self.to_date)

        self.to_time_entry = Gtk.Entry()
        self.to_time_entry.set_placeholder_text("23:59:59")
        self.to_time_entry.set_max_length(8)
        self.to_time_entry.connect("changed", self._on_time_field_edited)
        to_box.append(self.to_time_entry)

        range_box.append(to_box)

        time_box.append(range_box)
        time_frame.set_child(time_box)
        content.append(time_frame)

        cam_frame = Gtk.Frame(label="Cameras")
        cam_scroll = Gtk.ScrolledWindow()
        cam_scroll.set_min_content_height(150)
        cam_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        cam_scroll.set_hexpand(True)

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

        # Event types is opt-in (Events page only) — Recordings/Snapshots
        # don't pass event_types, so this row stays just the Cameras frame
        # at its original full width.
        filters_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        filters_row.append(cam_frame)

        if event_types is not None:
            # Not a Gtk.Frame: its label-widget slot sizes to the label's
            # own natural width and never stretches to the frame's width,
            # so a trailing Any/All combo can't be pushed flush right that
            # way — a spacer has nothing to expand into. Plain Box (normal
            # box layout, hexpand works as expected) styled via CSS
            # (.search-group-frame) to match Cameras' native Frame border.
            type_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            type_outer.add_css_class("search-group-frame")
            type_outer.set_hexpand(True)

            type_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            type_header.set_margin_top(6)
            type_header.set_margin_start(8)
            type_header.set_margin_end(8)
            type_header.append(Gtk.Label(label="Event Types"))
            type_header_spacer = Gtk.Box()
            type_header_spacer.set_hexpand(True)
            type_header.append(type_header_spacer)

            # Whether a multi-type search matches events containing ANY of
            # the checked bits (the historical/default behavior) or ALL of
            # them — e.g. "Person Detect" AND "Vehicle Detect" only, not
            # either alone. Purely a client-side combination of the same
            # per-bit tests already used elsewhere; DSM has no concept of
            # these decoded categories to filter on server-side.
            self.event_types_mode_combo = Gtk.ComboBoxText()
            self.event_types_mode_combo.append("or", "Any")
            self.event_types_mode_combo.append("and", "All")
            self.event_types_mode_combo.set_active_id(
                "and" if selected_event_types_match_all else "or"
            )
            self.event_types_mode_combo.set_tooltip_text(
                "Any: events matching at least one checked type.\n"
                "All: events matching every checked type at once."
            )
            type_header.append(self.event_types_mode_combo)
            type_outer.append(type_header)

            type_scroll = Gtk.ScrolledWindow()
            type_scroll.set_min_content_height(150)
            type_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            type_scroll.set_hexpand(True)

            self.type_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            self.type_box.set_margin_top(8)
            self.type_box.set_margin_bottom(8)
            self.type_box.set_margin_start(8)
            self.type_box.set_margin_end(8)

            self.all_types_btn = Gtk.CheckButton(label="All Event Types")
            self.all_types_btn.set_active(True)
            self.all_types_btn.connect("toggled", self._on_all_event_types_toggled)
            self.type_box.append(self.all_types_btn)

            for type_code, label in event_types:
                check = Gtk.CheckButton(label=label)
                check.set_active(type_code in (selected_event_type_ids or []))
                check.connect("toggled", self._on_event_type_toggled)
                self._event_type_checks[type_code] = check
                self.type_box.append(check)

            type_scroll.set_child(self.type_box)
            type_outer.append(type_scroll)
            filters_row.append(type_outer)

        content.append(filters_row)

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

        if event_types is not None:
            self._update_all_event_types_state()
            self._on_all_event_types_toggled(self.all_types_btn)

    def _on_all_cameras_toggled(self, btn: Gtk.CheckButton) -> None:
        active = btn.get_active()
        for check in self._camera_checks.values():
            check.set_sensitive(not active)

    def _on_camera_toggled(self, btn: Gtk.CheckButton) -> None:
        self._update_all_cameras_state()

    def _update_all_cameras_state(self) -> None:
        any_selected = any(c.get_active() for c in self._camera_checks.values())
        self.all_cam_btn.set_active(not any_selected)

    def _on_all_event_types_toggled(self, btn: Gtk.CheckButton) -> None:
        active = btn.get_active()
        for check in self._event_type_checks.values():
            check.set_sensitive(not active)
        if active:
            # "All Event Types" leaves no specific selection for Any/All to
            # combine — force it back to the harmless default and grey it
            # out, same rule _update_all_event_types_state applies below 2
            # selections. Without this it silently kept whatever value
            # (and enabled state) was left over from an earlier selection.
            self.event_types_mode_combo.set_active_id("or")
            self.event_types_mode_combo.set_sensitive(False)

    def _on_event_type_toggled(self, btn: Gtk.CheckButton) -> None:
        self._update_all_event_types_state()

    def _update_all_event_types_state(self) -> None:
        selected_count = sum(1 for c in self._event_type_checks.values() if c.get_active())
        self.all_types_btn.set_active(selected_count == 0)
        # Any/All is meaningless below 2 selections — grey it out rather
        # than leave a control that does nothing.
        self.event_types_mode_combo.set_sensitive(selected_count >= 2)

    def _sync_preset_buttons(self) -> None:
        """Update toggle state of preset buttons to match the active preset."""
        for key, btn in self._preset_buttons.items():
            btn.handler_block_by_func(self._on_preset_toggled)
            btn.set_active(key == self._selected_preset)
            btn.handler_unblock_by_func(self._on_preset_toggled)

    def _on_preset_toggled(self, btn: Gtk.ToggleButton, key: str) -> None:
        if not btn.get_active():
            # Deactivating — only clear if this was the active preset
            if self._selected_preset == key:
                self._selected_preset = None
            return

        self._time_range_set = True
        self._selected_preset = key
        from_ts, to_ts = preset_range(key)
        self._set_datetime(self.from_date, self.from_time_entry, datetime.fromtimestamp(from_ts))
        self._set_datetime(self.to_date, self.to_time_entry, datetime.fromtimestamp(to_ts))
        self._sync_preset_buttons()

    def _clear_preset_selection(self) -> None:
        """Drop the active preset because the user edited a date/time field
        directly — a real custom range takes precedence over a stale preset
        label, same as picking a date does on the page toolbars."""
        if self._selected_preset is None:
            return
        self._selected_preset = None
        self._sync_preset_buttons()

    def _on_time_field_edited(self, entry: Gtk.Entry) -> None:
        if self._syncing_fields:
            return
        self._time_range_set = True
        self._clear_preset_selection()

    def _set_datetime(self, calendar: Gtk.Calendar, time_entry: Gtk.Entry, dt: datetime) -> None:
        gdt = GLib.DateTime.new_local(
            dt.year, dt.month, dt.day, dt.hour, dt.minute, float(dt.second)
        )
        # Guarded so a preset's own programmatic update doesn't immediately
        # clear the preset it just set. A flag rather than handler blocking:
        # each calendar reports its date through three notify connections to
        # one handler, and handler_block_by_func blocks a single handler per
        # call, so it cannot cover them.
        self._syncing_fields = True
        try:
            calendar.select_day(gdt)
            time_entry.set_text(dt.strftime("%H:%M:%S"))
        finally:
            self._syncing_fields = False

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
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            # Not three numbers, or a field out of range ("24:00:00"):
            # midnight, the same as an unparseable entry has always meant.
            return datetime(year, month, day)

    def _get_selected_camera_ids(self) -> list[int] | None:
        """Return selected camera IDs, or None for all cameras.

        Ticking nothing means the same as "All Cameras": callers read an
        empty list as "a filter is set", which on Events matches its
        "sidebar has not loaded yet" guard and stops the page reloading.
        """
        if self.all_cam_btn.get_active():
            return None
        return [
            cam_id for cam_id, check in self._camera_checks.items() if check.get_active()
        ] or None

    def _get_selected_event_type_ids(self) -> list[str] | None:
        """Return selected event type IDs, or None if this dialog wasn't
        given an event_types list (Recordings/Snapshots) or "All Event
        Types" is selected."""
        if not self._event_type_checks or self.all_types_btn.get_active():
            return None
        return [
            type_code for type_code, check in self._event_type_checks.items() if check.get_active()
        ]

    def _get_event_types_match_all(self) -> bool:
        """True for "All" (AND), False for "Any" (OR, the default) — False
        when this dialog wasn't given an event_types list."""
        if not self._event_type_checks:
            return False
        return bool(self.event_types_mode_combo.get_active_id() == "and")

    def _on_calendar_date_changed(self, calendar: Gtk.Calendar, _pspec: object) -> None:
        """Changing a calendar's date counts as setting the time range.

        Otherwise the range only counted as set when a preset was used or a
        time of day was typed, so a search where the user had only picked
        dates dropped both bounds and quietly searched everything.

        Watching day, month and year rather than "day-selected": GtkCalendar
        suppresses that signal for every month and year navigation (the
        header arrows, scrolling the widget, Ctrl with an arrow key) and for
        a click landing on the day number already selected. Each of those
        still moves the date this dialog hands back, and a preset left
        selected across one of them makes the page recompute the range from
        the preset and discard the dates the user picked.
        """
        if self._syncing_fields:
            return
        self._time_range_set = True
        self._clear_preset_selection()

    def _get_from_time(self) -> datetime | None:
        """Return the start of the time range, or None if not set."""
        if not self.from_time_entry.get_text().strip() and not self._time_range_set:
            return None
        return self._get_datetime(self.from_date, self.from_time_entry, "00:00:00")

    def _get_to_time(self) -> datetime | None:
        """Return the end of the time range, or None if not set."""
        if not self.to_time_entry.get_text().strip() and not self._time_range_set:
            return None
        return self._get_datetime(self.to_date, self.to_time_entry, "23:59:59")

    def _on_search_clicked(self, btn: Gtk.Button) -> None:
        self._on_search(
            self._get_selected_camera_ids(),
            self._get_from_time(),
            self._get_to_time(),
            self._get_selected_event_type_ids(),
            self._get_event_types_match_all(),
            self._selected_preset,
        )
        self.close()

    def _on_reset_clicked(self, btn: Gtk.Button) -> None:
        self._on_reset()
        self.close()
