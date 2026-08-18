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

"""Hover-revealed video toolbar for a Live View slot."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera, PtzPatrol, PtzPreset
from surveillance.ui.icons import magnifier_zoom_icon, pan_tilt_icon
from surveillance.ui.mpv_widget import MpvGLArea


class SlotToolbar(Gtk.Revealer):
    """Hover-revealed toolbar over a Live View slot's video.

    Mute/volume, push-to-talk, PTZ/Zoom/Focus/Preset/Patrol (shown only for
    PTZ-capable cameras), and Snapshot — slides up from the bottom of the
    video on hover, matching DSM's own Monitor Center.
    """

    def __init__(self, index: int, player: MpvGLArea) -> None:
        super().__init__()
        self.index = index
        self.player = player
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_valign(Gtk.Align.END)
        self.set_halign(Gtk.Align.START)
        self.set_reveal_child(False)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        toolbar.add_css_class("slot-toolbar")

        self._audio_playable = True
        self._mute_btn = Gtk.Button()
        self._mute_btn.add_css_class("flat")
        self._mute_btn.set_visible(False)  # shown in assign() only if the camera has audio
        self._mute_btn.connect("clicked", self._on_mute_clicked)
        toolbar.append(self._mute_btn)
        self.update_mute_icon()

        # Push-to-talk — a toggle (tap to start talking, tap again to
        # stop), matching the mobile app's own gesture rather than a
        # press-and-hold (mirroring the PTZ buttons would need the mic
        # held open for the exact duration of a physical button press,
        # which doesn't fit a click-based UI as naturally). Only shown
        # for cameras with a speaker (has_speaker).
        # The indicator tracks the microphone exactly: PttSession.run()
        # opens the input stream before the CheckOccupied/WebSocket
        # handshake and buffers what it captures, so the mic is live from
        # the tap onwards and the button has to say so from the tap
        # onwards too.
        self._mic_active = False
        self._mic_btn = Gtk.Button()
        self._mic_btn.add_css_class("flat")
        self._mic_btn.set_icon_name("audio-input-microphone-symbolic")
        self._mic_btn.set_visible(False)  # shown in assign() only if the camera has a speaker
        self._mic_btn.set_tooltip_text("Push-to-talk")
        self._mic_btn.connect("clicked", self._on_mic_clicked)
        toolbar.append(self._mic_btn)

        # PTZ pad — services.ptz.move(), a discrete 4-button pad, so the
        # v2 up/down/left/right/home strings are enough; the newer
        # numeric-direction Move exists for free-angle click-to-pan.
        self._ptz_btn = Gtk.Button()
        self._ptz_btn.add_css_class("flat")
        self._ptz_btn.set_child(pan_tilt_icon())
        self._ptz_btn.set_visible(False)  # shown in assign() only if the camera is PTZ-capable
        self._ptz_btn.set_tooltip_text("Pan / Tilt")
        toolbar.append(self._ptz_btn)

        # Zoom — services.ptz.zoom() Start/Stop calls.
        self._zoom_btn = Gtk.Button()
        self._zoom_btn.add_css_class("flat")
        self._zoom_btn.set_child(magnifier_zoom_icon(zoom_in=True))
        self._zoom_btn.set_visible(False)  # shown in assign() only if the camera is PTZ-capable
        self._zoom_btn.set_tooltip_text("Zoom")
        toolbar.append(self._zoom_btn)

        # Focus — services.ptz.focus(), confirmed live against DSM's own
        # web UI (network capture): a distinct, newer PTZ API version.
        self._focus_btn = Gtk.Button()
        self._focus_btn.add_css_class("flat")
        self._focus_btn.set_icon_name("edit-select-all-symbolic")
        self._focus_btn.set_visible(False)  # shown in assign() only if the camera is PTZ-capable
        self._focus_btn.set_tooltip_text("Focus")
        toolbar.append(self._focus_btn)

        # Preset — jumps immediately on selection.
        self._preset_btn = Gtk.Button()
        self._preset_btn.add_css_class("flat")
        self._preset_btn.set_icon_name("starred-symbolic")
        self._preset_btn.set_visible(False)  # shown in assign() only if the camera is PTZ-capable
        self._preset_btn.set_tooltip_text("Preset")
        toolbar.append(self._preset_btn)

        # Patrol — like Preset, starts on selection.
        self._patrol_btn = Gtk.Button()
        self._patrol_btn.add_css_class("flat")
        self._patrol_btn.set_icon_name("media-playlist-repeat-symbolic")
        self._patrol_btn.set_visible(False)  # shown in assign() only if the camera is PTZ-capable
        self._patrol_btn.set_tooltip_text("Patrol")
        toolbar.append(self._patrol_btn)

        self._snapshot_btn = Gtk.Button()
        self._snapshot_btn.add_css_class("flat")
        self._snapshot_btn.set_icon_name("camera-photo-symbolic")
        self._snapshot_btn.set_tooltip_text("Take Snapshot")
        self._snapshot_btn.connect("clicked", lambda _btn: self._on_snapshot_clicked())
        toolbar.append(self._snapshot_btn)

        self.set_child(toolbar)

        # Volume popover — revealed on hovering the mute button specifically
        # (not the whole toolbar), matching DSM's own Monitor Center.
        self._volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self._volume_scale.set_size_request(100, -1)
        self._volume_scale.set_draw_value(False)
        self._volume_scale.set_value(50)
        self._volume_scale.connect("value-changed", self._on_volume_changed)
        self._volume_popover = self._make_popover(self._mute_btn, self._volume_scale)

        # PTZ popover — 4 press-and-hold direction buttons plus a Home
        # button, which is a single click (direction "home", no suffix).
        ptz_pad = Gtk.Grid()
        ptz_pad.set_row_homogeneous(True)
        ptz_pad.set_column_homogeneous(True)
        ptz_pad.set_row_spacing(2)
        ptz_pad.set_column_spacing(2)
        ptz_pad.add_css_class("ptz-pad")
        for row, col, direction, icon in (
            (0, 1, "up", "go-up-symbolic"),
            (1, 0, "left", "go-previous-symbolic"),
            (1, 2, "right", "go-next-symbolic"),
            (2, 1, "down", "go-down-symbolic"),
        ):
            dir_btn = Gtk.Button()
            dir_btn.set_icon_name(icon)
            self._attach_hold(dir_btn, "ptz", direction)
            ptz_pad.attach(dir_btn, col, row, 1, 1)
        home_btn = Gtk.Button()
        home_btn.set_icon_name("go-home-symbolic")
        home_btn.set_tooltip_text("Home")
        home_gesture = Gtk.GestureClick()
        home_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        home_gesture.connect("released", self._on_ptz_home)
        home_btn.add_controller(home_gesture)
        ptz_pad.attach(home_btn, 1, 1, 1, 1)
        self._ptz_popover = self._make_popover(self._ptz_btn, ptz_pad)

        # Zoom and Focus popovers — a minus/plus pair each, press and hold.
        self._zoom_popover = self._make_popover(
            self._zoom_btn, self._plus_minus_box("zoom", "Zoom Out", "Zoom In")
        )
        self._focus_popover = self._make_popover(
            self._focus_btn, self._plus_minus_box("focus", "Focus Out", "Focus In")
        )

        # Preset popover — a single dropdown, populated via set_presets()
        # once the camera's preset list has loaded (LiveView owns the API
        # call, same split as zoom/focus/PTZ). Selecting an entry jumps
        # immediately.
        self._preset_combo = Gtk.ComboBoxText()
        self._preset_combo.connect("changed", self._on_preset_changed)
        # Opening the combo's own dropdown list is a separate popup
        # surface, which fires a "leave" crossing on the parent popover's
        # motion controller (same class of gap the hover debounce already
        # handles between an icon and its popover) — without this, the
        # 200ms debounce tears the whole popover down before a selection
        # can be made. Cancel/re-arm the hide around the combo's own
        # popup-shown state so it survives the crossing.
        self._preset_combo.connect("notify::popup-shown", self._on_combo_popup_shown)
        # Selecting a preset is a one-shot "jump the camera there" command,
        # not a persistent setting — but Gtk.ComboBoxText's "changed" only
        # fires on an actual selection change, so re-picking the same
        # preset again (e.g. after someone nudged the camera manually) is
        # silently a no-op. Clearing the selection whenever the popover
        # closes means the next pick is always a real change, even if it's
        # the same preset as last time.
        self._preset_popover = self._make_popover(self._preset_btn, self._preset_combo)
        self._preset_popover.connect("closed", lambda _p: self._preset_combo.set_active(-1))

        # Patrol popover — a dropdown, same shape as Preset's. Selecting a
        # route asks the NAS to run it; Surveillance Station drives the
        # camera from there, so there is nothing to stop client-side.
        self._patrol_combo = Gtk.ComboBoxText()
        self._patrol_combo.connect("notify::popup-shown", self._on_combo_popup_shown)
        self._patrol_combo.connect("changed", self._on_patrol_changed)
        self._patrol_popover = self._make_popover(self._patrol_btn, self._patrol_combo)
        self._patrol_popover.connect("closed", lambda _p: self._patrol_combo.set_active(-1))

        # One shared debounced hide, armed on leaving *any* of the video,
        # the toolbar, or an open popover, and cancelled on entering *any*
        # of them — each popover renders in its own surface above its
        # button rather than as a normal child, so there's a gap between
        # the two a plain per-widget leave-hides-immediately would
        # otherwise collapse the whole toolbar in the middle of. Showing
        # one popover hides any other that's open, so only one is ever up.
        self._toolbar_hide_id = 0
        # True while a pad/zoom/focus button is actively pressed —
        # suppresses auto-hide so the popover can't get torn down mid-hold
        # (which silently drops the button's "released" signal, meaning the
        # matching Stop command never gets sent and the motor keeps
        # running server-side until manually stopped some other way).
        self._popover_button_held = False
        # True while a Preset/Patrol combo's own dropdown list is open —
        # see schedule_hide().
        self._combo_popup_open = False

        # The toolbar box carries the hide/cancel pair, not the individual
        # icons: its padding, its inter-button spacing and the Snapshot
        # button are all part of it, and arming the hide on leaving an icon
        # for any of those would collapse the toolbar under the pointer.
        toolbar_hover = Gtk.EventControllerMotion()
        toolbar_hover.connect("enter", lambda *_a: self.cancel_hide())
        toolbar_hover.connect("leave", lambda *_a: self.schedule_hide())
        toolbar.add_controller(toolbar_hover)

        for icon_btn, popover in (
            (self._mute_btn, self._volume_popover),
            (self._ptz_btn, self._ptz_popover),
            (self._zoom_btn, self._zoom_popover),
            (self._focus_btn, self._focus_popover),
            (self._preset_btn, self._preset_popover),
            (self._patrol_btn, self._patrol_popover),
        ):
            icon_hover = Gtk.EventControllerMotion()
            icon_hover.connect("enter", lambda *_a, p=popover: self._show_only_popover(p))
            icon_btn.add_controller(icon_hover)

            popover_hover = Gtk.EventControllerMotion()
            popover_hover.connect("enter", lambda *_a: self.cancel_hide())
            popover_hover.connect("leave", lambda *_a: self.schedule_hide())
            popover.add_controller(popover_hover)

        self._snapshot_trigger: object = None
        self._volume_changed_callback: object = None
        self._mute_changed_callback: object = None
        self._mic_callback: object = None
        self._zoom_callback: object = None
        self._focus_callback: object = None
        self._ptz_callback: object = None
        self._preset_callback: object = None
        self._patrol_callback: object = None

    def _make_popover(self, parent: Gtk.Widget, child: Gtk.Widget) -> Gtk.Popover:
        """A popover pinned above *parent*. Autohide is off: this class's
        own hover tracking decides when it goes away."""
        popover = Gtk.Popover()
        popover.set_autohide(False)
        popover.set_position(Gtk.PositionType.TOP)
        popover.set_parent(parent)
        popover.set_child(child)
        return popover

    def _plus_minus_box(self, axis: str, out_tooltip: str, in_tooltip: str) -> Gtk.Box:
        """The minus/plus button pair the Zoom and Focus popovers share."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for label, tooltip, control in (("\u2212", out_tooltip, "out"), ("+", in_tooltip, "in")):
            button = Gtk.Button(label=label)
            button.set_tooltip_text(tooltip)
            self._attach_hold(button, axis, control)
            box.append(button)
        return box

    def _attach_hold(self, button: Gtk.Button, axis: str, control: str) -> None:
        """Make *button* send Start while held and Stop on release.

        A capture-phase gesture rather than "clicked": these buttons live
        in a hover-managed popover, and the button's own default-phase
        click gesture can lose the sequence to that hover handling.
        """
        gesture = Gtk.GestureClick()
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("pressed", self._on_hold, axis, control, "Start")
        gesture.connect("released", self._on_hold, axis, control, "Stop")
        button.add_controller(gesture)

    def _on_hold(
        self,
        gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
        axis: str,
        control: str,
        move_type: str,
    ) -> None:
        held = move_type == "Start"
        # Suppress auto-hide while the button is down: tearing the popover
        # down mid-hold drops the "released" signal, so the matching Stop
        # never gets sent and the motor keeps running server-side.
        self._popover_button_held = held
        if held:
            self.cancel_hide()
        callback = {
            "ptz": self._ptz_callback,
            "zoom": self._zoom_callback,
            "focus": self._focus_callback,
        }[axis]
        if callback and callable(callback):
            callback(self.index, control, move_type)

    def set_snapshot_trigger(self, callback: object) -> None:
        """Called once by the owning CameraSlot at construction — the
        toolbar's own Snapshot button just triggers whatever CameraSlot's
        right-click "Take Snapshot" menu item would (a shared code path,
        not toolbar-specific behavior itself)."""
        self._snapshot_trigger = callback

    def _on_snapshot_clicked(self) -> None:
        if self._snapshot_trigger and callable(self._snapshot_trigger):
            self._snapshot_trigger()

    def set_volume_changed_callback(self, callback: object) -> None:
        """Callback(slot_index, volume) — LiveView persists it per-camera;
        this class only owns applying it to the player itself."""
        self._volume_changed_callback = callback

    def set_saved_volume(self, volume: int) -> None:
        """Apply a persisted volume level without re-triggering the
        change callback that would just save the same value straight
        back."""
        self._volume_scale.handler_block_by_func(self._on_volume_changed)
        self._volume_scale.set_value(volume)
        self._volume_scale.handler_unblock_by_func(self._on_volume_changed)
        self.player.set_volume(volume)

    def set_mute_changed_callback(self, callback: object) -> None:
        """Callback(slot_index, muted) — LiveView persists it per-camera;
        this class only owns applying it to the player itself."""
        self._mute_changed_callback = callback

    def set_saved_mute(self, muted: bool) -> None:
        """Apply a persisted mute state — a camera becoming visible again
        restores its last manual mute choice, not a fixed default (unlike
        losing visibility, which always force-mutes — see clear() and
        LiveView's _apply_layout()/pause_streams())."""
        self.player.set_mute(muted)
        self.update_mute_icon()

    def set_audio_playable(self, playable: bool) -> None:
        """Whether the assigned camera's audio can actually reach the
        player right now — separate from whether it *has* audio at all,
        which is what gates the mute button's visibility.

        False can mean the camera has no audio track, its protocol
        doesn't carry one (mjpeg has none at all), or — for a
        WebSocket-protocol camera — DSM's codec-info frame reported an
        audio codec ffmpeg can't mux (see ws_bridge.py; PCMU and AAC are
        supported), or that camera stopped sending audio mid-session and
        the bridge ended the stream to keep its video moving. Called
        again on the camera-status poll, since the last of those happens
        long after the stream started. Ghosts the button rather than
        hiding it, so a camera
        with a microphone that just isn't playable right now doesn't
        look like a camera without one, and says why in the tooltip:
        GTK picks with GTK_PICK_INSENSITIVE for tooltips, so a ghosted
        button still shows one."""
        self._audio_playable = playable
        self._mute_btn.set_sensitive(playable)
        if playable:
            self.update_mute_icon()
        else:
            self._mute_btn.set_tooltip_text(
                "Audio isn't available for this camera right now — no audio "
                "track, an unsupported codec, a protocol that doesn't carry "
                "one (mjpeg), or the camera stopped sending audio and it was "
                "dropped to keep the video going. Try switching protocol in "
                "the sidebar, or restart the stream."
            )

    def update_mute_icon(self) -> None:
        if self.player.muted:
            self._mute_btn.set_icon_name("audio-volume-muted-symbolic")
            if self._audio_playable:
                self._mute_btn.set_tooltip_text("Unmute")
        else:
            self._mute_btn.set_icon_name("audio-volume-high-symbolic")
            if self._audio_playable:
                self._mute_btn.set_tooltip_text("Mute")

    def _on_mute_clicked(self, btn: Gtk.Button) -> None:
        muted = not self.player.muted
        self.player.set_mute(muted)
        self.update_mute_icon()
        if self._mute_changed_callback and callable(self._mute_changed_callback):
            self._mute_changed_callback(self.index, muted)

    def set_mic_callback(self, callback: object) -> None:
        """Callback(slot_index, active) — active is True when the user just
        turned push-to-talk on, False when they just turned it off."""
        self._mic_callback = callback

    def set_mic_active(self, active: bool) -> None:
        """Reflect push-to-talk state in the button's icon/tooltip without
        re-triggering the callback — used to revert the toggle if starting
        a session fails (e.g. the camera's speaker is already in use)."""
        self._mic_active = active
        if active:
            # Pin the toolbar open for as long as the mic is: schedule_hide
            # refuses while _mic_active, and any hide already armed has to
            # go with it.
            self.cancel_hide()
            self.set_reveal_child(True)
            self._mic_btn.set_icon_name("audio-input-microphone-symbolic")
            self._mic_btn.add_css_class("mic-active")
            self._mic_btn.set_tooltip_text("Stop talking")
        else:
            self._mic_btn.remove_css_class("mic-active")
            self._mic_btn.set_tooltip_text("Push-to-talk")
            self.schedule_hide()  # released the pin above

    def _on_mic_clicked(self, btn: Gtk.Button) -> None:
        self.set_mic_active(not self._mic_active)
        if self._mic_callback and callable(self._mic_callback):
            self._mic_callback(self.index, self._mic_active)

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        volume = int(scale.get_value())
        self.player.set_volume(volume)
        if self._volume_changed_callback and callable(self._volume_changed_callback):
            self._volume_changed_callback(self.index, volume)

    def set_zoom_callback(self, callback: object) -> None:
        """Callback(slot_index, direction, move_type) — direction is "in"
        or "out", move_type is "Start" or "Stop"."""
        self._zoom_callback = callback

    def set_focus_callback(self, callback: object) -> None:
        """Callback(slot_index, control, move_type) — control is "in" or
        "out", move_type is "Start" or "Stop"."""
        self._focus_callback = callback

    def set_ptz_callback(self, callback: object) -> None:
        """Callback(slot_index, direction, move_type) — direction is "up",
        "down", "left", or "right", move_type is "Start" or "Stop"."""
        self._ptz_callback = callback

    def _on_ptz_home(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        if self._ptz_callback and callable(self._ptz_callback):
            self._ptz_callback(self.index, "home", "")

    def set_preset_callback(self, callback: object) -> None:
        """Callback(slot_index, preset_id)."""
        self._preset_callback = callback

    def set_presets(self, presets: list[PtzPreset]) -> None:
        """Populate the Preset dropdown — LiveView owns the ptz.list_presets()
        call (same split as zoom/focus/PTZ) and pushes the result here."""
        self._preset_combo.remove_all()
        for p in presets:
            self._preset_combo.append(str(p.id), p.name)

    def set_patrol_callback(self, callback: object) -> None:
        """Callback(slot_index, patrol_id)."""
        self._patrol_callback = callback

    def set_patrols(self, patrols: list[PtzPatrol]) -> None:
        """Populate the Patrol dropdown — see set_presets()."""
        self._patrol_combo.remove_all()
        for p in patrols:
            self._patrol_combo.append(str(p.id), p.name)

    def _on_preset_changed(self, combo: Gtk.ComboBoxText) -> None:
        preset_id_str = combo.get_active_id()
        if not preset_id_str:
            return
        if self._preset_callback and callable(self._preset_callback):
            self._preset_callback(self.index, int(preset_id_str))

    def _on_patrol_changed(self, combo: Gtk.ComboBoxText) -> None:
        patrol_id_str = combo.get_active_id()
        if not patrol_id_str:
            return
        if self._patrol_callback and callable(self._patrol_callback):
            self._patrol_callback(self.index, int(patrol_id_str))

    def _on_combo_popup_shown(self, combo: Gtk.ComboBoxText, pspec: object) -> None:
        self._combo_popup_open = combo.get_property("popup-shown")
        if self._combo_popup_open:
            self.cancel_hide()
        else:
            self.schedule_hide()

    def _all_popovers(self) -> tuple[Gtk.Popover, ...]:
        return (
            self._volume_popover,
            self._ptz_popover,
            self._zoom_popover,
            self._focus_popover,
            self._preset_popover,
            self._patrol_popover,
        )

    def _show_only_popover(self, popover: Gtk.Popover) -> None:
        self.cancel_hide()
        for other in self._all_popovers():
            if other is not popover:
                other.popdown()
        popover.popup()

    def cancel_hide(self) -> None:
        if self._toolbar_hide_id:
            GLib.source_remove(self._toolbar_hide_id)
            self._toolbar_hide_id = 0

    def schedule_hide(self) -> None:
        # Debounced rather than immediate: moving the pointer between the
        # video, an icon, and its popover (which renders in its own
        # surface, not as a normal child of either) is a leave/enter pair
        # on two different widgets each time, so hiding immediately on
        # any single leave would collapse things mid-transition.
        if self._popover_button_held:
            return  # re-armed by the next leave once _on_hold clears it
        if self._mic_active:
            # The lit mic button is the only thing telling the user their
            # microphone is open and being sent to the camera. Hiding the
            # toolbar would leave it live with nothing on screen saying so,
            # so keep it up until push-to-talk stops (set_mic_active
            # re-arms this).
            return
        if self._combo_popup_open:
            # A ComboBoxText's own dropdown list is yet another separate
            # popup surface (below the popover, below the icon) — moving
            # the pointer onto it fires its own "leave" on the popover's
            # motion controller every time, not just once on open, so
            # this has to be a standing guard (re-armed on close in
            # _on_combo_popup_shown), not a one-shot cancel.
            return
        self.cancel_hide()
        self._toolbar_hide_id = GLib.timeout_add(200, self._hide_toolbar)

    def _hide_toolbar(self) -> bool:
        self.set_reveal_child(False)
        for popover in self._all_popovers():
            popover.popdown()
        self._toolbar_hide_id = 0
        return False  # one-shot

    def notify_video_hover_enter(self, has_camera: bool) -> None:
        """Called by the owning CameraSlot when the pointer enters the
        video area (not this toolbar itself)."""
        self.cancel_hide()
        self.set_reveal_child(has_camera)

    def notify_video_hover_leave(self) -> None:
        """Called by the owning CameraSlot when the pointer leaves the
        video area."""
        self.schedule_hide()

    def _ptz_buttons(self) -> tuple[Gtk.Button, ...]:
        return (
            self._ptz_btn,
            self._zoom_btn,
            self._focus_btn,
            self._preset_btn,
            self._patrol_btn,
        )

    def assign(self, camera: Camera) -> None:
        self._mute_btn.set_visible(camera.has_audio)
        self.set_mic_active(False)
        self._mic_btn.set_visible(camera.has_speaker)
        for button in self._ptz_buttons():
            button.set_visible(camera.is_ptz)

    def clear(self) -> None:
        self.player.set_mute(True)
        self.update_mute_icon()
        self._mute_btn.set_visible(False)
        self.set_mic_active(False)
        self._mic_btn.set_visible(False)
        for button in self._ptz_buttons():
            button.set_visible(False)
        self._preset_combo.remove_all()
        self._patrol_combo.remove_all()
        self.cancel_hide()
        self._hide_toolbar()
