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

"""Recording playback dialog with transport controls."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Recording
from surveillance.services.recording import get_stream_url
from surveillance.ui.mpv_widget import MpvGLArea

if TYPE_CHECKING:
    from surveillance.app import SurveillanceApp

log = logging.getLogger(__name__)


class PlayerDialog(Gtk.Window):
    """Recording playback window with transport controls."""

    def __init__(self, parent: Gtk.Window, app: SurveillanceApp, recording: Recording) -> None:
        super().__init__()
        self.app = app
        self.recording = recording
        self._tick_id: int = 0

        start = datetime.fromtimestamp(recording.start_time)
        self.set_title(f"{recording.camera_name} - {start:%Y-%m-%d %H:%M:%S}")
        self.set_default_size(854, 530)
        self.set_transient_for(parent)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(main_box)

        # Video area
        verify_ssl = app.api.profile.verify_ssl if app.api else True
        self.player = MpvGLArea(tls_verify=verify_ssl)
        self.player.set_vexpand(True)
        main_box.append(self.player)

        # Transport controls
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_margin_top(4)
        controls.set_margin_bottom(4)
        controls.set_margin_start(8)
        controls.set_margin_end(8)

        # Play/pause
        self.play_btn = Gtk.Button()
        self.play_btn.set_icon_name("media-playback-pause-symbolic")
        self.play_btn.connect("clicked", self._on_play_pause)
        controls.append(self.play_btn)

        # Seek backward
        back_btn = Gtk.Button()
        back_btn.set_icon_name("media-seek-backward-symbolic")
        back_btn.set_tooltip_text("Back 10s")
        back_btn.connect("clicked", lambda _: self.player.seek(-10))
        controls.append(back_btn)

        # Seek forward
        fwd_btn = Gtk.Button()
        fwd_btn.set_icon_name("media-seek-forward-symbolic")
        fwd_btn.set_tooltip_text("Forward 10s")
        fwd_btn.connect("clicked", lambda _: self.player.seek(10))
        controls.append(fwd_btn)

        # Position slider
        self.position_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.position_scale.set_hexpand(True)
        self.position_scale.set_draw_value(False)
        self._seeking = False
        self.position_scale.connect("change-value", self._on_seek)
        controls.append(self.position_scale)

        # Time label
        self.time_label = Gtk.Label(label="00:00 / 00:00")
        self.time_label.add_css_class("monospace")
        controls.append(self.time_label)

        # Volume
        vol_btn = Gtk.VolumeButton()
        vol_btn.set_value(0.5)
        vol_btn.connect("value-changed", self._on_volume)
        controls.append(vol_btn)

        main_box.append(Gtk.Separator())
        main_box.append(controls)

        self.connect("close-request", self._on_close)

        # Start playback
        self._start_playback()

    def _start_playback(self) -> None:
        """Get stream URL and start playback."""
        if not self.app.api:
            return
        url = get_stream_url(self.app.api, self.recording)
        self._on_stream_url(url)

    def _on_stream_url(self, url: str) -> None:
        self.player.play(url)
        self.player.set_volume(50)
        self._tick_id = GLib.timeout_add(500, self._update_position)

    def _on_play_pause(self, btn: Gtk.Button) -> None:
        self.player.pause()
        if self.player.is_playing:
            btn.set_icon_name("media-playback-pause-symbolic")
        else:
            btn.set_icon_name("media-playback-start-symbolic")

    def _on_seek(self, scale: Gtk.Scale, scroll_type: Gtk.ScrollType, value: float) -> bool:
        duration = self.player.duration
        if duration:
            pos = value / 100.0 * duration
            self.player.seek_absolute(pos)
        return False

    def _on_volume(self, btn: Gtk.VolumeButton, value: float) -> None:
        self.player.set_volume(int(value * 100))

    def _update_position(self) -> bool:
        """Update position slider and time label."""
        pos = self.player.time_pos
        duration = self.player.duration

        if pos is not None and duration is not None and duration > 0:
            self.position_scale.set_value(pos / duration * 100)

            pos_min, pos_sec = divmod(int(pos), 60)
            dur_min, dur_sec = divmod(int(duration), 60)
            self.time_label.set_text(f"{pos_min:02d}:{pos_sec:02d} / {dur_min:02d}:{dur_sec:02d}")

        return True  # continue ticking

    def _on_close(self, window: Gtk.Window) -> bool:
        if self._tick_id:
            GLib.source_remove(self._tick_id)
        self.player.stop()
        return False
