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

"""Main application window."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera
from surveillance.ui.headerbar import AppHeaderBar
from surveillance.ui.sidebar import CameraSidebar

if TYPE_CHECKING:
    from surveillance.app import SurveillanceApp

log = logging.getLogger(__name__)


class MainWindow(Gtk.ApplicationWindow):
    """Main application window with sidebar and content stack."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.app: SurveillanceApp = self.get_application()  # type: ignore[assignment]
        self.set_title("Surveillance Station")
        self.set_default_size(1280, 720)

        # Header bar
        self.headerbar = AppHeaderBar(self)
        self.set_titlebar(self.headerbar)

        # Main layout: sidebar + content with draggable divider
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.set_child(self.paned)

        # Sidebar
        self.sidebar = CameraSidebar(self)
        self.paned.set_start_child(self.sidebar)
        self.paned.set_resize_start_child(False)
        self.paned.set_shrink_start_child(False)

        # Content stack
        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.paned.set_end_child(self.stack)
        self.paned.set_shrink_end_child(False)
        self.paned.set_position(220)

        # Placeholder pages (replaced by real widgets when connected)
        self._add_placeholder("live", "Live View", "Connect to view live streams")
        self._add_placeholder("recordings", "Recordings", "Connect to browse recordings")
        self._add_placeholder("snapshots", "Snapshots", "Connect to view snapshots")
        self._add_placeholder("events", "Events", "Connect to view events")
        self._add_placeholder("timelapse", "Time Lapse", "Connect to browse time lapse recordings")
        self._add_placeholder("licenses", "Licenses", "Connect to manage licenses")

        self.stack.set_visible_child_name("live")

        # Selected camera
        self.selected_camera: Camera | None = None
        self._homemode_poll_id: int = 0
        self._alerts_poll_id: int = 0

        # Show login if not connected
        if not self.app.api:
            self._schedule_login()

    def _add_placeholder(self, name: str, title: str, description: str) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name("camera-video-symbolic")
        icon.set_pixel_size(64)
        icon.add_css_class("dim-label")
        box.append(icon)

        title_label = Gtk.Label(label=title)
        title_label.add_css_class("title-1")
        box.append(title_label)

        desc_label = Gtk.Label(label=description)
        desc_label.add_css_class("dim-label")
        box.append(desc_label)

        self.stack.add_named(box, name)

    def _schedule_login(self) -> None:
        """Show login dialog after window is shown."""
        from gi.repository import GLib

        GLib.idle_add(self.show_login)

    def show_login(self) -> None:
        """Show the login dialog."""
        from surveillance.ui.login import LoginDialog

        dialog = LoginDialog(self.app, self)
        dialog.present()

    def on_connected(self) -> None:
        """Called after successful login."""
        self.headerbar.set_connected(True)
        self._setup_content_pages()
        self.sidebar.refresh(on_complete=self._restore_live_session)
        self.sidebar.start_polling()
        self._start_polling()

    def _restore_live_session(self, cameras: list[Camera]) -> None:
        """Restore live view camera assignments from last session."""
        live_view = self.stack.get_child_by_name("live")
        if live_view and hasattr(live_view, "restore_session"):
            live_view.restore_session(cameras)

    def _setup_content_pages(self) -> None:
        """Replace placeholders with real content widgets."""
        from surveillance.ui.events import EventsView
        from surveillance.ui.licenses import LicensesView
        from surveillance.ui.liveview import LiveView
        from surveillance.ui.recordings import RecordingsView
        from surveillance.ui.snapshots import SnapshotsView
        from surveillance.ui.timelapse import TimeLapseView

        # Remove placeholders and add real pages
        for name, widget_class in [
            ("live", LiveView),
            ("recordings", RecordingsView),
            ("snapshots", SnapshotsView),
            ("events", EventsView),
            ("timelapse", TimeLapseView),
            ("licenses", LicensesView),
        ]:
            old = self.stack.get_child_by_name(name)
            if old:
                self.stack.remove(old)
            widget = widget_class(self)
            self.stack.add_named(widget, name)

        # Restore last active page
        last_page = self.app.config.last_page
        if self.stack.get_child_by_name(last_page):
            self.stack.set_visible_child_name(last_page)
        else:
            self.stack.set_visible_child_name("live")

    def _start_polling(self) -> None:
        """Start background polling for alerts and home mode."""
        self._stop_polling()

        from gi.repository import GLib

        from surveillance.util.async_bridge import run_async

        self._homemode_available = True
        self._alerts_available = True

        # Poll home mode
        def _poll_homemode() -> bool:
            if not self._homemode_available or not self.app.api:
                return self._homemode_available
            from surveillance.services.homemode import get_homemode

            def _homemode_error(e: Exception) -> None:
                from surveillance.api.client import ApiError

                if isinstance(e, ApiError) and e.code in (102, 103, 104):
                    log.debug("Home mode API not available: %s", e)
                    self._homemode_available = False
                else:
                    log.debug("Home mode poll failed: %s", e)

            run_async(
                get_homemode(self.app.api),
                callback=lambda info: self.headerbar.set_home_mode(info.on),
                error_callback=_homemode_error,
            )
            return True

        self._homemode_poll_id = GLib.timeout_add_seconds(
            self.app.config.poll_interval_homemode, _poll_homemode
        )
        _poll_homemode()  # initial fetch

        # Poll alerts
        def _poll_alerts() -> bool:
            if not self._alerts_available or not self.app.api:
                return self._alerts_available
            from surveillance.services.event import count_unread_alerts

            def _alerts_error(e: Exception) -> None:
                from surveillance.api.client import ApiError

                if isinstance(e, ApiError) and e.code in (102, 103, 104):
                    log.debug("Notification API not available: %s", e)
                    self._alerts_available = False
                else:
                    log.debug("Alert poll failed: %s", e)

            run_async(
                count_unread_alerts(self.app.api),
                callback=lambda count: self.headerbar.set_notification_count(count),
                error_callback=_alerts_error,
            )
            return True

        self._alerts_poll_id = GLib.timeout_add_seconds(
            self.app.config.poll_interval_alerts, _poll_alerts
        )
        _poll_alerts()

    def _stop_polling(self) -> None:
        """Stop background polling for alerts and home mode."""
        from gi.repository import GLib

        if self._homemode_poll_id:
            GLib.source_remove(self._homemode_poll_id)
            self._homemode_poll_id = 0
        if self._alerts_poll_id:
            GLib.source_remove(self._alerts_poll_id)
            self._alerts_poll_id = 0

    def on_disconnected(self) -> None:
        """Clean up when disconnected (logout/quit)."""
        self.sidebar.stop_polling()
        self._stop_polling()
        self.headerbar.set_connected(False)
        from surveillance.services.recording import clear_snapshot_cache

        clear_snapshot_cache()

    def on_camera_selected(self, camera: Camera) -> None:
        """Handle camera selection from sidebar."""
        self.selected_camera = camera
        log.info("Selected camera: %s (id=%d)", camera.name, camera.id)

        # Notify the current visible page
        current_page = self.stack.get_visible_child()
        if current_page and hasattr(current_page, "on_camera_selected"):
            current_page.on_camera_selected(camera)

    def restart_camera_stream(self, camera_id: int) -> None:
        """Restart the stream for a camera after a protocol change."""
        live_view = self.stack.get_child_by_name("live")
        if live_view and hasattr(live_view, "restart_camera"):
            live_view.restart_camera(camera_id)

    def show_page(self, page_name: str) -> None:
        """Switch to a content page, pausing/resuming live streams as needed."""
        previous = self.stack.get_visible_child_name()
        self.stack.set_visible_child_name(page_name)
        self.app.config.last_page = page_name
        from surveillance.config import save_config

        save_config(self.app.config)

        live_view = self.stack.get_child_by_name("live")
        if not live_view or not hasattr(live_view, "pause_streams"):
            return
        if previous == "live" and page_name != "live":
            live_view.pause_streams()
        elif previous != "live" and page_name == "live":
            live_view.resume_streams()
