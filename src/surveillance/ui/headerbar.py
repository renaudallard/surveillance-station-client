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

"""Header bar with home mode toggle and notification bell."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, Gtk  # type: ignore[import-untyped]

from surveillance.ui.layouts import LAYOUT_VISIBLE

if TYPE_CHECKING:
    from surveillance.app import SurveillanceApp
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)

PAGE_TITLES: dict[str, str] = {
    "live": "Live View",
    "recordings": "Recordings",
    "snapshots": "Snapshots",
    "events": "Events",
    "timelapse": "Time Lapse",
    "licenses": "Licenses",
}


class AppHeaderBar(Gtk.HeaderBar):
    """Application header bar with controls."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__()
        self.window = window
        self.app: SurveillanceApp = window.get_application()  # type: ignore[assignment]
        self._page = "live"
        self._connected = False

        # Title, updated with the current page name by set_page()
        self.title_label = Gtk.Label(label="Surveillance Station")
        self.title_label.add_css_class("title")
        self.set_title_widget(self.title_label)

        # Sidebar toggle (left side, left of Home Mode)
        self.sidebar_btn = Gtk.ToggleButton()
        self.sidebar_btn.set_icon_name("sidebar-show-symbolic")
        self.sidebar_btn.set_active(self.app.config.sidebar_visible)
        self._update_sidebar_tooltip(self.app.config.sidebar_visible)
        self.sidebar_btn.connect("toggled", self._on_sidebar_toggled)
        self.pack_start(self.sidebar_btn)

        # Home mode toggle (left side)
        self.home_btn = Gtk.ToggleButton()
        self.home_btn.set_icon_name("go-home-symbolic")
        self.home_btn.set_tooltip_text("Home Mode")
        self.home_btn.connect("toggled", self._on_home_toggled)
        self.home_btn.set_sensitive(False)
        self.pack_start(self.home_btn)

        # Right side buttons
        # Grid layout selector
        self.grid_btn = Gtk.MenuButton()
        self.grid_btn.set_icon_name("view-grid-symbolic")
        self.grid_btn.set_tooltip_text("Grid Layout")
        self.grid_btn.set_sensitive(False)
        self._build_grid_menu()
        self.pack_end(self.grid_btn)

        # Notification bell
        self.notif_btn = Gtk.MenuButton()
        self.notif_btn.set_icon_name("bell-symbolic")
        self.notif_btn.set_tooltip_text("Notifications")
        self.notif_btn.set_sensitive(False)

        from surveillance.ui.notifications import NotificationPopover

        self.notif_popover = NotificationPopover(self.app)
        self.notif_btn.set_popover(self.notif_popover)

        # Badge overlay
        self.notif_overlay = Gtk.Overlay()
        self.notif_overlay.set_child(self.notif_btn)
        self.badge_label = Gtk.Label(label="")
        self.badge_label.add_css_class("notification-badge")
        self.badge_label.set_halign(Gtk.Align.END)
        self.badge_label.set_valign(Gtk.Align.START)
        self.badge_label.set_visible(False)
        self.notif_overlay.add_overlay(self.badge_label)
        self.pack_end(self.notif_overlay)

        # Theme selector
        self.theme_btn = Gtk.MenuButton()
        self.theme_btn.set_tooltip_text("Theme")
        self._update_theme_icon(self.app.config.theme)
        self._build_theme_popover()
        self.pack_end(self.theme_btn)

        # Logout button
        logout_btn = Gtk.Button()
        logout_btn.set_icon_name("system-log-out-symbolic")
        logout_btn.set_tooltip_text("Logout")
        logout_btn.set_action_name("app.logout")
        self.pack_end(logout_btn)

    def _update_sidebar_tooltip(self, visible: bool) -> None:
        self.sidebar_btn.set_tooltip_text("Hide Panel" if visible else "Show Panel")

    def _on_sidebar_toggled(self, btn: Gtk.ToggleButton) -> None:
        visible = btn.get_active()
        self._update_sidebar_tooltip(visible)
        self.window.toggle_sidebar(visible)

    def _on_home_toggled(self, btn: Gtk.ToggleButton) -> None:
        if not self.app.api:
            return
        from surveillance.services.homemode import switch_homemode
        from surveillance.util.async_bridge import run_async

        run_async(
            switch_homemode(self.app.api, btn.get_active()),
            error_callback=lambda e: log.error("Home mode toggle failed: %s", e),
        )

    def _build_grid_menu(self) -> None:
        """Build the grid button menu: layout choice plus clear current layout."""
        layouts = Gio.Menu()
        for layout_name in LAYOUT_VISIBLE:
            layouts.append(layout_name, f"win.grid-layout::{layout_name}")

        clear = Gio.Menu()
        clear.append("Clear Current Layout", "win.clear-layout")

        menu = Gio.Menu()
        menu.append_section(None, layouts)
        menu.append_section(None, clear)
        self.grid_btn.set_menu_model(menu)

    def set_page(self, page_name: str) -> None:
        """Show the current page in the title and enable the controls it owns."""
        self._page = page_name
        self.title_label.set_label(f"Surveillance Station — {PAGE_TITLES[page_name]}")
        self.grid_btn.set_sensitive(self._connected and page_name == "live")

    _THEME_ICONS: ClassVar[dict[str, str]] = {
        "auto": "display-brightness-symbolic",
        "dark": "weather-clear-night-symbolic",
        "light": "weather-clear-symbolic",
    }

    def _build_theme_popover(self) -> None:
        """Build a popover with radio buttons for theme selection."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        current = self.app.config.theme
        group: Gtk.CheckButton | None = None
        for key, label in [("auto", "System default"), ("dark", "Dark"), ("light", "Light")]:
            radio = Gtk.CheckButton(label=label)
            if group is not None:
                radio.set_group(group)
            else:
                group = radio
            if key == current:
                radio.set_active(True)
            radio.connect("toggled", self._on_theme_radio_toggled, key)
            box.append(radio)

        popover = Gtk.Popover()
        popover.set_child(box)
        self.theme_btn.set_popover(popover)

    def _on_theme_radio_toggled(self, radio: Gtk.CheckButton, theme: str) -> None:
        if not radio.get_active():
            return
        self.app.config.theme = theme
        self.app.apply_theme(theme)
        self._update_theme_icon(theme)
        from surveillance.config import save_config

        save_config(self.app.config)

    def _update_theme_icon(self, theme: str) -> None:
        self.theme_btn.set_icon_name(self._THEME_ICONS.get(theme, self._THEME_ICONS["auto"]))

    def set_home_mode(self, active: bool) -> None:
        """Update home mode button state without triggering the signal."""
        self.home_btn.handler_block_by_func(self._on_home_toggled)
        self.home_btn.set_active(active)
        self.home_btn.handler_unblock_by_func(self._on_home_toggled)
        if active:
            self.home_btn.add_css_class("home-mode-active")
        else:
            self.home_btn.remove_css_class("home-mode-active")

    def set_notification_count(self, count: int) -> None:
        """Update notification badge."""
        if count > 0:
            self.badge_label.set_text(str(min(count, 99)))
            self.badge_label.set_visible(True)
        else:
            self.badge_label.set_visible(False)

    def set_connected(self, connected: bool) -> None:
        """Enable/disable controls based on connection state."""
        self._connected = connected
        self.home_btn.set_sensitive(connected)
        self.notif_btn.set_sensitive(connected)
        self.grid_btn.set_sensitive(connected and self._page == "live")
