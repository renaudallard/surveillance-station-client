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
scale, pan, zoom, and click-to-seek all work), and so are the Live,
+-10s, Pause/Play, and speed-dropdown buttons. The recording-presence
bar is a static placeholder with no real data behind it yet -- wiring
it to DSM's real EnumInterval/ListBookmark data is deliberately a
separate piece of work from History mode itself, not yet started. The
event-jump buttons are still no-ops too.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone

import cairo
import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # type: ignore[import-untyped]

from surveillance.ui.icons import filter_icon, history_direction_icon, magnifier_zoom_icon

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
_DEFAULT_WINDOW_SECONDS = 2 * 3600  # what a fresh canvas opens with, and reset_view() restores

# How long the cursor must sit still before notifying the hover
# callback — without this, dragging the mouse across the ruler would
# fire one GetThumbnail request per pixel of motion.
_THUMBNAIL_DEBOUNCE_MS = 100

# A drag shorter than this (pixels) is treated as a plain click instead
# — same value and technique as mpv_widget.attach_zoom_pan_controls's
# own _DRAG_CLICK_THRESHOLD, for a consistent click/drag feel.
_DRAG_CLICK_THRESHOLD = 4

_TOOLBAR_ICON_SIZE = 16
# Approximate width of one icon button (icon + padding), used as the
# floor for the spacers flanking the transport cluster so it never
# crowds the buttons on either side.
_MIN_BUTTON_GAP_PX = 36

# Ruler ticks/labels' color while following "now" rather than showing a
# History position (see TimelineCanvas._history_position) — matching
# DSM's own web app, which shows its live clock in the same blue. Also
# the toolbar's own real-time clock's color (see Timeline._build_toolbar
# / style.css's .timeline-live-text), so every "this is live/current"
# cue across the app reads as one consistent signal.
_LIVE_TICK_COLOR = (0x35 / 255, 0x84 / 255, 0xE4 / 255)

# Recording-presence bar's color while showing a History position
# instead of "now" — plain grey rather than the accent blue it's drawn
# in otherwise, distinguishing the strip at a glance rather than
# relying on the ticks/labels' own (comparatively subtle) color change
# alone.
_HISTORY_PRESENCE_COLOR = (0.5, 0.5, 0.5)

