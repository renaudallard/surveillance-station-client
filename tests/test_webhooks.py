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

"""Tests for the outgoing webhooks."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Iterator

import httpx
import keyring.errors
import pytest
import respx

from surveillance import credentials
from surveillance.api.models import Camera, CameraStatus
from surveillance.config import WEBHOOK_EVENTS, WebhookConfig
from surveillance.services import webhooks
from surveillance.services.webhooks import WebhookDispatcher

TARGET = "http://127.0.0.1:8790/client-event"
OTHER_TARGET = "http://127.0.0.1:8791/client-event"


def _camera(camera_id: int, status: CameraStatus = CameraStatus.ENABLED) -> Camera:
    return Camera(id=camera_id, name=f"Camera {camera_id}", model="m", vendor="v", status=status)


@pytest.fixture(autouse=True)
def no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """No keyring in tests unless one asks for a token."""
    monkeypatch.setattr(webhooks, "webhook_token", lambda url: None)


@pytest.fixture
async def make_dispatcher() -> AsyncIterator[object]:
    started: list[WebhookDispatcher] = []

    def _make(targets: list[str] | None = None, **kwargs: object) -> WebhookDispatcher:
        settings = WebhookConfig(targets=targets or [TARGET], **kwargs)  # type: ignore[arg-type]
        dispatcher = WebhookDispatcher(settings, "test-nas", asyncio.get_running_loop())
        dispatcher.start()
        started.append(dispatcher)
        return dispatcher

    yield _make
    for dispatcher in started:
        dispatcher.stop()
    await asyncio.sleep(0)


def _bodies(route: respx.Route) -> list[dict[str, object]]:
    return [json.loads(call.request.content) for call in route.calls]


class TestPayload:
    async def test_shape(self, make_dispatcher: object) -> None:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(TARGET).respond(204)
            dispatcher = make_dispatcher()  # type: ignore[operator]
            dispatcher.emit("snapshot_saved", _camera(12), {"snapshot_id": 7})
            await dispatcher.wait_idle()

        (body,) = _bodies(route)
        assert set(body) == {"event", "profile", "camera_id", "camera_name", "at", "detail"}
        assert body["event"] == "snapshot_saved"
        assert body["profile"] == "test-nas"
        assert body["camera_id"] == 12
        assert body["camera_name"] == "Camera 12"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", str(body["at"]))
        assert body["detail"] == {"snapshot_id": 7}
        request = route.calls[0].request
        assert request.headers["content-type"] == "application/json"
        assert "authorization" not in request.headers

    async def test_not_camera_scoped(self, make_dispatcher: object) -> None:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(TARGET).respond(200)
            dispatcher = make_dispatcher()  # type: ignore[operator]
            dispatcher.emit("home_mode", detail={"enabled": True})
            await dispatcher.wait_idle()
        (body,) = _bodies(route)
        assert (body["camera_id"], body["camera_name"]) == (0, "")
        assert body["detail"] == {"enabled": True}

    async def test_bearer_token_from_the_keyring(
        self, make_dispatcher: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[str] = []

        def _token(url: str) -> str | None:
            asked.append(url)
            return "s3cret" if url == TARGET else None

        monkeypatch.setattr(webhooks, "webhook_token", _token)
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(TARGET).respond(200)
            other = mock.post(OTHER_TARGET).respond(200)
            dispatcher = make_dispatcher([TARGET, OTHER_TARGET])  # type: ignore[operator]
            dispatcher.emit("alert")
            await dispatcher.wait_idle()
        assert asked == [TARGET, OTHER_TARGET]
        assert route.calls[0].request.headers["authorization"] == "Bearer s3cret"
        # The other receiver never sees a token that is not its own.
        assert "authorization" not in other.calls[0].request.headers

    async def test_every_target_gets_the_event(self, make_dispatcher: object) -> None:
        with respx.mock(assert_all_called=False) as mock:
            first = mock.post(TARGET).respond(200)
            second = mock.post(OTHER_TARGET).respond(200)
            dispatcher = make_dispatcher([TARGET, OTHER_TARGET])  # type: ignore[operator]
            dispatcher.emit("alert")
            await dispatcher.wait_idle()
        assert len(first.calls) == len(second.calls) == 1

    async def test_events_left_out_are_not_sent(self, make_dispatcher: object) -> None:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(TARGET).respond(200)
            dispatcher = make_dispatcher(events=["alert"])  # type: ignore[operator]
            dispatcher.emit("layout_changed", detail={"grid": "2x2"})
            dispatcher.emit("alert")
            await dispatcher.wait_idle()
        assert [b["event"] for b in _bodies(route)] == ["alert"]


class TestDelivery:
    async def test_full_queue_drops_the_oldest(
        self, make_dispatcher: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(TARGET).respond(200)
            dispatcher = make_dispatcher()  # type: ignore[operator]
            # Queued all in one go, before the worker gets to send any.
            for n in range(webhooks.QUEUE_SIZE + 3):
                dispatcher.emit("alert", detail={"n": n})
            await dispatcher.wait_idle()
        sent = [b["detail"]["n"] for b in _bodies(route)]  # type: ignore[index]
        assert sent == list(range(3, webhooks.QUEUE_SIZE + 3))
        assert caplog.text.count("dropping its oldest events") == 1

    async def test_timeout_is_logged_once_and_dropped(
        self, make_dispatcher: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(TARGET)
            route.side_effect = httpx.ReadTimeout("timed out")
            dispatcher = make_dispatcher()  # type: ignore[operator]
            dispatcher.emit("alert")
            dispatcher.emit("alert")
            await dispatcher.wait_idle()
            assert len(route.calls) == 2
            assert caplog.text.count("failed on alert (ReadTimeout)") == 1

            route.side_effect = None
            route.return_value = httpx.Response(200)
            dispatcher.emit("alert")
            await dispatcher.wait_idle()
        assert caplog.text.count("takes events again") == 1

    async def test_timeout_seconds_caps_the_whole_post(
        self, make_dispatcher: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        """httpx's timeout is per network wait. A receiver answering a byte
        at a time stayed under it and held the target for as long as it
        liked, while the docs promise at most timeout_seconds."""

        async def _drip(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n")
            with contextlib.suppress(ConnectionError):
                for _ in range(6):
                    await writer.drain()
                    await asyncio.sleep(0.15)
                    writer.write(b"x")
                writer.close()

        server = await asyncio.start_server(_drip, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            dispatcher = make_dispatcher(  # type: ignore[operator]
                [f"http://127.0.0.1:{port}/hook"], timeout_seconds=0.3
            )
            loop = asyncio.get_running_loop()
            started = loop.time()
            dispatcher.emit("alert")
            await dispatcher.wait_idle()
            took = loop.time() - started
        finally:
            server.close()
        assert "failed on alert (TimeoutError)" in caplog.text
        assert took < 0.8

    async def test_a_target_down_does_not_hold_up_another(self, make_dispatcher: object) -> None:
        release = asyncio.Event()

        async def _hang(request: httpx.Request) -> httpx.Response:
            await release.wait()
            return httpx.Response(200)

        with respx.mock(assert_all_called=False) as mock:
            mock.post(TARGET).mock(side_effect=_hang)
            other = mock.post(OTHER_TARGET).respond(200)
            dispatcher = make_dispatcher([TARGET, OTHER_TARGET])  # type: ignore[operator]
            dispatcher.emit("alert")
            dispatcher.emit("alert")
            for _ in range(200):
                if len(other.calls) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(other.calls) == 2
            release.set()
            await dispatcher.wait_idle()

    async def test_log_line_keeps_secrets_out(
        self, make_dispatcher: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        target = "http://user:pw@127.0.0.1:8790/client-event?key=abc"
        with respx.mock(assert_all_called=False) as mock:
            mock.post(target).respond(500)
            dispatcher = make_dispatcher([target])  # type: ignore[operator]
            dispatcher.emit("alert")
            await dispatcher.wait_idle()
        assert "http://127.0.0.1:8790/client-event failed on alert (HTTP 500)" in caplog.text
        assert "pw" not in caplog.text
        assert "abc" not in caplog.text


class TestTransitions:
    """The note_*() methods turn polled state into events on a change."""

    @staticmethod
    def _dispatcher() -> tuple[WebhookDispatcher, list[tuple[str, int, object]]]:
        dispatcher = WebhookDispatcher(
            WebhookConfig(targets=[TARGET]), "test-nas", asyncio.new_event_loop()
        )
        sent: list[tuple[str, int, object]] = []
        dispatcher.emit = lambda event, camera=None, detail=None: sent.append(  # type: ignore[method-assign]
            (event, camera.id if camera else 0, detail)
        )
        return dispatcher, sent

    def test_alert_when_the_unread_count_rises(self) -> None:
        dispatcher, sent = self._dispatcher()
        dispatcher.note_unread_alerts(5)  # the baseline at login
        dispatcher.note_unread_alerts(5)
        dispatcher.note_unread_alerts(2)  # read in the popover
        dispatcher.note_unread_alerts(4)
        assert sent == [("alert", 0, {"unread": 4, "new": 2})]

    def test_home_mode_on_a_change(self) -> None:
        dispatcher, sent = self._dispatcher()
        dispatcher.note_home_mode(False)
        dispatcher.note_home_mode(False)
        dispatcher.note_home_mode(True)
        assert sent == [("home_mode", 0, {"enabled": True})]

    def test_camera_offline_and_online(self) -> None:
        dispatcher, sent = self._dispatcher()
        dispatcher.note_cameras([_camera(1), _camera(2)])
        dispatcher.note_cameras([_camera(1, CameraStatus.DISCONNECTED), _camera(2), _camera(3)])
        dispatcher.note_cameras([_camera(1), _camera(2), _camera(3)])
        assert sent == [
            ("camera_offline", 1, {"status": "disconnected"}),
            ("camera_online", 1, {"status": "enabled"}),
        ]

    def test_layout_changed_on_a_new_grid_or_camera(self) -> None:
        dispatcher, sent = self._dispatcher()
        dispatcher.note_layout("2x2", [1, 2, 0, 0])
        dispatcher.note_layout("2x2", [1, 2, 0, 0])  # saved again, unchanged
        dispatcher.note_layout("2x2", [1, 2, 3, 0])
        dispatcher.note_layout("1x1", [3])
        assert sent == [
            ("layout_changed", 0, {"grid": "2x2", "cameras": [1, 2, 0, 0]}),
            ("layout_changed", 0, {"grid": "2x2", "cameras": [1, 2, 3, 0]}),
            ("layout_changed", 0, {"grid": "1x1", "cameras": [3]}),
        ]


class TestModuleSwitch:
    @pytest.fixture(autouse=True)
    def _reset(self) -> Iterator[None]:
        yield
        webhooks.stop()

    def test_off_without_targets(self) -> None:
        webhooks.start(WebhookConfig(), "test-nas")
        assert webhooks._dispatcher is None
        webhooks.emit("alert")  # a no-op, not an error

    def test_off_without_events(self) -> None:
        webhooks.start(WebhookConfig(targets=[TARGET], events=[]), "test-nas")
        assert webhooks._dispatcher is None

    def test_on_with_targets_and_off_at_logout(self) -> None:
        webhooks.start(WebhookConfig(targets=[TARGET]), "test-nas")
        assert webhooks._dispatcher is not None
        webhooks.stop()
        assert webhooks._dispatcher is None

    async def test_a_failure_to_start_is_logged(
        self,
        make_dispatcher: object,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _broken(url: str) -> str:
            raise RuntimeError("no backend")

        monkeypatch.setattr(webhooks, "webhook_token", _broken)
        make_dispatcher()  # type: ignore[operator]
        for _ in range(200):
            if "Webhooks stopped" in caplog.text:
                break
            await asyncio.sleep(0.01)
        assert "Webhooks stopped" in caplog.text


class TestHelpers:
    def test_keyring_error_means_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.undo()  # the real webhook_token, not the autouse stand-in

        def _fail(service: str, name: str) -> str:
            raise keyring.errors.KeyringError("locked")

        monkeypatch.setattr(credentials.keyring, "get_password", _fail)
        assert webhooks.webhook_token(TARGET) is None

    def test_token_item_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.undo()
        asked: list[tuple[str, str]] = []

        def _get_password(service: str, name: str) -> str:
            asked.append((service, name))
            return "t"

        monkeypatch.setattr(credentials.keyring, "get_password", _get_password)
        assert webhooks.webhook_token(TARGET) == "t"
        assert asked == [("surveillance-station", f"webhook_token:{TARGET}")]

    def test_every_event_is_on_by_default(self) -> None:
        assert WebhookConfig().events == list(WEBHOOK_EVENTS)
