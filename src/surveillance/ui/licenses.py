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

"""License management view."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.api.models import License, LicenseInfo
from surveillance.services.license import (
    add_license_online,
    delete_license,
    load_licenses,
    offline_activate,
    offline_deactivate,
)
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)


def _mask_key(key: str) -> str:
    """Mask a license key, showing only first 4 and last 4 characters."""
    if len(key) <= 8:
        return key
    return key[:4] + "\u2026" + key[-4:]


class LicensesView(Gtk.Box):
    """License management view."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.app = window.app
        self._license_info: LicenseInfo | None = None

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)

        label = Gtk.Label(label="Licenses")
        label.add_css_class("title-4")
        label.set_xalign(0)
        toolbar.append(label)

        self.summary_label = Gtk.Label(label="")
        self.summary_label.add_css_class("dim-label")
        self.summary_label.set_hexpand(True)
        self.summary_label.set_xalign(0)
        toolbar.append(self.summary_label)

        refresh_btn = Gtk.Button()
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh")
        refresh_btn.connect("clicked", lambda _: self._load_licenses())
        toolbar.append(refresh_btn)

        add_btn = Gtk.Button()
        add_btn.set_icon_name("list-add-symbolic")
        add_btn.set_tooltip_text("Add License")
        add_btn.connect("clicked", lambda _: self._show_add_dialog())
        toolbar.append(add_btn)

        self.append(toolbar)
        self.append(Gtk.Separator())

        # License list
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.set_child(self.listbox)
        self.append(scroll)

        # Load on init
        self._load_licenses()

    def _load_licenses(self) -> None:
        if not self.app.api:
            return
        run_async(
            load_licenses(self.app.api),
            callback=self._on_licenses_loaded,
            error_callback=lambda e: log.error("Failed to load licenses: %s", e),
        )

    def _on_licenses_loaded(self, info: LicenseInfo) -> None:
        self._license_info = info
        self.summary_label.set_label(f"{info.key_used} / {info.key_total} used, max {info.key_max}")

        # Clear old rows
        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)

        for lic in info.licenses:
            row = self._create_license_row(lic)
            self.listbox.append(row)

    def _create_license_row(self, lic: License) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        # Key (masked)
        key_label = Gtk.Label(label=_mask_key(lic.key))
        key_label.set_xalign(0)
        key_label.set_size_request(160, -1)
        box.append(key_label)

        # Quota
        quota_label = Gtk.Label(label=f"{lic.quota} camera{'s' if lic.quota != 1 else ''}")
        quota_label.set_xalign(0)
        quota_label.set_size_request(100, -1)
        box.append(quota_label)

        # Expiry
        if lic.expired_date == 0:
            expiry_text = "No expiration"
        else:
            expiry_text = datetime.fromtimestamp(lic.expired_date).strftime("%Y-%m-%d")
        expiry_label = Gtk.Label(label=expiry_text)
        expiry_label.set_hexpand(True)
        expiry_label.set_xalign(0)
        box.append(expiry_label)

        # Status badge
        if lic.is_expired:
            status_text = "Expired"
            css_class = "error"
        elif lic.is_migrated:
            status_text = "Migrated"
            css_class = "warning"
        else:
            status_text = "Active"
            css_class = "success"
        status_label = Gtk.Label(label=status_text)
        status_label.add_css_class(css_class)
        status_label.set_size_request(80, -1)
        box.append(status_label)

        # Delete button
        del_btn = Gtk.Button()
        del_btn.set_icon_name("user-trash-symbolic")
        del_btn.set_tooltip_text("Delete")
        del_btn.connect("clicked", self._on_delete, lic)
        box.append(del_btn)

        row.set_child(box)
        return row

    def _on_delete(self, btn: Gtk.Button, lic: License) -> None:
        if not self.app.api:
            return
        self._show_delete_dialog(lic)

    def _show_delete_dialog(self, lic: License) -> None:
        """Show dialog to choose online or offline license deletion."""
        dialog = Gtk.Window(transient_for=self.window, modal=True)
        dialog.set_title("Delete License")
        dialog.set_default_size(400, -1)
        dialog.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        label = Gtk.Label(label=f"Delete license {_mask_key(lic.key)}?")
        label.set_xalign(0)
        box.append(label)

        online_radio = Gtk.CheckButton(label="Online (via NAS)")
        online_radio.set_active(True)
        box.append(online_radio)

        offline_radio = Gtk.CheckButton(label="Offline (direct to Synology)")
        offline_radio.set_group(online_radio)
        box.append(offline_radio)

        error_label = Gtk.Label()
        error_label.set_xalign(0)
        error_label.set_wrap(True)
        error_label.add_css_class("error")
        error_label.set_visible(False)
        box.append(error_label)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: dialog.close())
        btn_box.append(cancel_btn)

        del_btn = Gtk.Button(label="Delete")
        del_btn.add_css_class("destructive-action")
        del_btn.connect(
            "clicked",
            self._on_confirm_delete,
            lic,
            online_radio,
            error_label,
            dialog,
        )
        btn_box.append(del_btn)

        box.append(btn_box)
        dialog.set_child(box)
        dialog.present()

    def _on_confirm_delete(
        self,
        btn: Gtk.Button,
        lic: License,
        online_radio: Gtk.CheckButton,
        error_label: Gtk.Label,
        dialog: Gtk.Window,
    ) -> None:
        if not self.app.api:
            return

        def _on_success(_: object) -> None:
            dialog.close()
            self._load_licenses()

        def _on_error(e: Exception) -> None:
            error_label.set_label(str(e))
            error_label.set_visible(True)

        if online_radio.get_active():
            run_async(
                delete_license(self.app.api, [lic.id]),
                callback=_on_success,
                error_callback=_on_error,
            )
        else:
            run_async(
                offline_deactivate(self.app.api, [lic.key]),
                callback=_on_success,
                error_callback=_on_error,
            )

    def _show_add_dialog(self) -> None:
        dialog = Gtk.Window(transient_for=self.window, modal=True)
        dialog.set_title("Add License")
        dialog.set_default_size(450, -1)
        dialog.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        label = Gtk.Label(label="Enter license key(s), one per line:")
        label.set_xalign(0)
        box.append(label)

        key_entry = Gtk.TextView()
        key_entry.set_size_request(-1, 80)
        key_entry.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        key_frame = Gtk.Frame()
        key_frame.set_child(key_entry)
        box.append(key_frame)

        # Activation method
        online_radio = Gtk.CheckButton(label="Online (via NAS)")
        online_radio.set_active(True)
        box.append(online_radio)

        offline_radio = Gtk.CheckButton(label="Offline (direct to Synology)")
        offline_radio.set_group(online_radio)
        box.append(offline_radio)

        # Error label
        error_label = Gtk.Label()
        error_label.set_xalign(0)
        error_label.set_wrap(True)
        error_label.add_css_class("error")
        error_label.set_visible(False)
        box.append(error_label)

        # Buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: dialog.close())
        btn_box.append(cancel_btn)

        add_btn = Gtk.Button(label="Add")
        add_btn.add_css_class("suggested-action")
        add_btn.connect(
            "clicked",
            self._on_add_license,
            key_entry,
            online_radio,
            error_label,
            dialog,
        )
        btn_box.append(add_btn)

        box.append(btn_box)
        dialog.set_child(box)
        dialog.present()

    def _on_add_license(
        self,
        btn: Gtk.Button,
        key_entry: Gtk.TextView,
        online_radio: Gtk.CheckButton,
        error_label: Gtk.Label,
        dialog: Gtk.Window,
    ) -> None:
        if not self.app.api:
            return

        buf = key_entry.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        keys = [k.strip() for k in text.splitlines() if k.strip()]

        if not keys:
            error_label.set_label("Enter at least one license key.")
            error_label.set_visible(True)
            return

        error_label.set_visible(False)

        def _on_success(_: object) -> None:
            dialog.close()
            self._load_licenses()

        def _on_error(e: Exception) -> None:
            error_label.set_label(str(e))
            error_label.set_visible(True)

        if online_radio.get_active():
            run_async(
                add_license_online(self.app.api, keys),
                callback=_on_success,
                error_callback=_on_error,
            )
        else:
            run_async(
                offline_activate(self.app.api, keys),
                callback=_on_success,
                error_callback=_on_error,
            )