# History playback speed choices for _speed_btn's popover, and DSM's
# own literal multiplier string for each -- confirmed to genuinely
# scale playback by that factor, not just a hint left for mpv to
# interpret (see WebSocketBridge._history_play_params' own "speed"
# bullet for what's confirmed about the range).
_SPEED_OPTIONS: list[tuple[str, str]] = [
    ("0.125", "1/8x"),
    ("0.25", "1/4x"),
    ("0.5", "1/2x"),
    ("1", "1x"),
    ("2", "2x"),
    ("4", "4x"),
    ("8", "8x"),
    ("16", "16x"),
    ("32", "32x"),
    ("64", "64x"),
    ("100", "100x"),
]
_SPEED_LABELS: dict[str, str] = dict(_SPEED_OPTIONS)


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

    def __init__(self, window_seconds: float = _DEFAULT_WINDOW_SECONDS) -> None:
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

        self._seek_callback: Callable[[float], None] | None = None
        # The focus slot's own current position within History playback
        # (unix time) — None whenever it's on Live, in which case the
        # marker below tracks wall-clock "now" instead, same as before
        # History mode existed. LiveView owns advancing this every
        # second (this widget has no notion of a slot or a bridge to
        # tick it from) and re-syncing it when the focus slot changes.
        self._history_position: float | None = None

    def set_history_position(self, timestamp: float | None) -> None:
        """Set (or, for None, clear) the focus slot's History playback
        position — see the field's own comment in __init__."""
        self._history_position = timestamp
        self.queue_draw()

    def reset_view(self) -> None:
        """Zoom back out to the default window and pan back to "now",
        resuming live-following — called by LiveView whenever every slot
        returns to Live (the button, or a layout switch), so a view left
        zoomed/panned in from browsing History doesn't linger once
        there's no History position left to justify it."""
        self._window_seconds = _DEFAULT_WINDOW_SECONDS
        self._view_end = time.time()
        self._following = True
        self.queue_draw()

    def set_seek_callback(self, callback: Callable[[float], None]) -> None:
        """Set the click-to-seek target.

        *callback* receives the clicked timestamp — a plain click (see
        _DRAG_CLICK_THRESHOLD), not a drag past it, same as pan/zoom the
        canvas only knows about positions and timestamps, never cameras
        or slots; LiveView resolves which recording that time falls in
        for each slot, same division of responsibility as the hover
        thumbnail (see set_hover_callback).
        """
        self._seek_callback = callback

    def _attach_pan_controls(self) -> None:
        # drag-update reports the offset cumulative from drag-begin, not
        # incrementally, so track how much has already been applied —
        # same technique as mpv_widget.attach_zoom_pan_controls.
        drag_last = {"x": 0.0}
        drag_start = {"x": 0.0}

        def on_drag_begin(_gesture: Gtk.GestureDrag, x: float, _y: float) -> None:
            drag_last["x"] = 0.0
            drag_start["x"] = x

        def on_drag_update(gesture: Gtk.GestureDrag, offset_x: float, _offset_y: float) -> None:
            width = self.get_width()
            if width <= 0:
                return
            dx = offset_x - drag_last["x"]
            drag_last["x"] = offset_x
            self._view_end = pan_view_end(self._view_end, dx, self._window_seconds, width)
            self._clamp_to_live()
            self.queue_draw()

        def on_drag_end(_gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
            moved = abs(offset_x) >= _DRAG_CLICK_THRESHOLD or abs(offset_y) >= _DRAG_CLICK_THRESHOLD
            width = self.get_width()
            if moved or self._seek_callback is None or width <= 0:
                return
            start = self._view_end - self._window_seconds
            timestamp = start + (drag_start["x"] / width) * self._window_seconds
            self._seek_callback(timestamp)

        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", on_drag_begin)
        drag.connect("drag-update", on_drag_update)
        drag.connect("drag-end", on_drag_end)
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

    def _draw(self, _area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int) -> None:
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

        # Placeholder recording-presence bar — grey while showing a
        # History position instead of "now" (see _HISTORY_PRESENCE_COLOR).
        presence_y = _EVENT_MARKER_HEIGHT
        presence_color = _HISTORY_PRESENCE_COLOR if self._history_position is not None else accent
        cr.set_source_rgba(*presence_color, 0.5)
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

        # Ruler ticks + labels — blue while following "now", the same
        # signal the toolbar's own real-time clock and the "Live" label
        # give (see _LIVE_TICK_COLOR); plain otherwise, while showing a
        # History position instead.
        ruler_y = _EVENT_MARKER_HEIGHT + _PRESENCE_HEIGHT
        cr.set_source_rgb(*(fg if self._history_position is not None else _LIVE_TICK_COLOR))
        cr.set_line_width(1)
        step = self._pick_tick_step(width)
        first_tick = int(start // step) * step
        t = first_tick
        while t <= end:
            tx = x_for(t)
            cr.move_to(tx, ruler_y)
            cr.line_to(tx, ruler_y + 6)
            cr.stroke()
            label = (
                datetime.fromtimestamp(t, tz=timezone.utc)
                .astimezone()
                .strftime("%H:%M" if step < 86400 else "%m-%d")
            )
            extents = cr.text_extents(label)
            cr.move_to(tx - extents.width / 2 - extents.x_bearing, ruler_y + 18)
            cr.show_text(label)
            t += step

        # Playback marker: the focus slot's History position when it has
        # one, otherwise "now" — the same blue line either way, since
        # both mean the same thing, "this is what that slot is currently
        # showing". A History position is LiveView's own controlled
        # value (ticked once a second, not free-running) so it needs
        # none of the "now" inset trick below; plain x_for is exact.
        if self._history_position is not None:
            marker_x = x_for(self._history_position)
        elif self._following:
            # Draw a couple pixels in from the right edge rather than via
            # x_for(now) — "now" is resampled fresh on every redraw and is
            # always a hair ahead of the _view_end snapshot x_for is built
            # from, which would push it just past the edge and hide it.
            # The inset also keeps the full stroke width on-canvas: Cairo
            # strokes are centered on the path, so a line placed exactly
            # at x=width would have half its width clipped off.
            marker_x = width - _NOW_MARKER_WIDTH / 2
        else:
            # Once panned, x_for(now) is correct (and may legitimately be
            # off-screen, which is fine — no marker shown until the live
            # edge scrolls back into view).
            marker_x = x_for(now)
        if 0 <= marker_x <= width:
            cr.set_source_rgb(*accent)
            cr.set_line_width(_NOW_MARKER_WIDTH)
            cr.move_to(marker_x, 0)
            cr.line_to(marker_x, height)
            cr.stroke()


class Timeline(Gtk.Box):
    """Shared timeline strip mounted below the Live View grid.

    The current-time label, the canvas ruler, the zoom buttons, and
    click-to-seek/Live/+-10s/Pause/speed (see canvas.set_seek_callback/
    live_btn/back_10s_btn/forward_10s_btn/pause_btn/set_speed_callback)
    are live; the event-jump buttons are still placeholders with no
    behavior wired up yet.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("timeline")
        self._speed_callback: Callable[[str], None] | None = None
        self._reverse_callback: Callable[[bool], None] | None = None
        # Set while set_speed()/set_reverse() is driving a widget's
        # state itself (LiveView syncing the display, e.g. resetting
        # to 1x/Fwd on returning to Live) rather than the user
        # interacting with it -- _on_speed_radio_toggled/
        # _on_direction_toggled check this so that path never
        # re-invokes _speed_callback/_reverse_callback for a change
        # LiveView already knows about.
        self._suppress_playback_callback = False

        self.canvas = TimelineCanvas()
        self.canvas.set_margin_start(8)
        self.canvas.set_margin_end(8)
        self.append(self._build_toolbar())
        self.append(self.canvas)
        self.set_history_active(False)  # nothing to return to yet

        self._clock_id = GLib.timeout_add(1000, self._update_clock)
        self.connect("unrealize", self._on_unrealize)
        self._update_clock()

    def _on_unrealize(self, _widget: Gtk.Widget) -> None:
        if self._clock_id:
            GLib.source_remove(self._clock_id)
            self._clock_id = 0

    def set_history_active(self, active: bool) -> None:
        """Reflect whether any slot is currently playing recorded video
        rather than live — LiveView calls this after every seek and
        every return-to-live, since none of the widgets below have a
        way to know that on their own (they only ever emit "clicked",
        same division of responsibility as the seek/hover callbacks).

        Opacity plus set_sensitive rather than set_visible throughout:
        there's nothing for Next event/Forward 10s/Live to do while
        already live (nothing is "ahead" of live), but they still have
        to hold their layout space, or every button after them in the
        toolbar would shift each time this toggles. The "Live Stream"
        label swaps in for that same reason, rather than being shown
        alongside a hidden Live button.
        """
        self._history_only_box.set_sensitive(active)
        self._history_only_box.set_opacity(1.0 if active else 0.0)
        self._live_stream_label.set_opacity(0.0 if active else 1.0)
        self._speed_btn.set_sensitive(active)
        self._speed_btn.set_opacity(1.0 if active else 0.0)

    def set_paused(self, paused: bool) -> None:
        """Swap pause_btn's icon/tooltip to reflect LiveView's own
        paused state -- same division of responsibility as
        set_history_active: this widget only ever emits "clicked" and
        is told afterwards what that meant."""
        self.pause_btn.set_icon_name(
            "media-playback-start-symbolic" if paused else "media-playback-pause-symbolic"
        )
        self.pause_btn.set_tooltip_text("Play" if paused else "Pause")

    def set_speed_callback(self, callback: Callable[[str], None]) -> None:
        self._speed_callback = callback

    def set_reverse_callback(self, callback: Callable[[bool], None]) -> None:
        self._reverse_callback = callback

    def set_speed(self, value: str) -> None:
        """Reflect LiveView's own current speed (e.g. resetting the
        display to 1x on returning to Live) -- same division of
        responsibility as set_paused: this widget only ever emits a
        value on user selection, and is told separately what to show
        otherwise. Suppresses _speed_callback for the change this
        causes (see self._suppress_playback_callback's own comment), so
        LiveView syncing the display never talks back to itself."""
        radio = self._speed_radios.get(value)
        if radio is None or radio.get_active():
            return
        self._suppress_playback_callback = True
        radio.set_active(True)
        self._suppress_playback_callback = False

    def set_reverse(self, reverse: bool) -> None:
        """Reflect LiveView's own current direction (e.g. resetting to
        Fwd on returning to Live) -- same division of responsibility
        as set_speed, including suppressing _reverse_callback for the
        change this causes."""
        target = self._reverse_btn if reverse else self._forward_btn
        if target.get_active():
            return
        self._suppress_playback_callback = True
        target.set_active(True)
        self._suppress_playback_callback = False

    def _build_speed_popover(self) -> Gtk.Popover:
        """Radio-button popover for _speed_btn -- same shape as
        HeaderBar's own theme popover, plus a Fwd/Rev button pair at
        the top (see _on_direction_toggled) rather than a second
        dropdown or toolbar button, since direction only ever matters
        alongside a speed choice."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        # A grouped ToggleButton pair, same radio-group technique as
        # the speed choices below -- icon buttons matching the
        # transport cluster's own style, reading directly as "which of
        # these two" rather than needing a separate on/off indicator.
        reverse_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        reverse_row.set_halign(Gtk.Align.CENTER)
        self._reverse_btn = Gtk.ToggleButton()
        self._reverse_btn.set_child(history_direction_icon(reverse=True, size=_TOOLBAR_ICON_SIZE))
        self._reverse_btn.set_tooltip_text("Play backward")
        self._reverse_btn.connect("toggled", self._on_direction_toggled, True)
        reverse_row.append(self._reverse_btn)
        self._forward_btn = Gtk.ToggleButton()
        self._forward_btn.set_group(self._reverse_btn)
        self._forward_btn.set_child(history_direction_icon(reverse=False, size=_TOOLBAR_ICON_SIZE))
        self._forward_btn.set_tooltip_text("Play forward")
        self._forward_btn.set_active(True)
        self._forward_btn.connect("toggled", self._on_direction_toggled, False)
        reverse_row.append(self._forward_btn)
        box.append(reverse_row)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        group: Gtk.CheckButton | None = None
        for value, label in _SPEED_OPTIONS:
            radio = Gtk.CheckButton(label=label)
            if group is not None:
                radio.set_group(group)
            else:
                group = radio
            if value == "1":
                radio.set_active(True)
            radio.connect("toggled", self._on_speed_radio_toggled, value)
            self._speed_radios[value] = radio
            box.append(radio)
        popover = Gtk.Popover()
        popover.set_child(box)
        return popover

    def _on_speed_radio_toggled(self, radio: Gtk.CheckButton, value: str) -> None:
        if not radio.get_active():
            return
        self._speed_btn.set_label(_SPEED_LABELS[value])
        if self._speed_callback is not None and not self._suppress_playback_callback:
            self._speed_callback(value)

    def _on_direction_toggled(self, btn: Gtk.ToggleButton, reverse: bool) -> None:
        if not btn.get_active():
            return
        if self._reverse_callback is not None and not self._suppress_playback_callback:
            self._reverse_callback(reverse)

    def _update_clock(self) -> bool:
        now = datetime.now()
        self._time_label.set_label(now.strftime("%H:%M:%S"))
        self._date_label.set_label(now.strftime("%Y-%m-%d %a").upper())
        return True  # continue ticking

    def _build_toolbar(self) -> Gtk.Box:
        """Single row above the canvas: everything lives here to save
        the vertical space a separate header+footer would cost.

        Layout, left to right: clock, a fixed one-button gap, the
        filter/download/calendar/zoom/speed cluster, an expanding gap,
        then the transport cluster flush against the right edge. Every
        mode-dependent widget in the transport cluster stays mounted at
        all times and is hidden via opacity + set_sensitive rather than
        set_visible (see set_history_active), so nothing else in the
        toolbar shifts when switching between Live and History mode.
        """
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.add_css_class("timeline-toolbar")

        # Always blue, live or not -- it's a real-time clock regardless
        # of what the ruler below is showing, matching DSM's own web app.
        time_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._time_label = Gtk.Label(label="00:00:00", xalign=0)
        self._time_label.add_css_class("timeline-clock")
        self._time_label.add_css_class("timeline-live-text")
        self._date_label = Gtk.Label(label="", xalign=0)
        self._date_label.add_css_class("timeline-live-text")
        time_box.append(self._time_label)
        time_box.append(self._date_label)

        button_cluster = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        filter_btn = Gtk.Button()
        filter_btn.set_child(filter_icon(size=_TOOLBAR_ICON_SIZE))
        filter_btn.set_tooltip_text("Filter events")
        button_cluster.append(filter_btn)

        download_btn = Gtk.Button()
        download_btn.set_icon_name("document-save-symbolic")
        download_btn.set_tooltip_text("Download")
        button_cluster.append(download_btn)

        calendar_btn = Gtk.Button()
        calendar_btn.set_icon_name("x-office-calendar-symbolic")
        calendar_btn.set_tooltip_text("Jump to date/time")
        button_cluster.append(calendar_btn)

        # Same zoom_at() the canvas's own scroll-wheel handler uses, just
        # centered on the canvas midpoint since a button click has no
        # cursor position of its own to zoom toward.
        zoom_out_btn = Gtk.Button()
        zoom_out_btn.set_child(magnifier_zoom_icon(zoom_in=False, size=_TOOLBAR_ICON_SIZE))
        zoom_out_btn.set_tooltip_text("Zoom out timeline")
        zoom_out_btn.connect(
            "clicked", lambda _btn: self.canvas.zoom_at(-_ZOOM_STEP, self.canvas.get_width() / 2)
        )
        button_cluster.append(zoom_out_btn)

        zoom_in_btn = Gtk.Button()
        zoom_in_btn.set_child(magnifier_zoom_icon(zoom_in=True, size=_TOOLBAR_ICON_SIZE))
        zoom_in_btn.set_tooltip_text("Zoom in timeline")
        zoom_in_btn.connect(
            "clicked", lambda _btn: self.canvas.zoom_at(_ZOOM_STEP, self.canvas.get_width() / 2)
        )
        button_cluster.append(zoom_in_btn)

        # Playback speed only means anything once there's a History
        # position to play back from, so it's opacity/sensitivity-
        # toggled by set_history_active like the transport cluster's
        # own History-only widgets. A dropdown rather than the +/-
        # stepper this replaced: DSM only supports a handful of fixed
        # multipliers, and stepping through all of them one at a time
        # to reach the last is exactly the annoyance a dropdown avoids.
        self._speed_btn = Gtk.MenuButton(label="1x")
        self._speed_btn.set_tooltip_text("Playback speed")
        self._speed_radios: dict[str, Gtk.CheckButton] = {}
        self._speed_btn.set_popover(self._build_speed_popover())
        button_cluster.append(self._speed_btn)

        toolbar.append(time_box)
        toolbar.append(self._make_fixed_gap_spacer())
        toolbar.append(button_cluster)
        # Soaks up whatever space is left, pushing the transport
        # cluster flush against the toolbar's right edge — the floor
        # keeps it from crowding button_cluster in a narrow window.
        toolbar.append(self._make_min_gap_spacer())

        # Back 10s/Previous event/Pause stay live in both modes: Back
        # 10s and Previous event drop a live slot into History mode
        # first (LiveView's job -- this widget only ever emits the
        # click, same division of responsibility as the seek/hover
        # callbacks), but Pause deliberately doesn't -- pausing a live
        # slot freezes it in place without leaving Live mode at all
        # (see LiveView._pause_all_slots).
        transport = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        # Public (like live_btn/canvas): LiveView owns what "10s back"
        # means for each slot, the same division of responsibility as
        # the seek/hover/Live callbacks.
        self.back_10s_btn = Gtk.Button()
        self.back_10s_btn.set_icon_name("media-seek-backward-symbolic")
        self.back_10s_btn.set_tooltip_text("Back 10s")
        transport.append(self.back_10s_btn)

        prev_event_btn = Gtk.Button()
        prev_event_btn.set_icon_name("go-previous-symbolic")
        prev_event_btn.set_tooltip_text("Previous event")
        transport.append(prev_event_btn)

        # Public (like back_10s_btn/live_btn): LiveView owns what
        # pausing/resuming means for each slot, the same division of
        # responsibility as the seek/hover/Live callbacks. Icon/tooltip
        # toggled by set_paused, not by this widget deciding on its own
        # what a click meant.
        self.pause_btn = Gtk.Button()
        self.pause_btn.set_icon_name("media-playback-pause-symbolic")
        self.pause_btn.set_tooltip_text("Pause")
        transport.append(self.pause_btn)

        # Next event/Forward 10s/Live: nothing is "ahead" of live, so
        # these only make sense in History mode. Grouped in their own
        # box so set_history_active can toggle all three as one unit.
        self._history_only_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        next_event_btn = Gtk.Button()
        next_event_btn.set_icon_name("go-next-symbolic")
        next_event_btn.set_tooltip_text("Next event")
        self._history_only_box.append(next_event_btn)

        self.forward_10s_btn = Gtk.Button()
        self.forward_10s_btn.set_icon_name("media-seek-forward-symbolic")
        self.forward_10s_btn.set_tooltip_text("Forward 10s")
        self._history_only_box.append(self.forward_10s_btn)

        # Public (like self.canvas): LiveView owns what "return to live"
        # means for each slot, the same division of responsibility as
        # the seek/hover callbacks -- this widget only ever knows about
        # positions and timestamps, never cameras or streams.
        self.live_btn = Gtk.Button(label="Live")
        self.live_btn.set_tooltip_text("Return to live view")
        self.live_btn.add_css_class("timeline-live-active")
        self._history_only_box.append(self.live_btn)

        # Overlay, not a plain sibling: while live, "Live Stream" has to
        # read as centered across all three History-only buttons'
        # combined width (see set_history_active) rather than just
        # sitting in a slot sized to one of them -- and an overlay
        # child never grows the Overlay's own size, so the button
        # cluster is what sets that width, not the label.
        live_slot = Gtk.Overlay()
        live_slot.set_child(self._history_only_box)
        self._live_stream_label = Gtk.Label(label="Live Stream")
        self._live_stream_label.add_css_class("timeline-live-text")
        self._live_stream_label.set_halign(Gtk.Align.CENTER)
        self._live_stream_label.set_valign(Gtk.Align.CENTER)
        # Overlay children stay hit-testable at any opacity -- without
        # this, the label (centered across the full History-only span)
        # eats clicks meant for whichever button it happens to sit over
        # even while invisible at opacity 0.
        self._live_stream_label.set_can_target(False)
        live_slot.add_overlay(self._live_stream_label)
        transport.append(live_slot)

        toolbar.append(transport)

        return toolbar

    @staticmethod
    def _make_fixed_gap_spacer() -> Gtk.Box:
        """A non-expanding gap the width of one toolbar button, so the
        button cluster doesn't crowd the clock — fixed rather than the
        expanding floor _make_min_gap_spacer uses, since there's only
        one expanding gap in this toolbar and it belongs on the other
        side of the button cluster."""
        spacer = Gtk.Box()
        spacer.set_size_request(_MIN_BUTTON_GAP_PX, -1)
        return spacer

    @staticmethod
    def _make_min_gap_spacer() -> Gtk.Box:
        spacer = Gtk.Box(hexpand=True)
        spacer.set_size_request(_MIN_BUTTON_GAP_PX, -1)
        return spacer
