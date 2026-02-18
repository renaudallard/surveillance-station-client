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

"""Login dialog for Synology NAS connection."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.api.auth import login
from surveillance.api.client import SurveillanceAPI
from surveillance.config import ConnectionProfile, add_profile
from surveillance.credentials import get_credentials, store_credentials
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.app import SurveillanceApp

log = logging.getLogger(__name__)


class LoginDialog(Gtk.Dialog):
    """Login dialog for connecting to a Synology NAS."""

    def __init__(self, app: SurveillanceApp, parent: Gtk.Window) -> None:
        super().__init__(
            title="Connect to Synology NAS",
            transient_for=parent,
            modal=True,
        )
        self.app = app
        self.set_default_size(400, 350)

        content = self.get_content_area()
        content.set_spacing(12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        # Profile selector
        profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        profile_label = Gtk.Label(label="Profile:")
        self.profile_combo = Gtk.ComboBoxText()
        self.profile_combo.append("__new__", "New connection...")
        for name in app.config.profiles:
            self.profile_combo.append(name, name)
        if app.config.default_profile:
            self.profile_combo.set_active_id(app.config.default_profile)
        else:
            self.profile_combo.set_active_id("__new__")
        self.profile_combo.connect("changed", self._on_profile_changed)
        profile_box.append(profile_label)
        profile_box.append(self.profile_combo)
        content.append(profile_box)

        # Profile name
        self.name_entry = self._add_entry(content, "Profile name:", "my-nas")

        # Host
        self.host_entry = self._add_entry(content, "Host:", "192.168.1.100")

        # Port
        self.port_entry = self._add_entry(content, "Port:", "5001")

        # HTTPS checkbox
        self.https_check = Gtk.CheckButton(label="Use HTTPS")
        self.https_check.set_active(True)
        content.append(self.https_check)

        # Verify SSL checkbox
        self.verify_ssl_check = Gtk.CheckButton(label="Verify SSL certificate")
        self.verify_ssl_check.set_active(False)
        content.append(self.verify_ssl_check)

        # Separator
        content.append(Gtk.Separator())

        # Username
        self.user_entry = self._add_entry(content, "Username:", "admin")

        # Password
        self.pass_entry = self._add_entry(content, "Password:", "")
        self.pass_entry.set_visibility(False)
        self.pass_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)

        # Remember credentials
        self.remember_check = Gtk.CheckButton(label="Remember credentials")
        self.remember_check.set_active(True)
        content.append(self.remember_check)

        # Status label
        self.status_label = Gtk.Label(label="")
        self.status_label.add_css_class("error")
        content.append(self.status_label)

        # Buttons
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.connect_btn = self.add_button("Connect", Gtk.ResponseType.OK)
        self.connect_btn.add_css_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)

        self.connect("response", self._on_response)

        # Load default profile if available
        self._on_profile_changed(self.profile_combo)

    def _add_entry(self, parent: Gtk.Box, label_text: str, placeholder: str) -> Gtk.Entry:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        label = Gtk.Label(label=label_text)
        label.set_xalign(0)
        label.set_size_request(120, -1)
        entry = Gtk.Entry()
        entry.set_placeholder_text(placeholder)
        entry.set_hexpand(True)
        box.append(label)
        box.append(entry)
        parent.append(box)
        return entry

    def _on_profile_changed(self, combo: Gtk.ComboBoxText) -> None:
        profile_id = combo.get_active_id()
        if profile_id and profile_id != "__new__":
            profile = self.app.config.profiles.get(profile_id)
            if profile:
                self.name_entry.set_text(profile.name)
                self.host_entry.set_text(profile.host)
                self.port_entry.set_text(str(profile.port))
                self.https_check.set_active(profile.https)
                self.verify_ssl_check.set_active(profile.verify_ssl)
                # Try to load saved credentials
                creds = get_credentials(profile.name)
                if creds:
                    self.user_entry.set_text(creds[0])
                    self.pass_entry.set_text(creds[1])
        else:
            self.name_entry.set_text("")
            self.host_entry.set_text("")
            self.port_entry.set_text("5001")
            self.user_entry.set_text("")
            self.pass_entry.set_text("")

    def _on_response(self, dialog: Gtk.Dialog, response_id: int) -> None:
        if response_id != Gtk.ResponseType.OK:
            self.destroy()
            return

        host = self.host_entry.get_text().strip()
        port_str = self.port_entry.get_text().strip()
        username = self.user_entry.get_text().strip()
        password = self.pass_entry.get_text()
        profile_name = self.name_entry.get_text().strip() or host

        if not host or not username or not password:
            self.status_label.set_text("Host, username, and password are required")
            return

        try:
            port = int(port_str)
        except ValueError:
            self.status_label.set_text("Port must be a number")
            return

        self.status_label.set_text("Connecting...")
        self.connect_btn.set_sensitive(False)

        profile = ConnectionProfile(
            name=profile_name,
            host=host,
            port=port,
            https=self.https_check.get_active(),
            verify_ssl=self.verify_ssl_check.get_active(),
        )

        api = SurveillanceAPI(profile)
        run_async(
            self._do_connect(api, profile, username, password),
            callback=self._on_connect_success,
            error_callback=self._on_connect_error,
        )

    async def _do_connect(
        self,
        api: SurveillanceAPI,
        profile: ConnectionProfile,
        username: str,
        password: str,
    ) -> tuple[SurveillanceAPI, ConnectionProfile, str, str]:
        await api.discover_apis()
        await login(api, username, password)
        return api, profile, username, password

    def _on_connect_success(self, result: Any) -> None:
        api, profile, username, password = result

        # Save profile
        add_profile(self.app.config, profile)

        # Save credentials if requested
        if self.remember_check.get_active():
            store_credentials(profile.name, username, password)

        self.app.set_api(api)

        # Get parent window and tell it to load cameras
        parent = self.get_transient_for()
        if parent and hasattr(parent, "on_connected"):
            parent.on_connected()

        self.destroy()

    def _on_connect_error(self, error: Exception) -> None:
        self.status_label.set_text(f"Connection failed: {error}")
        self.connect_btn.set_sensitive(True)
