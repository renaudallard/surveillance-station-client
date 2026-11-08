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
import time
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera
from surveillance.services import webhooks
from surveillance.services.event import ALERTS_API
from surveillance.ui.headerbar import AppHeaderBar
from surveillance.ui.layouts import valid_layout
from surveillance.ui.sidebar import CameraSidebar

if TYPE_CHECKING:
    from collections.abc import Callable

    from surveillance.app import RemoteRequest, SurveillanceApp
    from surveillance.services.transactions import Transaction
    from surveillance.ui.liveview import LiveView

log = logging.getLogger(__name__)


class MainWindow(Gtk.ApplicationWindow):
    """Main application window with sidebar and content stack."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.app: SurveillanceApp = self.get_application()  # type: ignore[assignment]
        self.set_title("Surveillance Station")
        self.set_default_size(1280, 720)

        # Header bar
        self._setup_actions()
        self.headerbar = AppHeaderBar(self)
        self.set_titlebar(self.headerbar)

        # Main layout: sidebar + content with draggable divider
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.set_child(self.paned)

        # Sidebar
        self.sidebar = CameraSidebar(self)
        self.sidebar.set_visible(self.app.config.sidebar_visible)
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

        # Shown instead of the whole page stack while logged out (see
        # _show_logged_out_state). Simpler than a separate placeholder
        # per page, since none of them have anything to show without a
        # connection anyway, and the sidebar drops their nav rows while
        # logged out so none of them can be reached either.
        logged_out_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        logged_out_box.set_halign(Gtk.Align.CENTER)
        logged_out_box.set_valign(Gtk.Align.CENTER)
        logged_out_icon = Gtk.Image.new_from_icon_name("camera-video-symbolic")
        logged_out_icon.set_pixel_size(64)
        logged_out_icon.add_css_class("dim-label")
        logged_out_box.append(logged_out_icon)
        logged_out_label = Gtk.Label(label="Login to your DSM Server to use the application")
        logged_out_label.add_css_class("title-1")
        logged_out_label.add_css_class("dim-label")
        logged_out_box.append(logged_out_label)
        self.stack.add_named(logged_out_box, "logged_out")

        # Not a placeholder: these are this client's own tunables, with
        # nothing to fetch from the NAS, so the page works logged out
        # and _setup_content_pages leaves it alone.
        from surveillance.ui.settings import SettingsView

        self.stack.add_named(SettingsView(self), "settings")

        self._homemode_poll_id: int = 0
        self._alerts_poll_id: int = 0
        self._transactions_poll_id: int = 0

        # Command line requests (see apply_remote_request) waiting for a
        # login and its camera list, which most of them need.
        self._live_session_ready = False
        self._pending_remote_requests: list[RemoteRequest] = []

        # Window-wide mouse activity, used to keep the Live View timeline's
        # focus-slot frame lit (see LiveView.register_timeline_activity).
        # Capture phase so it sees every motion first, regardless of which
        # child widget the event is actually over or does with it.
        activity = Gtk.EventControllerMotion()
        activity.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        activity.connect("motion", self._on_window_motion)
        self.add_controller(activity)

        # Exit handlers. Both vfunc override and signal connection
        # for maximum compatibility across PyGObject versions.
        self.connect("close-request", lambda *_: self._force_exit("signal"))

        # Show login if not connected
        if self.app.api:
            self.stack.set_visible_child_name("live")
            self.headerbar.set_page("live")
        else:
            self._show_logged_out_state()
            self._schedule_login()

    def _setup_actions(self) -> None:
        """Register the window actions driven by the header bar menus."""
        self._grid_layout_action = Gio.SimpleAction.new_stateful(
            "grid-layout",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(valid_layout(self.app.config.grid_layout)),
        )
        self._grid_layout_action.connect("change-state", self._on_grid_layout_action)
        self.add_action(self._grid_layout_action)

        clear_action = Gio.SimpleAction.new("clear-layout", None)
        clear_action.connect("activate", self._on_clear_layout_action)
        self.add_action(clear_action)

    def _on_window_motion(
        self, _controller: Gtk.EventControllerMotion, _x: float, _y: float
    ) -> None:
        live_view = self.stack.get_child_by_name("live")
        if live_view and hasattr(live_view, "register_timeline_activity"):
            live_view.register_timeline_activity()

    def _on_grid_layout_action(self, action: Gio.SimpleAction, value: GLib.Variant) -> None:
        action.set_state(value)
        live_view = self.stack.get_child_by_name("live")
        if live_view and hasattr(live_view, "set_layout"):
            live_view.set_layout(value.get_string())

    def _on_clear_layout_action(self, action: Gio.SimpleAction, param: None) -> None:
        live_view = self.stack.get_child_by_name("live")
        if live_view and hasattr(live_view, "confirm_clear_layout"):
            live_view.confirm_clear_layout()

    def sync_grid_layout(self, layout: str) -> None:
        """Reflect a layout change made by the live view itself in the menu."""
        self._grid_layout_action.set_state(GLib.Variant.new_string(layout))

    def do_close_request(self) -> bool:
        """Vfunc override for close-request."""
        self._force_exit("vfunc")
        return True

    def _force_exit(self, source: str) -> bool:
        log.warning("exit requested via %s", source)
        self.app.exit_now()

    def _schedule_login(self) -> None:
        """Show login dialog after window is shown."""
        GLib.idle_add(self.show_login)

    def show_login(self) -> None:
        """Show the login dialog."""
        from surveillance.ui.login import LoginDialog

        dialog = LoginDialog(self.app, self)
        dialog.present()

    def _show_logged_out_state(self) -> None:
        """Drop every control that needs a connection to do anything and
        show a single login prompt in place of the whole page stack.
        There is nothing any of them has to show without one anyway, so
        gating each individually would just be more to keep in sync for
        no real benefit. The sidebar stays, trimmed to its Settings row
        (see CameraSidebar.set_logged_out): that page is this client's
        own tunables and works with no NAS behind it, and the sidebar is
        the only way to reach it. Safe to call before a first login too,
        not just on logout: set_connected also covers the header
        controls' initial hidden state, which AppHeaderBar.__init__
        itself only half-does (sensitivity, not visibility). Undone by
        _setup_content_pages() on login.
        """
        self.headerbar.set_connected(False)
        self.sidebar.set_logged_out(True)
        self.sidebar.set_visible(self.app.config.sidebar_visible)
        self.stack.set_visible_child_name("logged_out")
        self.headerbar.set_page("logged_out")

    def on_connected(self) -> None:
        """Called after successful login."""
        self.headerbar.set_connected(True)
        if self.app.api:
            webhooks.start(self.app.config.webhooks, self.app.api.profile.name)
        self._setup_content_pages()
        self.sidebar.refresh()
        self.sidebar.start_polling()
        self._start_polling()
        self._check_for_update()

    def _check_for_update(self) -> None:
        """Check GitHub once per launch for a release newer than this build.

        Best-effort only: get_latest_release() never raises, so no
        error_callback is needed here.
        """
        from surveillance import __version__
        from surveillance.services.update_check import get_latest_release, is_newer
        from surveillance.util.async_bridge import run_async

        async def _check() -> tuple[str, str] | None:
            release = await get_latest_release()
            if release and is_newer(release[0], __version__):
                return release
            return None

        run_async(_check(), callback=self._on_update_check_result)

    def _on_update_check_result(self, release: tuple[str, str] | None) -> None:
        if not release:
            return
        self.app.latest_release = release
        tag, _url = release
        show_dot = tag != self.app.config.dismissed_update_version
        self.sidebar.set_update_available(show_dot)
        self.headerbar.set_update_available(show_dot)

        # This check runs async and can finish after About was already
        # shown (e.g. it's the restored last_page at startup) — that
        # on_page_shown() call ran with no release known yet, so its
        # banner needs a nudge now rather than waiting for the next visit.
        if self.stack.get_visible_child_name() == "about":
            about_page = self.stack.get_child_by_name("about")
            if about_page and hasattr(about_page, "on_page_shown"):
                about_page.on_page_shown()

    def _restore_live_session(self, cameras: list[Camera]) -> None:
        """Restore live view camera assignments and refresh other pages' camera filters.

        Runs once the sidebar's camera list has actually loaded. Recordings,
        Snapshots, and Events build their camera filter dropdown from that
        same list at construction time, which happens before this — so
        without this, their filter would only ever show whatever camera
        happened to already be loaded (usually none) plus any camera the
        user later clicked in the sidebar.
        """
        live_view = self.stack.get_child_by_name("live")
        if live_view and hasattr(live_view, "restore_session"):
            live_view.restore_session(cameras)

        for page_name in ("recordings", "snapshots", "events"):
            page = self.stack.get_child_by_name(page_name)
            if page and hasattr(page, "refresh_camera_filter"):
                page.refresh_camera_filter()

        # Last, so a request made before the login overrides the page
        # and layout the session was restored to.
        self._live_session_ready = True
        pending, self._pending_remote_requests = self._pending_remote_requests, []
        for request in pending:
            self.apply_remote_request(request)

    def apply_remote_request(self, request: RemoteRequest) -> None:
        """Carry out a request from the command line (see
        surveillance.app.do_command_line): grid, then camera, then seek
        or live, then page. Anything for Live View shows that page first.

        Held back until logged in with the camera list loaded. What does
        not apply here (an unknown camera, a page not built) is logged
        and skipped; nothing a command line sends may break the window.
        """
        if not self._live_session_ready:
            log.info("Holding command line request until logged in: %s", request)
            self._pending_remote_requests.append(request)
            return
        try:
            self._run_remote_request(request)
        except Exception:
            log.exception("Command line request failed: %s", request)

    def _run_remote_request(self, request: RemoteRequest) -> None:
        wants_live_view = (
            request.grid is not None
            or request.camera_id is not None
            or request.seek is not None
            or request.live
        )
        if wants_live_view:
            live = self.stack.get_child_by_name("live")
            if live is None or not hasattr(live, "show_camera"):
                log.warning("Live View is not available, ignoring %s", request)
            else:
                self._run_live_view_request(live, request)
        if request.page is not None:
            if self.stack.get_child_by_name(request.page) is None:
                log.warning("Page %r is not available", request.page)
            else:
                self.show_page(request.page)

    def _run_live_view_request(self, live: LiveView, request: RemoteRequest) -> None:
        if self.stack.get_visible_child_name() != "live":
            self.show_page("live")
        if request.grid is not None:
            # Through the action, like the header bar's grid menu, so the
            # menu shows the new layout too.
            self._grid_layout_action.change_state(GLib.Variant.new_string(request.grid))
        if request.camera_id is not None:
            camera = next((c for c in self.sidebar.cameras if c.id == request.camera_id), None)
            if camera is None:
                log.warning("No camera with ID %d", request.camera_id)
            else:
                live.show_camera(camera, solo=request.solo)
        if request.seek is not None:
            live.seek_to(request.seek)
        elif request.live:
            live.return_to_live()

    def _setup_content_pages(self) -> None:
        """Build the real content widgets and undo _show_logged_out_state()."""
        self.sidebar.set_logged_out(False)
        self.sidebar.set_visible(self.app.config.sidebar_visible)

        from surveillance.ui.about import AboutView
        from surveillance.ui.emaps import EMapsView
        from surveillance.ui.events import EventsView
        from surveillance.ui.licenses import LicensesView
        from surveillance.ui.liveview import LiveView
        from surveillance.ui.recordings import RecordingsView
        from surveillance.ui.snapshots import SnapshotsView
        from surveillance.ui.timelapse import TimeLapseView

        # Replace with fresh widgets. A no-op the first time (nothing
        # to remove yet), and a clean rebuild against the new session on
        # a later re-login.
        for name, widget_class in [
            ("live", LiveView),
            ("recordings", RecordingsView),
            ("snapshots", SnapshotsView),
            ("events", EventsView),
            ("timelapse", TimeLapseView),
            ("maps", EMapsView),
            ("licenses", LicensesView),
            ("about", AboutView),
        ]:
            old = self.stack.get_child_by_name(name)
            if old:
                self.stack.remove(old)
            widget = widget_class(self)
            self.stack.add_named(widget, name)
        self.sidebar.on_cameras_updated = self._on_cameras_updated

        # Restore last active page
        last_page = self.app.config.last_page
        if not self.stack.get_child_by_name(last_page):
            last_page = "live"
        self.stack.set_visible_child_name(last_page)
        self.headerbar.set_page(last_page)
        self.sidebar.set_active_page(last_page)

        # This bypasses show_page(), which normally pauses Live View's
        # streams on navigating away — so without this, the session-restore
        # triggered by login (see _restore_live_session) would start every
        # camera in the last-used layout playing (with audio, if unmuted)
        # even though a different page is what's actually shown.
        if last_page != "live":
            live_view = self.stack.get_child_by_name("live")
            if live_view and hasattr(live_view, "pause_streams"):
                live_view.pause_streams()

    def _on_cameras_updated(self, cameras: list[Camera]) -> None:
        """After every camera list refresh, the periodic poll included."""
        live_view = self.stack.get_child_by_name("live")
        if live_view and hasattr(live_view, "sync_camera_statuses"):
            live_view.sync_camera_statuses(cameras)
        webhooks.note_cameras(cameras)
        # On the first list that loads, not only the one asked for at
        # login: when that one failed, the poll that filled the sidebar
        # later restored nothing, and every command line request was
        # held until logout.
        if not self._live_session_ready:
            self._restore_live_session(cameras)

    def _on_home_mode(self, on: bool) -> None:
        self.headerbar.set_home_mode(on)
        webhooks.note_home_mode(on)

    def _set_alerts_available(self, available: bool) -> None:
        self._alerts_available = available
        self.headerbar.set_notifications_available(available)

    def _on_unread_alerts(self, count: int) -> None:
        self.headerbar.set_notification_count(count)
        webhooks.note_unread_alerts(count)

    def _for_session(self, handler: Callable[[Any], None]) -> Callable[[Any], None]:
        """*handler*, for a poll's answer, run only while the session it
        asked is still the one logged in.

        An answer from the NAS logged out of could land after the next
        login: it became the next profile's webhook baseline, set its
        header, or switched its polling off.
        """
        api = self.app.api

        def _run(value: Any) -> None:
            if self.app.api is api:
                handler(value)

        return _run

    def _start_polling(self) -> None:
        """Start background polling for alerts and home mode."""
        self._stop_polling()

        from surveillance.util.async_bridge import run_async

        self._homemode_available = True
        self._set_alerts_available(self.app.api is not None and self.app.api.has_api(ALERTS_API))

        # Poll home mode
        def _poll_homemode() -> bool:
            if not self._homemode_available:
                # Returning False is GLib destroying this source, so drop the
                # id with it. _stop_polling() would otherwise call
                # source_remove() on a source that is already gone.
                self._homemode_poll_id = 0
                return False
            if not self.app.api:
                return True
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
                callback=self._for_session(lambda info: self._on_home_mode(info.on)),
                error_callback=self._for_session(_homemode_error),
            )
            return True

        self._homemode_poll_id = GLib.timeout_add_seconds(
            self.app.config.poll_interval_homemode, _poll_homemode
        )
        _poll_homemode()  # initial fetch

        # Poll alerts
        def _poll_alerts() -> bool:
            if not self._alerts_available:
                self._alerts_poll_id = 0
                return False
            if not self.app.api:
                return True
            from surveillance.services.event import count_unread_alerts

            def _alerts_error(e: Exception) -> None:
                from surveillance.api.client import ApiError

                if isinstance(e, ApiError) and e.code in (102, 103, 104):
                    log.debug("Notification API not available: %s", e)
                    self._set_alerts_available(False)
                else:
                    log.debug("Alert poll failed: %s", e)

            run_async(
                count_unread_alerts(self.app.api),
                callback=self._for_session(self._on_unread_alerts),
                error_callback=self._for_session(_alerts_error),
            )
            return True

        if self._alerts_available:
            self._alerts_poll_id = GLib.timeout_add_seconds(
                self.app.config.poll_interval_alerts, _poll_alerts
            )
            _poll_alerts()

        self._start_transactions_polling()

    def _start_transactions_polling(self) -> None:
        """Show new POS receipts (Surveillance Station Transactions) over
        the Live View slots showing each POS device's paired camera."""
        from surveillance.util.async_bridge import run_async

        self._transactions_available = True
        # POS device id -> camera id, None until first read.
        self._pos_cameras: dict[int, int] | None = None
        self._pos_cameras_at = 0.0
        self._seen_transactions: dict[str, str] = {}  # id -> text

        def _live_view() -> LiveView | None:
            from surveillance.ui.liveview import LiveView

            live = self.stack.get_child_by_name("live")
            return live if isinstance(live, LiveView) else None

        async def _fetch(
            shown: set[int], paired: dict[int, int] | None
        ) -> tuple[dict[int, int] | None, tuple[int, list[Transaction]]]:
            """Re-read the pairing when *paired* is None, then the
            receipts if a paired camera is among *shown*."""
            from surveillance.services.transactions import (
                get_paired_devices,
                get_recent_transactions,
            )

            api = self.app.api
            if api is None:
                return None, (0, [])
            cameras = None
            if paired is None:
                cameras = paired = await get_paired_devices(api)
            if not shown & set(paired.values()):
                return cameras, (0, [])
            return cameras, await get_recent_transactions(api)

        def _apply(result: tuple[dict[int, int] | None, tuple[int, list[Transaction]]]) -> None:
            cameras, (now, transactions) = result
            if cameras is not None:
                self._pos_cameras = cameras
                self._pos_cameras_at = time.monotonic()
            from surveillance.services.transactions import fresh_receipts
            from surveillance.ui import liveview

            live = _live_view()
            # The NAS's own clock, as receipt times are in it: a client
            # running a minute fast would otherwise see every one as stale.
            cutoff = (now or time.time()) - liveview._RECEIPT_SECONDS
            paired = self._pos_cameras or {}
            for tx in fresh_receipts(transactions, self._seen_transactions, cutoff):
                camera_id = paired.get(tx.pos_id)
                if live is not None and camera_id:
                    live.show_transaction(camera_id, tx.text)
            if len(self._seen_transactions) > 500:
                self._seen_transactions = {tx.id: tx.text for tx in transactions}

        def _transactions_error(e: Exception) -> None:
            from surveillance.api.auth import SessionExpiredError
            from surveillance.api.client import ApiError

            # 102-104: no such API/method/version; 105 even after the fresh
            # login request() makes for it: the account lacks the
            # Transactions privilege; 410: the Transactions add-on is off.
            # A failed re-login must not retry a login every poll; the
            # next connect starts polling again.
            if isinstance(e, SessionExpiredError) or (
                isinstance(e, ApiError) and e.code in (102, 103, 104, 105, 410)
            ):
                log.info("POS transactions overlay off: %s", e)
                self._transactions_available = False
            else:
                log.debug("Transactions poll failed: %s", e)

        def _poll_transactions() -> bool:
            if not self._transactions_available:
                self._transactions_poll_id = 0
                return False
            from surveillance.ui import liveview

            # Switched off on the Settings page: keep the timer, skip the
            # request, so switching it back on needs no restart.
            if not self.app.api or not liveview._RECEIPT_ENABLED:
                return True
            live = _live_view()
            if live is None:
                return True
            shown = live.displayed_camera_ids()
            paired = self._pos_cameras
            # The pairing rarely changes; re-read it every few minutes.
            if paired is not None and time.monotonic() - self._pos_cameras_at > 300:
                paired = None
            # Nothing to draw on: skip the request (each row carries a
            # snapshot) until a paired camera is on screen, also when
            # nothing is paired at all.
            if paired is not None and not shown & set(paired.values()):
                return True
            run_async(
                _fetch(shown, paired),
                callback=self._for_session(_apply),
                error_callback=self._for_session(_transactions_error),
            )
            return True

        self._transactions_poll_id = GLib.timeout_add_seconds(
            self.app.config.poll_interval_transactions, _poll_transactions
        )
        _poll_transactions()

    def _stop_polling(self) -> None:
        """Stop background polling for alerts and home mode."""
        if self._homemode_poll_id:
            GLib.source_remove(self._homemode_poll_id)
            self._homemode_poll_id = 0
        if self._alerts_poll_id:
            GLib.source_remove(self._alerts_poll_id)
            self._alerts_poll_id = 0
        if self._transactions_poll_id:
            GLib.source_remove(self._transactions_poll_id)
            self._transactions_poll_id = 0

    def on_disconnected(self) -> None:
        """Clean up when disconnected (logout/quit)."""
        self._live_session_ready = False
        webhooks.stop()
        self.sidebar.stop_polling()
        self.sidebar.clear()
        self._stop_polling()
        self._show_logged_out_state()

        live_view = self.stack.get_child_by_name("live")
        if live_view and hasattr(live_view, "stop_all"):
            live_view.stop_all()

        from surveillance.services.recording import clear_snapshot_cache

        clear_snapshot_cache()

    def on_camera_selected(self, camera: Camera) -> None:
        """Handle camera selection from sidebar."""
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

    def reload_all_streams(self) -> None:
        """Restart every live stream, from the header bar's Reload button."""
        live_view = self.stack.get_child_by_name("live")
        if live_view and hasattr(live_view, "reload_all_streams"):
            live_view.reload_all_streams()

    def clear_selected_slot(self) -> None:
        """Clear the selected grid slot on the visible page, if it has one."""
        current_page = self.stack.get_visible_child()
        if current_page and hasattr(current_page, "clear_selected_slot"):
            current_page.clear_selected_slot()

    def toggle_sidebar(self, visible: bool) -> None:
        """Show or hide the camera sidebar panel."""
        self.sidebar.set_visible(visible)
        self.app.config.sidebar_visible = visible

        from surveillance.config import save_config

        save_config(self.app.config)

    def toggle_timeline(self, visible: bool) -> None:
        """Show or hide the Live View timeline."""
        live_view = self.stack.get_child_by_name("live")
        if live_view and hasattr(live_view, "timeline"):
            live_view.timeline.set_visible(visible)
        self.app.config.timeline_visible = visible

        from surveillance.config import save_config

        save_config(self.app.config)

    def show_page(self, page_name: str) -> None:
        """Switch to a content page, pausing/resuming live streams as needed."""
        previous = self.stack.get_visible_child_name()
        self.stack.set_visible_child_name(page_name)
        self.headerbar.set_page(page_name)
        self.sidebar.set_active_page(page_name)
        self.app.config.last_page = page_name
        from surveillance.config import save_config

        save_config(self.app.config)

        new_page = self.stack.get_child_by_name(page_name)
        if new_page and hasattr(new_page, "on_page_shown"):
            new_page.on_page_shown()

        live_view = self.stack.get_child_by_name("live")
        if not live_view or not hasattr(live_view, "pause_streams"):
            return
        if previous == "live" and page_name != "live":
            live_view.pause_streams()
        elif previous != "live" and page_name == "live":
            live_view.resume_streams()
