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

"""PTZ (Pan-Tilt-Zoom) control service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from surveillance.api.models import PtzPatrol, PtzPreset

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI


async def move(api: SurveillanceAPI, camera_id: int, direction: str) -> None:
    """Move PTZ camera in a direction.

    direction: upStart, upStop, downStart, downStop, leftStart, leftStop,
               rightStart, rightStop, home
    """
    await api.request(
        api="SYNO.SurveillanceStation.PTZ",
        method="Move",
        version=2,
        extra_params={
            "cameraId": str(camera_id),
            "direction": direction,
        },
    )


async def zoom(api: SurveillanceAPI, camera_id: int, direction: str) -> None:
    """Zoom PTZ camera.

    control: inStart, inStop, outStart, outStop
    """
    await api.request(
        api="SYNO.SurveillanceStation.PTZ",
        method="Zoom",
        version=2,
        extra_params={
            "cameraId": str(camera_id),
            "control": direction,
        },
    )


async def list_presets(api: SurveillanceAPI, camera_id: int) -> list[PtzPreset]:
    """List PTZ presets for a camera."""
    data = await api.request(
        api="SYNO.SurveillanceStation.PTZ",
        method="ListPreset",
        version=2,
        extra_params={"cameraId": str(camera_id)},
    )
    return [PtzPreset.from_api(p) for p in data.get("presets", [])]


async def go_preset(api: SurveillanceAPI, camera_id: int, preset_id: int) -> None:
    """Move camera to a preset position."""
    await api.request(
        api="SYNO.SurveillanceStation.PTZ",
        method="GoPreset",
        version=2,
        extra_params={
            "cameraId": str(camera_id),
            "presetId": str(preset_id),
        },
    )


async def list_patrols(api: SurveillanceAPI, camera_id: int) -> list[PtzPatrol]:
    """List PTZ patrols for a camera."""
    data = await api.request(
        api="SYNO.SurveillanceStation.PTZ",
        method="ListPatrol",
        version=2,
        extra_params={"cameraId": str(camera_id)},
    )
    return [PtzPatrol.from_api(p) for p in data.get("patrols", [])]


async def run_patrol(api: SurveillanceAPI, camera_id: int, patrol_id: int) -> None:
    """Start a PTZ patrol."""
    await api.request(
        api="SYNO.SurveillanceStation.PTZ",
        method="RunPatrol",
        version=2,
        extra_params={
            "cameraId": str(camera_id),
            "patrolId": str(patrol_id),
        },
    )
