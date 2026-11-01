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

"""Maps page: Surveillance Station E-Maps with their cameras on them.

Each camera marker is a dot coloured by the camera's status in the
sidebar's camera list; hovering one names the camera, and clicking it
puts the camera in Live View the way a click in the sidebar does.

With Edit on, markers are dragged into place, added from the cameras not
yet on the map and removed with a right click, then saved as one list;
maps are created from an image file, given another image, or deleted.
The editing widgets and the working copy live in emaps_edit.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from pathlib import PurePath
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")

import cairo
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # type: ignore[import-untyped]

from surveillance.api.auth import SessionExpiredError
from surveillance.api.models import Camera, CameraStatus, EMap, EMapItem
from surveillance.services.emap import (
    DuplicateMapNameError,
    EMapService,
    EMapsTooOldError,
    StaleMapError,
    emaps_read_only,
    emaps_unavailable,
    marker_to_pixels,
)
from surveillance.ui.emaps_edit import (
    READ_ONLY_MESSAGE,
    EMapEditBar,
    MapEdits,
    NewMapDialog,
    ask_unsaved,
    confirm,
    map_name_from_file,
    marker_menu,
    pick_image,
    rasterise_to_png,
    show_error,
)
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)

_MARKER_RADIUS = 7.0
# A marker being dragged is drawn this big, so it shows through the pointer.
_DRAG_RADIUS = 10.0
# How far from a marker's centre the pointer still counts as on it.
_HIT_RADIUS = 12.0

_COLOUR_ENABLED = (0.20, 0.70, 0.33)
_COLOUR_OFFLINE = (0.85, 0.27, 0.22)
# A camera the sidebar's list does not hold: deleted, or not visible to
# this account.
_COLOUR_UNKNOWN = (0.55, 0.55, 0.55)

# Side of the blank square drawn when a map has no image that decodes
# and the NAS gave no size for it either.
_PLACEHOLDER_SIZE = 1000


def fit_image(
    image_width: float, image_height: float, area_width: float, area_height: float
) -> tuple[float, float, float]:
    """Where an image is drawn to fit an area with its aspect kept, centred:
    (x offset, y offset, scale)."""
    if image_width <= 0 or image_height <= 0 or area_width <= 0 or area_height <= 0:
        return 0.0, 0.0, 0.0
    scale = min(area_width / image_width, area_height / image_height)
    return (
        (area_width - image_width * scale) / 2,
        (area_height - image_height * scale) / 2,
        scale,
    )


def marker_to_widget(
    lng: float,
    lat: float,
    image_width: float,
    image_height: float,
    area_width: float,
    area_height: float,
) -> tuple[float, float]:
    """A marker's position in an area its map image is fitted into."""
    x_off, y_off, scale = fit_image(image_width, image_height, area_width, area_height)
    x, y = marker_to_pixels(lng, lat, image_height)
    return x_off + x * scale, y_off + y * scale


def widget_to_image(
    wx: float,
    wy: float,
    image_width: float,
    image_height: float,
    area_width: float,
    area_height: float,
) -> tuple[float, float]:
    """The image pixel under a point of the area the image is fitted into:
    the inverse of marker_to_widget, not clamped to the image."""
    x_off, y_off, scale = fit_image(image_width, image_height, area_width, area_height)
    if scale <= 0:
        return 0.0, 0.0
    return (wx - x_off) / scale, (wy - y_off) / scale


def marker_at(points: list[tuple[float, float]], x: float, y: float, radius: float) -> int | None:
    """Index of the point nearest (x, y) within *radius*, None if none is."""
    best: int | None = None
    best_distance = radius
    for index, (px, py) in enumerate(points):
        distance = math.hypot(px - x, py - y)
        if distance <= best_distance:
            best, best_distance = index, distance
    return best


def marker_colour(camera: Camera | None) -> tuple[float, float, float]:
    """Enabled cameras green, disconnected or disabled ones red, cameras
    missing from the list grey."""
    if camera is None:
        return _COLOUR_UNKNOWN
    return _COLOUR_ENABLED if camera.status == CameraStatus.ENABLED else _COLOUR_OFFLINE


