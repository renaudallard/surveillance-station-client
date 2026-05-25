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

"""Tests for service layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from surveillance.api.client import SurveillanceAPI
from surveillance.api.models import CameraStatus, HomeModeInfo, LicenseInfo, TimeLapseTask
from surveillance.config import ConnectionProfile


@pytest.fixture
def profile() -> ConnectionProfile:
    return ConnectionProfile(name="test", host="192.168.1.100")


@pytest.fixture
def api(profile: ConnectionProfile) -> SurveillanceAPI:
    client = SurveillanceAPI(profile)
    client.sid = "test-sid"
    return client


class TestCameraService:
    @pytest.mark.asyncio
    async def test_list_cameras(self, api: SurveillanceAPI) -> None:
        from surveillance.services.camera import list_cameras

        mock_data = {
            "cameras": [
                {
                    "id": 1,
                    "newName": "Front Door",
                    "ip": "192.168.1.50",
                    "port": 554,
                    "model": "DS-2CD2386G2",
                    "vendor": "Hikvision",
                    "status": 1,
                    "ptzDirection": 0,
                },
                {
                    "id": 2,
                    "newName": "Garage",
                    "ip": "192.168.1.51",
                    "port": 554,
                    "model": "C6W",
                    "vendor": "EZVIZ",
                    "status": 1,
                    "ptzDirection": 1,
                },
            ]
        }

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            cameras = await list_cameras(api)
            assert len(cameras) == 2
            assert cameras[0].name == "Front Door"
            assert cameras[0].status == CameraStatus.ENABLED
            assert cameras[0].is_ptz is False
            assert cameras[1].is_ptz is True


class TestLiveService:
    @pytest.mark.asyncio
    async def test_get_live_view_path_auto_websocket(self, api: SurveillanceAPI) -> None:
        from surveillance.services.live import get_live_view_path

        url = await get_live_view_path(api, 1)
        assert url.startswith("wss://")
        assert "id=1" in url

    @pytest.mark.asyncio
    async def test_get_live_view_path_rtsp(self, api: SurveillanceAPI) -> None:
        from surveillance.services.live import get_live_view_path

        mock_data = {
            "pathInfos": [
                {
                    "rtspPath": "rtsp://192.168.1.50:554/live",
                    "mjpegHttpPath": "/mjpeg/1",
                }
            ]
        }

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            url = await get_live_view_path(api, 1, protocol="rtsp")
            assert url == "rtsp://192.168.1.50:554/live"

    @pytest.mark.asyncio
    async def test_get_live_view_path_list_format(self, api: SurveillanceAPI) -> None:
        from surveillance.services.live import get_live_view_path

        mock_data = [
            {
                "rtspPath": "rtsp://192.168.1.50:554/Sms/1/1/1",
                "mjpegHttpPath": "/mjpeg/1",
            }
        ]

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            url = await get_live_view_path(api, 1, protocol="rtsp")
            assert url == "rtsp://192.168.1.50:554/Sms/1/1/1"

    @pytest.mark.asyncio
    async def test_get_live_view_path_mjpeg(self, api: SurveillanceAPI) -> None:
        from surveillance.services.live import get_live_view_path

        mock_data = {
            "pathInfos": [
                {
                    "rtspPath": "",
                    "mjpegHttpPath": "/mjpeg/1",
                }
            ]
        }

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            url = await get_live_view_path(api, 1, protocol="mjpeg")
            assert "/mjpeg/1" in url


class TestHomeModeService:
    @pytest.mark.asyncio
    async def test_get_homemode(self, api: SurveillanceAPI) -> None:
        from surveillance.services.homemode import get_homemode

        with patch.object(api, "request", new_callable=AsyncMock, return_value={"on": True}):
            info = await get_homemode(api)
            assert isinstance(info, HomeModeInfo)
            assert info.on is True

    @pytest.mark.asyncio
    async def test_switch_homemode(self, api: SurveillanceAPI) -> None:
        from surveillance.services.homemode import switch_homemode

        with patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock:
            await switch_homemode(api, True)
            mock.assert_called_once()
            call_kwargs = mock.call_args
            assert call_kwargs[1]["extra_params"]["on"] == "true"


class TestEventService:
    @pytest.mark.asyncio
    async def test_list_events(self, api: SurveillanceAPI) -> None:
        from surveillance.services.event import list_events

        mock_data = {
            "events": [
                {
                    "id": 1,
                    "cameraId": 1,
                    "cameraName": "Front Door",
                    "eventType": 1,
                    "startTime": 1700000000,
                    "stopTime": 1700000060,
                }
            ],
            "total": 1,
        }

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            events, total = await list_events(api)
            assert len(events) == 1
            assert total == 1
            assert events[0].event_type == 1

    @pytest.mark.asyncio
    async def test_count_unread_alerts(self, api: SurveillanceAPI) -> None:
        from surveillance.services.event import count_unread_alerts

        with patch.object(api, "request", new_callable=AsyncMock, return_value={"unread": 5}):
            count = await count_unread_alerts(api)
            assert count == 5


class TestLicenseService:
    @pytest.mark.asyncio
    async def test_load_licenses(self, api: SurveillanceAPI) -> None:
        from surveillance.services.license import load_licenses

        mock_data = {
            "key_max": 8,
            "key_total": 2,
            "key_used": 1,
            "license": [
                {
                    "id": 1,
                    "key": "AAAA-BBBB-CCCC-DDDD",
                    "quota": 1,
                    "expired_date": 0,
                }
            ],
        }

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            info = await load_licenses(api)
            assert isinstance(info, LicenseInfo)
            assert info.key_max == 8
            assert info.key_used == 1
            assert len(info.licenses) == 1
            assert info.licenses[0].key == "AAAA-BBBB-CCCC-DDDD"

    @pytest.mark.asyncio
    async def test_delete_license(self, api: SurveillanceAPI) -> None:
        from surveillance.services.license import delete_license

        with patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock:
            await delete_license(api, [1, 2])
            mock.assert_called_once()
            call_kwargs = mock.call_args
            assert call_kwargs[1]["extra_params"]["lic_list"] == "1,2"

    @pytest.mark.asyncio
    async def test_add_license_online(self, api: SurveillanceAPI) -> None:
        from surveillance.services.license import add_license_online

        with patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock:
            await add_license_online(api, ["KEY-1", "KEY-2"])
            mock.assert_called_once()
            call_kwargs = mock.call_args
            assert call_kwargs[1]["extra_params"]["licenseList"] == "KEY-1,KEY-2"

    def test_offline_encrypt(self) -> None:
        from surveillance.services.license import _offline_encrypt

        serial = "TESTSERIAL123"
        seed = 123456
        content = '{"method":"GetTimestamp"}'

        result = _offline_encrypt(content, serial, seed)
        # Verify it produces a valid base64 string
        import base64

        decoded = base64.b64decode(result)
        assert len(decoded) > 0
        # AES-CBC with PKCS7 always produces blocks of 16 bytes
        assert len(decoded) % 16 == 0

        # Verify deterministic: same inputs produce same output
        result2 = _offline_encrypt(content, serial, seed)
        assert result == result2


class TestTimeLapseService:
    @pytest.mark.asyncio
    async def test_list_tasks(self, api: SurveillanceAPI) -> None:
        from surveillance.services.timelapse import list_tasks

        mock_data = {
            "task": [
                {
                    "id": 1,
                    "name": "Front Door Lapse",
                    "cameraId": 3,
                    "cameraName": "Front Door",
                    "enabled": True,
                    "status": 0,
                },
                {
                    "id": 2,
                    "name": "Backyard Lapse",
                    "cameraId": 5,
                    "cameraName": "Backyard",
                    "enabled": False,
                    "status": 1,
                },
            ],
            "total": 2,
        }

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            tasks = await list_tasks(api)
            assert len(tasks) == 2
            assert isinstance(tasks[0], TimeLapseTask)
            assert tasks[0].name == "Front Door Lapse"
            assert tasks[1].enabled is False

    @pytest.mark.asyncio
    async def test_list_recordings(self, api: SurveillanceAPI) -> None:
        from surveillance.services.timelapse import list_recordings

        mock_data = {
            "events": [
                {
                    "id": 10,
                    "cameraId": 3,
                    "camera_name": "Front Door",
                    "startTime": 1700000000,
                    "stopTime": 1700003600,
                    "taskId": 1,
                    "event_size_bytes": 5242880,
                    "status_flags": 0,
                }
            ],
            "total": 1,
        }

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data) as mock:
            recordings, total = await list_recordings(api, task_id=1, offset=0, limit=50)
            assert len(recordings) == 1
            assert total == 1
            assert recordings[0].camera_name == "Front Door"
            call_kwargs = mock.call_args
            assert call_kwargs[1]["extra_params"]["lapseId"] == "1"
            assert call_kwargs[1]["extra_params"]["start"] == "0"
            assert call_kwargs[1]["extra_params"]["limit"] == "50"

    @pytest.mark.asyncio
    async def test_delete_recordings(self, api: SurveillanceAPI) -> None:
        from surveillance.services.timelapse import delete_recordings

        with patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock:
            await delete_recordings(api, [10, 11])
            mock.assert_called_once()
            call_kwargs = mock.call_args
            assert call_kwargs[1]["extra_params"]["idList"] == "10,11"

    @pytest.mark.asyncio
    async def test_lock_recordings(self, api: SurveillanceAPI) -> None:
        from surveillance.services.timelapse import lock_recordings

        with patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock:
            await lock_recordings(api, [10])
            mock.assert_called_once()
            call_kwargs = mock.call_args
            assert call_kwargs[1]["extra_params"]["idList"] == "10"

    @pytest.mark.asyncio
    async def test_unlock_recordings(self, api: SurveillanceAPI) -> None:
        from surveillance.services.timelapse import unlock_recordings

        with patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock:
            await unlock_recordings(api, [10, 11, 12])
            mock.assert_called_once()
            call_kwargs = mock.call_args
            assert call_kwargs[1]["extra_params"]["idList"] == "10,11,12"


class TestRecordingService:
    @pytest.mark.asyncio
    async def test_list_recordings_basic(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        mock_data = {
            "events": [
                {
                    "id": 1,
                    "cameraId": 1,
                    "cameraName": "Front Door",
                    "startTime": 1700000000,
                    "stopTime": 1700000060,
                }
            ],
            "total": 1,
        }

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            recordings, total = await list_recordings(api)
            assert len(recordings) == 1
            assert total == 1
            assert recordings[0].camera_name == "Front Door"

    @pytest.mark.asyncio
    async def test_list_recordings_with_camera_ids(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        mock_data = {"events": [], "total": 0}

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data) as mock:
            await list_recordings(api, camera_ids=[1, 3, 5])
            call_kwargs = mock.call_args
            assert call_kwargs[1]["extra_params"]["cameraIds"] == "1,3,5"

    @pytest.mark.asyncio
    async def test_list_recordings_with_time_range(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        mock_data = {"events": [], "total": 0}

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data) as mock:
            await list_recordings(api, from_time=1700000000, to_time=1700086400)
            call_kwargs = mock.call_args
            assert call_kwargs[1]["extra_params"]["fromTime"] == "1700000000"
            assert call_kwargs[1]["extra_params"]["toTime"] == "1700086400"

    @pytest.mark.asyncio
    async def test_list_recordings_with_all_filters(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        mock_data = {"events": [], "total": 0}

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data) as mock:
            await list_recordings(
                api,
                camera_ids=[2, 4],
                from_time=1700000000,
                to_time=1700086400,
                offset=100,
                limit=20,
            )
            call_kwargs = mock.call_args
            assert call_kwargs[1]["extra_params"]["cameraIds"] == "2,4"
            assert call_kwargs[1]["extra_params"]["fromTime"] == "1700000000"
            assert call_kwargs[1]["extra_params"]["toTime"] == "1700086400"
            assert call_kwargs[1]["extra_params"]["offset"] == "100"
            assert call_kwargs[1]["extra_params"]["limit"] == "20"


class TestDownloadRecordingValidation:
    """Tests for download_recording response validation."""

    @pytest.mark.asyncio
    async def test_empty_response_raises(self, api: SurveillanceAPI, tmp_path: object) -> None:
        from pathlib import Path

        from surveillance.services.recording import download_recording

        out = Path(str(tmp_path)) / "out.mp4"
        with (
            patch.object(api, "download", new_callable=AsyncMock, return_value=b""),
            pytest.raises(ValueError, match="empty response"),
        ):
            await download_recording(api, 1, out)
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_html_doctype_response_raises(
        self, api: SurveillanceAPI, tmp_path: object
    ) -> None:
        from pathlib import Path

        from surveillance.services.recording import download_recording

        html = b"<!doctype html><html><body>Login</body></html>"
        out = Path(str(tmp_path)) / "out.mp4"
        with (
            patch.object(api, "download", new_callable=AsyncMock, return_value=html),
            pytest.raises(ValueError, match="HTML page"),
        ):
            await download_recording(api, 1, out)
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_html_tag_response_raises(
        self, api: SurveillanceAPI, tmp_path: object
    ) -> None:
        from pathlib import Path

        from surveillance.services.recording import download_recording

        html = b"<html><body>Login</body></html>"
        out = Path(str(tmp_path)) / "out.mp4"
        with (
            patch.object(api, "download", new_callable=AsyncMock, return_value=html),
            pytest.raises(ValueError, match="HTML page"),
        ):
            await download_recording(api, 1, out)

    @pytest.mark.asyncio
    async def test_json_error_body_raises(self, api: SurveillanceAPI, tmp_path: object) -> None:
        from pathlib import Path

        from surveillance.services.recording import download_recording

        json_err = b'{"success":false,"error":{"code":105}}'
        out = Path(str(tmp_path)) / "out.mp4"
        with (
            patch.object(api, "download", new_callable=AsyncMock, return_value=json_err),
            pytest.raises(ValueError, match="error code 105"),
        ):
            await download_recording(api, 1, out)
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_successful_download_writes_file(
        self, api: SurveillanceAPI, tmp_path: object
    ) -> None:
        from pathlib import Path

        from surveillance.services.recording import download_recording

        # Minimal ftyp-box header so it looks like a real MP4
        video_data = b"\x00\x00\x00\x18ftyp" + b"isom" + b"\x00" * 8
        out = Path(str(tmp_path)) / "recording.mp4"
        with patch.object(api, "download", new_callable=AsyncMock, return_value=video_data):
            result = await download_recording(api, 42, out)
        assert result == out
        assert out.exists()
        assert out.read_bytes() == video_data

    @pytest.mark.asyncio
    async def test_write_error_cleans_up_file(
        self, api: SurveillanceAPI, tmp_path: object
    ) -> None:
        from pathlib import Path
        from unittest.mock import patch as _patch

        from surveillance.services.recording import download_recording

        video_data = b"\x00\x00\x00\x18ftyp" + b"\x00" * 12
        out = Path(str(tmp_path)) / "recording.mp4"
        with (
            patch.object(api, "download", new_callable=AsyncMock, return_value=video_data),
            _patch.object(Path, "write_bytes", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            await download_recording(api, 42, out)
        assert not out.exists()


class TestMpvSafeSetOption:
    """Tests for safe_set_mpv_option() without requiring a real mpv instance.

    These tests import from surveillance.ui.mpv_widget which in turn imports
    GTK4 via gi.repository.  They are skipped automatically in environments
    where GTK4 (PyGObject + C extension) is not installed.
    """

    @pytest.fixture(autouse=True)
    def _require_gtk(self) -> None:
        try:
            import gi  # noqa: F401

            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk  # noqa: F401
        except Exception:
            pytest.skip("GTK4 not available in this environment")

    def test_normal_set_succeeds(self) -> None:
        from unittest.mock import MagicMock

        from surveillance.ui.mpv_widget import safe_set_mpv_option

        mock_player = MagicMock()
        result = safe_set_mpv_option(mock_player, "cache", "no")
        assert result is True
        mock_player.__setitem__.assert_called_once_with("cache", "no")

    def test_type_error_returns_false_without_raising(self) -> None:
        from unittest.mock import MagicMock

        from surveillance.ui.mpv_widget import safe_set_mpv_option

        mock_player = MagicMock()
        mock_player.__setitem__ = MagicMock(
            side_effect=TypeError(
                "Tried to get/set mpv property using wrong format, "
                "or passed invalid value\noptions/demuxer-lavf-probesize\nvalue: b'0'"
            )
        )
        result = safe_set_mpv_option(mock_player, "demuxer-lavf-probesize", 0)
        assert result is False  # no crash, just False

    def test_value_error_returns_false_without_raising(self) -> None:
        from unittest.mock import MagicMock

        from surveillance.ui.mpv_widget import safe_set_mpv_option

        mock_player = MagicMock()
        mock_player.__setitem__ = MagicMock(side_effect=ValueError("bad value"))
        result = safe_set_mpv_option(mock_player, "container-fps-override", 0)
        assert result is False

    def test_arbitrary_exception_returns_false_without_raising(self) -> None:
        from unittest.mock import MagicMock

        from surveillance.ui.mpv_widget import safe_set_mpv_option

        mock_player = MagicMock()
        mock_player.__setitem__ = MagicMock(side_effect=RuntimeError("mpv died"))
        result = safe_set_mpv_option(mock_player, "untimed", True)
        assert result is False


class TestWebSocketBridgeErrors:
    """Tests for WebSocketBridge error handling without a real WebSocket server."""

    @pytest.mark.asyncio
    async def test_connection_error_calls_on_error_callback(self) -> None:
        from unittest.mock import AsyncMock, patch

        from surveillance.services.ws_bridge import WebSocketBridge

        errors: list[str] = []
        bridge = WebSocketBridge(
            "wss://192.0.2.1/stream",  # TEST-NET address, guaranteed to fail
            verify_ssl=False,
            sid="test-sid",
            on_error=errors.append,
        )

        with patch(
            "websockets.asyncio.client.connect",
            side_effect=OSError("Connection refused"),
        ):
            await bridge.start()
            # Give the pump task time to run and fail
            import asyncio

            await asyncio.sleep(0.05)
            await bridge.stop()

        assert bridge.error is not None
        assert len(errors) == 1

    @pytest.mark.asyncio
    async def test_http_502_classified_correctly(self) -> None:
        from unittest.mock import patch

        from surveillance.services.ws_bridge import WebSocketBridge

        errors: list[str] = []
        bridge = WebSocketBridge(
            "wss://192.0.2.1/stream",
            verify_ssl=False,
            sid="test-sid",
            on_error=errors.append,
        )

        # Simulate an InvalidStatus-like error message containing "502"
        with patch(
            "websockets.asyncio.client.connect",
            side_effect=Exception("server rejected WebSocket connection: HTTP 502"),
        ):
            await bridge.start()
            import asyncio

            await asyncio.sleep(0.05)
            await bridge.stop()

        assert bridge.error is not None
        assert "502" in bridge.error

    @pytest.mark.asyncio
    async def test_no_on_error_does_not_crash(self) -> None:
        """Bridge with no on_error callback should still handle errors gracefully."""
        from unittest.mock import patch

        from surveillance.services.ws_bridge import WebSocketBridge

        bridge = WebSocketBridge(
            "wss://192.0.2.1/stream",
            verify_ssl=False,
            sid="test-sid",
            # no on_error
        )

        with patch(
            "websockets.asyncio.client.connect",
            side_effect=OSError("Connection refused"),
        ):
            await bridge.start()
            import asyncio

            await asyncio.sleep(0.05)
            await bridge.stop()

        # Just verify no exception propagated and error is recorded
        assert bridge.error is not None
