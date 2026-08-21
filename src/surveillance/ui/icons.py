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

"""Shared composite-icon helpers.

Some glyphs this app needs (a magnifying glass with +/-, a 4-way pan
arrow, a filter funnel) aren't shipped by symbolic icon themes under
any single name, so these stack two symbolic icons on one square
canvas instead.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

ICON_SIZE = 24


def icon_overlay(size: int, *icons: tuple[str, int, int]) -> Gtk.Overlay:
    """Stack symbolic icons centered on a `size`-pixel square.

    Each entry is (name, pixel size, offset); *offset* shifts the icon
    up and left. A Gtk.Overlay takes its size from its main child, so a
    plain box provides the square and every icon is an overlay child.
    """
    overlay = Gtk.Overlay()
    canvas = Gtk.Box()
    canvas.set_size_request(size, size)
    overlay.set_child(canvas)
    for name, pixel_size, offset in icons:
        image = Gtk.Image.new_from_icon_name(name)
        image.set_pixel_size(pixel_size)
        image.set_halign(Gtk.Align.CENTER)
        image.set_valign(Gtk.Align.CENTER)
        image.set_margin_end(offset)
        image.set_margin_bottom(offset)
        overlay.add_overlay(image)
    return overlay


def magnifier_zoom_icon(zoom_in: bool, size: int = ICON_SIZE) -> Gtk.Overlay:
    """Magnifying glass with a +/- badge, used for every zoom control.

    system-search-symbolic's lens circle is centered at (6.5, 6.5) in
    its 16x16 viewBox, not (8, 8) — the handle sticking out to the
    bottom-right pulls the icon's overall bounding box off from the
    circle's true center. Nudge the badge up-left to compensate (a
    margin shifts a centered widget by half its amount, so use 2x the
    (0.5 - 6.5/16) offset fraction).
    """
    badge_offset = round(size * (0.5 - 6.5 / 16) * 2)
    badge_icon = "list-add-symbolic" if zoom_in else "list-remove-symbolic"
    return icon_overlay(
        size,
        ("system-search-symbolic", size, 0),
        (badge_icon, size // 3, badge_offset),
    )


def filter_icon(size: int = ICON_SIZE) -> Gtk.Overlay:
    """Funnel glyph for the timeline's "filter events" button.

    No icon theme ships one: pan-down-symbolic's solid downward
    triangle -- the same shape the playback-speed dropdown's own arrow
    uses -- reads as the funnel's wide mouth, and list-remove-symbolic
    (already reused above as a generic mark rather than its literal
    "remove" meaning) rotated vertical via style.css's .rotate-ccw-90
    becomes the spout.
    """
    overlay = Gtk.Overlay()
    canvas = Gtk.Box()
    canvas.set_size_request(size, size)
    overlay.set_child(canvas)

    mouth = Gtk.Image.new_from_icon_name("pan-down-symbolic")
    mouth.set_pixel_size(size)
    mouth.set_halign(Gtk.Align.CENTER)
    mouth.set_valign(Gtk.Align.CENTER)
    mouth.set_margin_bottom(round(size * 0.25))
    overlay.add_overlay(mouth)

    spout = Gtk.Image.new_from_icon_name("list-remove-symbolic")
    spout.set_pixel_size(round(size * 0.55))
    spout.add_css_class("rotate-ccw-90")
    spout.set_halign(Gtk.Align.CENTER)
    spout.set_valign(Gtk.Align.CENTER)
    spout.set_margin_top(round(size * 0.35))
    overlay.add_overlay(spout)

    return overlay


def history_direction_icon(reverse: bool, size: int = ICON_SIZE) -> Gtk.Overlay:
    """Looping-arrow + play-triangle glyph for the Live View timeline's
    Fwd/Rev playback-direction buttons.

    No stock icon reads as "play backward"/"play forward on a loop",
    so stack view-refresh-symbolic's loop arrow with
    media-playback-start-symbolic's triangle. The reverse variant is
    the same two glyphs mirrored via style.css's .flip-horizontal
    rather than separately drawn -- both read as "play" only in the
    direction their triangle points, so mirroring the whole icon
    (loop arrow included) reads as the opposite direction rather than
    just an oddly-aimed triangle.
    """
    overlay = Gtk.Overlay()
    canvas = Gtk.Box()
    canvas.set_size_request(size, size)
    overlay.set_child(canvas)

    loop = Gtk.Image.new_from_icon_name("view-refresh-symbolic")
    loop.set_pixel_size(size)
    loop.set_halign(Gtk.Align.CENTER)
    loop.set_valign(Gtk.Align.CENTER)
    if reverse:
        loop.add_css_class("flip-horizontal")
    overlay.add_overlay(loop)

    triangle = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
    triangle.set_pixel_size(round(size * 0.5))
    triangle.set_halign(Gtk.Align.CENTER)
    triangle.set_valign(Gtk.Align.CENTER)
    if reverse:
        triangle.add_css_class("flip-horizontal")
    overlay.add_overlay(triangle)

    return overlay


def pan_tilt_icon(size: int = ICON_SIZE) -> Gtk.Overlay:
    """4-way arrow for PTZ pan/tilt controls.

    No single stock icon reads as "pan/tilt", so overlay the left-right
    and up-down arrow glyphs into a 4-way arrow instead of picking an
    unrelated one (e.g. a gamepad).
    """
    return icon_overlay(
        size,
        ("object-flip-horizontal-symbolic", size, 0),
        ("object-flip-vertical-symbolic", size, 0),
    )
