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
import base64
import collections
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from surveillance.api.models import Recording

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI

log = logging.getLogger(__name__)

_thumbnail_semaphore = asyncio.Semaphore(8)


async def list_recordings(
    api: SurveillanceAPI,
    camera_id: int | None = None,
    camera_ids: list[int] | None = None,
    from_time: int | None = None,
    to_time: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Recording], int]:
    """List recordings, optionally filtered by cameras and time range.

    Args:
        api: SurveillanceAPI instance
        camera_id: Single camera ID filter (legacy, use camera_ids for multiple)
        camera_ids: List of camera IDs to filter (comma-separated for API)
        from_time: Unix timestamp for start of time range
        to_time: Unix timestamp for end of time range
        offset: Pagination offset
        limit: Maximum number of recordings to return

    Returns (recordings, total_count).
    """
    params: dict[str, str] = {
        "offset": str(offset),
        "limit": str(limit),
    }
    if camera_ids:
        params["cameraIds"] = ",".join(str(cid) for cid in camera_ids)
    elif camera_id is not None:
        params["cameraIds"] = str(camera_id)
    if from_time is not None:
        params["fromTime"] = str(from_time)
    if to_time is not None:
        params["toTime"] = str(to_time)

    data = await api.request(
        api="SYNO.SurveillanceStation.Recording",
        method="List",
        version=5,
        extra_params=params,
    )

    raw = data.get("events", data.get("recordings", []))
    if raw:
        log.debug("Recording API first item keys: %s", list(raw[0].keys()))
    recordings = [Recording.from_api(r) for r in raw]
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
            "timestamp": str(int(time.time())),
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


_snapshot_cache: collections.OrderedDict[int, bytes] = collections.OrderedDict()
_recording_thumbnail_cache: collections.OrderedDict[int, bytes] = collections.OrderedDict()
_thumbnail_failed_cameras: dict[int, float] = {}
_THUMBNAIL_FAIL_TTL = 300.0  # seconds before retrying GetThumbnail

_MAX_SNAPSHOT_CACHE = 32
_MAX_THUMBNAIL_CACHE = 128


def _cache_put(
    cache: collections.OrderedDict[int, bytes], key: int, value: bytes, limit: int
) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def clear_snapshot_cache() -> None:
    """Clear the thumbnail snapshot cache."""
    _snapshot_cache.clear()
    _recording_thumbnail_cache.clear()
    _thumbnail_failed_cameras.clear()


async def fetch_recording_thumbnail(
    api: SurveillanceAPI,
    rec: Recording,
) -> bytes:
    """Fetch a thumbnail for a recording.

    Tries Recording.GetThumbnail first (works for offline cameras),
    falls back to a live Camera.GetSnapshot.
    """
    if rec.id in _recording_thumbnail_cache:
        return _recording_thumbnail_cache[rec.id]

    if rec.camera_id in _snapshot_cache:
        return _snapshot_cache[rec.camera_id]

    async with _thumbnail_semaphore:
        # Re-check caches after acquiring semaphore (another coroutine
        # may have populated them while we waited).
        if rec.id in _recording_thumbnail_cache:
            return _recording_thumbnail_cache[rec.id]
        if rec.camera_id in _snapshot_cache:
            return _snapshot_cache[rec.camera_id]

        # Try recording-specific thumbnail (stored on NAS, works offline)
        fail_time = _thumbnail_failed_cameras.get(rec.camera_id)
        if fail_time is None or (time.monotonic() - fail_time) > _THUMBNAIL_FAIL_TTL:
            try:
                data = await api.request(
                    api="SYNO.SurveillanceStation.Recording",
                    method="GetThumbnail",
                    version=5,
                    extra_params={
                        "cameraId": str(rec.camera_id),
                        "archId": str(rec.arch_id),
                        "mountId": str(rec.mount_id),
                        "targetTime": str(rec.start_time),
                        "blFallbackByLoadEvt": "true",
                        "rec_group": "0",
                        "eventInfo": json.dumps(
                            {
                                "cameraId": rec.camera_id,
                                "archId": rec.arch_id,
                                "mountId": rec.mount_id,
                                "rec_group": 0,
                                "startTime": rec.start_time,
                                "endTime": rec.start_time,
                                "eventType": rec.event_type,
                            }
                        ),
                    },
                )
                thumbs = data if isinstance(data, list) else [data]
                for thumb in thumbs:
                    b64 = thumb.get("thumbnail", "")
                    if b64:
                        image_data = base64.b64decode(b64)
                        if image_data:
                            _cache_put(
                                _recording_thumbnail_cache,
                                rec.id,
                                image_data,
                                _MAX_THUMBNAIL_CACHE,
                            )
                            return image_data
            except Exception as exc:
                log.debug(
                    "Recording thumbnail not available for %d: %s",
                    rec.id,
                    exc,
                )
                _thumbnail_failed_cameras[rec.camera_id] = time.monotonic()

        # Fallback: live camera snapshot
        try:
            data_bytes = await api.download(
                api="SYNO.SurveillanceStation.Camera",
                method="GetSnapshot",
                version=8,
                extra_params={"cameraId": str(rec.camera_id)},
            )
        except Exception as exc:
            log.warning("Snapshot failed for camera %d: %s", rec.camera_id, exc)
            return b""

    if data_bytes:
        _cache_put(_snapshot_cache, rec.camera_id, data_bytes, _MAX_SNAPSHOT_CACHE)
    return data_bytes


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
        _cache_put(_snapshot_cache, camera_id, data, _MAX_SNAPSHOT_CACHE)
    return data
