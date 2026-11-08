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

"""Tests for the command line's remote control options (no display needed)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from surveillance.api.models import Camera, CameraStatus
from surveillance.app import RemoteRequest, SurveillanceApp, parse_remote_request
from surveillance.ui.liveview import LiveView
from surveillance.ui.window import MainWindow


class TestParseRemoteRequest:
    def test_no_remote_option_is_no_request(self) -> None:
        assert parse_remote_request({}) is None

    def test_each_option(self) -> None:
        request = parse_remote_request(
            {"grid": "3x3", "camera": "12", "solo": True, "page": "events"}
        )
        assert request == RemoteRequest(grid="3x3", camera_id=12, solo=True, page="events")
        assert parse_remote_request({"live": True}) == RemoteRequest(live=True)
        assert parse_remote_request({"page": "maps"}) == RemoteRequest(page="maps")

    def test_seek_with_zone(self) -> None:
        request = parse_remote_request({"seek": "2026-10-30T22:15:00Z"})
        assert request is not None
        assert request.seek == datetime(2026, 10, 30, 22, 15, tzinfo=UTC).timestamp()

    def test_seek_without_zone_is_local_time(self) -> None:
        request = parse_remote_request({"seek": "2026-10-30T22:15"})
        assert request is not None
        assert request.seek == datetime(2026, 10, 30, 22, 15).timestamp()

    @pytest.mark.parametrize(
        "options",
        [
            {"grid": "5x5"},
            {"camera": "0"},
            {"camera": "-3"},
            {"camera": "abc"},
            {"camera": ""},
            {"camera": " 12"},
            {"solo": True},
            {"seek": "yesterday"},
            {"seek": "2026-10-30T22:15", "live": True},
            {"page": "nowhere"},
        ],
    )
    def test_malformed_option_is_rejected(self, options: dict[str, object]) -> None:
        with pytest.raises(ValueError):
            parse_remote_request(options)


def _command_line(options: dict[str, object]) -> MagicMock:
    command_line = MagicMock()
    command_line.get_options_dict.return_value.end.return_value.unpack.return_value = options
    return command_line


class TestDoCommandLine:
    """What a launch hands the primary instance, first or second."""

    @staticmethod
    def _app() -> Any:
        return SimpleNamespace(activate=MagicMock(), _window=MagicMock())

    def test_request_reaches_the_window(self) -> None:
        app = self._app()
        status = SurveillanceApp.do_command_line(app, _command_line({"page": "events"}))
        assert status == 0
        app.activate.assert_called_once()
        app._window.apply_remote_request.assert_called_once_with(RemoteRequest(page="events"))

    def test_plain_launch_only_raises_the_window(self) -> None:
        app = self._app()
        assert SurveillanceApp.do_command_line(app, _command_line({})) == 0
        app.activate.assert_called_once()
        app._window.apply_remote_request.assert_not_called()

    def test_malformed_option_is_reported_to_the_caller(self) -> None:
        app = self._app()
        command_line = _command_line({"grid": "nope"})
        assert SurveillanceApp.do_command_line(app, command_line) == 2
        app._window.apply_remote_request.assert_not_called()
        command_line.printerr_literal.assert_called_once()


def _camera(camera_id: int) -> Camera:
    return Camera(
        id=camera_id, name=f"Camera {camera_id}", model="m", vendor="v", status=CameraStatus.ENABLED
    )


def _window(*, ready: bool = True, visible: str = "live", pages: tuple[str, ...] = ()) -> Any:
    """A MainWindow stand-in with a mocked Live View and page stack."""
    live = MagicMock()
    children: dict[str, MagicMock] = {name: MagicMock() for name in pages}
    children["live"] = live
    window = SimpleNamespace(
        _live_session_ready=ready,
        _pending_remote_requests=[],
        stack=SimpleNamespace(
            get_child_by_name=children.get,
            get_visible_child_name=lambda: visible,
        ),
        sidebar=SimpleNamespace(cameras=[_camera(3), _camera(12)]),
        _grid_layout_action=MagicMock(),
        show_page=MagicMock(),
        live=live,
    )
    window._run_remote_request = lambda r: MainWindow._run_remote_request(window, r)  # type: ignore[arg-type]
    window._run_live_view_request = lambda lv, r: MainWindow._run_live_view_request(window, lv, r)  # type: ignore[arg-type]
    window.apply_remote_request = lambda r: MainWindow.apply_remote_request(window, r)  # type: ignore[arg-type]
    return window


class TestApplyRemoteRequest:
    def test_grid_goes_through_the_layout_action(self) -> None:
        window = _window()
        window.apply_remote_request(RemoteRequest(grid="4x4"))
        (variant,), _ = window._grid_layout_action.change_state.call_args
        assert variant.get_string() == "4x4"
        window.show_page.assert_not_called()

    def test_camera_into_the_focused_slot(self) -> None:
        window = _window()
        window.apply_remote_request(RemoteRequest(camera_id=12))
        window.live.show_camera.assert_called_once_with(_camera(12), solo=False)

    def test_camera_solo(self) -> None:
        window = _window()
        window.apply_remote_request(RemoteRequest(camera_id=3, solo=True))
        window.live.show_camera.assert_called_once_with(_camera(3), solo=True)

    def test_unknown_camera_is_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        window = _window()
        window.apply_remote_request(RemoteRequest(camera_id=99, live=True))
        window.live.show_camera.assert_not_called()
        # The rest of the request still applies.
        window.live.return_to_live.assert_called_once()
        assert "No camera with ID 99" in caplog.text

    def test_seek_and_live(self) -> None:
        window = _window()
        window.apply_remote_request(RemoteRequest(seek=1_800_000_000.0))
        window.live.seek_to.assert_called_once_with(1_800_000_000.0)
        window.live.return_to_live.assert_not_called()
        window.apply_remote_request(RemoteRequest(live=True))
        window.live.return_to_live.assert_called_once()

    def test_live_view_request_shows_live_view_first(self) -> None:
        window = _window(visible="recordings", pages=("recordings",))
        window.apply_remote_request(RemoteRequest(live=True))
        window.show_page.assert_called_once_with("live")

    def test_page_is_applied_last(self) -> None:
        window = _window(visible="recordings", pages=("recordings", "events"))
        window.apply_remote_request(RemoteRequest(camera_id=3, page="events"))
        assert [c.args for c in window.show_page.call_args_list] == [("live",), ("events",)]

    def test_page_not_built_is_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        window = _window()
        window.apply_remote_request(RemoteRequest(page="timelapse"))
        window.show_page.assert_not_called()
        assert "not available" in caplog.text

    def test_held_until_logged_in(self) -> None:
        window = _window(ready=False)
        request = RemoteRequest(grid="2x2")
        window.apply_remote_request(request)
        assert window._pending_remote_requests == [request]
        window._grid_layout_action.change_state.assert_not_called()

    def test_failure_does_not_escape(self, caplog: pytest.LogCaptureFixture) -> None:
        window = _window()
        window.live.seek_to.side_effect = RuntimeError("boom")
        window.apply_remote_request(RemoteRequest(seek=1.0))
        assert "Command line request failed" in caplog.text

    def test_held_requests_run_once_the_session_is_restored(self) -> None:
        window = _window(ready=False)
        window.apply_remote_request(RemoteRequest(camera_id=12))
        MainWindow._restore_live_session(window, [_camera(12)])
        assert window._live_session_ready
        assert window._pending_remote_requests == []
        window.live.restore_session.assert_called_once()
        window.live.show_camera.assert_called_once_with(_camera(12), solo=False)

    def test_restored_by_a_later_poll_when_the_first_list_failed(self) -> None:
        """The camera list asked for at login can fail. The poll that
        loads it later restored nothing, so the layout stayed empty and
        every request was held until logout."""
        window = _window(ready=False)
        window._restore_live_session = lambda c: MainWindow._restore_live_session(window, c)
        window.apply_remote_request(RemoteRequest(camera_id=12))
        for _poll in range(2):
            MainWindow._on_cameras_updated(window, [_camera(12)])
        assert window._live_session_ready
        assert window._pending_remote_requests == []
        window.live.restore_session.assert_called_once()
        window.live.show_camera.assert_called_once_with(_camera(12), solo=False)


class TestLiveViewEntryPoints:
    """What the window calls on Live View, each the path the UI takes."""

    @staticmethod
    def _page() -> Any:
        page = SimpleNamespace(_timeline_focus_slot=5, calls=[])
        for name in (
            "_select_slot",
            "on_camera_selected",
            "_assign_to_slot",
            "_save_session",
            "_on_calendar_jump",
            "_return_all_to_live",
        ):
            setattr(page, name, lambda *a, name=name: page.calls.append((name, *a)))
        return page

    def test_camera_goes_to_the_slot_the_timeline_tracks(self) -> None:
        page = self._page()
        LiveView.show_camera(page, _camera(12))
        assert page.calls == [("_assign_to_slot", 5, _camera(12)), ("_save_session",)]

    def test_solo_is_the_sidebar_click_with_no_slot_selected(self) -> None:
        page = self._page()
        LiveView.show_camera(page, _camera(12), solo=True)
        assert page.calls == [("_select_slot", None), ("on_camera_selected", _camera(12))]

    def test_seek_is_the_calendar_jump(self) -> None:
        page = self._page()
        LiveView.seek_to(page, 1_800_000_000.0)
        assert page.calls == [("_on_calendar_jump", datetime.fromtimestamp(1_800_000_000.0))]

    def test_live_is_the_live_button(self) -> None:
        page = self._page()
        LiveView.return_to_live(page)
        assert page.calls == [("_return_all_to_live",)]