def _decode_image(content: bytes) -> GdkPixbuf.Pixbuf | None:
    """The map image, None when it does not decode. GdkPixbuf reads SVG as
    well as PNG and JPEG, where the SVG loader is installed."""
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(content)
        loader.close()
    except GLib.Error as e:
        log.warning("E-Map image could not be decoded: %s", e)
        return None
    pixbuf: GdkPixbuf.Pixbuf | None = loader.get_pixbuf()
    return pixbuf


def _upload_name(filename: str) -> str:
    """The name an image is uploaded under: the chosen file's, as PNG."""
    return f"{PurePath(filename).stem or 'map'}.png"


class EMapsView(Gtk.Box):
    """List of E-Maps on the left, the selected one on the right."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.window = window
        self.app = window.app
        self._service = EMapService(self.app.api) if self.app.api else None
        self._listed = False
        self._unavailable = False
        # Set once an edit came back with 105: the account views, only.
        self._read_only = False
        self._emap: EMap | None = None
        self._pixbuf: GdkPixbuf.Pixbuf | None = None
        # Bumped on every map pick, so a slow answer for a map picked
        # earlier cannot replace the one picked since.
        self._request = 0
        self._redraw_id = 0
        self._filling_list = False
        self._current_row: Gtk.ListBoxRow | None = None
        # Editing: the working copy while Edit is on, the marker being
        # dragged and where the pointer took hold of it, and whether a
        # save, upload or delete is in flight.
        self._edits: MapEdits | None = None
        self._drag_index: int | None = None
        self._drag_grip = (0.0, 0.0)
        self._busy = False
        self._toggling = False

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_hexpand(True)
        paned.set_position(220)
        self.append(paned)

        # Left: the map list
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_top(8)
        header.set_margin_bottom(8)
        header.set_margin_start(8)
        header.set_margin_end(8)
        title = Gtk.Label(label="E-Maps")
        title.add_css_class("title-4")
        title.set_hexpand(True)
        title.set_xalign(0)
        header.append(title)
        self._new_btn = Gtk.Button()
        self._new_btn.set_icon_name("list-add-symbolic")
        self._new_btn.set_tooltip_text("New map…")
        self._new_btn.set_sensitive(False)
        self._new_btn.connect("clicked", lambda _: self._on_new_map())
        header.append(self._new_btn)
        self._edit_btn = Gtk.ToggleButton()
        self._edit_btn.set_icon_name("document-edit-symbolic")
        self._edit_btn.set_tooltip_text("Edit this map")
        self._edit_btn.set_sensitive(False)
        self._edit_btn.connect("toggled", self._on_edit_toggled)
        header.append(self._edit_btn)
        self._refresh_btn = Gtk.Button()
        self._refresh_btn.set_icon_name("view-refresh-symbolic")
        self._refresh_btn.set_tooltip_text("Refresh")
        self._refresh_btn.connect("clicked", lambda _: self._settle_edits(self._refresh))
        header.append(self._refresh_btn)
        left.append(header)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-selected", self._on_row_selected)
        scroll.set_child(self.listbox)
        left.append(scroll)
        paned.set_start_child(left)
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(False)

        # Right: the map, or a message in its place
        self._content = Gtk.Stack()
        self._content.set_hexpand(True)
        self._content.set_vexpand(True)

        self._message = Gtk.Label(label="Loading E-Maps…")
        self._message.add_css_class("dim-label")
        self._message.set_wrap(True)
        self._message.set_justify(Gtk.Justification.CENTER)
        self._content.add_named(self._message, "message")

        map_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.area = Gtk.DrawingArea()
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.set_draw_func(self._draw)
        self.area.set_has_tooltip(True)
        self.area.connect("query-tooltip", self._on_query_tooltip)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.area.add_controller(motion)
        click = Gtk.GestureClick(button=1)
        click.connect("released", self._on_click)
        self.area.add_controller(click)
        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.area.add_controller(drag)
        right_click = Gtk.GestureClick(button=3)
        right_click.connect("pressed", self._on_right_click)
        self.area.add_controller(right_click)
        map_box.append(self.area)
        self._edit_bar = EMapEditBar(
            unplaced_cameras=self._unplaced_cameras,
            on_add_camera=self._on_add_camera,
            on_change_image=self._on_change_image,
            on_delete_map=self._on_delete_map,
            on_discard=self._on_discard,
            on_save=self._on_save,
        )
        self._edit_bar.set_visible(False)
        map_box.append(self._edit_bar)
        self._image_note = Gtk.Label(
            label="The map image could not be shown; its cameras are placed on a blank map."
        )
        self._image_note.add_css_class("dim-label")
        self._image_note.add_css_class("caption")
        self._image_note.set_margin_top(4)
        self._image_note.set_margin_bottom(4)
        self._image_note.set_visible(False)
        map_box.append(self._image_note)
        self._edit_note = Gtk.Label(label=READ_ONLY_MESSAGE)
        self._edit_note.add_css_class("dim-label")
        self._edit_note.add_css_class("caption")
        self._edit_note.set_margin_top(4)
        self._edit_note.set_margin_bottom(4)
        self._edit_note.set_visible(False)
        map_box.append(self._edit_note)
        self._content.add_named(map_box, "map")

        paned.set_end_child(self._content)
        paned.set_shrink_end_child(False)

        # Loaded the first time the page is on screen, not at login: a NAS
        # without E-Maps then costs nothing to users who never open it.
        self.connect("map", self._on_mapped)
        self.connect("unmap", self._on_unmapped)

    # -- loading -------------------------------------------------------

    def _on_mapped(self, _widget: Gtk.Widget) -> None:
        if not self._listed:
            self._load_list()
        else:
            self.area.queue_draw()
        # Camera status changes arrive with the sidebar's poll; redraw on
        # the same beat while the page is shown.
        if not self._redraw_id:
            self._redraw_id = GLib.timeout_add_seconds(
                self.app.config.poll_interval_cameras, self._on_redraw_tick
            )

    def _on_unmapped(self, _widget: Gtk.Widget) -> None:
        if self._redraw_id:
            GLib.source_remove(self._redraw_id)
            self._redraw_id = 0

    def _on_redraw_tick(self) -> bool:
        self.area.queue_draw()
        return True

    def _refresh(self) -> None:
        if self._service is not None:
            self._service.clear()
        self._load_list()

    def _load_list(self) -> None:
        service = self._service
        if service is None or self._unavailable:
            return
        self._listed = True
        self._refresh_btn.set_sensitive(False)
        run_async(
            service.list_maps(),
            callback=lambda maps: self._on_list_loaded(service, maps),
            error_callback=self._on_load_error,
        )

    def _on_list_loaded(self, service: EMapService, maps: list[EMap]) -> None:
        if self.app.api is not service.api:
            return
        self._refresh_btn.set_sensitive(True)
        self._current_row = None
        self._filling_list = True
        try:
            while (row := self.listbox.get_row_at_index(0)) is not None:
                self.listbox.remove(row)
            for emap in sorted(maps, key=lambda m: (m.name.casefold(), m.id)):
                self.listbox.append(self._create_row(emap))
        finally:
            self._filling_list = False
        self._update_edit_controls()

        if not maps:
            self._emap = None
            self._end_edit()
            self._show_message("No E-Maps have been set up in Surveillance Station.")
            return
        rows = [self.listbox.get_row_at_index(i) for i in range(len(maps))]
        wanted = self.app.config.last_emap_id
        row = next((r for r in rows if r.emap.id == wanted), rows[0])  # type: ignore[union-attr]
        self.listbox.select_row(row)

    def _create_row(self, emap: EMap) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        label = Gtk.Label(label=emap.name or f"E-Map {emap.id}")
        label.set_xalign(0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_margin_top(6)
        label.set_margin_bottom(6)
        label.set_margin_start(8)
        label.set_margin_end(8)
        row.set_child(label)
        row.emap = emap  # type: ignore[attr-defined]
        return row

    def _map_names(self) -> list[str]:
        names: list[str] = []
        index = 0
        while (row := self.listbox.get_row_at_index(index)) is not None:
            names.append(row.emap.name)  # type: ignore[attr-defined]
            index += 1
        return names

    def _on_row_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None or self._filling_list or row is self._current_row:
            return
        if self._edits is not None and self._edits.dirty:
            # The list has moved already; put it back, ask, then move it
            # again once the edits are saved or dropped.
            previous = self._current_row
            self._select_quietly(previous)
            self._settle_edits(lambda: self.listbox.select_row(row))
            return
        self._current_row = row
        emap: EMap = row.emap  # type: ignore[attr-defined]
        if self.app.config.last_emap_id != emap.id:
            self.app.config.last_emap_id = emap.id
            self._save_config()
        self._load_map(emap.id)

    def _select_quietly(self, row: Gtk.ListBoxRow | None) -> None:
        """Move the list's selection without loading anything."""
        self._filling_list = True
        try:
            if row is None:
                self.listbox.unselect_all()
            else:
                self.listbox.select_row(row)
        finally:
            self._filling_list = False

    def _save_config(self) -> None:
        from surveillance.config import save_config

        save_config(self.app.config)

    def _load_map(self, emap_id: int) -> None:
        service = self._service
        if service is None:
            return
        self._request += 1
        request = self._request
        self._edits = None
        self._drag_index = None
        self._edit_bar.set_visible(False)
        self._show_message("Loading the map…")

        def _loaded(result: tuple[EMap | None, bytes | None]) -> None:
            if request != self._request or self.app.api is not service.api:
                return
            emap, content = result
            if emap is None:
                self._emap = None
                self._update_edit_controls()
                self._show_message("This E-Map no longer exists. Refresh the list.")
                return
            self._show_map(emap, _decode_image(content) if content else None)

        def _failed(e: Exception) -> None:
            if request == self._request:
                self._on_load_error(e)

        run_async(service.load_map_with_image(emap_id), callback=_loaded, error_callback=_failed)

    def _reload_current(self) -> None:
        if self._emap is not None:
            self._load_map(self._emap.id)

    def _on_load_error(self, error: Exception) -> None:
        self._refresh_btn.set_sensitive(True)
        if emaps_unavailable(error):
            log.info("E-Maps not available: %s", error)
            self._unavailable = True
            self._refresh_btn.set_sensitive(False)
            self.listbox.set_sensitive(False)
            self._emap = None
            self._end_edit()
            self._update_edit_controls()
            if isinstance(error, SessionExpiredError):
                # Not the NAS's E-Maps: the login request() makes for an
                # ended session failed.
                self._show_message(
                    "E-Maps could not be loaded: logging in to the NAS again failed. "
                    "Log out and back in to try again."
                )
            elif isinstance(error, EMapsTooOldError):
                self._show_message(
                    "E-Maps are not available: this Surveillance Station's E-Map "
                    "interface is older than the one the Maps page reads."
                )
            else:
                self._show_message(
                    "E-Maps are not available: this Surveillance Station has none, "
                    "or this account may not view them."
                )
            return
        log.error("Failed to load E-Maps: %s", error)
        self._listed = False
        self._show_message(f"E-Maps could not be loaded: {error}")

    def _show_message(self, text: str) -> None:
        self._message.set_label(text)
        self._content.set_visible_child_name("message")

    def _show_map(self, emap: EMap, pixbuf: GdkPixbuf.Pixbuf | None) -> None:
        self._emap = emap
        self._pixbuf = pixbuf
        self._image_note.set_visible(pixbuf is None)
        self._content.set_visible_child_name("map")
        self._update_edit_controls()
        # Edit stays on across maps and across the reload after a save;
        # the working copy starts over from what the NAS now holds.
        if self._edit_btn.get_active():
            self._begin_edit()
        self.area.queue_draw()

    # -- drawing and pointer -------------------------------------------

    def _image_size(self) -> tuple[float, float]:
        """The size markers are placed against: the decoded image's, else
        the one the NAS reports, else a blank square."""
        if self._pixbuf is not None:
            return float(self._pixbuf.get_width()), float(self._pixbuf.get_height())
        if self._emap is not None and self._emap.width > 0 and self._emap.height > 0:
            return float(self._emap.width), float(self._emap.height)
        # lng is a fraction of the height and can run past 1: widen the
        # square to keep every marker on it.
        widest = max((m.lng for m in self._emap.cameras), default=1.0) if self._emap else 1.0
        height = float(_PLACEHOLDER_SIZE)
        return height * max(1.0, widest + 0.05), height

    def _markers(self) -> list[EMapItem]:
        """The camera markers shown: the working copy's while editing."""
        if self._edits is not None:
            return [item for _, item in self._edits.markers]
        return list(self._emap.cameras) if self._emap is not None else []

    def _marker_points(
        self, area_w: float, area_h: float
    ) -> tuple[list[EMapItem], list[tuple[float, float]]]:
        """The camera markers and where each sits in an area_w x area_h
        drawing area."""
        if self._emap is None:
            return [], []
        width, height = self._image_size()
        markers = self._markers()
        points = [marker_to_widget(m.lng, m.lat, width, height, area_w, area_h) for m in markers]
        return markers, points

    def _cameras_by_id(self) -> dict[int, Camera]:
        return {cam.id: cam for cam in self.window.sidebar.cameras}

    def _draw(self, _area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int) -> None:
        if self._emap is None:
            return
        image_w, image_h = self._image_size()
        x_off, y_off, scale = fit_image(image_w, image_h, width, height)
        if scale <= 0:
            return

        cr.save()
        cr.translate(x_off, y_off)
        cr.scale(scale, scale)
        if self._pixbuf is not None:
            Gdk.cairo_set_source_pixbuf(cr, self._pixbuf, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint()
        else:
            cr.rectangle(0, 0, image_w, image_h)
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.15)
            cr.fill()
        cr.restore()

        cameras = self._cameras_by_id()
        markers, points = self._marker_points(width, height)
        dragged = self._dragged_marker()
        for marker, (x, y) in zip(markers, points, strict=True):
            radius = _DRAG_RADIUS if marker is dragged else _MARKER_RADIUS
            cr.arc(x, y, radius, 0, 2 * math.pi)
            cr.set_source_rgb(*marker_colour(cameras.get(marker.ref_item_id)))
            cr.fill_preserve()
            cr.set_source_rgb(1, 1, 1)
            cr.set_line_width(2)
            cr.stroke()

    def _dragged_marker(self) -> EMapItem | None:
        if self._edits is None or self._drag_index is None:
            return None
        return self._edits.items[self._drag_index]

    def _hit(self, x: float, y: float) -> int | None:
        """Index into _markers() of the marker under (x, y)."""
        _markers, points = self._marker_points(self.area.get_width(), self.area.get_height())
        return marker_at(points, x, y, _HIT_RADIUS)

    def _marker_under(self, x: float, y: float) -> EMapItem | None:
        hit = self._hit(x, y)
        return None if hit is None else self._markers()[hit]

    def _camera_name(self, marker: EMapItem) -> str:
        camera = self._cameras_by_id().get(marker.ref_item_id)
        return camera.name if camera else f"Camera {marker.ref_item_id}"

    def _on_query_tooltip(
        self,
        _area: Gtk.DrawingArea,
        x: int,
        y: int,
        _keyboard: bool,
        tooltip: Gtk.Tooltip,
    ) -> bool:
        marker = self._marker_under(x, y)
        if marker is None:
            return False
        tooltip.set_text(self._camera_name(marker))
        return True

    def _on_motion(self, _controller: Gtk.EventControllerMotion, x: float, y: float) -> None:
        if self._drag_index is not None:
            return
        over = self._marker_under(x, y) is not None
        if not over:
            self.area.set_cursor_from_name(None)
        else:
            self.area.set_cursor_from_name("grab" if self._edits is not None else "pointer")

    def _on_click(self, _gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        # While editing a press on a marker starts a drag, not Live View.
        if n_press != 1 or self._edits is not None:
            return
        marker = self._marker_under(x, y)
        if marker is None:
            return
        camera = self._cameras_by_id().get(marker.ref_item_id)
        if camera is None:
            return
        # Live View first: the window hands a camera pick to the page on
        # screen, and this way it is Live View's, as from the sidebar.
        self.window.show_page("live")
        self.window.on_camera_selected(camera)

    # -- editing: markers ----------------------------------------------

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        if self._edits is None or self._busy:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        hit = self._hit(x, y)
        if hit is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._drag_index = self._edits.markers[hit][0]
        # Where the pointer took hold, relative to the marker's centre,
        # so the marker does not jump under it.
        _markers, points = self._marker_points(self.area.get_width(), self.area.get_height())
        px, py = points[hit]
        self._drag_grip = (px - x, py - y)
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.area.set_cursor_from_name("grabbing")
        self.area.queue_draw()

    def _on_drag_update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._edits is None or self._drag_index is None:
            return
        _ok, sx, sy = gesture.get_start_point()
        gx, gy = self._drag_grip
        image_w, image_h = self._image_size()
        ix, iy = widget_to_image(
            sx + dx + gx,
            sy + dy + gy,
            image_w,
            image_h,
            self.area.get_width(),
            self.area.get_height(),
        )
        self._edits.move(self._drag_index, ix, iy)
        self._update_edit_bar()
        self.area.queue_draw()

    def _on_drag_end(self, _gesture: Gtk.GestureDrag, _dx: float, _dy: float) -> None:
        self._drag_index = None
        self.area.set_cursor_from_name("grab")
        self.area.queue_draw()

    def _on_right_click(
        self, _gesture: Gtk.GestureClick, _n_press: int, x: float, y: float
    ) -> None:
        if self._edits is None or self._busy:
            return
        hit = self._hit(x, y)
        if hit is None:
            return
        _index, item = self._edits.markers[hit]
        marker_menu(self.area, x, y, self._camera_name(item), lambda: self._remove_marker(item))

    def _remove_marker(self, item: EMapItem) -> None:
        if self._edits is None:
            return
        for index, candidate in enumerate(self._edits.items):
            if candidate is item:
                self._edits.remove(index)
                break
        self._update_edit_bar()
        self.area.queue_draw()

    def _on_add_camera(self, camera: Camera) -> None:
        if self._edits is None:
            return
        self._edits.add_camera(camera.id)
        self._update_edit_bar()
        self.area.queue_draw()

    def _unplaced_cameras(self) -> list[Camera]:
        if self._edits is None:
            return []
        return self._edits.unplaced(self.window.sidebar.cameras)

    # -- editing: mode -------------------------------------------------

    def _on_edit_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._toggling:
            return
        if button.get_active():
            if self._emap is None or self._read_only or self._busy:
                self._set_toggle(False)
                return
            self._begin_edit()
        elif self._edits is not None and self._edits.dirty:
            self._set_toggle(True)
            self._settle_edits(self._end_edit)
        else:
            self._end_edit()

    def _set_toggle(self, active: bool) -> None:
        self._toggling = True
        try:
            self._edit_btn.set_active(active)
        finally:
            self._toggling = False

    def _begin_edit(self) -> None:
        """Start, or start over, editing the map on screen."""
        if self._emap is None:
            return
        width, height = self._image_size()
        self._edits = MapEdits(self._emap, width, height)
        self._drag_index = None
        self._set_toggle(True)
        self._edit_bar.set_visible(True)
        self._update_edit_bar()
        self.area.queue_draw()

    def _end_edit(self) -> None:
        self._edits = None
        self._drag_index = None
        self._set_toggle(False)
        self._edit_bar.set_visible(False)
        self.area.set_cursor_from_name(None)
        self.area.queue_draw()

    def _settle_edits(self, then: Callable[[], None]) -> None:
        """Run *then* once unsaved edits are dealt with: at once when there
        are none, else after the user chose to save them, drop them, or
        stay (in which case *then* does not run)."""
        if self._edits is None or not self._edits.dirty or self._emap is None:
            then()
            return

        def _chosen(choice: str) -> None:
            if choice == "discard":
                self._begin_edit()
                then()
            elif choice == "save":
                self._save_markers(then)

        ask_unsaved(self.window, self._emap.name, _chosen)

    def _update_edit_bar(self) -> None:
        if self._edits is not None:
            self._edit_bar.set_state(self._edits.dirty, len(self._edits.markers))

    def _update_edit_controls(self) -> None:
        editable = self._service is not None and not self._unavailable and not self._read_only
        self._new_btn.set_sensitive(editable and not self._busy)
        self._edit_btn.set_sensitive(editable and self._emap is not None and not self._busy)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._edit_bar.set_busy(busy)
        self._update_edit_controls()
        if not busy:
            self._update_edit_bar()

    def _on_discard(self) -> None:
        self._begin_edit()

    def _on_save(self) -> None:
        if self._edits is None or self._emap is None or not self._edits.dirty:
            return
        count = len(self._edits.markers)
        confirm(
            self.window,
            f"Save {count} marker{'s' if count != 1 else ''} to {self._emap.name}?",
            "This replaces the marker list on the NAS.",
            "Save",
            lambda: self._save_markers(None),
        )

    def _save_markers(self, then: Callable[[], None] | None) -> None:
        service, edits, emap = self._service, self._edits, self._emap
        if service is None or edits is None or emap is None:
            return
        self._set_busy(True)

        def _saved(_result: None) -> None:
            if self.app.api is not service.api:
                return
            self._set_busy(False)
            self._reload_current()
            if then is not None:
                then()

        run_async(
            service.save_markers(emap.id, list(edits.items), edits.base_item_ids),
            callback=_saved,
            error_callback=self._on_edit_failed,
        )

    # -- editing: maps -------------------------------------------------

    def _on_new_map(self) -> None:
        self._settle_edits(lambda: pick_image(self.window, self._on_new_map_image))

    def _on_new_map_image(self, filename: str, content: bytes) -> None:
        try:
            png = rasterise_to_png(content)
        except ValueError as e:
            show_error(self.window, "This image cannot be used for a map", str(e))
            return
        NewMapDialog(
            self.window,
            map_name_from_file(filename),
            self._map_names(),
            lambda name: self._create_map(name, _upload_name(filename), png),
        ).present()

    def _create_map(self, name: str, upload_name: str, png: bytes) -> None:
        service = self._service
        if service is None:
            return
        self._set_busy(True)

        async def _create() -> int:
            uploaded = await service.upload_image(png, upload_name)
            return await service.create_map(name, uploaded)

        def _created(new_id: int) -> None:
            if self.app.api is not service.api:
                return
            self._set_busy(False)
            self.app.config.last_emap_id = new_id
            self._save_config()
            self._refresh()

        run_async(_create(), callback=_created, error_callback=self._on_edit_failed)

    def _on_change_image(self) -> None:
        self._settle_edits(lambda: pick_image(self.window, self._on_replacement_image))

    def _on_replacement_image(self, filename: str, content: bytes) -> None:
        service, emap = self._service, self._emap
        if service is None or emap is None:
            return
        try:
            png = rasterise_to_png(content)
        except ValueError as e:
            show_error(self.window, "This image cannot be used for a map", str(e))
            return
        self._set_busy(True)

        async def _replace() -> None:
            uploaded = await service.upload_image(png, _upload_name(filename))
            await service.replace_image(emap.id, uploaded)

        def _replaced(_result: None) -> None:
            if self.app.api is not service.api:
                return
            self._set_busy(False)
            self._reload_current()

        run_async(_replace(), callback=_replaced, error_callback=self._on_edit_failed)

    def _on_delete_map(self) -> None:
        emap = self._emap
        if emap is None:
            return
        confirm(
            self.window,
            f"Delete the E-Map {emap.name}?",
            "The map, its image and its markers are removed from the NAS. This cannot be undone.",
            "Delete",
            lambda: self._delete_map(emap),
        )

    def _delete_map(self, emap: EMap) -> None:
        service = self._service
        if service is None:
            return
        self._set_busy(True)

        def _deleted(_result: None) -> None:
            if self.app.api is not service.api:
                return
            self._set_busy(False)
            self._end_edit()
            self._emap = None
            self._refresh()

        run_async(
            service.delete_map(emap.id), callback=_deleted, error_callback=self._on_edit_failed
        )

    # -- editing: errors -----------------------------------------------

    def _on_edit_failed(self, error: Exception) -> None:
        """An upload, save or delete failed. 105 means the account may only
        view maps, and the page stays read only from then on."""
        self._set_busy(False)
        if emaps_read_only(error):
            log.info("E-Maps are read only for this account: %s", error)
            self._set_read_only()
            self._show_edit_error("The map could not be changed", READ_ONLY_MESSAGE)
            return
        if isinstance(error, StaleMapError):
            log.warning("E-Map save refused: %s", error)
            self._show_edit_error("The map changed on the NAS", str(error))
            self._reload_current()
            return
        if isinstance(error, DuplicateMapNameError):
            self._show_edit_error("That name is taken", str(error))
            return
        log.error("E-Map edit failed: %s", error)
        self._show_edit_error("The map could not be changed", str(error))

    def _set_read_only(self) -> None:
        self._read_only = True
        self._end_edit()
        self._edit_note.set_visible(True)
        self._update_edit_controls()

    def _show_edit_error(self, message: str, detail: str) -> None:
        show_error(self.window, message, detail)
