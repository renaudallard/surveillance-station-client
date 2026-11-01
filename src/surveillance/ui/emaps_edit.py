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

"""Editing E-Maps on the Maps page.

The working copy of a map's items while it is edited (MapEdits, plain
Python), the image preparation an upload needs, the bar of edit buttons
under the map, and the dialogs those open. The page itself (emaps.py)
wires them to the drawing area and the service.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Iterable
from pathlib import PurePath

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # type: ignore[import-untyped]

from surveillance.api.models import Camera, EMap, EMapItem
from surveillance.services.emap import pixels_to_marker

log = logging.getLogger(__name__)

# What a Spectator is told when an edit comes back with 105.
READ_ONLY_MESSAGE = "This account may view maps but not edit them."

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# An SVG has no pixel size of its own; it is drawn with its longer side
# this many pixels, enough for a floor plan on a large screen.
SVG_RASTER_SIDE = 2000
# Picked for the file chooser; what GdkPixbuf decodes and the NAS shows.
IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/svg+xml")


# -- image preparation -------------------------------------------------


def is_png(content: bytes) -> bool:
    return content.startswith(_PNG_MAGIC)


def rasterise_to_png(content: bytes) -> bytes:
    """The PNG to upload for a chosen image file.

    A PNG goes as it is. JPEG and SVG are decoded with GdkPixbuf and
    re-encoded as PNG, since Emap.Image Upload takes nothing else. Raises
    ValueError when the file does not decode.
    """
    if is_png(content):
        return content
    loader = GdkPixbuf.PixbufLoader()
    loader.connect("size-prepared", _scale_vector_image)
    try:
        loader.write(content)
        loader.close()
    except GLib.Error as e:
        raise ValueError(f"The image could not be decoded: {e.message}") from e
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise ValueError("The image could not be decoded")
    ok, buffer = pixbuf.save_to_bufferv("png", [], [])
    if not ok:
        raise ValueError("The image could not be encoded as PNG")
    return bytes(buffer)


def _scale_vector_image(loader: GdkPixbuf.PixbufLoader, width: int, height: int) -> None:
    """Draw an SVG at SVG_RASTER_SIDE on its longer side; a raster image
    keeps its pixels."""
    fmt = loader.get_format()
    if fmt is None or fmt.get_name() != "svg" or width <= 0 or height <= 0:
        return
    scale = SVG_RASTER_SIDE / max(width, height)
    loader.set_size(max(1, round(width * scale)), max(1, round(height * scale)))


def map_name_from_file(filename: str) -> str:
    """The name offered for a new map: the file's, without its suffix."""
    name = PurePath(filename).name
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name.replace("_", " ").strip() or "New map"


# -- geometry ----------------------------------------------------------


def clamp_to_image(x: float, y: float, width: float, height: float) -> tuple[float, float]:
    """(x, y) pulled onto the image when it is off it."""
    return min(max(x, 0.0), width), min(max(y, 0.0), height)


# -- working copy ------------------------------------------------------


class MapEdits:
    """A map's items as the user is changing them.

    Holds every item, cameras or not: Emap Save replaces the whole list,
    so what is not held is lost. Only the camera markers are shown and
    moved. Positions are kept in image pixels of a *width* x *height*
    image and clamped to it.
    """

    def __init__(self, emap: EMap, width: float, height: float) -> None:
        self.base = emap
        self.width = width
        self.height = height
        self.items: list[EMapItem] = list(emap.items)
        self.dirty = False

    @property
    def base_item_ids(self) -> list[int]:
        """What the save's stale check compares the NAS against."""
        return [item.id for item in self.base.items]

    @property
    def markers(self) -> list[tuple[int, EMapItem]]:
        """The camera markers with their index in items."""
        return [(i, item) for i, item in enumerate(self.items) if item.is_camera]

    @property
    def camera_ids(self) -> set[int]:
        return {item.ref_item_id for _, item in self.markers}

    def unplaced(self, cameras: Iterable[Camera]) -> list[Camera]:
        """The cameras not yet on this map, by name."""
        placed = self.camera_ids
        return sorted(
            (cam for cam in cameras if cam.id not in placed),
            key=lambda c: (c.name.casefold(), c.id),
        )

    def add_camera(self, camera_id: int) -> int:
        """A marker for *camera_id* at the image centre, by its index."""
        lng, lat = pixels_to_marker(self.width / 2, self.height / 2, self.height)
        self.items.append(EMapItem.new_camera(camera_id, lng, lat))
        self.dirty = True
        return len(self.items) - 1

    def move(self, index: int, x: float, y: float) -> None:
        """Put item *index* at image pixel (x, y), clamped to the image."""
        x, y = clamp_to_image(x, y, self.width, self.height)
        lng, lat = pixels_to_marker(x, y, self.height)
        item = self.items[index]
        if (item.lng, item.lat) != (lng, lat):
            self.items[index] = dataclasses.replace(item, lng=lng, lat=lat)
            self.dirty = True

    def remove(self, index: int) -> None:
        del self.items[index]
        self.dirty = True


