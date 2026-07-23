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

"""Events and alerts list view."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera, Event, Recording, decode_detection_labels
from surveillance.config import load_search_filters, save_config, save_search_filters
from surveillance.services.event import (
    MOTION_EVENT_FLAG,
    PERSON_DETECTED_FLAG,
    UNKNOWN_MOTION_FLAG,
    list_granular_events,
)
from surveillance.services.recording import (
    PRESET_LAST7D,
    PRESET_LAST24H,
    PRESET_TODAY,
    PRESET_YESTERDAY,
    preset_range,
)
from surveillance.ui.advanced_search import AdvancedSearchDialog
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)

# Event.event_type here is the *raw* event_map flag value (see
# services.event module docstring) — not Synology's documented "reason"
# enum (0-10), which is a different field on a different, currently-unused
# API path (Event::List's "mode"). Only the flag values below are confirmed
# (against actual recorded video, not just camera settings); every other
# flag is shown to the user as a bare number rather than a guess.
EVENT_TYPES = {
    MOTION_EVENT_FLAG: "Motion Detection",
    UNKNOWN_MOTION_FLAG: "Unknown Motion",
    PERSON_DETECTED_FLAG: "Person Detected",
}


def _format_event_type(event_type: int) -> str:
    """Format as "Event type <id> (<description>)", with an honest
    "(Unknown)" when we don't actually know what a flag value means."""
    return f"Event type {event_type} ({EVENT_TYPES.get(event_type, 'Unknown')})"


_PAGE_SIZE = 100
_COMBO_LABEL_MAX_LEN = 22


