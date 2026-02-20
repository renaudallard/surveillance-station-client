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


PROTOCOL_LABELS: dict[str, str] = {
    "auto": "Auto (best available)",
    "websocket": "WebSocket",
    "webapi": "WebApi HTTP Stream",
    "mjpeg": "MJPEG",
    "rtsp_over_http": "RTSP over HTTP",
    "rtsp": "RTSP",
    "multicast": "Multicast",
    "direct": "Direct RTSP URL",
}

# Ordered list of API response fields tried by "auto" (after websocket and webapi)
_AUTO_FIELDS = ("mjpegHttpPath", "rtspOverHttpPath", "rtspPath", "multicstPath")

# Map protocol name -> API response field
_PROTO_FIELD: dict[str, str] = {
    "rtsp": "rtspPath",
    "rtsp_over_http": "rtspOverHttpPath",
    "mjpeg": "mjpegHttpPath",
    "multicast": "multicstPath",
}


def _build_webapi_url(api: SurveillanceAPI, camera_id: int) -> str:
    """Build a WebApi HTTP stream URL (LiveviewSrc.Play)."""
    return api.get_stream_url(
        "entry.cgi",
        {
            "api": "SYNO.SurveillanceStation.Player.LiveviewSrc",
            "method": "Play",
            "version": "1",
            "camera": str(camera_id),
            "archId": "0",
            "profileType": "0",
            "itemType": "camera",
        },
    )


def _build_ws_live_url(api: SurveillanceAPI, camera_id: int) -> str:
    """Build a WebSocket live stream URL."""
    # wss://host:port/ss_webstream_task/?method=MixStream&stmSrc=0&blAudio=true
    #   &dsId=0&id={camId}&devType=1&profile=0
    scheme = "wss" if api.base_url.startswith("https") else "ws"
    host_port = api.base_url.split("://", 1)[1]
    return (
        f"{scheme}://{host_port}/ss_webstream_task/"
        f"?method=MixStream&stmSrc=0&blAudio=true"
        f"&dsId=0&id={camera_id}&devType=1&profile=0"
    )


async def get_live_view_path(
    api: SurveillanceAPI,
    camera_id: int,
    protocol: str = "auto",
    override_url: str = "",
) -> str:
    """Get the live view URL for a camera.

    *protocol* selects which stream path to use:
      auto, rtsp, rtsp_over_http, mjpeg, multicast, webapi, websocket, direct.
    When *protocol* is ``"direct"``, *override_url* is returned as-is.
    """
    if protocol == "direct" and override_url:
        return override_url

    if protocol == "webapi":
        return _build_webapi_url(api, camera_id)

    if protocol == "websocket":
        return _build_ws_live_url(api, camera_id)

    # Auto: try websocket first, then webapi, then API-based protocols
    if protocol == "auto":
        return _build_ws_live_url(api, camera_id)

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

    # Specific protocol requested
    if protocol in _PROTO_FIELD:
        field_name = _PROTO_FIELD[protocol]
        value: str = info.get(field_name, "")
        if not value:
            raise ValueError(f"Protocol {protocol!r} not available for camera {camera_id}")
        if field_name == "mjpegHttpPath":
            return f"{api.base_url}{value}"
        return value

    raise ValueError(f"Unknown protocol {protocol!r}")


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
