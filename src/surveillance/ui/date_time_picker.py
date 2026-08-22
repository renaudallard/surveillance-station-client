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

"""Single date/time picker: a calendar plus a time-of-day entry,
combined into one datetime value.

Deliberately its own small widget rather than folded into whatever
embeds it (see Timeline's calendar-jump popover) -- callers that care
about recording availability (see set_month_availability) can mark it
and get day-level selection refusal plus exact-time validity tracking
for free; callers that don't (there are none yet, but the split keeps
it possible) just never call it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # type: ignore[import-untyped]


class DateTimePicker(Gtk.Box):
    """Gtk.Calendar plus a "HH:MM:SS" Gtk.Entry, read/written together
    as a single datetime.

    Day-level availability is opt-in: set_month_availability(year,
    month, days, intervals) marks which days (1-31) of that month have
    something -- GtkCalendar's own marks only ever apply to the
    currently displayed month, so a stale answer for a month since
    navigated away from is dropped rather than misapplied. Once a
    month's availability is known, clicking a day outside it is
    refused (silently reverted to the last valid selection).

    Time-of-day is never restricted the same way -- a click can't be
    refused for something that hasn't been typed yet -- but the exact
    (day, time) combination's own validity against *intervals* is
    tracked and reported through set_validity_changed_callback, so an
    embedder can grey out its own confirm action rather than ever
    firing a jump to a moment with nothing recorded.

    Both GtkCalendar's own mark_day (a subtle style change some themes
    render more visibly than others -- confirmed close to invisible in
    at least one real GTK4 theme) and a plain-text status label are
    used together for the "indicate" half of that: the label is the
    one guaranteed to actually show something regardless of theme.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        self.calendar = Gtk.Calendar()
        self.append(self.calendar)

        self.status_label = Gtk.Label(xalign=0)
        self.status_label.add_css_class("dim-label")
        self.append(self.status_label)

        self.time_entry = Gtk.Entry()
        self.time_entry.set_placeholder_text("00:00:00")
        self.time_entry.set_max_length(8)
        self.time_entry.connect("changed", self._on_time_changed)
        self.append(self.time_entry)

        # (year, month) of the month set_month_availability last
        # answered for -- None means "unknown", which lets any day
        # through rather than refusing everything before the first
        # answer arrives, and counts the exact-time check below as
        # invalid (nothing to validate against yet). Both reset to
        # None on every month change until a fresh answer lands.
        self._available_days: set[int] | None = None
        self._available_intervals: list[tuple[int, int]] | None = None
        gdt = self.calendar.get_date()
        self._last_valid_date = gdt
        self._last_valid_month = (gdt.get_year(), gdt.get_month())
        # Set while this widget is applying its own change (set_datetime,
        # or reverting an invalid day) -- so that doesn't get validated
        # against itself the same way a user click would be.
        self._programmatic_change = False
        self._month_changed_callback: Callable[[int, int], None] | None = None
        self._validity_changed_callback: Callable[[bool], None] | None = None

        for prop in ("notify::day", "notify::month", "notify::year"):
            self.calendar.connect(prop, self._on_date_notify)

    def get_datetime(self) -> datetime:
        gdt = self.calendar.get_date()
        time_str = self.time_entry.get_text().strip() or "00:00:00"
        try:
            hour, minute, second = map(int, time_str.split(":"))
        except ValueError:
            hour, minute, second = 0, 0, 0
        return datetime(
            gdt.get_year(), gdt.get_month(), gdt.get_day_of_month(), hour, minute, second
        )

    def set_datetime(self, dt: datetime) -> None:
        """Set the picker to *dt* and ask (via set_month_changed_callback)
        for that month's own availability, the same as navigating there
        by hand would."""
        gdt = GLib.DateTime.new_local(
            dt.year, dt.month, dt.day, dt.hour, dt.minute, float(dt.second)
        )
        self._programmatic_change = True
        try:
            self.calendar.select_day(gdt)
        finally:
            self._programmatic_change = False
        self.time_entry.set_text(dt.strftime("%H:%M:%S"))
        self._last_valid_date = gdt
        self._last_valid_month = (dt.year, dt.month)
        self._available_days = None
        self._available_intervals = None
        self._notify_month_changed(dt.year, dt.month)
        self._refresh_status_and_validity()

    def set_month_changed_callback(self, callback: Callable[[int, int], None]) -> None:
        """*callback* receives (year, month) [1-12] whenever the
        displayed month changes, including from set_datetime -- the
        embedder is expected to answer with set_month_availability once
        it knows that month's own data."""
        self._month_changed_callback = callback

    def set_validity_changed_callback(self, callback: Callable[[bool], None]) -> None:
        """*callback* receives whether the exact currently-selected
        (day, time) has a known recording -- fires whenever that could
        have changed: a new month's availability arriving, the day
        changing, or the time field being edited. False (never valid)
        until the first set_month_availability answer arrives, since
        there's nothing yet to confirm a recording against -- an
        embedder gating a confirm action on this should start it
        disabled, not assume the best.
        """
        self._validity_changed_callback = callback
        callback(self.is_current_selection_valid())

    def is_current_selection_valid(self) -> bool:
        """Whether the exact (day, time) currently selected falls
        within a known recording -- see set_validity_changed_callback."""
        if self._available_intervals is None:
            return False
        timestamp = self._current_timestamp()
        if timestamp is None:
            return False
        return any(start <= timestamp <= stop for start, stop in self._available_intervals)

    def _current_timestamp(self) -> float | None:
        try:
            return self.get_datetime().timestamp()
        except ValueError:
            return None  # an out-of-range time typed into time_entry, e.g. "99:99:99"

    def set_month_availability(
        self, year: int, month: int, days: set[int], intervals: list[tuple[int, int]]
    ) -> None:
        """Both which days (1-31) of (year, month) have any recording
        (day-level marking/refusal) and the merged recording spans for
        exact-time validity (see is_current_selection_valid) -- always
        supplied together, since both come from the same month-wide
        presence query. Ignored if the calendar has since navigated to
        a different month, since neither ever makes sense for any
        month but whichever is currently displayed.
        """
        gdt = self.calendar.get_date()
        if (gdt.get_year(), gdt.get_month()) != (year, month):
            return
        self.calendar.clear_marks()
        for day in days:
            self.calendar.mark_day(day)
        self._available_days = days
        self._available_intervals = intervals
        self._refresh_status_and_validity()

    def _refresh_status_and_validity(self) -> None:
        if self._available_days is None:
            self.status_label.set_label("Checking recordings for this month…")
        elif not self._available_days:
            self.status_label.set_label("No recordings this month")
        elif not self.is_current_selection_valid():
            self.status_label.set_label("No recording at that exact time — pick another")
        else:
            count = len(self._available_days)
            self.status_label.set_label(
                f"{count} day{'s' if count != 1 else ''} with recordings this month"
            )
        if self._validity_changed_callback is not None:
            self._validity_changed_callback(self.is_current_selection_valid())

    def _on_time_changed(self, _entry: Gtk.Entry) -> None:
        self._refresh_status_and_validity()

    def _on_date_notify(self, calendar: Gtk.Calendar, _pspec: object) -> None:
        if self._programmatic_change:
            return
        gdt = calendar.get_date()
        year, month, day = gdt.get_year(), gdt.get_month(), gdt.get_day_of_month()
        if (year, month) != self._last_valid_month:
            # A different month has no availability answer yet (marks
            # don't carry over -- GtkCalendar's own marks are always
            # for "whichever month is currently shown"), so nothing to
            # refuse against until set_month_availability answers for it.
            self._last_valid_month = (year, month)
            self._last_valid_date = gdt
            self._available_days = None
            self._available_intervals = None
            self._notify_month_changed(year, month)
            self._refresh_status_and_validity()
            return
        if self._available_days is not None and day not in self._available_days:
            self.status_label.set_label("No recordings on that day — pick another")
            self._programmatic_change = True
            try:
                calendar.select_day(self._last_valid_date)
            finally:
                self._programmatic_change = False
            return
        self._last_valid_date = gdt
        self._refresh_status_and_validity()

    def _notify_month_changed(self, year: int, month: int) -> None:
        if self._month_changed_callback is not None:
            self._month_changed_callback(year, month)
