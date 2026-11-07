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
from dataclasses import dataclass
from datetime import datetime
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
from surveillance.ui.layouts import LAYOUT_VISIBLE
from surveillance.util.async_bridge import setup_async

log = logging.getLogger(__name__)

APP_ID = "org.surveillance.app"
CSS_PATH = Path(__file__).parent / "data" / "style.css"
# The app's own symbolic icons (surveillance-*-symbolic), for glyphs no
# icon theme ships under one name on every desktop. GTK recolours them
# to the theme like any stock symbolic icon.
ICONS_PATH = Path(__file__).parent / "data" / "icons"

# Pages --page can switch to, by their name in the window's page stack.
REMOTE_PAGES = ("live", "recordings", "events", "snapshots", "timelapse", "maps", "settings")


@dataclass(frozen=True)
class RemoteRequest:
    """What one command line asks the running instance to do.

    Parsed from the options GApplication hands the primary instance,
    whether it is that instance's own command line or one forwarded from
    a second launch. MainWindow.apply_remote_request carries it out, in
    the order the fields are listed here.
    """

    grid: str | None = None
    camera_id: int | None = None
    solo: bool = False
    # Unix time, already converted from the --seek argument.
    seek: float | None = None
    live: bool = False
    page: str | None = None


def parse_remote_request(options: dict[str, object]) -> RemoteRequest | None:
    """Turn the command line's options into a RemoteRequest.

    *options* is the unpacked GVariantDict GOption filled in, keyed by
    long option name. Returns None when none of the remote control
    options is given, and raises ValueError, with a message for the
    user, when one is malformed. Only checks what the options say on
    their own: whether a camera ID exists is up to the window, which has
    the camera list.
    """
    keys = ("grid", "camera", "solo", "seek", "live", "page")
    if not any(key in options for key in keys):
        return None

    # A named layout preset would be resolved here too, into its grid and
    # cameras, once the configuration has presets: --preset NAME then
    # becomes one more option feeding the same request.
    grid = options.get("grid")
    if grid is not None and grid not in LAYOUT_VISIBLE:
        raise ValueError(f"Unknown grid {grid!r}, expected one of {', '.join(LAYOUT_VISIBLE)}")

    camera_id: int | None = None
    camera_arg = options.get("camera")
    if camera_arg is not None:
        # A string option, not an INT one: GApplication leaves an INT
        # option set to 0 out of the options, so --camera 0 arrived as no
        # option at all, and GOption itself refused a non-number with a
        # status of its own.
        text = str(camera_arg)
        if not (text.isascii() and text.isdigit()) or int(text) == 0:
            raise ValueError(f"Invalid camera ID {text!r}")
        camera_id = int(text)
    solo = bool(options.get("solo", False))
    if solo and camera_id is None:
        raise ValueError("--solo needs --camera")

    seek: float | None = None
    seek_arg = options.get("seek")
    live = bool(options.get("live", False))
    if seek_arg is not None:
        if live:
            raise ValueError("--seek and --live cannot be combined")
        try:
            # A time without a zone is local time, as on the timeline.
            seek = datetime.fromisoformat(str(seek_arg)).timestamp()
        except ValueError:
            raise ValueError(
                f"Invalid time {seek_arg!r}, expected ISO 8601 such as 2026-01-31T22:15"
            ) from None

    page = options.get("page")
    if page is not None and page not in REMOTE_PAGES:
        raise ValueError(f"Unknown page {page!r}, expected one of {', '.join(REMOTE_PAGES)}")

    return RemoteRequest(
        grid=grid if isinstance(grid, str) else None,
        camera_id=camera_id,
        solo=solo,
        seek=seek,
        live=live,
        page=page if isinstance(page, str) else None,
    )


class SurveillanceApp(Gtk.Application):
    """Main application."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            # So the options below reach the instance already running,
            # through do_command_line, when the app is launched again.
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
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
        # Remote control: parsed here and carried out by the instance
        # already running, or by this one once logged in. See
        # do_command_line.
        remote_options = [
            ("grid", GLib.OptionArg.STRING, "Switch Live View to a grid layout", "1x1|2x2|3x3|4x4"),
            ("camera", GLib.OptionArg.STRING, "Show a camera in the focused Live View slot", "ID"),
            ("solo", GLib.OptionArg.NONE, "With --camera: show it alone, in 1x1", None),
            ("seek", GLib.OptionArg.STRING, "Play every Live View slot from that time", "TIME"),
            ("live", GLib.OptionArg.NONE, "Return every Live View slot to live", None),
            ("page", GLib.OptionArg.STRING, "Switch to a page", "|".join(REMOTE_PAGES)),
        ]
        for name, arg, description, arg_description in remote_options:
            self.add_main_option(name, 0, GLib.OptionFlags.NONE, arg, description, arg_description)
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
        """Let icon lookups find the app's own icons in ICONS_PATH.

        Some of them stand in for an Adwaita icon of the same meaning,
        such as the notification bell, Maps and the panel toggles. They
        are shipped so the app finds them even on a system using a theme
        without those names, such as the plain GNOME icon theme.
        """
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

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        """Run for every launch, in the instance that is already running
        when there is one: raise the window, then carry out whatever the
        remote control options ask for. Returns the launching process's
        exit status, 2 for a malformed option."""
        self.activate()
        options = command_line.get_options_dict().end().unpack()
        try:
            request = parse_remote_request(options)
        except ValueError as e:
            log.warning("Ignoring command line: %s", e)
            # printerr_literal is GLib 2.80 and later.
            if hasattr(command_line, "printerr_literal"):
                command_line.printerr_literal(f"surveillance: {e}\n")
            return 2
        if request is not None and self._window is not None:
            self._window.apply_remote_request(request)
        return 0

    def set_api(self, api: SurveillanceAPI, event_backend: EventBackend) -> None:
        """Set the active API connection and its event backend, and with
        them whose camera-keyed settings are in effect. Before the pages
        are built: each reads its cameras' settings as it starts."""
        self.config.activate_profile(api.profile.name)
        self.api = api
        self.event_backend = event_backend

    def exit_now(self, status: int = 0) -> NoReturn:
        """Save the config, mark the log complete and exit at once.

        Every way out ends here: the Quit action, closing the window and
        the signals. Without the save, a setting changed in the last
        second was still waiting on save_config's debounce and was lost.
        *status* is the process's exit status, the one run() returned
        for a launch that only handed its command line over.
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
        os._exit(status)

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
