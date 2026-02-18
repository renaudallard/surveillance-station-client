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

"""Recording management service."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from surveillance.api.models import Recording

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI

log = logging.getLogger(__name__)

_thumbnail_semaphore = asyncio.Semaphore(3)


async def list_recordings(
    api: SurveillanceAPI,
    camera_id: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Recording], int]:
    """List recordings, optionally filtered by camera.

    Returns (recordings, total_count).
    """
    params: dict[str, str] = {
        "offset": str(offset),
        "limit": str(limit),
    }
    if camera_id is not None:
        params["cameraIds"] = str(camera_id)

    data = await api.request(
        api="SYNO.SurveillanceStation.Recording",
        method="List",
        version=5,
        extra_params=params,
    )

    recordings = [Recording.from_api(r) for r in data.get("events", data.get("recordings", []))]
    total = data.get("total", len(recordings))
    return recordings, total


def get_stream_url(api: SurveillanceAPI, rec: Recording) -> str:
    """Build a playback URL for a recording.

    Uses SYNO.SurveillanceStation.Stream EventStream (modern) with a
    fallback to SYNO.SurveillanceStation.Streaming EventStream (legacy).
    """
    # Modern: SYNO.SurveillanceStation.Stream method=EventStream
    stream_api = "SYNO.SurveillanceStation.Stream"
    if stream_api in api._api_info:
        return api.get_stream_url(
            api._get_api_path(stream_api).removeprefix("/webapi/"),
            {
                "api": stream_api,
                "method": "EventStream",
                "version": str(api._get_api_version(stream_api, 1)),
                "eventId": str(rec.id),
                "mountId": str(rec.mount_id),
                "recEvtType": str(rec.event_type),
                "archId": str(rec.arch_id),
            },
        )

    # Legacy: SYNO.SurveillanceStation.Streaming method=EventStream
    legacy_api = "SYNO.SurveillanceStation.Streaming"
    return api.get_stream_url(
        api._get_api_path(legacy_api).removeprefix("/webapi/"),
        {
            "api": legacy_api,
            "method": "EventStream",
            "version": str(api._get_api_version(legacy_api, 2)),
            "eventId": str(rec.id),
            "mountId": str(rec.mount_id),
            "framestart": "0",
            "timestamp": str(int(__import__("time").time())),
        },
    )


async def download_recording(
    api: SurveillanceAPI,
    recording_id: int,
    output_path: Path,
) -> Path:
    """Download a recording to disk."""
    data = await api.download(
        api="SYNO.SurveillanceStation.Recording",
        method="Download",
        version=5,
        extra_params={"id": str(recording_id)},
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return output_path


async def delete_recording(api: SurveillanceAPI, recording_id: int) -> None:
    """Delete a recording."""
    await api.request(
        api="SYNO.SurveillanceStation.Recording",
        method="Delete",
        version=5,
        extra_params={"idList": str(recording_id)},
    )


_snapshot_cache: dict[int, bytes] = {}


def clear_snapshot_cache() -> None:
    """Clear the thumbnail snapshot cache."""
    _snapshot_cache.clear()


async def fetch_camera_snapshot(
    api: SurveillanceAPI,
    camera_id: int,
) -> bytes:
    """Fetch a JPEG snapshot for a camera, with per-camera caching."""
    if camera_id in _snapshot_cache:
        return _snapshot_cache[camera_id]

    async with _thumbnail_semaphore:
        try:
            data = await api.download(
                api="SYNO.SurveillanceStation.Camera",
                method="GetSnapshot",
                version=8,
                extra_params={"cameraId": str(camera_id)},
            )
        except Exception as exc:
            log.warning("Snapshot failed for camera %d: %s", camera_id, exc)
            return b""

    if data:
        _snapshot_cache[camera_id] = data
    return data
