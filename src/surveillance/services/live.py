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
    "auto": "Auto (WebSocket)",
    "websocket": "WebSocket",
    "mjpeg": "MJPEG",
    "rtsp_over_http": "RTSP over HTTP",
    "rtsp": "RTSP",
    "multicast": "Multicast",
    "direct": "Direct RTSP URL",
}

# Protocols whose stream URL is an RTSP one, so audio reaches mpv as soon
# as the camera has a track. "auto" and "websocket" are not here because
# they only carry audio when ws_bridge can mux it (see its module
# docstring), which is not known until DSM reports the codec --
# liveview's _update_slot_audio handles that case separately. "mjpeg" is
# a still-image stream and never has audio.
AUDIO_PROTOCOLS = frozenset({"rtsp", "rtsp_over_http", "multicast", "direct"})

# Map protocol name -> API response field
_PROTO_FIELD: dict[str, str] = {
    "rtsp": "rtspPath",
    "rtsp_over_http": "rtspOverHttpPath",
    "mjpeg": "mjpegHttpPath",
    "multicast": "multicstPath",
}


# A locally-generated mpv URL showing a "camera offline" card. Uses
# libavdevice's lavfi input to synthesize a black frame with a static
# message, entirely without network I/O — unlike a real RTSP/WebSocket URL
# for an unreachable camera, this can never block mpv's demuxer on a dead
# connection, so it's always safe to play. The camera's own name is already
# shown in the slot header, so it isn't repeated here.
OFFLINE_PLACEHOLDER_URL = (
    "av://lavfi:color=c=black:s=1280x720:r=5,"
    "drawtext=text='Camera offline':fontcolor=white:fontsize=36:"
    "x=(w-text_w)/2:y=(h-text_h)/2"
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


def get_history_view_path(api: SurveillanceAPI) -> str:
    """Build a WebSocket URL for History (recorded) playback.

    Unlike get_live_view_path/_build_ws_live_url, no camera or protocol
    choice applies here -- History is WS-only (see ws_bridge.py's
    module docstring: RTSP has no historical/timestamp param at all)
    and nothing here identifies a camera or recording, which is instead
    selected by an in-band message WebSocketBridge sends once connected
    (WebSocketBridge(history_recording=..., history_target=...)).
    """
    scheme = "wss" if api.base_url.startswith("https") else "ws"
    host_port = api.base_url.split("://", 1)[1]
    return f"{scheme}://{host_port}/ss_webstream_task/?method=MixStream&stmSrc=2&dsId=0&id=0"


async def get_live_view_path(
    api: SurveillanceAPI,
    camera_id: int,
    protocol: str = "auto",
    override_url: str = "",
) -> str:
    """Get the live view URL for a camera.

    *protocol* selects which stream path to use:
      auto, rtsp, rtsp_over_http, mjpeg, multicast, websocket, direct.
    When *protocol* is ``"direct"``, *override_url* is returned as-is.
    """
    if protocol == "direct" and override_url:
        return override_url

    # "auto" is WebSocket: it is what DSM's own web client uses and the
    # only transport that carries audio for the common camera. There is no
    # fallback to the API-based protocols -- picking one of those is a
    # per-camera override, made by right-clicking the camera in the sidebar.
    if protocol in ("auto", "websocket"):
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
        if field_name == "mjpegHttpPath" and not value.startswith(("http://", "https://")):
            return f"{api.base_url}{value}"
        return value

    raise ValueError(f"Unknown protocol {protocol!r}")
