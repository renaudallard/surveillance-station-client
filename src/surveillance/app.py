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
from typing import NoReturn

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, GLib, Gtk  # type: ignore[import-untyped]

from surveillance.api.client import SurveillanceAPI
from surveillance.config import AppConfig, load_config
from surveillance.services.event_backend import EventBackend
from surveillance.services.legacy_event import LegacyEventBackend
from surveillance.util.async_bridge import setup_async

log = logging.getLogger(__name__)

APP_ID = "org.surveillance.app"
CSS_PATH = Path(__file__).parent / "data" / "style.css"
# The app's own symbolic icons (surveillance-*-symbolic), for glyphs no
# icon theme ships under one name on every desktop. GTK recolours them
# to the theme like any stock symbolic icon.
ICONS_PATH = Path(__file__).parent / "data" / "icons"


class SurveillanceApp(Gtk.Application):
    """Main application."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        # Registered for --help only. main() strips both flags from argv
        # before we run, because logging has to be configured before any
        # of this is imported, so GOption never actually parses them. Left
        # unregistered they were simply missing from the help output.
        # OPTIONAL_ARG is not usable here (GLib allows it only on a
        # callback arg), hence --log-file=PATH with the bare form spelled
        # out in the description instead.
        self.add_main_option(
            "debug",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Enable debug logging to stderr",
            None,
        )
        self.add_main_option(
            "log-file",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.STRING,
            "Also write logs to a file; omit PATH for an auto-named one",
            "PATH",
        )
        self.config: AppConfig = AppConfig()
        # Set once do_startup has read the real config. Until then
        # self.config is the empty default above, and saving it would
        # replace the user's profiles: Gio returns from run() without
        # starting up on an unknown option, or when it hands a second
        # launch over to the instance already running.
        self._config_loaded = False
        self.api: SurveillanceAPI | None = None
        # Where the Events page and the Live View timeline get events
        # from, picked per NAS at login (see services.event_backend).
        self.event_backend: EventBackend = LegacyEventBackend()
        self._window: Gtk.ApplicationWindow | None = None
        # (tag_name, html_url) of the latest GitHub release, once the
        # startup update check completes and finds something newer than
        # this build — see MainWindow._check_for_update().
        self.latest_release: tuple[str, str] | None = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        # Actions first: they depend on nothing, and PyGObject swallows an
        # exception raised in a vfunc override, so anything failing later
        # here would otherwise leave Ctrl+Q and Logout permanently dead
        # while the window still comes up.
        self._setup_actions()
        setup_async()
        self.config = load_config()
        self._config_loaded = True

        # Before any window/stream exists, so a saved override is already
        # in effect the first time a camera plays.
        from surveillance.settings_registry import apply_persisted_settings

        apply_persisted_settings(self.config)

    def apply_theme(self, theme: str) -> None:
        """Apply theme: 'auto' follows OS, 'dark' forces dark, 'light' forces light."""
        settings = Gtk.Settings.get_default()
        if not settings:
            return
        if theme == "dark":
            settings.set_property("gtk-application-prefer-dark-theme", True)
        else:
            # "light" and "auto": False lets the OS color-scheme preference take effect
            settings.set_property("gtk-application-prefer-dark-theme", False)

    def _add_icon_path(self) -> None:
        """Let icon lookups find the app's own icons in ICONS_PATH."""
        display = Gdk.Display.get_default()
        if display:
            Gtk.IconTheme.get_for_display(display).add_search_path(str(ICONS_PATH))

    def _load_css(self) -> None:
        """Load application CSS."""
        if not CSS_PATH.exists():
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(CSS_PATH))
        display = Gdk.Display.get_default()
        if not display:
            return
        Gtk.StyleContext.add_provider_for_display(
            display,
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

            # Before the window, whose widgets look the icons up.
            self._add_icon_path()
            self._window = MainWindow(application=self)
            # Once per process, alongside the window. A second launch of a
            # single-instance app activates the running one again, and
            # every call adds another provider to the display that nothing
            # ever removes.
            self._load_css()
        self.apply_theme(self.config.theme)
        self._window.present()

    def set_api(self, api: SurveillanceAPI, event_backend: EventBackend) -> None:
        """Set the active API connection and its event backend, and with
        them whose camera-keyed settings are in effect. Before the pages
        are built: each reads its cameras' settings as it starts."""
        self.config.activate_profile(api.profile.name)
        self.api = api
        self.event_backend = event_backend

    def exit_now(self) -> NoReturn:
        """Save the config, mark the log complete and exit at once.

        Every way out ends here: the Quit action, closing the window and
        the signals. Without the save, a setting changed in the last
        second was still waiting on save_config's debounce and was lost.
        """
        import contextlib
        import os

        if self._config_loaded:
            with contextlib.suppress(Exception):
                from surveillance.config import save_config_now

                save_config_now(self.config)
        # Graceful shutdown, see surveillance.logfile.mark_complete.
        from surveillance.logfile import mark_complete

        mark_complete()
        os._exit(0)

    def _on_quit(self, action: Gio.SimpleAction, param: None) -> None:
        self.exit_now()

    def _on_logout(self, action: Gio.SimpleAction, param: None) -> None:
        if self._window:
            self._window.on_disconnected()
        if self.api:
            from surveillance.api.auth import logout
            from surveillance.util.async_bridge import run_async

            api = self.api
            self.api = None

            async def _cleanup() -> None:
                await logout(api)
                await api.close()

            # In the background, with the dialog shown at once: logging in
            # again does not need the old session gone. Shown only once
            # the cleanup returned, which can take the whole 30s request
            # timeout, it left the header's Login button live meanwhile,
            # and a login made through that was followed by a second
            # dialog over the new session.
            run_async(_cleanup())
        if self._window:
            self._window.show_login()
