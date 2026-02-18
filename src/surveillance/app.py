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

"""GTK4 Application class for Surveillance Station client."""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, Gtk  # type: ignore[import-untyped]

from surveillance.api.client import SurveillanceAPI
from surveillance.config import AppConfig, load_config
from surveillance.util.async_bridge import setup_async

log = logging.getLogger(__name__)

APP_ID = "org.surveillance.app"
CSS_PATH = Path(__file__).parent.parent.parent / "data" / "style.css"


class SurveillanceApp(Gtk.Application):
    """Main application."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.config: AppConfig = AppConfig()
        self.api: SurveillanceAPI | None = None
        self._window: Gtk.ApplicationWindow | None = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        setup_async()
        self.config = load_config()
        self.apply_theme(self.config.dark_theme)
        self._load_css()
        self._setup_actions()

    def apply_theme(self, dark: bool) -> None:
        """Apply or remove the dark GTK theme variant."""
        settings = Gtk.Settings.get_default()
        if settings:
            settings.set_property("gtk-application-prefer-dark-theme", dark)

    def _load_css(self) -> None:
        """Load application CSS."""
        css_file = CSS_PATH
        if not css_file.exists():
            # Try installed location
            css_file = Path(__file__).parent / "data" / "style.css"
        if css_file.exists():
            provider = Gtk.CssProvider()
            provider.load_from_path(str(css_file))
            Gtk.StyleContext.add_provider_for_display(
                self.get_active_window().get_display() if self.get_active_window() else None,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _setup_actions(self) -> None:
        """Set up application actions."""
        actions = [
            ("quit", self._on_quit),
            ("logout", self._on_logout),
        ]
        for name, handler in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self) -> None:
        if self._window is None:
            from surveillance.ui.window import MainWindow

            self._window = MainWindow(application=self)
        self._window.present()

    def set_api(self, api: SurveillanceAPI) -> None:
        """Set the active API connection."""
        self.api = api

    def _on_quit(self, action: Gio.SimpleAction, param: None) -> None:
        if self.api:
            from surveillance.api.auth import logout
            from surveillance.util.async_bridge import run_async

            run_async(logout(self.api), callback=lambda _: self.quit())
        else:
            self.quit()

    def _on_logout(self, action: Gio.SimpleAction, param: None) -> None:
        if self.api:
            from surveillance.api.auth import logout
            from surveillance.util.async_bridge import run_async

            def _done(_: object) -> None:
                self.api = None
                if self._window:
                    self._window.show_login()

            run_async(logout(self.api), callback=_done)
