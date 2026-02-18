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

"""Data models for Synology Surveillance Station API responses."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class CameraStatus(IntEnum):
    DISABLED = 0
    ENABLED = 1
    DISCONNECTED = 2

    @classmethod
    def _missing_(cls, value: object) -> CameraStatus:
        """Handle unknown status values from the API."""
        val: int = int(value)  # type: ignore[call-overload]
        obj = int.__new__(cls, val)
        obj._name_ = f"UNKNOWN_{val}"
        obj._value_ = val
        result: CameraStatus = obj
        return result


@dataclass
class Camera:
    """Surveillance camera."""

    id: int
    name: str
    ip: str
    port: int
    model: str
    vendor: str
    status: CameraStatus
    host: str = ""
    is_ptz: bool = False
    resolution: str = ""
    fps: int = 0
    channel: int = 0

    @classmethod
    def from_api(cls, data: dict) -> Camera:  # type: ignore[type-arg]
        return cls(
            id=data.get("id", 0),
            name=data.get("newName", data.get("name", "")),
            ip=data.get("ip", ""),
            port=data.get("port", 0),
            model=data.get("model", ""),
            vendor=data.get("vendor", ""),
            status=CameraStatus(data.get("status", 0)),
            host=data.get("host", ""),
            is_ptz=bool(data.get("ptzDirection", 0)),
            resolution=data.get("resolution", ""),
            fps=data.get("fps", 0),
            channel=data.get("channel", 0),
        )


DETECTION_LABELS: dict[int, str] = {
    1: "Person",
    2: "Vehicle",
    3: "Animal",
    4: "Tampering",
    6: "Crowd",
    7: "License Plate",
    8: "Over-occupancy",
}


def decode_detection_labels(bitmask: int) -> list[str]:
    """Decode a defaultLabel bitmask into human-readable detection tags."""
    labels = []
    for bit, name in DETECTION_LABELS.items():
        if bitmask & (1 << bit):
            labels.append(name)
    return labels


@dataclass
class Recording:
    """A camera recording."""

    id: int
    camera_id: int
    camera_name: str
    start_time: int  # unix timestamp
    stop_time: int
    file_size: int = 0
    event_type: int = 0
    mount_id: int = 0
    arch_id: int = 0
    detection_label: int = 0

    @classmethod
    def from_api(cls, data: dict) -> Recording:  # type: ignore[type-arg]
        return cls(
            id=data.get("id", 0),
            camera_id=data.get("cameraId", 0),
            camera_name=data.get("cameraName", ""),
            start_time=data.get("startTime", 0),
            stop_time=data.get("stopTime", 0),
            file_size=data.get("fileSize", 0),
            event_type=data.get("type", 0),
            mount_id=data.get("mountId", 0),
            arch_id=data.get("archId", 0),
            detection_label=data.get("defaultLabel", 0),
        )


@dataclass
class Snapshot:
    """A camera snapshot."""

    id: int
    camera_id: int
    camera_name: str
    create_time: int
    file_size: int = 0

    @classmethod
    def from_api(cls, data: dict) -> Snapshot:  # type: ignore[type-arg]
        return cls(
            id=data.get("id", 0),
            camera_id=data.get("cameraId", 0),
            camera_name=data.get("cameraName", ""),
            create_time=data.get("createTime", 0),
            file_size=data.get("fileSize", 0),
        )


@dataclass
class Event:
    """A surveillance event (recording triggered by motion, alarm, etc.)."""

    id: int
    camera_id: int
    camera_name: str
    event_type: int  # mode: 1=motion, 2=alarm, 3=manual, etc.
    start_time: int
    stop_time: int = 0
    mount_id: int = 0
    arch_id: int = 0
    detection_label: int = 0

    @classmethod
    def from_api(cls, data: dict) -> Event:  # type: ignore[type-arg]
        return cls(
            id=data.get("id", 0),
            camera_id=data.get("cameraId", 0),
            camera_name=data.get("camera_name", data.get("cameraName", "")),
            event_type=data.get("mode", data.get("type", data.get("eventType", 0))),
            start_time=data.get("startTime", 0),
            stop_time=data.get("stopTime", 0),
            mount_id=data.get("mountId", 0),
            arch_id=data.get("archId", 0),
            detection_label=data.get("defaultLabel", 0),
        )


@dataclass
class Alert:
    """A surveillance alert."""

    id: int
    camera_id: int
    camera_name: str
    alert_type: int
    timestamp: int
    is_read: bool = False

    @classmethod
    def from_api(cls, data: dict) -> Alert:  # type: ignore[type-arg]
        return cls(
            id=data.get("id", 0),
            camera_id=data.get("cameraId", 0),
            camera_name=data.get("cameraName", ""),
            alert_type=data.get("alertType", 0),
            timestamp=data.get("timestamp", 0),
            is_read=data.get("isRead", False),
        )


@dataclass
class HomeModeInfo:
    """Home mode status."""

    on: bool = False

    @classmethod
    def from_api(cls, data: dict) -> HomeModeInfo:  # type: ignore[type-arg]
        return cls(on=data.get("on", False))


@dataclass
class PtzPreset:
    """PTZ preset position."""

    id: int
    name: str
    position: int = 0

    @classmethod
    def from_api(cls, data: dict) -> PtzPreset:  # type: ignore[type-arg]
        return cls(
            id=data.get("id", 0),
            name=data.get("name", ""),
            position=data.get("position", 0),
        )


@dataclass
class PtzPatrol:
    """PTZ patrol route."""

    id: int
    name: str

    @classmethod
    def from_api(cls, data: dict) -> PtzPatrol:  # type: ignore[type-arg]
        return cls(
            id=data.get("id", 0),
            name=data.get("name", ""),
        )


@dataclass
class ApiInfo:
    """API endpoint information from SYNO.API.Info."""

    path: str
    min_version: int
    max_version: int

    @classmethod
    def from_api(cls, data: dict) -> ApiInfo:  # type: ignore[type-arg]
        return cls(
            path=data.get("path", ""),
            min_version=data.get("minVersion", 1),
            max_version=data.get("maxVersion", 1),
        )
