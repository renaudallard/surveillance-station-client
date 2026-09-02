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

"""App settings view: renders surveillance.settings_registry generically."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[import-untyped]

from surveillance.config import save_config
from surveillance.settings_registry import (
    SECTIONS,
    BoolSetting,
    Setting,
    reset_all_settings,
    reset_bool_setting,
    reset_setting,
    update_bool_setting,
    update_setting,
)

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow


class SettingsView(Gtk.Box):
    """App settings view: a toolbar (Reset all) plus one section per
    surveillance.settings_registry.SECTIONS entry, each a list of rows
    (label, value spinner or switch, per-row reset button)."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.app = window.app
        self._spin_buttons: dict[str, Gtk.SpinButton] = {}
        self._switches: dict[str, Gtk.Switch] = {}

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        toolbar.append(spacer)

        reset_all_btn = Gtk.Button(label="Reset all settings to default")
        reset_all_btn.connect("clicked", self._on_reset_all_clicked)
        toolbar.append(reset_all_btn)

        self.append(toolbar)
        self.append(Gtk.Separator())

        # Scrollable body: one section per SECTIONS entry
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        body.set_margin_top(16)
        body.set_margin_bottom(16)
        body.set_margin_start(16)
        body.set_margin_end(16)

        for section in SECTIONS:
            section_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

            title = Gtk.Label(label=section.title)
            title.add_css_class("title-4")
            title.set_xalign(0)
            section_box.append(title)
            section_box.append(Gtk.Separator())

            for setting in section.settings:
                section_box.append(self._create_setting_row(setting))
            for bool_setting in section.bool_settings:
                section_box.append(self._create_bool_setting_row(bool_setting))

            body.append(section_box)

        scroll.set_child(body)
        self.append(scroll)

    def _create_setting_row(self, setting: Setting) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_top(4)
        row.set_margin_bottom(4)

        label = Gtk.Label(label=setting.label)
        label.set_xalign(0)
        label.set_hexpand(True)
        label.set_tooltip_text(setting.tooltip)
        row.append(label)

        adjustment = Gtk.Adjustment(
            value=setting.get(),
            lower=setting.minimum,
            upper=setting.maximum,
            step_increment=setting.step,
        )
        spin = Gtk.SpinButton(adjustment=adjustment, digits=2)
        spin.set_tooltip_text(setting.tooltip)
        spin.connect("value-changed", self._on_value_changed, setting)
        self._spin_buttons[setting.key] = spin
        row.append(spin)

        reset_btn = Gtk.Button()
        reset_btn.set_icon_name("edit-undo-symbolic")
        reset_btn.set_tooltip_text("Reset to default")
        reset_btn.connect("clicked", self._on_reset_one_clicked, setting)
        row.append(reset_btn)

        return row

    def _create_bool_setting_row(self, setting: BoolSetting) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_top(4)
        row.set_margin_bottom(4)

        label = Gtk.Label(label=setting.label)
        label.set_xalign(0)
        label.set_hexpand(True)
        label.set_tooltip_text(setting.tooltip)
        row.append(label)

        switch = Gtk.Switch()
        switch.set_active(setting.get())
        switch.set_valign(Gtk.Align.CENTER)
        switch.set_tooltip_text(setting.tooltip)
        switch.connect("notify::active", self._on_bool_value_changed, setting)
        self._switches[setting.key] = switch
        row.append(switch)

        reset_btn = Gtk.Button()
        reset_btn.set_icon_name("edit-undo-symbolic")
        reset_btn.set_tooltip_text("Reset to default")
        reset_btn.connect("clicked", self._on_reset_one_bool_clicked, setting)
        row.append(reset_btn)

        return row

    def _set_spin_quietly(self, setting: Setting, value: float) -> None:
        """Update a row's spinner without re-triggering _on_value_changed:
        used when a reset already applied the value itself, the same
        division of responsibility as e.g. Timeline.set_speed()."""
        spin = self._spin_buttons[setting.key]
        spin.handler_block_by_func(self._on_value_changed)
        spin.set_value(value)
        spin.handler_unblock_by_func(self._on_value_changed)

    def _on_value_changed(self, spin: Gtk.SpinButton, setting: Setting) -> None:
        update_setting(self.app.config, setting, spin.get_value())
        save_config(self.app.config)

    def _on_reset_one_clicked(self, btn: Gtk.Button, setting: Setting) -> None:
        reset_setting(self.app.config, setting)
        save_config(self.app.config)
        self._set_spin_quietly(setting, setting.default)

    def _set_switch_quietly(self, setting: BoolSetting, value: bool) -> None:
        """Same division of responsibility as _set_spin_quietly, for a
        switch row instead of a spinner."""
        switch = self._switches[setting.key]
        switch.handler_block_by_func(self._on_bool_value_changed)
        switch.set_active(value)
        switch.handler_unblock_by_func(self._on_bool_value_changed)

    def _on_bool_value_changed(
        self, switch: Gtk.Switch, pspec: object, setting: BoolSetting
    ) -> None:
        update_bool_setting(self.app.config, setting, switch.get_active())
        save_config(self.app.config)

    def _on_reset_one_bool_clicked(self, btn: Gtk.Button, setting: BoolSetting) -> None:
        reset_bool_setting(self.app.config, setting)
        save_config(self.app.config)
        self._set_switch_quietly(setting, setting.default)

    def _on_reset_all_clicked(self, btn: Gtk.Button) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_message("Reset all settings to default?")
        dialog.set_detail("Every setting on this page will be restored to its default value.")
        dialog.set_buttons(["Cancel", "Reset All"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def _on_response(d: Gtk.AlertDialog, result: object) -> None:
            try:
                idx = d.choose_finish(result)
            except Exception:
                return
            if idx == 1:
                self._do_reset_all()

        dialog.choose(self.window, None, _on_response)

    def _do_reset_all(self) -> None:
        reset_all_settings(self.app.config)
        save_config(self.app.config)
        for section in SECTIONS:
            for setting in section.settings:
                self._set_spin_quietly(setting, setting.default)
            for bool_setting in section.bool_settings:
                self._set_switch_quietly(bool_setting, bool_setting.default)
