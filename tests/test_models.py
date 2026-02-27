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

"""Tests for API data models."""

from surveillance.api.models import (
    Alert,
    ApiInfo,
    Camera,
    CameraStatus,
    Event,
    HomeModeInfo,
    License,
    LicenseInfo,
    PtzPatrol,
    PtzPreset,
    Recording,
    Snapshot,
    TimeLapseRecording,
    TimeLapseTask,
)


class TestCamera:
    def test_from_api_basic(self) -> None:
        data = {
            "id": 1,
            "newName": "Front Door",
            "ip": "192.168.1.50",
            "port": 554,
            "model": "DS-2CD2386G2",
            "vendor": "Hikvision",
            "status": 1,
            "host": "192.168.1.50",
            "ptzDirection": 1,
            "resolution": "3840x2160",
            "fps": 25,
            "channel": 0,
        }
        cam = Camera.from_api(data)
        assert cam.id == 1
        assert cam.name == "Front Door"
        assert cam.status == CameraStatus.ENABLED
        assert cam.is_ptz is True
        assert cam.vendor == "Hikvision"

    def test_from_api_fallback_name(self) -> None:
        data = {"id": 2, "name": "Back Yard", "status": 0}
        cam = Camera.from_api(data)
        assert cam.name == "Back Yard"
        assert cam.status == CameraStatus.DISABLED

    def test_from_api_defaults(self) -> None:
        cam = Camera.from_api({})
        assert cam.id == 0
        assert cam.name == ""
        assert cam.status == CameraStatus.DISABLED
        assert cam.is_ptz is False


class TestRecording:
    def test_from_api(self) -> None:
        data = {
            "id": 100,
            "cameraId": 1,
            "cameraName": "Front Door",
            "startTime": 1700000000,
            "stopTime": 1700003600,
            "type": "motion",
            "mountId": 0,
        }
        rec = Recording.from_api(data)
        assert rec.id == 100
        assert rec.camera_id == 1
        assert rec.start_time == 1700000000
        assert rec.stop_time == 1700003600


class TestSnapshot:
    def test_from_api(self) -> None:
        data = {
            "id": 50,
            "cameraId": 2,
            "cameraName": "Garage",
            "createTime": 1700001000,
            "fileSize": 102400,
        }
        snap = Snapshot.from_api(data)
        assert snap.id == 50
        assert snap.camera_name == "Garage"
        assert snap.file_size == 102400


class TestEvent:
    def test_from_api(self) -> None:
        data = {
            "id": 200,
            "cameraId": 1,
            "cameraName": "Front Door",
            "eventType": 1,
            "startTime": 1700000000,
            "stopTime": 1700000060,
            "reason": "Motion detected",
        }
        event = Event.from_api(data)
        assert event.id == 200
        assert event.event_type == 1


class TestAlert:
    def test_from_api(self) -> None:
        data = {
            "id": 300,
            "cameraId": 1,
            "cameraName": "Front Door",
            "alertType": 0,
            "timestamp": 1700000000,
            "isRead": False,
        }
        alert = Alert.from_api(data)
        assert alert.id == 300
        assert alert.is_read is False


class TestHomeModeInfo:
    def test_from_api_on(self) -> None:
        info = HomeModeInfo.from_api({"on": True})
        assert info.on is True

    def test_from_api_off(self) -> None:
        info = HomeModeInfo.from_api({"on": False})
        assert info.on is False

    def test_from_api_default(self) -> None:
        info = HomeModeInfo.from_api({})
        assert info.on is False


class TestPtzPreset:
    def test_from_api(self) -> None:
        preset = PtzPreset.from_api({"id": 1, "name": "Home", "position": 0})
        assert preset.id == 1
        assert preset.name == "Home"


class TestPtzPatrol:
    def test_from_api(self) -> None:
        patrol = PtzPatrol.from_api({"id": 1, "name": "Perimeter"})
        assert patrol.id == 1
        assert patrol.name == "Perimeter"


class TestApiInfo:
    def test_from_api(self) -> None:
        info = ApiInfo.from_api(
            {
                "path": "entry.cgi",
                "minVersion": 1,
                "maxVersion": 9,
            }
        )
        assert info.path == "entry.cgi"
        assert info.min_version == 1
        assert info.max_version == 9


