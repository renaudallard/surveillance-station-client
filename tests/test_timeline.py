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

"""Tests for the Live View timeline's pan/zoom math (no GTK required)."""

from __future__ import annotations

from surveillance.ui.timeline import (
    _MAX_WINDOW_SECONDS,
    _MIN_WINDOW_SECONDS,
    clamp_to_live,
    compute_zoom,
    pan_view_end,
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
