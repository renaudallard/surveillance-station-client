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
from surveillance.api.models import CameraStatus, HomeModeInfo, LicenseInfo
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
            url = await get_live_view_path(api, 1)
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
            url = await get_live_view_path(api, 1)
            assert url == "rtsp://192.168.1.50:554/Sms/1/1/1"

    @pytest.mark.asyncio
    async def test_get_live_view_path_mjpeg_fallback(self, api: SurveillanceAPI) -> None:
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
            url = await get_live_view_path(api, 1)
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
