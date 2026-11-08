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

"""Notification popover for alert display."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Alert
from surveillance.services.event import (
    alert_text,
    clear_alerts,
    count_unread_alerts,
    list_alerts,
    load_alert_templates,
    mark_alerts_read,
)
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI
    from surveillance.app import SurveillanceApp

log = logging.getLogger(__name__)


class NotificationPopover(Gtk.Popover):
    """Popover showing recent alerts/notifications.

    Opening it marks them read, as the web app's panel does. They are
    listed first, so the unread ones still show in bold this time.
    """

    def __init__(self, app: SurveillanceApp, headerbar: Any = None) -> None:
        super().__init__()
        self.app = app
        # Kept so marking alerts read or clearing them can push the new
        # unread count straight to the badge instead of leaving it stale
        # until the next alert poll comes round.
        self._headerbar = headerbar
        # Read once per session: the strings are about a megabyte.
        self._templates: dict[str, str] = {}
        self._templates_api: SurveillanceAPI | None = None

        self.set_size_request(350, 400)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label="Notifications")
        title.add_css_class("title-4")
        title.set_hexpand(True)
        title.set_xalign(0)
        header.append(title)

        self.clear_btn = Gtk.Button(label="Clear all")
        self.clear_btn.set_sensitive(False)
        self.clear_btn.connect("clicked", self._on_clear_all)
        header.append(self.clear_btn)

        box.append(header)
        box.append(Gtk.Separator())

        # Alert list
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.set_child(self.listbox)
        box.append(scroll)

        self.set_child(box)
        self.connect("notify::visible", self._on_visible_changed)

    def _on_visible_changed(self, popover: Gtk.Popover, pspec: object) -> None:
        if self.get_visible():
            self.refresh()

    def refresh(self) -> None:
        """Refresh the alert list."""
        api = self.app.api
        if not api:
            return
        templates = self._templates if api is self._templates_api else {}
        run_async(
            self._load(api, templates),
            callback=lambda result: self._on_alerts_loaded(api, *result),
            error_callback=self._on_load_failed,
        )

    @staticmethod
    async def _load(
        api: SurveillanceAPI, templates: dict[str, str]
    ) -> tuple[list[Alert], dict[str, str]]:
        alerts = await list_alerts(api)
        if alerts and not templates:
            templates = await load_alert_templates(api)
        return alerts, templates

    def _on_alerts_loaded(
        self, api: SurveillanceAPI, alerts: list[Alert], templates: dict[str, str]
    ) -> None:
        self._templates, self._templates_api = templates, api
        self._clear()
        self.clear_btn.set_sensitive(bool(alerts))

        if not alerts:
            self._show_placeholder("No notifications")
            return

        for alert in alerts:
            self.listbox.append(self._create_alert_row(alert))

        if not all(alert.is_read for alert in alerts):
            run_async(
                mark_alerts_read(api),
                callback=lambda _: self._update_badge(api),
                error_callback=lambda e: log.error("Mark read failed: %s", e),
            )

    def _on_load_failed(self, error: Exception) -> None:
        log.error("Failed to load alerts: %s", error)
        self._show_failure("Could not load notifications")

    def _on_clear_all(self, btn: Gtk.Button) -> None:
        api = self.app.api
        if not api:
            return
        btn.set_sensitive(False)
        run_async(
            clear_alerts(api),
            callback=lambda _: self._after_cleared(api),
            error_callback=self._on_clear_failed,
        )

    def _after_cleared(self, api: SurveillanceAPI) -> None:
        self.refresh()
        self._update_badge(api)

    def _on_clear_failed(self, error: Exception) -> None:
        log.error("Failed to clear alerts: %s", error)
        self._show_failure("Could not clear notifications")

    def _show_failure(self, text: str) -> None:
        self._clear()
        self.clear_btn.set_sensitive(False)
        self._show_placeholder(text)

    def _clear(self) -> None:
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)

    def _show_placeholder(self, text: str) -> None:
        label = Gtk.Label(label=text)
        label.add_css_class("dim-label")
        label.set_margin_top(24)
        self.listbox.append(Gtk.ListBoxRow(child=label))

    def _create_alert_row(self, alert: Alert) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(4)

        text_label = Gtk.Label(label=alert_text(alert, self._templates))
        if not alert.is_read:
            text_label.add_css_class("camera-label")
        text_label.set_xalign(0)
        text_label.set_wrap(True)
        text_label.set_max_width_chars(40)
        box.append(text_label)

        time_str = datetime.fromtimestamp(alert.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        time_label = Gtk.Label(label=time_str)
        time_label.add_css_class("dim-label")
        time_label.add_css_class("caption")
        time_label.set_xalign(0)
        box.append(time_label)

        row.set_child(box)
        return row

    def _update_badge(self, api: SurveillanceAPI) -> None:
        """Push the new unread count to the badge, which the alert poll
        would otherwise leave stale for up to a poll interval."""
        if self._headerbar is None:
            return
        run_async(
            count_unread_alerts(api),
            callback=self._headerbar.set_notification_count,
            error_callback=lambda e: log.error("Unread count refresh failed: %s", e),
        )
