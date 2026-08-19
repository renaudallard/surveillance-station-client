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

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from surveillance.api.client import SurveillanceAPI
from surveillance.api.models import (
    CameraStatus,
    HomeModeInfo,
    LicenseInfo,
    Recording,
    TimeLapseTask,
)
from surveillance.config import ConnectionProfile


def _stream_mock(data: bytes) -> MagicMock:
    """Stand-in for SurveillanceAPI.stream_download.

    It is an async generator function, not a coroutine, so AsyncMock is the
    wrong shape: calling it must return something `async for` can iterate.
    """

    def _call(**kwargs: object) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            if data:
                yield data

        return _gen()

    return MagicMock(side_effect=_call)


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

    def test_get_history_view_path(self, api: SurveillanceAPI) -> None:
        from surveillance.services.live import get_history_view_path

        url = get_history_view_path(api)
        assert url.startswith("wss://")
        assert "stmSrc=2" in url
        # No camera/recording here -- selected by WebSocketBridge's
        # in-band action=play message instead (see its module docstring).
        assert "id=0" in url

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


class TestOfflinePlaceholderUrl:
    """OFFLINE_PLACEHOLDER_URL is a valid, self-contained mpv lavfi URL."""

    def test_is_a_lavfi_url_with_offline_text(self) -> None:
        from surveillance.services.live import OFFLINE_PLACEHOLDER_URL

        assert OFFLINE_PLACEHOLDER_URL.startswith("av://lavfi:")
        assert "text='Camera offline'" in OFFLINE_PLACEHOLDER_URL


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


