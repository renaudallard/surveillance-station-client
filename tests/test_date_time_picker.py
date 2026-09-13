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

"""Tests for DateTimePicker's own status/validity logic, run on a bare
instance (bypassing __init__, which needs a display for its real GTK
widgets) with stand-ins for calendar/status_label/selected_date_label."""

from __future__ import annotations

from dataclasses import dataclass, field

from surveillance.ui.date_time_picker import DateTimePicker


@dataclass
class FakeDateTime:
    year: int
    month: int
    day: int

    def get_year(self) -> int:
        return self.year

    def get_month(self) -> int:
        return self.month

    def get_day_of_month(self) -> int:
        return self.day

    def format(self, fmt: str) -> str:
        assert fmt == "%Y-%m-%d"
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"


@dataclass
class FakeCalendar:
    """select_day() updates get_date() the way the real GtkCalendar
    eventually does, since these tests exercise the app's own logic
    rather than GTK's separate, untestable-without-a-display timing
    (see _on_date_notify's own comment on that)."""

    date: FakeDateTime
    selected: list[FakeDateTime] = field(default_factory=list)

    def get_date(self) -> FakeDateTime:
        return self.date

    def select_day(self, gdt: FakeDateTime) -> None:
        self.selected.append(gdt)
        self.date = gdt


class FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def set_label(self, text: str) -> None:
        self.text = text

    def get_label(self) -> str:
        return self.text


class FakeEntry:
    def __init__(self, text: str = "00:00:00") -> None:
        self.text = text

    def get_text(self) -> str:
        return self.text


def _make_picker(
    *,
    date: FakeDateTime,
    available_days: set[int] | None = None,
    available_intervals: list[tuple[int, int]] | None = None,
    time_text: str = "00:00:00",
) -> DateTimePicker:
    """A DateTimePicker with real widgets swapped for stand-ins, state set
    up as if the calendar were already sitting on *date*."""
    picker = DateTimePicker.__new__(DateTimePicker)
    picker.calendar = FakeCalendar(date)  # type: ignore[assignment]
    picker.status_label = FakeLabel()  # type: ignore[assignment]
    picker.selected_date_label = FakeLabel()  # type: ignore[assignment]
    picker.time_entry = FakeEntry(time_text)  # type: ignore[assignment]
    picker._available_days = available_days
    picker._available_intervals = available_intervals
    picker._last_valid_date = date  # type: ignore[assignment]
    picker._last_valid_month = (date.year, date.month)
    picker._programmatic_change = False
    picker._month_changed_callback = None
    picker._validity_changed_callback = None
    return picker


class TestDayUnavailableMessage:
    def test_names_the_month_when_it_has_no_recordings_at_all(self) -> None:
        picker = _make_picker(date=FakeDateTime(2026, 8, 31), available_days=set())
        assert picker._day_unavailable_message() == "No recordings this month"

    def test_names_the_day_when_other_days_do_have_recordings(self) -> None:
        picker = _make_picker(date=FakeDateTime(2026, 9, 4), available_days={5, 6, 7})
        assert picker._day_unavailable_message() == "No recordings on that day — pick another"


class TestRefreshStatusAndValidity:
    def test_flags_a_day_missing_from_a_month_with_some_recordings(self) -> None:
        """A day accepted before its month's availability was known (see
        _on_date_notify), whose answer later arrives and turns out not to
        include it, must be reported as the wrong day -- not fall through
        to the exact-time check below, which would call it the wrong time
        instead (both are true, but only one is the actual cause)."""
        picker = _make_picker(
            date=FakeDateTime(2026, 9, 3),
            available_days={5, 6, 7, 8, 9},
            available_intervals=[],
        )
        picker._refresh_status_and_validity()
        assert picker.status_label.get_label() == "No recordings on that day — pick another"

    def test_flags_a_whole_empty_month_the_same_way(self) -> None:
        picker = _make_picker(date=FakeDateTime(2026, 8, 31), available_days=set())
        picker._refresh_status_and_validity()
        assert picker.status_label.get_label() == "No recordings this month"

    def test_flags_an_available_day_with_no_matching_time(self) -> None:
        picker = _make_picker(
            date=FakeDateTime(2026, 9, 6),
            available_days={5, 6, 7},
            available_intervals=[(1000, 2000)],
            time_text="00:00:00",
        )
        picker._refresh_status_and_validity()
        assert picker.status_label.get_label() == "No recording at that exact time — pick another"

    def test_always_updates_the_selected_date_label(self) -> None:
        picker = _make_picker(date=FakeDateTime(2026, 9, 6), available_days=None)
        picker._refresh_status_and_validity()
        assert picker.selected_date_label.get_label() == "Selected: 2026-09-06"


class TestOnDateNotify:
    def test_ignores_the_deferred_notification_from_its_own_revert(self) -> None:
        """calendar.select_day() (used below to revert a refused click)
        does not fire notify::day/month/year synchronously in real GTK,
        so the guard around it can't rely on timing -- see the method's
        own comment. This exercises that guard directly: a notification
        reporting the date already considered selected must be a no-op,
        not reprocessed as a fresh click."""
        picker = _make_picker(date=FakeDateTime(2026, 9, 5), available_days={5, 6, 7})
        picker.status_label.set_label("untouched")
        picker._on_date_notify(picker.calendar, None)
        assert picker.status_label.get_label() == "untouched"

    def test_refuses_a_day_with_no_recording_and_reverts(self) -> None:
        picker = _make_picker(date=FakeDateTime(2026, 8, 31), available_days={5, 6, 7})
        clicked = FakeDateTime(2026, 8, 25)
        picker.calendar.date = clicked
        picker._on_date_notify(picker.calendar, None)
        assert picker.status_label.get_label() == "No recordings on that day — pick another"
        assert picker.calendar.get_date() == picker._last_valid_date
        assert picker._programmatic_change is False

    def test_refuses_every_day_in_a_fully_empty_month(self) -> None:
        picker = _make_picker(date=FakeDateTime(2026, 8, 31), available_days=set())
        clicked = FakeDateTime(2026, 8, 25)
        picker.calendar.date = clicked
        picker._on_date_notify(picker.calendar, None)
        assert picker.status_label.get_label() == "No recordings this month"

    def test_accepts_a_day_within_the_same_known_month(self) -> None:
        picker = _make_picker(
            date=FakeDateTime(2026, 9, 5),
            available_days={5, 6, 7},
            available_intervals=[(0, 9999999999)],
        )
        clicked = FakeDateTime(2026, 9, 6)
        picker.calendar.date = clicked
        picker._on_date_notify(picker.calendar, None)
        assert picker._last_valid_date == clicked
        assert picker.status_label.get_label() == "3 days with recordings this month"
