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

"""Tests for the Live View timeline's pan/zoom math and Download popover
logic (no GTK required)."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from datetime import datetime

import pytest

from surveillance.ui.timeline import (
    _MAX_WINDOW_SECONDS,
    _MIN_WINDOW_SECONDS,
    clamp_to_live,
    compute_zoom,
    max_speed_for_slots,
    pan_view_end,
    parse_custom_download_range,
    pick_tick_step,
    quick_download_label,
    quick_download_range,
    tick_label,
    tick_times,
)


class TestPanViewEnd:
    def test_dragging_right_reveals_earlier_time(self) -> None:
        # Content follows the cursor: dragging right moves the window's
        # right edge backward (earlier), the "grab and drag" convention.
        new_end = pan_view_end(view_end=1000.0, dx=100.0, window_seconds=800.0, width=800.0)
        assert new_end < 1000.0

    def test_dragging_left_reveals_later_time(self) -> None:
        new_end = pan_view_end(view_end=1000.0, dx=-100.0, window_seconds=800.0, width=800.0)
        assert new_end > 1000.0

    def test_magnitude_matches_dragged_fraction_of_the_window(self) -> None:
        # Dragging a quarter of the widget's width should shift the view
        # by a quarter of the visible window, regardless of window size.
        new_end = pan_view_end(view_end=1000.0, dx=200.0, window_seconds=800.0, width=800.0)
        assert new_end == 1000.0 - 200.0

    def test_no_drag_leaves_view_end_unchanged(self) -> None:
        new_end = pan_view_end(view_end=1000.0, dx=0.0, window_seconds=800.0, width=800.0)
        assert new_end == 1000.0


class TestComputeZoom:
    def test_zoom_in_shrinks_the_window(self) -> None:
        new_window, _ = compute_zoom(
            window_seconds=7200.0, view_end=1000.0, delta=0.15, cursor_x=400.0, width=800.0
        )
        assert new_window < 7200.0

    def test_zoom_out_grows_the_window(self) -> None:
        new_window, _ = compute_zoom(
            window_seconds=7200.0, view_end=1000.0, delta=-0.15, cursor_x=400.0, width=800.0
        )
        assert new_window > 7200.0

    def test_keeps_the_timestamp_under_the_cursor_fixed(self) -> None:
        window_seconds, view_end, cursor_x, width = 7200.0, 100_000.0, 200.0, 800.0
        start = view_end - window_seconds
        t_cursor = start + (cursor_x / width) * window_seconds

        new_window, new_view_end = compute_zoom(window_seconds, view_end, 0.15, cursor_x, width)

        new_start = new_view_end - new_window
        new_cursor_x = (t_cursor - new_start) / new_window * width
        assert abs(new_cursor_x - cursor_x) < 1e-9

    def test_window_clamps_to_the_minimum(self) -> None:
        new_window, _ = compute_zoom(
            window_seconds=_MIN_WINDOW_SECONDS * 1.05,
            view_end=1000.0,
            delta=0.9,
            cursor_x=400.0,
            width=800.0,
        )
        assert new_window == _MIN_WINDOW_SECONDS

    def test_window_clamps_to_the_maximum(self) -> None:
        new_window, _ = compute_zoom(
            window_seconds=_MAX_WINDOW_SECONDS * 0.95,
            view_end=1000.0,
            delta=-0.9,
            cursor_x=400.0,
            width=800.0,
        )
        assert new_window == _MAX_WINDOW_SECONDS


class TestClampToLive:
    def test_view_end_past_now_is_pinned_and_resumes_following(self) -> None:
        view_end, following = clamp_to_live(view_end=2000.0, now=1000.0)
        assert view_end == 1000.0
        assert following is True

    def test_view_end_exactly_now_resumes_following(self) -> None:
        view_end, following = clamp_to_live(view_end=1000.0, now=1000.0)
        assert view_end == 1000.0
        assert following is True

    def test_view_end_in_the_past_is_left_alone(self) -> None:
        view_end, following = clamp_to_live(view_end=500.0, now=1000.0)
        assert view_end == 500.0
        assert following is False


class TestQuickDownloadRange:
    def test_live_looks_backward_from_the_reference_point(self) -> None:
        # Live's Quick Save is "grab what led up to now", not "play
        # forward from here" -- a sign flip here would silently invert
        # the feature, and there is no "after" to offer live anyway.
        end = datetime(2026, 8, 22, 12, 0, 0)
        start, returned_end = quick_download_range(end, minutes=5, history_active=False)
        assert returned_end == end
        assert start == datetime(2026, 8, 22, 11, 55, 0)

    def test_live_one_minute(self) -> None:
        end = datetime(2026, 8, 22, 12, 0, 0)
        start, _end = quick_download_range(end, minutes=1, history_active=False)
        assert start == datetime(2026, 8, 22, 11, 59, 0)

    def test_history_centers_on_the_reference_point(self) -> None:
        # History has both directions already available once paused on
        # a moment of interest, so the range straddles it instead of
        # only looking backward.
        reference = datetime(2026, 8, 22, 12, 0, 0)
        start, end = quick_download_range(reference, minutes=5, history_active=True)
        assert start == datetime(2026, 8, 22, 11, 55, 0)
        assert end == datetime(2026, 8, 22, 12, 5, 0)

    def test_history_one_minute(self) -> None:
        reference = datetime(2026, 8, 22, 12, 0, 0)
        start, end = quick_download_range(reference, minutes=1, history_active=True)
        assert start == datetime(2026, 8, 22, 11, 59, 0)
        assert end == datetime(2026, 8, 22, 12, 1, 0)


class TestQuickDownloadLabel:
    def test_live_label(self) -> None:
        assert quick_download_label(5, history_active=False) == "Download last 5 min"

    def test_history_label(self) -> None:
        assert quick_download_label(5, history_active=True) == "Download -5 to +5 min"

    def test_labels_differ_for_every_configured_option(self) -> None:
        # Guards against the two modes ever accidentally converging on
        # the same wording for a given button.
        for minutes in (1, 2, 5):
            assert quick_download_label(minutes, False) != quick_download_label(minutes, True)


class TestParseCustomDownloadRange:
    def test_valid_range_round_trips(self) -> None:
        start, end = parse_custom_download_range("2026-08-22 10:00:00", "2026-08-22 10:00:30")
        assert start == datetime(2026, 8, 22, 10, 0, 0)
        assert end == datetime(2026, 8, 22, 10, 0, 30)

    def test_strips_surrounding_whitespace(self) -> None:
        start, end = parse_custom_download_range(
            "  2026-08-22 10:00:00  ", "  2026-08-22 10:00:30  "
        )
        assert start == datetime(2026, 8, 22, 10, 0, 0)
        assert end == datetime(2026, 8, 22, 10, 0, 30)

    def test_unparseable_start_raises_with_format_hint(self) -> None:
        with pytest.raises(ValueError, match="YYYY-MM-DD HH:MM:SS"):
            parse_custom_download_range("not a date", "2026-08-22 10:00:30")

    def test_unparseable_end_raises_with_format_hint(self) -> None:
        with pytest.raises(ValueError, match="YYYY-MM-DD HH:MM:SS"):
            parse_custom_download_range("2026-08-22 10:00:00", "not a date")

    def test_end_before_start_raises(self) -> None:
        with pytest.raises(ValueError, match="End must be after start"):
            parse_custom_download_range("2026-08-22 10:00:30", "2026-08-22 10:00:00")

    def test_end_equal_start_raises(self) -> None:
        with pytest.raises(ValueError, match="End must be after start"):
            parse_custom_download_range("2026-08-22 10:00:00", "2026-08-22 10:00:00")


class TestMaxSpeedForSlots:
    """The speed budget divides across slots, but 1x is always on offer:
    real time is what Live already decodes on every slot."""

    @pytest.fixture
    def budget(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[float], None]]:
        from surveillance.ui import timeline

        original = timeline._MAX_SPEED_SLOT_PRODUCT

        def _set(value: float) -> None:
            monkeypatch.setattr(timeline, "_MAX_SPEED_SLOT_PRODUCT", value)

        yield _set
        timeline._MAX_SPEED_SLOT_PRODUCT = original

    def test_divides_the_budget_across_slots(self, budget: Callable[[float], None]) -> None:
        budget(100.0)
        assert max_speed_for_slots(1) == 100.0
        assert max_speed_for_slots(4) == 25.0
        assert max_speed_for_slots(16) == 6.25

    def test_never_drops_below_real_time(self, budget: Callable[[float], None]) -> None:
        budget(8.0)
        assert max_speed_for_slots(9) == 1.0
        budget(1.0)
        assert max_speed_for_slots(16) == 1.0

    def test_no_slots_means_the_whole_budget(self, budget: Callable[[float], None]) -> None:
        budget(100.0)
        assert max_speed_for_slots(0) == 100.0


def _labels(end: datetime, window_seconds: float, width: int = 1000) -> list[str]:
    step = pick_tick_step(window_seconds, width)
    end_ts = end.timestamp()
    return [tick_label(tick, step) for tick in tick_times(end_ts - window_seconds, end_ts, step)]


class TestRulerTicks:
    """Ticks sit on local wall-clock multiples of the step, labelled with
    what tells them apart at that step."""

    @pytest.fixture
    def brussels(self) -> Iterator[None]:
        # A zone an hour or two east of UTC, where a UTC-aligned two-hour
        # grid read as odd local hours for half the year.
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Brussels"
        time.tzset()
        yield
        if saved is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = saved
        time.tzset()

    def test_step_picks_the_smallest_that_fits(self) -> None:
        assert pick_tick_step(180, 1000) == 30
        assert pick_tick_step(86400, 1000) == 7200
        assert pick_tick_step(8 * 86400, 1000) == 86400
        assert pick_tick_step(30 * 86400, 1000) == 604800

    @pytest.mark.usefixtures("brussels")
    def test_two_hour_ticks_land_on_even_local_hours_all_year(self) -> None:
        for end in (datetime(2026, 1, 15, 12, 0), datetime(2026, 7, 15, 12, 0)):
            labels = _labels(end, 86400)
            assert labels[0] == "12:00"
            assert all(int(label[:2]) % 2 == 0 for label in labels), labels
            assert len(labels) == 13

    def test_sub_minute_ticks_carry_seconds(self) -> None:
        labels = _labels(datetime(2026, 9, 10, 12, 3, 0), 180)
        assert labels == [
            "12:00:00",
            "12:00:30",
            "12:01:00",
            "12:01:30",
            "12:02:00",
            "12:02:30",
            "12:03:00",
        ]

    def test_day_ticks_are_dated_and_anchored_to_the_calendar(self) -> None:
        end = datetime(2026, 9, 10, 12, 0)
        step = pick_tick_step(8 * 86400, 1000)
        assert step == 86400
        labels = _labels(end, 8 * 86400)
        assert labels == ["09-03", "09-04", "09-05", "09-06", "09-07", "09-08", "09-09", "09-10"]
        # A two-day grid does not slide with the window: panning by one
        # day keeps the same tick days.
        two_days = 172800
        first = {
            t.date() for t in tick_times(end.timestamp() - 8 * 86400, end.timestamp(), two_days)
        }
        shifted = {
            t.date()
            for t in tick_times(end.timestamp() - 9 * 86400, end.timestamp() - 86400, two_days)
        }
        assert first & shifted == first - {max(first)} or first == shifted

    @pytest.mark.usefixtures("brussels")
    def test_a_dst_gap_yields_the_hour_that_exists(self) -> None:
        # 2026-03-29 02:00 CET becomes 03:00 CEST: no tick is labelled
        # with an hour that never happened, and none is drawn twice.
        start = datetime(2026, 3, 29, 0, 30).timestamp()
        end = datetime(2026, 3, 29, 4, 30).timestamp()
        assert [tick_label(t, 3600) for t in tick_times(start, end, 3600)] == [
            "01:00",
            "03:00",
            "04:00",
        ]