# -- dialogs -----------------------------------------------------------


def confirm(
    window: Gtk.Window, message: str, detail: str, action: str, on_confirm: Callable[[], None]
) -> None:
    """Ask before doing something that cannot be undone, as Live View's
    layout clearing does."""
    dialog = Gtk.AlertDialog()
    dialog.set_message(message)
    dialog.set_detail(detail)
    dialog.set_buttons(["Cancel", action])
    dialog.set_cancel_button(0)
    dialog.set_default_button(0)

    def _on_response(d: Gtk.AlertDialog, result: object) -> None:
        try:
            index = d.choose_finish(result)
        except GLib.Error:
            return
        if index == 1:
            on_confirm()

    dialog.choose(window, None, _on_response)


def ask_unsaved(window: Gtk.Window, map_name: str, on_choice: Callable[[str], None]) -> None:
    """What to do with unsaved marker edits before leaving the map."""
    dialog = Gtk.AlertDialog()
    dialog.set_message(f"Save the marker changes to {map_name}?")
    dialog.set_detail("The markers moved, added or removed on this map are not saved yet.")
    dialog.set_buttons(["Stay", "Discard", "Save"])
    dialog.set_cancel_button(0)
    dialog.set_default_button(2)
    choices = ("stay", "discard", "save")

    def _on_response(d: Gtk.AlertDialog, result: object) -> None:
        try:
            index = d.choose_finish(result)
        except GLib.Error:
            index = 0
        on_choice(choices[index] if 0 <= index < len(choices) else "stay")

    dialog.choose(window, None, _on_response)


def show_error(window: Gtk.Window, message: str, detail: str) -> None:
    dialog = Gtk.AlertDialog()
    dialog.set_message(message)
    dialog.set_detail(detail)
    dialog.set_buttons(["OK"])
    dialog.show(window)


def pick_image(window: Gtk.Window, on_picked: Callable[[str, bytes], None]) -> None:
    """Choose a PNG, JPEG or SVG file; *on_picked* gets its name and bytes."""
    dialog = Gtk.FileDialog()
    dialog.set_title("Choose a map image")
    image_filter = Gtk.FileFilter()
    image_filter.set_name("Images (PNG, JPEG, SVG)")
    for mime in IMAGE_MIME_TYPES:
        image_filter.add_mime_type(mime)
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(image_filter)
    dialog.set_filters(filters)
    dialog.set_default_filter(image_filter)

    def _on_open(d: Gtk.FileDialog, result: object) -> None:
        try:
            gfile = d.open_finish(result)
        except GLib.Error:
            return  # Cancelled
        if gfile is None:
            return
        try:
            _ok, content, _etag = gfile.load_contents(None)
        except GLib.Error as e:
            show_error(window, "The image could not be read", e.message)
            return
        on_picked(gfile.get_basename() or "map.png", bytes(content))

    dialog.open(window, None, _on_open)


class NewMapDialog(Gtk.Window):
    """Name a new map. Create is held back while the name is empty or
    one of *existing_names*: the NAS would take a duplicate, this client
    does not."""

    def __init__(
        self,
        parent: Gtk.Window,
        suggested_name: str,
        existing_names: Iterable[str],
        on_create: Callable[[str], None],
    ) -> None:
        super().__init__(title="New E-Map", transient_for=parent, modal=True)
        self.set_default_size(360, -1)
        self._existing = {name.casefold() for name in existing_names}
        self._on_create = on_create

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        self.set_child(content)

        label = Gtk.Label(label="Name of the new map")
        label.set_xalign(0)
        content.append(label)
        self.entry = Gtk.Entry()
        self.entry.set_text(suggested_name)
        self.entry.set_activates_default(True)
        self.entry.connect("changed", lambda _e: self._validate())
        content.append(self.entry)
        self._hint = Gtk.Label()
        self._hint.add_css_class("dim-label")
        self._hint.add_css_class("caption")
        self._hint.set_xalign(0)
        content.append(self._hint)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        buttons.append(cancel)
        self.create_btn = Gtk.Button(label="Create")
        self.create_btn.add_css_class("suggested-action")
        self.create_btn.connect("clicked", self._on_create_clicked)
        buttons.append(self.create_btn)
        content.append(buttons)
        self.set_default_widget(self.create_btn)
        self._validate()

    def _validate(self) -> None:
        name = self.entry.get_text().strip()
        duplicate = name.casefold() in self._existing
        self.create_btn.set_sensitive(bool(name) and not duplicate)
        self._hint.set_label("A map of that name exists already." if duplicate else "")

    def _on_create_clicked(self, _button: Gtk.Button) -> None:
        name = self.entry.get_text().strip()
        if not name or name.casefold() in self._existing:
            return
        self.close()
        self._on_create(name)


# -- the edit bar ------------------------------------------------------