def _truncate_label(text: str, max_len: int = _COMBO_LABEL_MAX_LEN) -> str:
    """Cap a combo entry's display text so a long camera/type name can't
    keep growing the dropdown's width — GTK sizes a closed ComboBoxText
    from the longest entry ever appended, and GTK CSS has no max-width to
    cap that."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


class EventsView(Gtk.Box):
    """Events and alerts list with filters."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.app = window.app
        self._events: list[Event] = []
        self._camera_id: int | None = None
        self._event_type_filter: int | None = None
        self._page: int = 0
        self._search_camera_ids: list[int] | None = None
        self._search_from_time: int | None = None
        self._search_to_time: int | None = None
        self._search_time_preset: str = PRESET_TODAY
        # Advanced-search-only, plural event type selection (Events-only
        # feature, hidden on Recordings/Snapshots) — takes precedence over
        # the quick single-select Type: combo below when set. See
        # _render_events().
        self._search_event_types: list[int] | None = None
        self._loading = False
        self._reload_pending = False

        self._load_search_from_config()

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_halign(Gtk.Align.END)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)

        # Camera filter
        filter_label = Gtk.Label(label="Camera:")
        toolbar.append(filter_label)

        self.camera_combo = Gtk.ComboBoxText()
        self.camera_combo.append("all", "All cameras")
        self.camera_combo.set_active_id("all")
        self.camera_combo.connect("changed", self._on_filter_changed)
        # Caps width via CSS (see .filter-combo in style.css) — set_size_request
        # alone only sets a floor, not a ceiling: GTK still requests width for
        # the longest camera name ever appended (e.g. "HIKVISION
        # DS-2CD2387G2-LSU/SL"), so it can keep growing as more cameras are
        # added, eventually squeezing neighboring widgets in the same row.
        self.camera_combo.add_css_class("filter-combo")
        # KNOWN COSMETIC QUIRK (canonical comment — recordings.py and
        # snapshots.py points here due to identical issues): this combo's
        # internal dropdown arrow can render with a near-zero gap to its own
        # border instead of the usual ~14px, shrinking the visual gap before
        # the next toolbar button. The box's own spacing=6 is confirmed
        # byte-identical (checked via AT-SPI widget geometry, survives a
        # forced window relayout) — this is NOT a margin/padding bug.
        #
        # Ruled out as the mechanism: (1) explicit set_size_request() on
        # this combo — no change; (2) forcing both of this page's combos to
        # the same width — didn't make the arrows match, and reproduced the
        # ~14px dead-space look on both when widened; (3) removing the CSS
        # min-width floor entirely, letting the combo auto-size to its bare
        # natural width — STILL left ~14px of dead space between arrow and
        # border, suggesting ~14px of arrow-to-border padding is normal
        # GtkComboBoxText behavior and near-zero-gap is the actual anomaly.
        # Given it isn't reproducible on demand and doesn't track any single
        # variable we've tried, root cause is unresolved. Not investigated
        # further given the effort-to-value ratio; low priority.
        toolbar.append(self.camera_combo)

        # Refresh reloads everything (camera + time range, re-queried from
        # the server) — the Type filter is then reapplied client-side
        # automatically, since it never needed a query to begin with. Placed
        # right after Camera since that's the control it's most associated
        # with, not because it's scoped to it.
        refresh_btn = Gtk.Button()
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh")
        refresh_btn.connect("clicked", lambda _: self._load_events())
        toolbar.append(refresh_btn)

        # Event type filter — client-side only: list_granular_events() already
        # classifies each event locally from event_map, so switching this
        # doesn't need a new server query, just re-rendering the list we have.
        # Populated dynamically from whatever types are actually present in
        # the current unfiltered result (see _sync_event_type_combo) since
        # event_type is a raw, mostly-unidentified flag value, not a fixed
        # enum we can list up front.
        type_filter_label = Gtk.Label(label="Event type:")
        toolbar.append(type_filter_label)

        self.event_type_combo = Gtk.ComboBoxText()
        self.event_type_combo.append("all", "All types")
        self.event_type_combo.set_active_id("all")
        self.event_type_combo.connect("changed", self._on_event_type_filter_changed)
        # Same reasoning as camera_combo — entries like "Event type 33554689
        # (Person Detected)" are long enough to cause the same squeeze.
        self.event_type_combo.add_css_class("filter-combo")
        toolbar.append(self.event_type_combo)

        search_btn = Gtk.Button()
        search_btn.set_icon_name("system-search-symbolic")
        search_btn.set_tooltip_text("Advanced search (custom range, multiple cameras)")
        search_btn.connect("clicked", self._on_search_clicked)
        toolbar.append(search_btn)

        self._reset_btn = Gtk.Button(label="Reset")
        self._reset_btn.set_icon_name("edit-clear-symbolic")
        self._reset_btn.set_tooltip_text("Clear all filters")
        self._reset_btn.connect("clicked", self._on_reset_clicked)
        toolbar.append(self._reset_btn)

        self.append(toolbar)

        # Quick date presets
        preset_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        preset_bar.set_margin_start(8)
        preset_bar.set_margin_end(8)
        preset_bar.set_margin_bottom(6)

        preset_label = Gtk.Label(label="Quick filter:")
        preset_label.add_css_class("dim-label")
        preset_label.add_css_class("caption")
        preset_bar.append(preset_label)

        # Independent ToggleButtons, not a radio group: clicking the active
        # one deselects it, which is a real, supported state here (no preset
        # active — see _on_preset_toggled), same pattern as the Recordings
        # page's own quick-filter buttons.
        self._preset_buttons: dict[str, Gtk.ToggleButton] = {}
        for key, text in [
            (PRESET_TODAY, "Today"),
            (PRESET_YESTERDAY, "Yesterday"),
            (PRESET_LAST24H, "Last 24 h"),
        ]:
            btn = Gtk.ToggleButton(label=text)
            btn.add_css_class("flat")
            btn.add_css_class("caption")
            btn.connect("toggled", self._on_preset_toggled, key)
            preset_bar.append(btn)
            self._preset_buttons[key] = btn

        self.append(preset_bar)

        # Filter summary
        self.filter_summary = Gtk.Label(label="")
        self.filter_summary.set_margin_start(8)
        self.filter_summary.set_margin_bottom(4)
        self.filter_summary.add_css_class("dim-label")
        self.filter_summary.add_css_class("caption")
        self.filter_summary.set_xalign(0)
        self.append(self.filter_summary)

        self.append(Gtk.Separator())

        # Events list
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-activated", self._on_row_activated)
        scroll.set_child(self.listbox)
        self.append(scroll)

        # Pagination — client-side: RecordingPicker::EnumInterval (unlike
        # Recording::List) has no offset/limit, so list_granular_events()
        # always returns the complete, correctly-filtered list for the
        # selected range. Rendering all of it as GTK rows at once is what
        # broke down with large result sets; this only limits how many rows
        # are built at a time, it doesn't re-query the server per page.
        page_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        page_box.set_halign(Gtk.Align.CENTER)
        page_box.set_margin_top(4)
        page_box.set_margin_bottom(4)

        self.prev_btn = Gtk.Button(label="Previous")
        self.prev_btn.connect("clicked", self._on_prev)
        self.prev_btn.set_sensitive(False)
        page_box.append(self.prev_btn)

        self.page_label = Gtk.Label(label="")
        page_box.append(self.page_label)

        self.next_btn = Gtk.Button(label="Next")
        self.next_btn.connect("clicked", self._on_next)
        self.next_btn.set_sensitive(False)
        page_box.append(self.next_btn)

        self.append(page_box)

        # Populate camera filter and load initial data
        self._known_camera_ids: set[int] = set()
        self._sync_preset_buttons()
        self.refresh_camera_filter()
        self._load_events()

    def _sync_preset_buttons(self) -> None:
        """Update toggle state of preset buttons to match current preset."""
        for key, btn in self._preset_buttons.items():
            btn.handler_block_by_func(self._on_preset_toggled)
            btn.set_active(key == self._search_time_preset)
            btn.handler_unblock_by_func(self._on_preset_toggled)

    def _on_preset_toggled(self, btn: Gtk.ToggleButton, key: str) -> None:
        if not btn.get_active():
            # Deactivating — only clear if this was the active preset
            if self._search_time_preset == key:
                self._search_time_preset = ""
                self._search_from_time = None
                self._search_to_time = None
                self._page = 0
                self._save_search_to_config()
                self._load_events()
            return

        # Activating this preset — deactivate all others
        self._search_time_preset = key
        from_ts, to_ts = preset_range(key)
        self._search_from_time = from_ts
        self._search_to_time = to_ts
        self._page = 0

        for other_key, other_btn in self._preset_buttons.items():
            if other_key != key:
                other_btn.handler_block_by_func(self._on_preset_toggled)
                other_btn.set_active(False)
                other_btn.handler_unblock_by_func(self._on_preset_toggled)

        self._save_search_to_config()
        self._load_events()

    def on_page_shown(self) -> None:
        """Refresh whenever this page becomes visible (called from
        MainWindow.show_page) — matches Snapshots' behavior, since events
        can appear from elsewhere between visits."""
        self._load_events()

    def refresh_camera_filter(self) -> None:
        """Populate camera filter from sidebar camera list.

        Also reloads events: with no camera selected, _load_events() queries
        every camera in sidebar.cameras directly (unlike the Recordings page,
        which just asks the server for "all cameras"), so the initial
        construction-time load — which races ahead of the sidebar's async
        camera fetch — sees an empty camera list and finds nothing.
        """
        added = False
        for cam in self.window.sidebar.cameras:
            if cam.id not in self._known_camera_ids:
                self.camera_combo.append(str(cam.id), _truncate_label(cam.name))
                self._known_camera_ids.add(cam.id)
                added = True
        if added and self._camera_id is None and self._search_camera_ids is None:
            self._load_events()

    def _on_filter_changed(self, combo: Gtk.ComboBoxText) -> None:
        active = combo.get_active_id()
        if active == "all":
            self._camera_id = None
        else:
            try:
                self._camera_id = int(active) if active else None
            except ValueError:
                self._camera_id = None
        self._load_events()

    def _on_reset_clicked(self, btn: Gtk.Button) -> None:
        self._search_camera_ids = None
        self._search_from_time = None
        self._search_to_time = None
        self._search_time_preset = ""
        self._search_event_types = None
        self._camera_id = None
        self.camera_combo.handler_block_by_func(self._on_filter_changed)
        self.camera_combo.set_active_id("all")
        self.camera_combo.handler_unblock_by_func(self._on_filter_changed)

        self._event_type_filter = None
        self.event_type_combo.handler_block_by_func(self._on_event_type_filter_changed)
        self.event_type_combo.set_active_id("all")
        self.event_type_combo.handler_unblock_by_func(self._on_event_type_filter_changed)

        self._sync_preset_buttons()

        self._page = 0
        self._save_search_to_config()
        self._load_events()

    def _on_search_clicked(self, btn: Gtk.Button) -> None:
        """Open the advanced search dialog."""
        from_time = None
        to_time = None
        if self._search_from_time:
            from_time = datetime.fromtimestamp(self._search_from_time)
        if self._search_to_time:
            to_time = datetime.fromtimestamp(self._search_to_time)

        def _on_search(
            camera_ids: list[int] | None,
            from_dt: datetime | None,
            to_dt: datetime | None,
            event_type_ids: list[int] | None,
        ) -> None:
            self._search_camera_ids = camera_ids
            self._search_from_time = int(from_dt.timestamp()) if from_dt else None
            self._search_to_time = int(to_dt.timestamp()) if to_dt else None
            self._search_event_types = event_type_ids
            # Custom range clears preset
            self._search_time_preset = ""
            self._sync_preset_buttons()
            self._page = 0
            self._save_search_to_config()
            self._load_events()

        def _on_reset() -> None:
            self._on_reset_clicked(btn)

        dialog = AdvancedSearchDialog(
            self.window,
            self.window.sidebar.cameras,
            on_search=_on_search,
            on_reset=_on_reset,
            selected_ids=self._search_camera_ids,
            from_time=from_time,
            to_time=to_time,
            title="Search Events",
            event_types=list(EVENT_TYPES.items()),
            selected_event_type_ids=self._search_event_types,
            show_extended_presets=False,
        )
        dialog.present()

    def _load_events(self) -> None:
        if not self.app.api:
            return
        if self._loading:
            # A request (e.g. for a previous preset/camera selection) is
            # already in flight. Queries here can take many seconds for
            # "All cameras" + a wide range, so requests to switch the filter
            # mid-flight are common — remember to reload once it completes,
            # rather than silently dropping the request (which would leave
            # stale results on screen with no way to refresh but Refresh).
            self._reload_pending = True
            return

        # Recompute time range so presets like "Today" stay current after midnight
        if self._search_time_preset:
            self._search_from_time, self._search_to_time = preset_range(self._search_time_preset)

        if self._search_from_time is not None and self._search_to_time is not None:
            from_time, to_time = self._search_from_time, self._search_to_time
        else:
            # No preset and no custom range set (all quick filters deselected,
            # or the search dialog was used with the time fields left blank).
            # A literal unbounded range (from=0) isn't something DSM can
            # actually serve — confirmed directly: it returns 502 Bad Gateway
            # in ~2s rather than just being slow, so there's no timeout value
            # that fixes it. Capped at 30 days instead: comfortably covers
            # real-world retention (this NAS's actual history turned out to
            # be ~2 weeks) while staying within ranges DSM actually completes
            # (14-90 days all measured at 100-120s+ for all cameras — still
            # slow, but it works).
            to_time = int(time.time())
            from_time = to_time - 30 * 86400

        self._update_filter_summary()

        camera_ids = self._search_camera_ids
        if camera_ids is None and self._camera_id is not None:
            camera_ids = [self._camera_id]
        if camera_ids is None:
            camera_ids = [cam.id for cam in self.window.sidebar.cameras]
        if not camera_ids:
            # Sidebar hasn't loaded its camera list yet. Don't set _loading
            # here: refresh_camera_filter() calls us again once cameras are
            # known, and if we left _loading set from this no-op call, that
            # retry would silently be dropped by the guard above.
            return
        self._loading = True
        self._reload_pending = False
        # Wide-range/all-camera queries can take 50-120s+ (measured against
        # the real NAS) — without this, the page just looks empty/broken
        # for that whole stretch rather than visibly working.
        self.page_label.set_text("Loading…")
        camera_names = {cam.id: cam.name for cam in self.window.sidebar.cameras}

        run_async(
            list_granular_events(self.app.api, camera_ids, camera_names, from_time, to_time),
            callback=self._on_events_loaded,
            error_callback=self._on_load_error,
        )

    def _on_load_error(self, error: Exception) -> None:
        self._loading = False
        self.page_label.set_text("Failed to load events")
        log.error("Failed to load events: %s", error)
        if self._reload_pending:
            self._load_events()

    def _on_events_loaded(self, events: list[Event]) -> None:
        self._loading = False
        if self._reload_pending:
            self._load_events()
            return
        self._events = events
        self._page = 0
        self._sync_event_type_combo()
        self._render_events()

    def _update_filter_summary(self) -> None:
        """Always show active filter state above the list."""
        parts: list[str] = []
        parts += self._camera_filter_parts()
        parts += self._type_filter_parts()
        parts += self._time_filter_parts()
        if parts:
            self.filter_summary.set_text("Active filters: " + " | ".join(parts))
        else:
            self.filter_summary.set_text(
                "No filters active — showing the last 30 days "
                "(a genuinely unbounded query isn't something DSM can serve)"
            )

    def _camera_filter_parts(self) -> list[str]:
        cam_map = {c.id: c.name for c in self.window.sidebar.cameras}
        if self._search_camera_ids:
            names = [cam_map.get(cid, str(cid)) for cid in self._search_camera_ids]
            return [f"Cameras: {', '.join(names)}"]
        if self._camera_id is not None:
            return [f"Camera: {cam_map.get(self._camera_id, str(self._camera_id))}"]
        return []

    def _type_filter_parts(self) -> list[str]:
        """Summarize the advanced-search plural Event Types filter — the
        quick single-select Type: combo already shows its own state
        directly in the toolbar, so it doesn't need a summary entry too."""
        if self._search_event_types:
            names = [EVENT_TYPES.get(t, f"type {t}") for t in self._search_event_types]
            return [f"Event types: {', '.join(names)}"]
        return []

    def _time_filter_parts(self) -> list[str]:
        _PRESET_LABELS = {
            PRESET_TODAY: "Today",
            PRESET_YESTERDAY: "Yesterday",
            PRESET_LAST24H: "Last 24 h",
            PRESET_LAST7D: "Last 7 days",
        }
        if self._search_time_preset:
            label = _PRESET_LABELS.get(self._search_time_preset, self._search_time_preset)
            return [f"Time: {label}"]
        parts: list[str] = []
        if self._search_from_time:
            ts = datetime.fromtimestamp(self._search_from_time)
            parts.append(f"From: {ts:%Y-%m-%d %H:%M}")
        if self._search_to_time:
            ts = datetime.fromtimestamp(self._search_to_time)
            parts.append(f"To: {ts:%Y-%m-%d %H:%M}")
        return parts

    def _load_search_from_config(self) -> None:
        """Load search filters from config."""
        camera_ids, from_time, to_time, time_preset = load_search_filters(
            self.app.config, "events_search"
        )
        if camera_ids:
            self._search_camera_ids = camera_ids
        if from_time:
            self._search_from_time = from_time
        if to_time:
            self._search_to_time = to_time
        self._search_time_preset = time_preset
        # Not part of load_search_filters()/save_search_filters() — those
        # are shared with Recordings/Snapshots, which have no event-type
        # filter at all.
        if self.app.config.events_search_event_types:
            self._search_event_types = self.app.config.events_search_event_types

    def _save_search_to_config(self) -> None:
        """Save search filters to config."""
        save_search_filters(
            self.app.config,
            "events_search",
            self._search_camera_ids,
            self._search_from_time,
            self._search_to_time,
            self._search_time_preset,
        )
        self.app.config.events_search_event_types = self._search_event_types or []
        save_config(self.app.config)

    def _sync_event_type_combo(self) -> None:
        """Repopulate the type filter from types actually present in self._events."""
        types_present = sorted({e.event_type for e in self._events})
        valid_ids = {"all", *(str(t) for t in types_present)}
        previous_selection = self.event_type_combo.get_active_id()

        self.event_type_combo.handler_block_by_func(self._on_event_type_filter_changed)
        self.event_type_combo.remove_all()
        self.event_type_combo.append("all", "All types")
        for type_code in types_present:
            self.event_type_combo.append(
                str(type_code), _truncate_label(_format_event_type(type_code))
            )

        if previous_selection in valid_ids:
            self.event_type_combo.set_active_id(previous_selection)
        else:
            self.event_type_combo.set_active_id("all")
            self._event_type_filter = None
        self.event_type_combo.handler_unblock_by_func(self._on_event_type_filter_changed)

    def _on_event_type_filter_changed(self, combo: Gtk.ComboBoxText) -> None:
        active = combo.get_active_id()
        if active == "all":
            self._event_type_filter = None
        else:
            try:
                self._event_type_filter = int(active) if active else None
            except ValueError:
                self._event_type_filter = None
        self._page = 0
        self._render_events()

    def _render_events(self) -> None:
        """Rebuild the visible list from self._events, applying the type
        filter and paging.

        Purely local — list_granular_events() already classified every event
        from event_map, so changing the type filter or page doesn't need a
        new query; it just re-slices what's already in memory. Only limiting
        how many GTK rows get built at once (self._page) is new here — the
        underlying list is still the complete, correct result for the
        selected time range, unlike Recording::List's server-side paging.
        """
        # Advanced search's plural Event Types filter takes precedence over
        # the quick single-select Type: combo, same precedence the camera
        # filters use (see _load_events()).
        if self._search_event_types:
            events = [e for e in self._events if e.event_type in self._search_event_types]
        elif self._event_type_filter is None:
            events = self._events
        else:
            events = [e for e in self._events if e.event_type == self._event_type_filter]

        total_pages = max(1, (len(events) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self._page = max(0, min(self._page, total_pages - 1))
        start = self._page * _PAGE_SIZE
        page_events = events[start : start + _PAGE_SIZE]

        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)

        for event in page_events:
            row = self._create_event_row(event)
            self.listbox.append(row)

        if len(events) == len(self._events):
            total_text = f"{len(events)} total"
        else:
            total_text = f"{len(events)} of {len(self._events)} total"

        self.prev_btn.set_sensitive(self._page > 0)
        self.next_btn.set_sensitive(self._page < total_pages - 1)
        self.page_label.set_text(f"Page {self._page + 1} of {total_pages} ({total_text})")

    def _on_prev(self, btn: Gtk.Button) -> None:
        self._page = max(0, self._page - 1)
        self._render_events()

    def _on_next(self, btn: Gtk.Button) -> None:
        self._page += 1
        self._render_events()

    def _create_event_row(self, event: Event) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.add_css_class("event-row")
        row._event = event  # type: ignore[attr-defined]

        if event.event_type == MOTION_EVENT_FLAG:
            row.add_css_class("motion")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        # Info column
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)

        # Top line: type badge + camera name
        top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        type_label = Gtk.Label(label=_format_event_type(event.event_type))
        type_label.add_css_class("caption")
        if event.event_type == MOTION_EVENT_FLAG:
            type_label.add_css_class("accent")
        top_box.append(type_label)

        cam_label = Gtk.Label(label=event.camera_name)
        cam_label.add_css_class("camera-label")
        top_box.append(cam_label)
        info_box.append(top_box)

        # Time range
        start = datetime.fromtimestamp(event.start_time)
        if event.stop_time:
            stop = datetime.fromtimestamp(event.stop_time)
            time_str = f"{start:%Y-%m-%d %H:%M:%S} - {stop:%H:%M:%S}"
        else:
            time_str = f"{start:%Y-%m-%d %H:%M:%S} (ongoing)"
        time_label = Gtk.Label(label=time_str)
        time_label.set_xalign(0)
        time_label.add_css_class("dim-label")
        time_label.add_css_class("caption")
        info_box.append(time_label)

        # Duration
        if event.stop_time:
            duration = event.stop_time - event.start_time
            mins, secs = divmod(duration, 60)
            dur_label = Gtk.Label(label=f"{mins}m {secs}s")
            dur_label.set_xalign(0)
            dur_label.add_css_class("dim-label")
            dur_label.add_css_class("caption")
            info_box.append(dur_label)

        # Smart detection labels
        det_labels = decode_detection_labels(event.detection_label)
        if det_labels:
            det_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            for tag in det_labels:
                tag_label = Gtk.Label(label=tag)
                tag_label.add_css_class("caption")
                tag_label.add_css_class("accent")
                det_box.append(tag_label)
            info_box.append(det_box)

        box.append(info_box)

        # Play button
        play_btn = Gtk.Button()
        play_btn.set_icon_name("media-playback-start-symbolic")
        play_btn.set_tooltip_text("Play recording")
        play_btn.set_valign(Gtk.Align.CENTER)
        play_btn.connect("clicked", self._on_play, event)
        box.append(play_btn)

        row.set_child(box)
        return row

    def _on_row_activated(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        event = row._event  # type: ignore[attr-defined]
        self._play_event(event)

    def _on_play(self, btn: Gtk.Button, event: Event) -> None:
        self._play_event(event)

    def _play_event(self, event: Event) -> None:
        """Open the recording for this event in the player."""
        from surveillance.ui.player import PlayerDialog

        rec = Recording(
            id=event.id,
            camera_id=event.camera_id,
            camera_name=event.camera_name,
            start_time=event.start_time,
            stop_time=event.stop_time,
            # NOT event.event_type: that holds this page's own type
            # classification (see EVENT_TYPES) used for the type icon/label,
            # not a real "recEvtType" value. The raw API response has no
            # separate "type" field for playback purposes — Recording.from_api's
            # event_type always defaults to 0 in practice, and passing the
            # classification value instead as recEvtType to EventStream causes
            # playback to fail ("Playback failed" / stream URL rejected) for
            # event-triggered recordings.
            event_type=0,
            mount_id=event.mount_id,
            arch_id=event.arch_id,
        )
        dialog = PlayerDialog(self.window, self.app, rec, start_offset=event.seek_offset)
        dialog.present()

    def _ensure_camera_in_combo(self, camera_id: int, name: str) -> None:
        """Add a camera to the combo if not already present."""
        if not hasattr(self, "_known_camera_ids"):
            self._known_camera_ids = set()
        if camera_id not in self._known_camera_ids:
            self.camera_combo.append(str(camera_id), _truncate_label(name))
            self._known_camera_ids.add(camera_id)

    def on_camera_selected(self, camera: Camera) -> None:
        """Handle camera selection from sidebar."""
        self._ensure_camera_in_combo(camera.id, camera.name)
        self._camera_id = camera.id
        self.camera_combo.handler_block_by_func(self._on_filter_changed)
        self.camera_combo.set_active_id(str(camera.id))
        self.camera_combo.handler_unblock_by_func(self._on_filter_changed)
        self._load_events()
