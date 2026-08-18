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

"""Shared Live View timeline.

Visual scaffold only, in progress: the ruler is the one functional
piece (a live-updating time scale). The recording-presence bar is a
static placeholder with no real data behind it yet, and every button
is a no-op. See ~/Projects/surveillance-timeline-design.md for the
full design and what's still open.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import cairo
import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # type: ignore[import-untyped]

from surveillance.ui.icons import magnifier_zoom_icon

# Candidate tick spacings (seconds); the smallest that still leaves each
# label enough room on screen is picked at draw time.
_TICK_STEPS = [30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200]

_MIN_LABEL_SPACING_PX = 70
_RULER_HEIGHT = 28
_PRESENCE_HEIGHT = 22
_EVENT_MARKER_HEIGHT = 8
_CANVAS_HEIGHT = _RULER_HEIGHT + _PRESENCE_HEIGHT + _EVENT_MARKER_HEIGHT

_TOOLBAR_ICON_SIZE = 16
# Approximate width of one icon button (icon + padding), used as the
# floor for the spacers flanking the transport cluster so it never
# crowds the buttons on either side.
_MIN_BUTTON_GAP_PX = 36


class TimelineCanvas(Gtk.DrawingArea):
    """Draws the time ruler plus placeholder presence/event rows.

    Shows a trailing window ending at "now", matching Monitor Center's
    live behavior. Redraws once a second so the ruler visibly ticks
    forward; pan/zoom are not wired up yet.
    """

    def __init__(self, window_seconds: int = 2 * 3600) -> None:
        super().__init__()
        self.set_hexpand(True)
        self.set_content_height(_CANVAS_HEIGHT)
        self.add_css_class("timeline-canvas")
        self._window_seconds = window_seconds
        self.set_draw_func(self._draw)
        self._tick_id = GLib.timeout_add(1000, self._on_tick)
        self.connect("unrealize", self._on_unrealize)

    def _on_unrealize(self, _widget: Gtk.Widget) -> None:
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0

    def _on_tick(self) -> bool:
        self.queue_draw()
        return True  # continue ticking

    def _pick_tick_step(self, width: int) -> int:
        max_ticks = max(1, width // _MIN_LABEL_SPACING_PX)
        for step in _TICK_STEPS:
            if self._window_seconds / step <= max_ticks:
                return step
        return _TICK_STEPS[-1]

    def _theme_color(
        self, name: str, fallback: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        found, rgba = self.get_style_context().lookup_color(name)
        if found:
            return (rgba.red, rgba.green, rgba.blue)
        return fallback

    def _draw(
        self, _area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int
    ) -> None:
        now = time.time()
        start = now - self._window_seconds

        bg = self._theme_color("theme_bg_color", (0.15, 0.15, 0.15))
        fg = self._theme_color("theme_fg_color", (0.8, 0.8, 0.8))
        accent = self._theme_color("accent_color", (0.3, 0.5, 0.9))
        warning = self._theme_color("warning_color", (0.9, 0.6, 0.1))

        cr.set_source_rgb(*bg)
        cr.paint()

        def x_for(t: float) -> float:
            return (t - start) / self._window_seconds * width

        # Placeholder event markers (deterministic pseudo-pattern, no
        # real bookmark data yet).
        cr.set_source_rgb(*warning)
        bucket = 300  # 5 min
        first_bucket = int(start // bucket) * bucket
        b = first_bucket
        while b <= now:
            if (b // bucket) % 7 == 0:
                bx = x_for(b)
                cr.rectangle(bx, 0, 2, _EVENT_MARKER_HEIGHT)
                cr.fill()
            b += bucket

        # Placeholder recording-presence bar.
        presence_y = _EVENT_MARKER_HEIGHT
        cr.set_source_rgba(*accent, 0.5)
        seg = 120  # 2 min segments
        first_seg = int(start // seg) * seg
        s = first_seg
        while s <= now:
            if (s * 2654435761) % 100 < 85:  # ~85% filled, stable pattern
                sx0 = x_for(s)
                sx1 = x_for(s + seg)
                cr.rectangle(sx0, presence_y, max(1.0, sx1 - sx0), _PRESENCE_HEIGHT)
                cr.fill()
            s += seg

        # Ruler ticks + labels.
        ruler_y = _EVENT_MARKER_HEIGHT + _PRESENCE_HEIGHT
        cr.set_source_rgb(*fg)
        cr.set_line_width(1)
        step = self._pick_tick_step(width)
        first_tick = int(start // step) * step
        t = first_tick
        while t <= now:
            tx = x_for(t)
            cr.move_to(tx, ruler_y)
            cr.line_to(tx, ruler_y + 6)
            cr.stroke()
            label = datetime.fromtimestamp(t, tz=timezone.utc).astimezone().strftime(
                "%H:%M" if step < 86400 else "%m-%d"
            )
            extents = cr.text_extents(label)
            cr.move_to(tx - extents.width / 2 - extents.x_bearing, ruler_y + 18)
            cr.show_text(label)
            t += step

        # "Now" marker at the right edge.
        cr.set_source_rgb(*accent)
        cr.set_line_width(2)
        cr.move_to(width - 1, 0)
        cr.line_to(width - 1, height)
        cr.stroke()


class Timeline(Gtk.Box):
    """Shared timeline strip mounted below the Live View grid.

    Only the current-time label and the canvas ruler are live; every
    button here is a placeholder with no behavior wired up yet.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("timeline")

        self.append(self._build_toolbar())
        self.canvas = TimelineCanvas()
        self.append(self.canvas)

        self._clock_id = GLib.timeout_add(1000, self._update_clock)
        self.connect("unrealize", self._on_unrealize)
        self._update_clock()

    def _on_unrealize(self, _widget: Gtk.Widget) -> None:
        if self._clock_id:
            GLib.source_remove(self._clock_id)
            self._clock_id = 0

    def _update_clock(self) -> bool:
        now = datetime.now()
        self._time_label.set_label(now.strftime("%H:%M:%S"))
        self._date_label.set_label(now.strftime("%Y-%m-%d %a").upper())
        return True  # continue ticking

    def _build_toolbar(self) -> Gtk.Box:
        """Single row above the canvas: everything lives here to save
        the vertical space a separate header+footer would cost."""
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.add_css_class("timeline-toolbar")

        time_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._time_label = Gtk.Label(label="00:00:00", xalign=0)
        self._time_label.add_css_class("timeline-clock")
        self._date_label = Gtk.Label(label="", xalign=0)
        self._date_label.add_css_class("dim-label")
        time_box.append(self._time_label)
        time_box.append(self._date_label)

        # Filter/download/calendar/zoom cluster and the Live/speed
        # cluster swap sides from where DSM puts them — purely a
        # visual-balance choice, the smaller cluster on the left reads
        # more symmetric against the transport cluster. Time/date stays
        # put on the far left regardless.
        button_cluster = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        for icon_name, tooltip in (
            ("system-search-symbolic", "Filter events"),
            ("document-save-symbolic", "Download"),
        ):
            btn = Gtk.Button()
            btn.set_icon_name(icon_name)
            btn.set_tooltip_text(tooltip)
            button_cluster.append(btn)

        calendar_btn = Gtk.Button()
        calendar_btn.set_icon_name("x-office-calendar-symbolic")
        calendar_btn.set_tooltip_text("Jump to date/time")
        button_cluster.append(calendar_btn)

        zoom_out_btn = Gtk.Button()
        zoom_out_btn.set_child(magnifier_zoom_icon(zoom_in=False, size=_TOOLBAR_ICON_SIZE))
        zoom_out_btn.set_tooltip_text("Zoom out")
        button_cluster.append(zoom_out_btn)

        zoom_in_btn = Gtk.Button()
        zoom_in_btn.set_child(magnifier_zoom_icon(zoom_in=True, size=_TOOLBAR_ICON_SIZE))
        zoom_in_btn.set_tooltip_text("Zoom in")
        button_cluster.append(zoom_in_btn)

        live_btn = Gtk.Button(label="Live")
        live_btn.set_tooltip_text("Return to live view")

        speed_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        speed_box.add_css_class("linked")
        minus_btn = Gtk.Button()
        minus_btn.set_icon_name("list-remove-symbolic")
        minus_btn.set_tooltip_text("Slower")
        speed_label = Gtk.Label(label="1x")
        speed_label.set_margin_start(4)
        speed_label.set_margin_end(4)
        plus_btn = Gtk.Button()
        plus_btn.set_icon_name("list-add-symbolic")
        plus_btn.set_tooltip_text("Faster")
        speed_box.append(minus_btn)
        speed_box.append(speed_label)
        speed_box.append(plus_btn)

        live_cluster = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        live_cluster.append(live_btn)
        live_cluster.append(speed_box)

        transport = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for icon_name, tooltip in (
            ("go-previous-symbolic", "Previous event"),
            ("media-seek-backward-symbolic", "Back 10s"),
            ("media-playback-pause-symbolic", "Pause"),
            ("media-seek-forward-symbolic", "Forward 10s"),
            ("go-next-symbolic", "Next event"),
        ):
            btn = Gtk.Button()
            btn.set_icon_name(icon_name)
            btn.set_tooltip_text(tooltip)
            transport.append(btn)

        toolbar.append(time_box)
        toolbar.append(live_cluster)
        # Keeps the transport cluster from ever crowding the buttons on
        # either side, even in a narrow window — each spacer holds at
        # least one button's width and only grows from there.
        toolbar.append(self._make_min_gap_spacer())
        toolbar.append(transport)
        toolbar.append(self._make_min_gap_spacer())
        toolbar.append(button_cluster)

        return toolbar

    @staticmethod
    def _make_min_gap_spacer() -> Gtk.Box:
        spacer = Gtk.Box(hexpand=True)
        spacer.set_size_request(_MIN_BUTTON_GAP_PX, -1)
        return spacer
