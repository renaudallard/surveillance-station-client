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

"""Two-state view for the Live View timeline's Filter-events popover.

Its own file for the same reason as DateTimePicker: keeps Timeline and
LiveView from growing a whole checklist-widget's worth of construction
code. Unlike DateTimePicker, the two states here are structural, not
just data updates -- scanning per-camera event history is a real,
sometimes slow-on-first-use network cost (see AppConfig.
event_type_history and LiveView._on_filter_popover_show), so this
shows a running per-camera checklist while that happens, then swaps to
the real interactive event-type checklist (mirroring
AdvancedSearchDialog's own "All Event Types" + per-type checkboxes +
Any/All combo) once every camera is done. Cancel is available in
either state; Apply only once there is something to apply.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]


class EventTypeFilterView(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_size_request(280, -1)

        self._cancel_callback: Callable[[], None] | None = None
        self._apply_callback: Callable[[set[str] | None, bool], None] | None = None

        self._stack = Gtk.Stack()
        self.append(self._stack)

        self._stack.add_named(self._build_scanning_page(), "scanning")
        self._stack.add_named(self._build_checklist_page(), "checklist")
        self._stack.add_named(self._build_empty_page(), "empty")
        self._stack.set_visible_child_name("scanning")

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_homogeneous(True)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        footer.append(cancel_btn)
        # Insensitive during the scanning page -- nothing to apply yet.
        self._apply_btn = Gtk.Button(label="Apply")
        self._apply_btn.add_css_class("suggested-action")
        self._apply_btn.set_sensitive(False)
        self._apply_btn.connect("clicked", self._on_apply_clicked)
        footer.append(self._apply_btn)
        self.append(footer)

    def _build_scanning_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header = Gtk.Label(
            label="Scanning recording history for event types…\n"
            "This can take a while the first time.",
            wrap=True,
        )
        header.set_xalign(0)
        box.append(header)

        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(150)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scan_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        scroll.set_child(self._scan_list)
        box.append(scroll)

        # camera display name -> its own progress row -- see
        # show_scanning/mark_camera_scanned. Keyed by name rather than
        # camera_id, same "canvas/view only knows labels, never
        # cameras" split as everywhere else in the timeline UI; two
        # active cameras sharing an identical display name would share
        # a row, an accepted edge case rather than threading camera_id
        # through a widget that otherwise never needs it.
        self._scan_rows: dict[str, Gtk.Label] = {}
        return box

    def _build_checklist_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label="Event Types")
        title.set_xalign(0)
        title.set_hexpand(True)
        header_row.append(title)
        # Any/All: same purely-client-side combination as Advanced
        # Search's own event-type filter (see services.event_bits) --
        # DSM has no concept of these decoded categories to filter on
        # server-side.
        self._match_all_combo = Gtk.ComboBoxText()
        self._match_all_combo.append("or", "Any")
        self._match_all_combo.append("and", "All")
        self._match_all_combo.set_active_id("or")
        self._match_all_combo.set_tooltip_text(
            "Any: matches an event/marker with at least one checked type.\n"
            "All: only one with every checked type present at once."
        )
        header_row.append(self._match_all_combo)
        box.append(header_row)

        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(150)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._type_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroll.set_child(self._type_box)
        box.append(scroll)

        self._all_types_btn = Gtk.CheckButton(label="All Event Types")
        self._all_types_btn.set_active(True)
        self._all_types_btn.connect("toggled", self._on_all_types_toggled)
        self._type_box.append(self._all_types_btn)

        self._type_checks: dict[str, Gtk.CheckButton] = {}
        return box

    def _build_empty_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_valign(Gtk.Align.CENTER)
        label = Gtk.Label(
            label="No events have been recorded yet for the cameras in this layout.",
            wrap=True,
        )
        label.add_css_class("dim-label")
        box.append(label)
        return box

    def set_cancel_callback(self, callback: Callable[[], None]) -> None:
        self._cancel_callback = callback

    def set_apply_callback(self, callback: Callable[[set[str] | None, bool], None]) -> None:
        """*callback* receives (selected_keys, match_all) -- selected_keys
        is None for "All Event Types" (no filtering), the same
        None-means-unfiltered convention AdvancedSearchDialog's own
        _get_selected_event_type_ids() uses."""
        self._apply_callback = callback

    def show_scanning(self, camera_names: list[str]) -> None:
        """Reset to the scanning page with one pending row per name in
        *camera_names* -- called once when the popover opens, before
        any per-camera scan has actually started."""
        for child in list(self._scan_list):
            self._scan_list.remove(child)
        self._scan_rows.clear()
        for name in camera_names:
            label = Gtk.Label(label=f"… {name}")
            label.set_xalign(0)
            self._scan_rows[name] = label
            self._scan_list.append(label)
        self._apply_btn.set_sensitive(False)
        self._stack.set_visible_child_name("scanning")

    def mark_camera_scanned(self, name: str) -> None:
        label = self._scan_rows.get(name)
        if label is not None:
            label.set_label(f"✓ {name}")

    def show_options(
        self,
        options: list[tuple[str, str, str]],
        selected_keys: set[str] | None,
        match_all: bool,
    ) -> None:
        """Switch to the checklist page, built fresh from *options*
        (key, display_label, tooltip_notes) triples -- e.g. from
        services.event_bits.build_filter_options -- since the set of
        known types can grow between one popover open and the next.
        *selected_keys*/*match_all* restore a previous selection (None
        selected_keys means "All Event Types" was in effect). An empty
        *options* -- nothing has ever been recorded for any camera in
        the layout -- shows a dedicated message instead of a checklist
        with nothing in it.
        """
        if not options:
            self._apply_btn.set_sensitive(False)
            self._stack.set_visible_child_name("empty")
            return

        for check in list(self._type_checks.values()):
            self._type_box.remove(check)
        self._type_checks.clear()

        for key, label, notes in options:
            check = Gtk.CheckButton(label=label)
            if notes:
                check.set_tooltip_text(notes)
            check.set_active(selected_keys is not None and key in selected_keys)
            check.connect("toggled", self._on_type_toggled)
            self._type_checks[key] = check
            self._type_box.append(check)

        self._all_types_btn.set_active(selected_keys is None)
        self._match_all_combo.set_active_id("and" if match_all else "or")
        self._update_all_types_sensitivity()
        self._apply_btn.set_sensitive(True)
        self._stack.set_visible_child_name("checklist")

    def _on_all_types_toggled(self, btn: Gtk.CheckButton) -> None:
        active = btn.get_active()
        for check in self._type_checks.values():
            check.set_sensitive(not active)
        if active:
            self._match_all_combo.set_active_id("or")
            self._match_all_combo.set_sensitive(False)

    def _on_type_toggled(self, _btn: Gtk.CheckButton) -> None:
        self._update_all_types_sensitivity()

    def _update_all_types_sensitivity(self) -> None:
        selected_count = sum(1 for c in self._type_checks.values() if c.get_active())
        self._all_types_btn.set_active(selected_count == 0)
        # Any/All is meaningless below 2 selections.
        self._match_all_combo.set_sensitive(selected_count >= 2)

    def _get_selected_keys(self) -> set[str] | None:
        if self._all_types_btn.get_active():
            return None
        return {key for key, check in self._type_checks.items() if check.get_active()} or None

    def _on_cancel_clicked(self, _btn: Gtk.Button) -> None:
        if self._cancel_callback is not None:
            self._cancel_callback()

    def _on_apply_clicked(self, _btn: Gtk.Button) -> None:
        if self._apply_callback is not None:
            match_all = self._match_all_combo.get_active_id() == "and"
            self._apply_callback(self._get_selected_keys(), match_all)
