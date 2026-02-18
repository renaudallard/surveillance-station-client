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
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Alert
from surveillance.services.event import list_alerts, mark_alert_read
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.app import SurveillanceApp

log = logging.getLogger(__name__)

ALERT_TYPES = {
    0: "Motion detected",
    1: "Connection lost",
    2: "Connection restored",
    3: "Alarm triggered",
    4: "Camera disabled",
    5: "Camera enabled",
}


class NotificationPopover(Gtk.Popover):
    """Popover showing recent alerts/notifications."""

    def __init__(self, app: SurveillanceApp) -> None:
        super().__init__()
        self.app = app
        self._alerts: list[Alert] = []

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

        mark_all_btn = Gtk.Button(label="Mark all read")
        mark_all_btn.connect("clicked", self._on_mark_all_read)
        header.append(mark_all_btn)

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
        if not self.app.api:
            return
        run_async(
            list_alerts(self.app.api, limit=20),
            callback=self._on_alerts_loaded,
            error_callback=lambda e: log.error("Failed to load alerts: %s", e),
        )

    def _on_alerts_loaded(self, result: tuple[list[Alert], int]) -> None:
        alerts, _ = result
        self._alerts = alerts

        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)

        if not alerts:
            empty = Gtk.Label(label="No notifications")
            empty.add_css_class("dim-label")
            empty.set_margin_top(24)
            self.listbox.append(Gtk.ListBoxRow(child=empty))
            return

        for alert in alerts:
            row = self._create_alert_row(alert)
            self.listbox.append(row)

    def _create_alert_row(self, alert: Alert) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(4)

        # Top line: type + camera
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        type_text = ALERT_TYPES.get(alert.alert_type, f"Alert {alert.alert_type}")
        type_label = Gtk.Label(label=type_text)
        if not alert.is_read:
            type_label.add_css_class("camera-label")
        type_label.set_xalign(0)
        type_label.set_hexpand(True)
        top.append(type_label)

        cam_label = Gtk.Label(label=alert.camera_name)
        cam_label.add_css_class("dim-label")
        top.append(cam_label)
        box.append(top)

        # Bottom line: timestamp
        time_str = datetime.fromtimestamp(alert.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        time_label = Gtk.Label(label=time_str)
        time_label.add_css_class("dim-label")
        time_label.add_css_class("caption")
        time_label.set_xalign(0)
        box.append(time_label)

        row.set_child(box)
        return row

    def _on_mark_all_read(self, btn: Gtk.Button) -> None:
        if not self.app.api:
            return
        for alert in self._alerts:
            if not alert.is_read:
                run_async(mark_alert_read(self.app.api, alert.id))
        self.refresh()