class EMapEditBar(Gtk.Box):
    """The buttons shown under a map while it is edited.

    *unplaced_cameras* is asked each time the Add camera popover opens,
    so it lists the sidebar's cameras not on the map at that moment.
    """

    def __init__(
        self,
        unplaced_cameras: Callable[[], list[Camera]],
        on_add_camera: Callable[[Camera], None],
        on_change_image: Callable[[], None],
        on_delete_map: Callable[[], None],
        on_discard: Callable[[], None],
        on_save: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(8)
        self.set_margin_end(8)
        self._unplaced_cameras = unplaced_cameras
        self._on_add_camera = on_add_camera

        self.add_btn = Gtk.MenuButton(label="Add camera…")
        self._camera_list = Gtk.ListBox()
        self._camera_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._camera_list.connect("row-activated", self._on_camera_row_activated)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_max_content_height(320)
        scroll.set_propagate_natural_height(True)
        scroll.set_child(self._camera_list)
        self.add_popover = Gtk.Popover()
        self.add_popover.set_child(scroll)
        self.add_popover.connect("show", lambda _p: self._fill_camera_list())
        self.add_btn.set_popover(self.add_popover)
        self.append(self.add_btn)

        self.change_btn = Gtk.Button(label="Change image…")
        self.change_btn.connect("clicked", lambda _b: on_change_image())
        self.append(self.change_btn)

        self.delete_btn = Gtk.Button(label="Delete map")
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.connect("clicked", lambda _b: on_delete_map())
        self.append(self.delete_btn)

        self.status = Gtk.Label()
        self.status.add_css_class("dim-label")
        self.status.set_hexpand(True)
        self.status.set_xalign(1)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.append(self.status)

        self.discard_btn = Gtk.Button(label="Discard")
        self.discard_btn.connect("clicked", lambda _b: on_discard())
        self.append(self.discard_btn)
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", lambda _b: on_save())
        self.append(self.save_btn)
        self.set_state(dirty=False, markers=0)

    def set_state(self, dirty: bool, markers: int) -> None:
        self.save_btn.set_sensitive(dirty)
        self.discard_btn.set_sensitive(dirty)
        count = f"{markers} camera{'s' if markers != 1 else ''} on this map"
        self.status.set_label(f"{count} · unsaved changes" if dirty else count)

    def set_busy(self, busy: bool) -> None:
        """While a save, upload or delete runs nothing else may start."""
        for button in (self.add_btn, self.change_btn, self.delete_btn):
            button.set_sensitive(not busy)
        if busy:
            self.save_btn.set_sensitive(False)
            self.discard_btn.set_sensitive(False)

    def _fill_camera_list(self) -> None:
        while (row := self._camera_list.get_row_at_index(0)) is not None:
            self._camera_list.remove(row)
        cameras = self._unplaced_cameras()
        if not cameras:
            label = Gtk.Label(label="Every camera is on this map already.")
            label.add_css_class("dim-label")
            _pad(label)
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_child(label)
            self._camera_list.append(row)
            return
        for camera in cameras:
            label = Gtk.Label(label=f"{camera.name}  ({camera.id})")
            label.set_xalign(0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            _pad(label)
            row = Gtk.ListBoxRow()
            row.set_child(label)
            row.camera = camera  # type: ignore[attr-defined]
            self._camera_list.append(row)

    def _on_camera_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        camera: Camera | None = getattr(row, "camera", None)
        self.add_popover.popdown()
        if camera is not None:
            self._on_add_camera(camera)


def _pad(label: Gtk.Label) -> None:
    label.set_margin_top(6)
    label.set_margin_bottom(6)
    label.set_margin_start(10)
    label.set_margin_end(10)


def marker_menu(
    area: Gtk.Widget, x: float, y: float, camera_name: str, on_remove: Callable[[], None]
) -> Gtk.Popover:
    """A popover at (x, y) on *area* offering to remove the marker there.

    Built for one showing and unparented once closed, so the drawing area
    never holds a stale popover when the page is torn down.
    """
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    title = Gtk.Label(label=camera_name)
    title.add_css_class("dim-label")
    title.set_ellipsize(Pango.EllipsizeMode.END)
    title.set_max_width_chars(30)
    box.append(title)
    remove = Gtk.Button(label="Remove")
    remove.add_css_class("destructive-action")
    box.append(remove)

    popover = Gtk.Popover()
    popover.set_child(box)
    popover.set_parent(area)
    popover.set_has_arrow(True)
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    popover.set_pointing_to(rect)

    def _remove(_button: Gtk.Button) -> None:
        popover.popdown()
        on_remove()

    def _closed(p: Gtk.Popover) -> None:
        # Not from inside the signal: GTK is still using the popover.
        GLib.idle_add(_unparent, p)

    remove.connect("clicked", _remove)
    popover.connect("closed", _closed)
    popover.popup()
    return popover


def _unparent(popover: Gtk.Popover) -> bool:
    if popover.get_parent() is not None:
        popover.unparent()
    return False
