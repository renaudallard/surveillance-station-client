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

"""Live view stream URL management."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI


async def get_live_view_path(
    api: SurveillanceAPI,
    camera_id: int,
    override_url: str = "",
) -> str:
    """Get the live view URL for a camera.

    If *override_url* is set it is returned directly (for cameras whose
    Synology-proxied stream is broken).  Otherwise the best available
    path from the Surveillance Station API is returned.
    """
    if override_url:
        return override_url

    data = await api.request(
        api="SYNO.SurveillanceStation.Camera",
        method="GetLiveViewPath",
        version=9,
        extra_params={"idList": str(camera_id)},
    )

    # Response 'data' can be a list directly or a dict with pathInfos
    paths: list[dict[str, str]] = []
    if isinstance(data, list):
        paths = data
    elif isinstance(data, dict):
        paths = data.get("pathInfos", data.get("cameras", [])) or []

    if not paths:
        raise ValueError(f"No live view path for camera {camera_id}")

    info = paths[0]

    # Prefer RTSP unicast
    rtsp_path: str = info.get("rtspPath", "")
    if rtsp_path:
        return rtsp_path

    # RTSP over HTTP (tunneled through DSM port)
    rtsp_http: str = info.get("rtspOverHttpPath", "")
    if rtsp_http:
        return rtsp_http

    # Fall back to MJPEG over HTTP
    mjpeg_path: str = info.get("mjpegHttpPath", "")
    if mjpeg_path:
        return f"{api.base_url}{mjpeg_path}"

    # Fall back to multicast
    mcast: str = info.get("multicstPath", "")
    if mcast:
        return mcast

    raise ValueError(f"No usable stream path for camera {camera_id}")


def get_snapshot_url(api: SurveillanceAPI, camera_id: int) -> str:
    """Get URL for a live snapshot from a camera."""
    return api.get_stream_url(
        "entry.cgi",
        {
            "api": "SYNO.SurveillanceStation.Camera",
            "method": "GetSnapshot",
            "version": "9",
            "cameraId": str(camera_id),
        },
    )
