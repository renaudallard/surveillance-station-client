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

Visual scaffold, in progress: the ruler is live (a live-updating time
scale, pan and zoom both work), and so are the zoom buttons. The
recording-presence bar is a static placeholder with no real data
behind it yet, and every other button is a no-op. See
~/Projects/surveillance-timeline-design.md for the full design and
what's still open.
"""

from __future__ import annotations

import time
from collections.abc import Callable
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
_NOW_MARKER_WIDTH = 3

# Fraction of the visible window shrunk/grown per scroll tick or zoom
# button click — same value as mpv_widget's own _ZOOM_STEP, for a
# consistent scroll-to-zoom "feel" across the app.
_ZOOM_STEP = 0.15
_MIN_WINDOW_SECONDS = 180  # 3 min — below this, tick labels have no room
_MAX_WINDOW_SECONDS = 30 * 86400  # 30 days — matches this app's other "how far back is sane" bound

# How long the cursor must sit still before notifying the hover
# callback — without this, dragging the mouse across the ruler would
# fire one GetThumbnail request per pixel of motion.
_THUMBNAIL_DEBOUNCE_MS = 100

_TOOLBAR_ICON_SIZE = 16
# Approximate width of one icon button (icon + padding), used as the
# floor for the spacers flanking the transport cluster so it never
# crowds the buttons on either side.
_MIN_BUTTON_GAP_PX = 36


def pan_view_end(view_end: float, dx: float, window_seconds: float, width: float) -> float:
    """New window-right-edge timestamp for a drag of *dx* pixels.

    Content follows the cursor (grab-and-drag feel): dragging right
    (positive dx) reveals earlier time, same convention as video pan.
    """
    return view_end - dx * window_seconds / width


def compute_zoom(
    window_seconds: float, view_end: float, delta: float, cursor_x: float, width: float
) -> tuple[float, float]:
    """New (window_seconds, view_end) for a zoom of *delta* (positive
    zooms in) centered on cursor_x — the timestamp under the cursor
    stays under it, the same "zoom to point" behavior as scroll-to-zoom
    on a video slot. window_seconds is clamped to
    [_MIN_WINDOW_SECONDS, _MAX_WINDOW_SECONDS].
    """
    new_window = max(_MIN_WINDOW_SECONDS, min(_MAX_WINDOW_SECONDS, window_seconds * (1 - delta)))
    start = view_end - window_seconds
    fraction = cursor_x / width
    t_cursor = start + fraction * window_seconds
    new_view_end = t_cursor + (1 - fraction) * new_window
    return new_window, new_view_end


def clamp_to_live(view_end: float, now: float) -> tuple[float, bool]:
    """Pin *view_end* to *now* and report "following" once a pan/zoom
    would otherwise push it past the live edge — the same
    catch-up-and-resume-autoscroll behavior for both gestures.
    """
    if view_end >= now:
        return now, True
    return view_end, False


class TimelineCanvas(Gtk.DrawingArea):
    """Draws the time ruler plus placeholder presence/event rows.

    Shows a trailing window that follows "now" by default, matching
    Monitor Center's live behavior. Dragging pans and scrolling (or the
    toolbar's zoom buttons) zooms the view — purely local display
    changes, same as DSM's own web app: neither touches video playback
    or switches any slot into History mode. Either one stops the window
    following "now"; panning/zooming back past the live edge clamps to
    "now" and resumes following, same as autoscroll in a chat view.
    """

    def __init__(self, window_seconds: float = 2 * 3600) -> None:
        super().__init__()
        self.set_hexpand(True)
        self.set_content_height(_CANVAS_HEIGHT)
        self.add_css_class("timeline-canvas")
        self._window_seconds = window_seconds
        self._view_end = time.time()
        self._following = True
        self.set_draw_func(self._draw)
        self._tick_id = GLib.timeout_add(1000, self._on_tick)
        self.connect("unrealize", self._on_unrealize)
        self._attach_pan_controls()
        self._attach_zoom_controls()

        self._hover_callback: Callable[[float, float], None] | None = None
        self._hover_leave_callback: Callable[[], None] | None = None
        self._thumbnail_debounce_id = 0
        self._attach_thumbnail_hover()

    def _attach_pan_controls(self) -> None:
        # drag-update reports the offset cumulative from drag-begin, not
        # incrementally, so track how much has already been applied —
        # same technique as mpv_widget.attach_zoom_pan_controls.
        drag_last = {"x": 0.0}

        def on_drag_begin(_gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
            drag_last["x"] = 0.0

        def on_drag_update(gesture: Gtk.GestureDrag, offset_x: float, _offset_y: float) -> None:
            width = self.get_width()
            if width <= 0:
                return
            dx = offset_x - drag_last["x"]
            drag_last["x"] = offset_x
            self._view_end = pan_view_end(self._view_end, dx, self._window_seconds, width)
            self._clamp_to_live()
            self.queue_draw()

        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", on_drag_begin)
        drag.connect("drag-update", on_drag_update)
        self.add_controller(drag)

    def _attach_zoom_controls(self) -> None:
        # EventControllerScroll's "scroll" signal has no position, so a
        # motion controller tracks the last-known pointer position for
        # it to use — same technique as mpv_widget.attach_zoom_pan_controls.
        pointer = {"x": 0.0}

        def on_motion(_controller: Gtk.EventControllerMotion, x: float, _y: float) -> None:
            pointer["x"] = x

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", on_motion)
        self.add_controller(motion)

        def on_scroll(_controller: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
            # Scroll up (dy negative — "away from the user") zooms in,
            # matching mpv_widget's convention for video zoom.
            self.zoom_at(-dy * _ZOOM_STEP, pointer["x"])
            return True  # handled — don't let it bubble past the widget

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", on_scroll)
        self.add_controller(scroll)

    def set_hover_callback(self, callback: Callable[[float, float], None]) -> None:
        """Set the hover-preview position source.

        *callback* receives (local_x, timestamp) once the cursor has sat
        still over the ruler for a moment. The canvas only knows about
        positions and timestamps — LiveView owns resolving which camera
        that is, fetching its thumbnail, and displaying it, since a
        Gtk.Popover can't reliably paint over the video grid's GL
        surfaces the way a same-render-tree Gtk.Overlay can (confirmed
        live: the popover reported itself visible and mapped, but never
        actually appeared on screen).
        """
        self._hover_callback = callback

    def set_hover_leave_callback(self, callback: Callable[[], None]) -> None:
        self._hover_leave_callback = callback

    def _attach_thumbnail_hover(self) -> None:
        hover = Gtk.EventControllerMotion()
        hover.connect("motion", self._on_thumbnail_hover_motion)
        hover.connect("leave", self._on_thumbnail_hover_leave)
        self.add_controller(hover)

    def _on_thumbnail_hover_motion(
        self, _controller: Gtk.EventControllerMotion, x: float, _y: float
    ) -> None:
        if self._thumbnail_debounce_id:
            GLib.source_remove(self._thumbnail_debounce_id)
        self._thumbnail_debounce_id = GLib.timeout_add(
            _THUMBNAIL_DEBOUNCE_MS, self._notify_hover, x
        )

    def _on_thumbnail_hover_leave(self, _controller: Gtk.EventControllerMotion) -> None:
        if self._thumbnail_debounce_id:
            GLib.source_remove(self._thumbnail_debounce_id)
            self._thumbnail_debounce_id = 0
        if self._hover_leave_callback:
            self._hover_leave_callback()

    def _notify_hover(self, x: float) -> bool:
        self._thumbnail_debounce_id = 0
        width = self.get_width()
        if not self._hover_callback or width <= 0:
            return False  # one-shot timeout, don't repeat
        timestamp = self._view_end - self._window_seconds + (x / width) * self._window_seconds
        self._hover_callback(x, timestamp)
        return False  # one-shot timeout, don't repeat

    def zoom_at(self, delta: float, cursor_x: float) -> None:
        """Zoom in/out by *delta* (positive zooms in), keeping the
        timestamp under cursor_x fixed on screen — the same "zoom to
        point" behavior as scroll-to-zoom on a video slot.
        """
        width = self.get_width()
        if width <= 0:
            return
        new_window, new_view_end = compute_zoom(
            self._window_seconds, self._view_end, delta, cursor_x, width
        )
        if new_window == self._window_seconds:
            return
        self._window_seconds = new_window
        self._view_end = new_view_end
        self._clamp_to_live()
        self.queue_draw()

    def _clamp_to_live(self) -> None:
        """Pin the view to "now" and resume following once a pan/zoom
        would otherwise push it past the live edge — the same
        catch-up-and-resume-autoscroll behavior for both gestures.
        """
        self._view_end, self._following = clamp_to_live(self._view_end, time.time())

    def _on_unrealize(self, _widget: Gtk.Widget) -> None:
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0
        if self._thumbnail_debounce_id:
            GLib.source_remove(self._thumbnail_debounce_id)
            self._thumbnail_debounce_id = 0

    def _on_tick(self) -> bool:
        if self._following:
            self._view_end = time.time()
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
        end = self._view_end
        start = end - self._window_seconds

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
        while b <= end:
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
        while s <= end:
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
        while t <= end:
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

        # "Now" marker. While following, draw it a couple pixels in from
        # the right edge rather than via x_for(now) — "now" is resampled
        # fresh on every redraw and is always a hair ahead of the
        # _view_end snapshot x_for is built from, which would push it
        # just past the edge and hide it. The inset also keeps the full
        # stroke width on-canvas: Cairo strokes are centered on the
        # path, so a line placed exactly at x=width would have half its
        # width clipped off. Once panned, x_for(now) is correct (and may
        # legitimately be off-screen, which is fine — no marker shown
        # until the live edge scrolls back into view).
        now_x = width - _NOW_MARKER_WIDTH / 2 if self._following else x_for(now)
        if 0 <= now_x <= width:
            cr.set_source_rgb(*accent)
            cr.set_line_width(_NOW_MARKER_WIDTH)
            cr.move_to(now_x, 0)
            cr.line_to(now_x, height)
            cr.stroke()


class Timeline(Gtk.Box):
    """Shared timeline strip mounted below the Live View grid.

    The current-time label, the canvas ruler, and the zoom buttons are
    live; every other button here is still a placeholder with no
    behavior wired up yet.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("timeline")

        self.canvas = TimelineCanvas()
        self.append(self._build_toolbar())
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

        # Same zoom_at() the canvas's own scroll-wheel handler uses, just
        # centered on the canvas midpoint since a button click has no
        # cursor position of its own to zoom toward.
        zoom_out_btn = Gtk.Button()
        zoom_out_btn.set_child(magnifier_zoom_icon(zoom_in=False, size=_TOOLBAR_ICON_SIZE))
        zoom_out_btn.set_tooltip_text("Zoom out")
        zoom_out_btn.connect(
            "clicked", lambda _btn: self.canvas.zoom_at(-_ZOOM_STEP, self.canvas.get_width() / 2)
        )
        button_cluster.append(zoom_out_btn)

        zoom_in_btn = Gtk.Button()
        zoom_in_btn.set_child(magnifier_zoom_icon(zoom_in=True, size=_TOOLBAR_ICON_SIZE))
        zoom_in_btn.set_tooltip_text("Zoom in")
        zoom_in_btn.connect(
            "clicked", lambda _btn: self.canvas.zoom_at(_ZOOM_STEP, self.canvas.get_width() / 2)
        )
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