class TestLicense:
    def test_from_api(self) -> None:
        data = {
            "id": 1,
            "key": "ABCD-1234-EFGH-5678",
            "quota": 2,
            "expired_date": 1735689600,
            "isExpired": False,
            "isMigrated": False,
            "ownerDsId": 42,
        }
        lic = License.from_api(data)
        assert lic.id == 1
        assert lic.key == "ABCD-1234-EFGH-5678"
        assert lic.quota == 2
        assert lic.expired_date == 1735689600
        assert lic.is_expired is False
        assert lic.is_migrated is False
        assert lic.owner_ds_id == 42

    def test_from_api_defaults(self) -> None:
        lic = License.from_api({})
        assert lic.id == 0
        assert lic.key == ""
        assert lic.quota == 0
        assert lic.expired_date == 0
        assert lic.is_expired is False
        assert lic.is_migrated is False
        assert lic.owner_ds_id == 0


class TestLicenseInfo:
    def test_from_api(self) -> None:
        data = {
            "key_max": 8,
            "key_total": 4,
            "key_used": 3,
            "license": [
                {
                    "id": 1,
                    "key": "AAAA-BBBB-CCCC-DDDD",
                    "quota": 1,
                    "expired_date": 0,
                },
                {
                    "id": 2,
                    "key": "EEEE-FFFF-GGGG-HHHH",
                    "quota": 2,
                    "expired_date": 1735689600,
                    "isExpired": True,
                },
            ],
        }
        info = LicenseInfo.from_api(data)
        assert info.key_max == 8
        assert info.key_total == 4
        assert info.key_used == 3
        assert len(info.licenses) == 2
        assert info.licenses[0].key == "AAAA-BBBB-CCCC-DDDD"
        assert info.licenses[0].expired_date == 0
        assert info.licenses[1].is_expired is True


class TestTimeLapseTask:
    def test_from_api(self) -> None:
        data = {
            "id": 1,
            "name": "Front Door Lapse",
            "cameraId": 3,
            "cameraName": "Front Door",
            "enabled": True,
            "status": 2,
        }
        task = TimeLapseTask.from_api(data)
        assert task.id == 1
        assert task.name == "Front Door Lapse"
        assert task.camera_id == 3
        assert task.camera_name == "Front Door"
        assert task.enabled is True
        assert task.status == 2

    def test_from_api_defaults(self) -> None:
        task = TimeLapseTask.from_api({})
        assert task.id == 0
        assert task.name == ""
        assert task.camera_id == 0
        assert task.camera_name == ""
        assert task.enabled is True
        assert task.status == 0


class TestTimeLapseRecording:
    def test_from_api(self) -> None:
        data = {
            "id": 10,
            "cameraId": 3,
            "camera_name": "Front Door",
            "startTime": 1700000000,
            "stopTime": 1700003600,
            "taskId": 1,
            "event_size_bytes": 5242880,
            "mountId": 0,
            "archId": 0,
            "status_flags": 0,
            "recording": False,
            "path": "/volume1/surveillance/timelapse/1.mp4",
        }
        rec = TimeLapseRecording.from_api(data)
        assert rec.id == 10
        assert rec.camera_id == 3
        assert rec.camera_name == "Front Door"
        assert rec.start_time == 1700000000
        assert rec.stop_time == 1700003600
        assert rec.task_id == 1
        assert rec.file_size == 5242880
        assert rec.is_locked is False
        assert rec.recording is False
        assert rec.path == "/volume1/surveillance/timelapse/1.mp4"

    def test_from_api_locked(self) -> None:
        data = {
            "id": 11,
            "cameraId": 3,
            "cameraName": "Front Door",
            "startTime": 1700000000,
            "stopTime": 1700003600,
            "status_flags": 4,
        }
        rec = TimeLapseRecording.from_api(data)
        assert rec.is_locked is True

    def test_from_api_camera_name_fallback(self) -> None:
        data = {
            "id": 12,
            "cameraId": 3,
            "cameraName": "Garage",
            "startTime": 1700000000,
            "stopTime": 1700003600,
        }
        rec = TimeLapseRecording.from_api(data)
        assert rec.camera_name == "Garage"