class TestPtzService:
    @pytest.mark.asyncio
    async def test_move(self, api: SurveillanceAPI) -> None:
        from surveillance.services.ptz import move

        with patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock:
            await move(api, 40, "upStart")
            call_kwargs = mock.call_args[1]
            assert call_kwargs["method"] == "Move"
            assert call_kwargs["version"] == 2
            assert call_kwargs["extra_params"]["cameraId"] == "40"
            assert call_kwargs["extra_params"]["direction"] == "upStart"

    @pytest.mark.asyncio
    async def test_zoom(self, api: SurveillanceAPI) -> None:
        from surveillance.services.ptz import zoom

        with patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock:
            await zoom(api, 40, "inStart")
            call_kwargs = mock.call_args[1]
            assert call_kwargs["method"] == "Zoom"
            assert call_kwargs["extra_params"]["control"] == "inStart"

    @pytest.mark.asyncio
    async def test_focus(self, api: SurveillanceAPI) -> None:
        from surveillance.services.ptz import focus

        with patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock:
            await focus(api, 40, "in", "Start")
            call_kwargs = mock.call_args[1]
            assert call_kwargs["method"] == "Focus"
            assert call_kwargs["version"] == 6
            assert call_kwargs["extra_params"]["moveType"] == "Start"
            assert call_kwargs["extra_params"]["control"] == "in"

    @pytest.mark.asyncio
    async def test_go_preset(self, api: SurveillanceAPI) -> None:
        from surveillance.services.ptz import go_preset

        with patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock:
            await go_preset(api, 40, 6)
            call_kwargs = mock.call_args[1]
            assert call_kwargs["method"] == "GoPreset"
            assert call_kwargs["extra_params"]["presetId"] == "6"

    @pytest.mark.asyncio
    async def test_list_presets(self, api: SurveillanceAPI) -> None:
        from surveillance.services.ptz import list_presets

        mock_data = {"presets": [{"id": 1, "name": "Front"}, {"id": 2, "name": "Gate"}]}
        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            presets = await list_presets(api, 40)
            assert len(presets) == 2
            assert presets[0].name == "Front"

    @pytest.mark.asyncio
    async def test_list_patrols(self, api: SurveillanceAPI) -> None:
        from surveillance.services.ptz import list_patrols

        mock_data = {"patrols": [{"id": 2, "name": "Perimeter"}]}
        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            patrols = await list_patrols(api, 40)
            assert len(patrols) == 1
            assert patrols[0].name == "Perimeter"

    @pytest.mark.asyncio
    async def test_run_patrol(self, api: SurveillanceAPI) -> None:
        from surveillance.services.ptz import run_patrol

        with patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock:
            await run_patrol(api, 40, 2)
            call_kwargs = mock.call_args[1]
            assert call_kwargs["method"] == "RunPatrol"
            assert call_kwargs["version"] == 2
            assert call_kwargs["extra_params"]["cameraId"] == "40"
            assert call_kwargs["extra_params"]["patrolId"] == "2"


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

    @pytest.mark.asyncio
    async def test_list_granular_events_decodes_event_map(self, api: SurveillanceAPI) -> None:
        """event_map is a run-length-encoded bitmap: [ticks, flag, reserved]
        entries at a fixed interval. This decodes a synthetic response
        covering baseline (flag=1), a real event (flag=513, bits {0,9} =
        Audio detected — see EVENT_BITMASK.md), and the "not processed yet"
        placeholder (flag=0) seen at the live edge of a still-recording
        segment — which must NOT produce a phantom event. event_type is
        always the raw flag value, never reclassified here."""
        from surveillance.services.event import list_granular_events

        from_time = 1700000000
        mock_data = {
            "cameras": [
                [
                    {
                        "camera_id": 1,
                        "mountId": 7,
                        "archId": 3,
                        "event": [
                            {
                                "id": 555,
                                "start": from_time,
                                "stop": from_time + 100,
                            }
                        ],
                        "event_map": [
                            [2, 1, 0],  # 10s baseline
                            [3, 513, 0],  # 15s real event (Audio detected)
                            [4, 0, 0],  # 20s "not processed yet" — not an event
                        ],
                    }
                ]
            ]
        }

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            events = await list_granular_events(
                api, [1], {1: "Front Door"}, from_time, from_time + 100
            )

        assert len(events) == 1
        event = events[0]
        assert event.id == 555
        assert event.camera_name == "Front Door"
        assert event.event_type == 513
        assert event.start_time == from_time + 10
        assert event.stop_time == from_time + 25
        assert event.mount_id == 7
        assert event.arch_id == 3
        assert event.seek_offset == 10
        assert event.reserved == 0

    @pytest.mark.asyncio
    async def test_list_granular_events_reserved_field_passthrough(
        self, api: SurveillanceAPI
    ) -> None:
        """The event_map RLE tuple's 3rd element (reserved) must round-trip
        onto Event.reserved — it carries Object Removal Detection on
        Hikvision via overflow once the 32-bit flag budget is exhausted
        (see EVENT_BITMASK.md), and was previously discarded entirely."""
        from surveillance.services.event import list_granular_events

        from_time = 1700000000
        mock_data = {
            "cameras": [
                [
                    {
                        "camera_id": 36,
                        "event": [{"id": 1, "start": from_time, "stop": from_time + 100}],
                        "event_map": [[1, 1, 1]],  # flag=1 (non-event) but reserved=1
                    }
                ]
            ]
        }

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            events = await list_granular_events(
                api, [36], {36: "Cam 83"}, from_time, from_time + 100
            )

        # flag=1 alone is a non-event flag, but reserved=1 still means
        # something happened (Object Removal Detection) — must not be
        # dropped just because the main flag looks quiet.
        assert len(events) == 1
        assert events[0].event_type == 1
        assert events[0].reserved == 1

    @pytest.mark.asyncio
    async def test_list_granular_events_unrecognized_flag_passes_through(
        self, api: SurveillanceAPI
    ) -> None:
        """A flag value we don't know the meaning of (e.g. the bit8 pattern
        seen on cameras with Person Detect and Tampering both off) must NOT
        be reclassified/guessed — event_type is the raw flag, unchanged."""
        from surveillance.services.event import list_granular_events

        from_time = 1700000000
        mock_data = {
            "cameras": [
                [
                    {
                        "camera_id": 40,
                        "event": [
                            {
                                "id": 999,
                                "start": from_time,
                                "stop": from_time + 100,
                                "mountId": 0,
                                "archId": 0,
                            }
                        ],
                        "event_map": [[5, 257, 0]],  # bit0|bit8, meaning unconfirmed
                    }
                ]
            ]
        }

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            events = await list_granular_events(
                api, [40], {40: "CAM 59"}, from_time, from_time + 100
            )

        assert len(events) == 1
        assert events[0].event_type == 257

    @pytest.mark.asyncio
    async def test_list_granular_events_no_cameras(self, api: SurveillanceAPI) -> None:
        from surveillance.services.event import list_granular_events

        with patch.object(api, "request", new_callable=AsyncMock) as mock_request:
            events = await list_granular_events(api, [], {}, 1700000000, 1700000100)

        assert events == []
        mock_request.assert_not_called()


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

    @pytest.mark.asyncio
    async def test_offline_activate_sends_encdata_to_the_nas(self, api: SurveillanceAPI) -> None:
        """The license server's encData is what installs the keys, so it has
        to reach the NAS along with the seed it was signed under."""
        from surveillance.services import license as lic_mod

        reply = {"success": True, "encData": "SIGNED-BLOB"}
        with (
            patch.object(
                lic_mod, "get_device_info", new_callable=AsyncMock, return_value=("SN", "DS")
            ),
            patch.object(
                lic_mod, "_offline_request", new_callable=AsyncMock, return_value=(reply, 424242)
            ),
            patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock,
        ):
            await lic_mod.offline_activate(api, ["KEY-1", "KEY-2"])

        params = mock.call_args[1]["extra_params"]
        assert mock.call_args[1]["method"] == "AddKey"
        assert params["licenseList"] == "KEY-1,KEY-2"
        assert params["encData"] == "SIGNED-BLOB"
        assert params["encSeed"] == "424242"

    @pytest.mark.asyncio
    async def test_offline_activate_rejects_a_failed_reply(self, api: SurveillanceAPI) -> None:
        """A rejection arrives as HTTP 200, so nothing may reach the NAS."""
        from surveillance.services import license as lic_mod

        reply = {"success": False, "error_code": 407}
        with (
            patch.object(
                lic_mod, "get_device_info", new_callable=AsyncMock, return_value=("SN", "DS")
            ),
            patch.object(
                lic_mod, "_offline_request", new_callable=AsyncMock, return_value=(reply, 1)
            ),
            patch.object(api, "request", new_callable=AsyncMock) as mock,
            pytest.raises(lic_mod.OfflineLicenseError, match="407"),
        ):
            await lic_mod.offline_activate(api, ["KEY-1"])

        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_offline_activate_reports_blocked_keys(self, api: SurveillanceAPI) -> None:
        from surveillance.services import license as lic_mod

        reply = {"success": True, "has_blocked": True, "checkList": {"KEY-1": 1}}
        with (
            patch.object(
                lic_mod, "get_device_info", new_callable=AsyncMock, return_value=("SN", "DS")
            ),
            patch.object(
                lic_mod, "_offline_request", new_callable=AsyncMock, return_value=(reply, 1)
            ),
            patch.object(api, "request", new_callable=AsyncMock) as mock,
            pytest.raises(lic_mod.OfflineLicenseError, match="KEY-1"),
        ):
            await lic_mod.offline_activate(api, ["KEY-1"])

        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_offline_deactivate_deletes_on_the_nas(self, api: SurveillanceAPI) -> None:
        """Releasing a key at Synology leaves it installed until the NAS is
        told to delete it."""
        from surveillance.services import license as lic_mod

        with (
            patch.object(
                lic_mod, "get_device_info", new_callable=AsyncMock, return_value=("SN", "DS")
            ),
            patch.object(lic_mod, "offline_get_timestamp", new_callable=AsyncMock, return_value=99),
            patch.object(
                lic_mod,
                "_offline_request",
                new_callable=AsyncMock,
                return_value=({"success": True}, 1),
            ),
            patch.object(api, "request", new_callable=AsyncMock, return_value={}) as mock,
        ):
            await lic_mod.offline_deactivate(api, ["KEY-1"], [7])

        assert mock.call_args[1]["method"] == "DeleteKey"
        assert mock.call_args[1]["extra_params"]["lic_list"] == "7"

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
            assert tasks[1].name == "Backyard Lapse"

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

    @pytest.mark.asyncio
    async def test_find_recording_at_returns_the_covering_recording(
        self, api: SurveillanceAPI
    ) -> None:
        from surveillance.services.recording import find_recording_at

        mock_data = {
            "events": [
                {"id": 100, "cameraId": 21, "startTime": 1700000000, "stopTime": 1700001800},
                {"id": 101, "cameraId": 21, "startTime": 1700001800, "stopTime": 1700003600},
            ],
            "total": 2,
        }
        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            rec = await find_recording_at(api, camera_id=21, target_unix=1700002000)
            assert rec is not None
            assert rec.id == 101

    @pytest.mark.asyncio
    async def test_find_recording_at_falls_back_to_the_nearest_recording_in_a_gap(
        self, api: SurveillanceAPI
    ) -> None:
        """Motion-only recording routinely leaves gaps with nothing
        covering the exact clicked time -- the search must still land
        on something nearby rather than refuse to seek at all."""
        from surveillance.services.recording import find_recording_at

        mock_data = {
            "events": [
                {"id": 100, "cameraId": 21, "startTime": 1700000000, "stopTime": 1700000100},
                {"id": 101, "cameraId": 21, "startTime": 1700005000, "stopTime": 1700005100},
            ],
            "total": 2,
        }
        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            # 1700000200 is 100s past recording 100's end and 4800s before
            # recording 101's start -- nearer to 100.
            rec = await find_recording_at(api, camera_id=21, target_unix=1700000200)
            assert rec is not None
            assert rec.id == 100

    @pytest.mark.asyncio
    async def test_find_recording_at_returns_none_when_nothing_recorded(
        self, api: SurveillanceAPI
    ) -> None:
        from surveillance.services.recording import find_recording_at

        mock_data = {"events": [], "total": 0}
        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            rec = await find_recording_at(api, camera_id=21, target_unix=1700000000)
            assert rec is None

    @pytest.mark.asyncio
    async def test_find_recording_at_searches_a_window_around_the_target(
        self, api: SurveillanceAPI
    ) -> None:
        from surveillance.services.recording import _HISTORY_SEEK_WINDOW, find_recording_at

        mock_data = {"events": [], "total": 0}
        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data) as mock:
            await find_recording_at(api, camera_id=21, target_unix=1700003600)
            call_kwargs = mock.call_args
            assert call_kwargs[1]["extra_params"]["cameraIds"] == "21"
            assert call_kwargs[1]["extra_params"]["fromTime"] == str(
                1700003600 - _HISTORY_SEEK_WINDOW
            )
            assert call_kwargs[1]["extra_params"]["toTime"] == str(
                1700003600 + _HISTORY_SEEK_WINDOW
            )

    @pytest.mark.asyncio
    async def test_fetch_recording_thumbnail_decodes_and_caches(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import clear_snapshot_cache, fetch_recording_thumbnail

        clear_snapshot_cache()
        raw = b"fake-thumbnail-bytes"
        mock_data = [{"thumbnail": base64.b64encode(raw).decode()}]
        rec = Recording(
            id=42,
            camera_id=39,
            camera_name="CAM 58",
            start_time=1700000000,
            stop_time=1700000060,
            mount_id=1,
            arch_id=2,
        )

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data) as mock:
            result = await fetch_recording_thumbnail(api, rec)
            assert result == raw
            event_info = json.loads(mock.call_args[1]["extra_params"]["eventInfo"])
            assert event_info == [
                {
                    "cameraId": 39,
                    "archId": 2,
                    "mountId": 1,
                    "rec_group": 0,
                    "targetTime": 1700000000,
                }
            ]

            # Cached by recording id — a second call must not re-request.
            result2 = await fetch_recording_thumbnail(api, rec)
            assert result2 == raw
            assert mock.call_count == 1
        clear_snapshot_cache()

    @pytest.mark.asyncio
    async def test_fetch_recording_thumbnail_empty_on_failure(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import clear_snapshot_cache, fetch_recording_thumbnail

        clear_snapshot_cache()
        rec = Recording(
            id=43, camera_id=39, camera_name="CAM 58", start_time=1700000000, stop_time=1700000060
        )
        with patch.object(api, "request", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            assert await fetch_recording_thumbnail(api, rec) == b""
        clear_snapshot_cache()


class TestFetchCameraThumbnailAt:
    """Hover-preview image source for the Live View timeline — unlike
    fetch_recording_thumbnail this isn't tied to a specific Recording
    row, so every call is a fresh request (see fetch_camera_thumbnail_at's
    own docstring)."""

    @pytest.mark.asyncio
    async def test_returns_decoded_bytes_with_expected_params(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import fetch_camera_thumbnail_at

        raw = b"fake-thumbnail-bytes"
        mock_data = [{"thumbnail": base64.b64encode(raw).decode()}]

        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data) as mock:
            result = await fetch_camera_thumbnail_at(api, camera_id=39, timestamp=1700000000)
            assert result == raw
            event_info = json.loads(mock.call_args[1]["extra_params"]["eventInfo"])
            assert event_info == [
                {
                    "cameraId": 39,
                    "archId": 0,
                    "mountId": 0,
                    "rec_group": 0,
                    "targetTime": 1700000000,
                }
            ]

    @pytest.mark.asyncio
    async def test_empty_thumbnail_is_empty_bytes(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import fetch_camera_thumbnail_at

        mock_data = [{"thumbnail": ""}]
        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data):
            assert await fetch_camera_thumbnail_at(api, camera_id=39, timestamp=1700000000) == b""

    @pytest.mark.asyncio
    async def test_request_failure_is_empty_bytes(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import fetch_camera_thumbnail_at

        with patch.object(api, "request", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            assert await fetch_camera_thumbnail_at(api, camera_id=39, timestamp=1700000000) == b""

    @pytest.mark.asyncio
    async def test_not_cached_across_calls(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import fetch_camera_thumbnail_at

        raw = b"fake-thumbnail-bytes"
        mock_data = [{"thumbnail": base64.b64encode(raw).decode()}]
        with patch.object(api, "request", new_callable=AsyncMock, return_value=mock_data) as mock:
            await fetch_camera_thumbnail_at(api, camera_id=39, timestamp=1700000000)
            await fetch_camera_thumbnail_at(api, camera_id=39, timestamp=1700000000)
            assert mock.call_count == 2


class TestWsBridgeClassify:
    def test_http_502_in_message(self) -> None:
        from surveillance.services.ws_bridge import _classify_error

        msg = _classify_error(Exception("server rejected WebSocket connection: HTTP 502"))
        assert "502" in msg

    def test_bad_gateway_case_insensitive(self) -> None:
        from surveillance.services.ws_bridge import _classify_error

        msg = _classify_error(Exception("Server returned: Bad Gateway"))
        assert "502" in msg

    def test_tls_error(self) -> None:
        import ssl

        from surveillance.services.ws_bridge import _classify_error

        msg = _classify_error(ssl.SSLError("certificate verify failed"))
        assert "TLS" in msg

    def test_generic_fallback(self) -> None:
        from surveillance.services.ws_bridge import _classify_error

        msg = _classify_error(RuntimeError("something else"))
        assert "RuntimeError" in msg
        assert "something else" in msg


class TestDownloadRecordingValidation:
    """Server-response checks in recording.download_recording."""

    @pytest.mark.asyncio
    async def test_empty_response_raises(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording

        out = tmp_path / "out.mp4"
        with (
            patch.object(api, "stream_download", _stream_mock(b"")),
            pytest.raises(ValueError, match="empty response"),
        ):
            await download_recording(api, 1, out)
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_html_doctype_response_raises(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording

        body = b"<!DOCTYPE html><html><body>login</body></html>"
        out = tmp_path / "out.mp4"
        with (
            patch.object(api, "stream_download", _stream_mock(body)),
            pytest.raises(ValueError, match="HTML"),
        ):
            await download_recording(api, 1, out)
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_html_tag_response_raises(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording

        body = b"\n  <html><body>login</body></html>"
        out = tmp_path / "out.mp4"
        with (
            patch.object(api, "stream_download", _stream_mock(body)),
            pytest.raises(ValueError, match="HTML"),
        ):
            await download_recording(api, 1, out)
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_html_tag_with_attributes_raises(
        self, api: SurveillanceAPI, tmp_path: Path
    ) -> None:
        """A login page with no doctype and an attribute on the root tag is
        still HTML, not video."""
        from surveillance.services.recording import download_recording

        body = b'<html lang="en"><body>login</body></html>'
        out = tmp_path / "out.mp4"
        with (
            patch.object(api, "stream_download", _stream_mock(body)),
            pytest.raises(ValueError, match="HTML"),
        ):
            await download_recording(api, 1, out)
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_successful_download_writes_file(
        self, api: SurveillanceAPI, tmp_path: Path
    ) -> None:
        from surveillance.services.recording import download_recording

        # Minimal ftyp-box header so it looks like a real MP4
        body = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8
        out = tmp_path / "out.mp4"
        with patch.object(api, "stream_download", _stream_mock(body)):
            result = await download_recording(api, 42, out)
        assert result == out
        assert out.read_bytes() == body


class TestStreamToFile:
    """Downloads are written as they arrive so a large recording never has
    to fit in memory."""

    @pytest.mark.asyncio
    async def test_joins_every_chunk_in_order(self, tmp_path: Path) -> None:
        from surveillance.services.download import stream_to_file

        parts = [b"\x00\x00\x00\x18ftypisom", b"middle" * 500, b"tail"]

        async def _gen() -> AsyncIterator[bytes]:
            for part in parts:
                yield part

        out = tmp_path / "rec.mp4"
        await stream_to_file(_gen(), out, "Recording 1")
        assert out.read_bytes() == b"".join(parts)

    @pytest.mark.asyncio
    async def test_removes_the_partial_file_when_the_stream_dies(self, tmp_path: Path) -> None:
        from surveillance.services.download import stream_to_file

        async def _gen() -> AsyncIterator[bytes]:
            yield b"\x00\x00\x00\x18ftypisom" + b"x" * 2000
            raise OSError("connection reset")

        out = tmp_path / "rec.mp4"
        with pytest.raises(OSError, match="connection reset"):
            await stream_to_file(_gen(), out, "Recording 1")
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_rejects_an_error_page_before_writing(self, tmp_path: Path) -> None:
        from surveillance.services.download import stream_to_file

        async def _gen() -> AsyncIterator[bytes]:
            yield b'<html lang="en"><body>login</body></html>'

        out = tmp_path / "rec.mp4"
        with pytest.raises(ValueError, match="HTML"):
            await stream_to_file(_gen(), out, "Recording 1")
        assert not out.exists()


class TestG711:
    """audioop.lin2ulaw is deprecated (3.12) / removed (3.13) -- this is a
    dependency-free reimplementation, verified bit-for-bit against every
    possible 16-bit sample value."""

    @staticmethod
    def _reference_ulaw(sample: int) -> int:
        """ITU-T G.711 mu-law, as CPython's audioop implemented it.

        Written out here rather than compared against audioop itself:
        that module was removed in 3.13, which is both what CI runs and
        the whole reason services/g711.py exists, so importing it would
        skip or fail the test on exactly the versions that matter.
        """
        pcm = sample >> 2  # 16-bit sample into the encoder's 14-bit domain
        if pcm < 0:
            pcm, mask = -pcm, 0x7F
        else:
            mask = 0xFF
        pcm = min(pcm, 8158)
        pcm += 33
        seg = 8
        for i, bound in enumerate((0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)):
            if pcm <= bound:
                seg = i
                break
        uval = 0x7F if seg >= 8 else (seg << 4) | ((pcm >> (seg + 1)) & 0x0F)
        return (uval ^ mask) & 0xFF

    def test_bit_exact_over_full_16_bit_range(self) -> None:
        import struct

        from surveillance.services.g711 import lin2ulaw

        samples = range(-32768, 32768)
        encoded = lin2ulaw(struct.pack("<%dh" % len(samples), *samples))
        expected = bytes(self._reference_ulaw(s) for s in samples)
        assert encoded == expected

    def test_rejects_odd_length(self) -> None:
        from surveillance.services.g711 import lin2ulaw

        with pytest.raises(ValueError, match="multiple of 2"):
            lin2ulaw(b"\x00\x00\x00")


class TestPttService:
    @pytest.mark.asyncio
    async def test_check_occupied_true(self, api: SurveillanceAPI) -> None:
        from surveillance.services.ptt import check_occupied

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"isOccupied": True}
        ) as mock:
            assert await check_occupied(api, 39) is True
            call_kwargs = mock.call_args[1]
            assert call_kwargs["api"] == "SYNO.SurveillanceStation.AudioOut"
            assert call_kwargs["method"] == "CheckOccupied"
            assert call_kwargs["extra_params"]["devId"] == 39

    @pytest.mark.asyncio
    async def test_check_occupied_false(self, api: SurveillanceAPI) -> None:
        from surveillance.services.ptt import check_occupied

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"isOccupied": False}
        ):
            assert await check_occupied(api, 39) is False

    @pytest.mark.asyncio
    async def test_run_raises_when_occupied(self, api: SurveillanceAPI) -> None:
        """run() must bail out before opening a WebSocket or touching the
        mic at all when the camera's speaker is already in use."""
        from surveillance.services.ptt import PttOccupiedError, PttSession

        session = PttSession(39)
        with (
            patch.object(session, "_start_capture", return_value=object()),
            patch.object(session, "_stop_capture"),
            patch.object(api, "request", new_callable=AsyncMock, return_value={"isOccupied": True}),
            pytest.raises(PttOccupiedError),
        ):
            await session.run(api)

    @pytest.mark.asyncio
    async def test_capture_is_closed_when_the_device_fails_to_start(
        self, api: SurveillanceAPI
    ) -> None:
        """_open_stream() assigns self._stream before start()ing it, so a
        device that opens but refuses to start is already ours to close."""
        from surveillance.services.ptt import PttSession

        closed = []

        class _FailsToStart:
            def start(self) -> None:
                raise RuntimeError("device busy")

            def stop(self) -> None:
                closed.append("stop")

            def close(self) -> None:
                closed.append("close")

        session = PttSession(39)

        def _open(*_args: object) -> None:
            session._stream = _FailsToStart()
            session._stream.start()

        with (
            patch.object(session, "_open_stream", _open),
            pytest.raises(RuntimeError, match="device busy"),
        ):
            await session.run(api)

        assert closed == ["stop", "close"], "the open capture device was left running"
        assert session._stream is None

    def test_build_ws_url(self, api: SurveillanceAPI) -> None:
        from surveillance.services.ptt import _build_ws_url

        url = _build_ws_url(api, 39)
        assert url.startswith("wss://" if api.base_url.startswith("https") else "ws://")
        assert "method=AudioOut" in url
        assert "dsId=0" in url
        assert "id=39" in url
        assert "type=1" in url

    def test_double_bytes(self) -> None:
        from surveillance.services.ptt import _double_bytes

        assert _double_bytes(b"") == b""
        assert _double_bytes(b"\x01\x02\x03") == b"\x01\x01\x02\x02\x03\x03"
