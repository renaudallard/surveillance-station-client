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

"""Camera management service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from surveillance.api.models import Camera

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI


async def list_cameras(api: SurveillanceAPI) -> list[Camera]:
    """List all cameras."""
    data = await api.request(
        api="SYNO.SurveillanceStation.Camera",
        method="List",
        version=9,
        extra_params={"basic": "true", "streamInfo": "true", "ptz": "true"},
    )
    cameras_data = data.get("cameras", [])
    return [Camera.from_api(c) for c in cameras_data]


async def get_camera_info(api: SurveillanceAPI, camera_id: int) -> Camera:
    """Get detailed info for a single camera."""
    data = await api.request(
        api="SYNO.SurveillanceStation.Camera",
        method="GetInfo",
        version=9,
        extra_params={"cameraIds": str(camera_id), "basic": "true", "ptz": "true"},
    )
    cameras = data.get("cameras", [])
    if not cameras:
        raise ValueError(f"Camera {camera_id} not found")
    return Camera.from_api(cameras[0])


async def enable_camera(api: SurveillanceAPI, camera_id: int) -> None:
    """Enable a camera."""
    await api.request(
        api="SYNO.SurveillanceStation.Camera",
        method="Enable",
        version=9,
        extra_params={"cameraIds": str(camera_id)},
    )


async def disable_camera(api: SurveillanceAPI, camera_id: int) -> None:
    """Disable a camera."""
    await api.request(
        api="SYNO.SurveillanceStation.Camera",
        method="Disable",
        version=9,
        extra_params={"cameraIds": str(camera_id)},
    )
