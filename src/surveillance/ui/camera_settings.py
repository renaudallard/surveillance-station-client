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

"""Per-camera settings dialog, opened by right-clicking a camera in the
sidebar."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera
from surveillance.config import save_config_now
from surveillance.services.live import (
    PROTOCOL_LABELS,
    STREAM_PROFILE_LABELS,
    WEBSOCKET_PROTOCOLS,
    StreamProfile,
    app_stream_profile,
)

if TYPE_CHECKING:
    from surveillance.app import SurveillanceApp
    from surveillance.ui.window import MainWindow


def validate_rtsp_url(url: str) -> str | None:
    """Return an error message if *url* is not a valid RTSP stream URL."""
    if not url:
        return "URL must not be empty."
    try:
        parsed = urlparse(url)
    except ValueError:
        return "Invalid URL syntax."
    if parsed.scheme not in ("rtsp", "rtsps", "rtmp", "http", "https"):
        return (
            f"Unsupported scheme “{parsed.scheme or ''}”. "
            "Expected rtsp://, rtsps://, rtmp://, http://, or https://."
        )
    if not parsed.hostname:
        return "URL must contain a hostname."
    return None


class CameraSettingsDialog(Gtk.Window):
    """Choose a camera's streaming protocol, its direct URL, and the
    stream profile a WebSocket stream asks for."""

    def __init__(self, window: MainWindow, cam: Camera) -> None:
        super().__init__(transient_for=window, modal=True)
        self.window = window
        self.app: SurveillanceApp = window.get_application()  # type: ignore[assignment]
        self.cam = cam
        self.set_title(f"Camera Settings — {cam.name}")
        self.set_default_size(450, -1)
        self.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        label = Gtk.Label(label=f"Choose stream protocol for camera {cam.id} ({cam.name}).")
        label.set_wrap(True)
        label.set_xalign(0)
        box.append(label)

        current_proto = self.app.config.camera_protocols.get(cam.id, "auto")

        # Radio buttons for each protocol
        group: Gtk.CheckButton | None = None
        self._radios: dict[str, Gtk.CheckButton] = {}
        for proto_key, proto_label in PROTOCOL_LABELS.items():
            radio = Gtk.CheckButton(label=proto_label)
            if group is not None:
                radio.set_group(group)
            else:
                group = radio
            if proto_key == current_proto:
                radio.set_active(True)
            self._radios[proto_key] = radio
            box.append(radio)

        # Direct URL entry (shown below the radios)
        self._url_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        url_label = Gtk.Label(label="Direct RTSP URL:")
        url_label.set_xalign(0)
        self._url_box.append(url_label)
        self._url_entry = Gtk.Entry()
        self._url_entry.set_placeholder_text("rtsp://user:pass@camera-ip:554/stream")
        existing_url = self.app.config.camera_overrides.get(cam.id, "")
        if existing_url:
            self._url_entry.set_text(existing_url)
        self._url_box.append(self._url_entry)
        self._url_box.set_sensitive(current_proto == "direct")
        box.append(self._url_box)

        # Error label (hidden by default)
        self._error_label = Gtk.Label()
        self._error_label.set_xalign(0)
        self._error_label.set_wrap(True)
        self._error_label.add_css_class("error")
        self._error_label.set_visible(False)
        box.append(self._error_label)

        box.append(Gtk.Separator())

        # Live View stream profile, for a WebSocket stream only
        self._profile_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        profile_label = Gtk.Label(label="Live View stream profile")
        profile_label.set_xalign(0)
        profile_label.set_hexpand(True)
        self._profile_row.append(profile_label)
        self._profiles = list(STREAM_PROFILE_LABELS)
        self._profile_dropdown = Gtk.DropDown.new_from_strings(list(STREAM_PROFILE_LABELS.values()))
        current_profile = self.app.config.camera_live_view_stream_profiles.get(
            cam.id, StreamProfile.CAMERA
        )
        if current_profile in self._profiles:
            self._profile_dropdown.set_selected(self._profiles.index(current_profile))
        self._profile_row.append(self._profile_dropdown)
        box.append(self._profile_row)

        self._profile_note = Gtk.Label()
        self._profile_note.set_xalign(0)
        self._profile_note.set_wrap(True)
        self._profile_note.add_css_class("dim-label")
        box.append(self._profile_note)
        self._update_profile_row(current_proto)

        for proto_key, radio in self._radios.items():
            radio.connect("toggled", self._on_radio_toggled, proto_key)

        # Buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        btn_box.append(cancel_btn)

        apply_btn = Gtk.Button(label="Apply")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self._on_apply)
        btn_box.append(apply_btn)

        box.append(btn_box)
        self.set_child(box)

    def _on_radio_toggled(self, radio: Gtk.CheckButton, key: str) -> None:
        """Enable the URL entry only while "direct" is selected."""
        if radio.get_active():
            self._url_box.set_sensitive(key == "direct")
            self._error_label.set_visible(False)
            self._update_profile_row(key)

    def _update_profile_row(self, protocol: str) -> None:
        """Offer the stream profile only where it can apply, and say why
        not otherwise."""
        forced = app_stream_profile()
        note = ""
        if protocol == "direct":
            note = (
                "Live View stream profile is not available with a direct URL: "
                "the URL decides the stream."
            )
        elif protocol not in WEBSOCKET_PROTOCOLS:
            note = (
                "Live View stream profile is not available when using "
                f"{PROTOCOL_LABELS[protocol]}. The stream always uses the camera's "
                "Live View setting in Surveillance Station."
            )
        elif forced is not StreamProfile.CAMERA:
            note = (
                f"Overridden by the Live View stream profile on the Settings page "
                f"({STREAM_PROFILE_LABELS[forced]})."
            )
        self._profile_row.set_visible(protocol in WEBSOCKET_PROTOCOLS)
        self._profile_dropdown.set_sensitive(forced is StreamProfile.CAMERA)
        self._profile_note.set_label(note)
        self._profile_note.set_visible(bool(note))

    def _on_apply(self, _btn: Gtk.Button) -> None:
        cam = self.cam
        config = self.app.config

        # Find selected protocol
        selected = "auto"
        for proto_key, radio in self._radios.items():
            if radio.get_active():
                selected = proto_key
                break

        # Validate direct URL before saving
        url = self._url_entry.get_text().strip()
        if selected == "direct":
            err = validate_rtsp_url(url)
            if err:
                self._error_label.set_label(err)
                self._error_label.set_visible(True)
                return

        # Save protocol
        if selected == "auto":
            config.camera_protocols.pop(cam.id, None)
        else:
            config.camera_protocols[cam.id] = selected

        # Save direct URL
        if selected == "direct":
            config.camera_overrides[cam.id] = url
        else:
            config.camera_overrides.pop(cam.id, None)

        # Save stream profile, kept even while the protocol can't use it
        profile = self._profiles[self._profile_dropdown.get_selected()]
        if profile is StreamProfile.CAMERA:
            config.camera_live_view_stream_profiles.pop(cam.id, None)
        else:
            config.camera_live_view_stream_profiles[cam.id] = str(profile)

        save_config_now(config)
        self.close()

        # Restart the stream if the camera is currently displayed
        self.window.restart_camera_stream(cam.id)
