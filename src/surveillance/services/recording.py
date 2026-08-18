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
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from surveillance.api.models import Recording
from surveillance.services.download import stream_to_file

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI

log = logging.getLogger(__name__)

_thumbnail_semaphore = asyncio.Semaphore(8)

PRESET_TODAY = "today"
PRESET_YESTERDAY = "yesterday"
PRESET_LAST24H = "last24h"
PRESET_LAST7D = "last7d"
PRESET_LAST30D = "last30d"

# Recording.Download by recording id needs version 6 or later. Version 5
# returns a 400 "Execution failed" with no file. The official client uses
# version 4 for its own download, but with a different, event-based param
# set (eventId/mountId/archId), so version 4 does not apply to the id-based
# call this client makes.
RECORDING_DOWNLOAD_VERSION = 6


def preset_range(preset: str) -> tuple[int, int]:
    """Return (from_time, to_time) unix timestamps for a named time preset."""
    now = datetime.now()
    if preset == PRESET_TODAY:
        # Full day (00:00:00-23:59:59), not "now": every day-named preset
        # (Today, Yesterday, and the two "Last N days" below) names calendar
        # days, so it's expected to cover them in full regardless of when
        # the query runs — including the as-yet-unelapsed rest of today.
        # DSM tolerates a to_time past the current moment (it can only ever
        # have data up to now anyway), so this doesn't need special-casing.
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        return int(start.timestamp()), int(end.timestamp())
    if preset == PRESET_YESTERDAY:
        yesterday = now - timedelta(days=1)
        start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)
        return int(start.timestamp()), int(end.timestamp())
    if preset == PRESET_LAST24H:
        # The one duration-named (not day-named) preset: a literal rolling
        # 24h window ending at "now", not a calendar-day boundary.
        return int((now - timedelta(hours=24)).timestamp()), int(now.timestamp())
    if preset == PRESET_LAST7D:
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        return int(start.timestamp()), int(end.timestamp())
    if preset == PRESET_LAST30D:
        start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        return int(start.timestamp()), int(end.timestamp())
    raise ValueError(f"unknown preset: {preset}")


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
    """Download a recording to disk.

    Validates the response content before writing so that empty or corrupt
    files are never created.  If a partial file was created but the write
    fails, it is removed before re-raising the exception.

    Raises:
        ValueError: API returned an error, HTML page, or empty body.
        ApiError: Synology API error with numeric code.
        OSError: File-system write failure (partial file is cleaned up).
    """
    log.debug("Downloading recording %d to %s", recording_id, output_path)
    chunks = api.stream_download(
        api="SYNO.SurveillanceStation.Recording",
        method="Download",
        version=RECORDING_DOWNLOAD_VERSION,
        extra_params={"id": str(recording_id)},
    )
    return await stream_to_file(chunks, output_path, f"Recording {recording_id}")


_recording_thumbnail_cache: collections.OrderedDict[int, bytes] = collections.OrderedDict()

_MAX_THUMBNAIL_CACHE = 128

# Bumped by clear_snapshot_cache(); a fetch that started before the bump
# still returns its image to the row that asked, but does not put it in
# the cache the next NAS will read.
_cache_generation = 0


def _cache_put(
    cache: collections.OrderedDict[int, bytes], key: int, value: bytes, limit: int
) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def clear_snapshot_cache() -> None:
    """Clear the thumbnail cache and disown fetches already in flight.

    Called on disconnect from the GTK thread while up to _thumbnail
    semaphore's worth of GetThumbnail requests are still running on the
    asyncio thread, plus however many are queued behind it. Clearing
    alone would let those finish and re-populate the cache with the old
    NAS's entries, and the key is a bare recording id with no notion of
    which server it came from, so logging into a second NAS could show
    the first one's thumbnail for a colliding id.
    """
    global _cache_generation

    _cache_generation += 1
    _recording_thumbnail_cache.clear()


async def _request_thumbnail(
    api: SurveillanceAPI, camera_id: int, arch_id: int, mount_id: int, target_time: int
) -> bytes:
    """Recording.GetThumbnail request/response handling, shared by every
    thumbnail source. eventInfo must be a JSON array of objects matching
    the APK format (dsId + blFallbackByLoadEvt + eventInfo only). Callers
    are responsible for holding _thumbnail_semaphore.
    """
    try:
        data = await api.request(
            api="SYNO.SurveillanceStation.Recording",
            method="GetThumbnail",
            version=5,
            extra_params={
                "dsId": "0",
                "blFallbackByLoadEvt": "true",
                "eventInfo": json.dumps(
                    [
                        {
                            "cameraId": camera_id,
                            "archId": arch_id,
                            "mountId": mount_id,
                            "rec_group": 0,
                            "targetTime": target_time,
                        }
                    ]
                ),
            },
        )
        thumbs = data if isinstance(data, list) else [data]
        for thumb in thumbs:
            b64 = thumb.get("thumbnail", "")
            if b64:
                image_data = base64.b64decode(b64)
                if image_data:
                    return image_data
    except Exception as exc:
        log.debug("Thumbnail request failed for camera %d at %d: %s", camera_id, target_time, exc)
    return b""


async def fetch_recording_thumbnail(
    api: SurveillanceAPI,
    rec: Recording,
) -> bytes:
    """Fetch a thumbnail for a recording, cached by recording id."""
    if rec.id in _recording_thumbnail_cache:
        return _recording_thumbnail_cache[rec.id]

    generation = _cache_generation

    async with _thumbnail_semaphore:
        if rec.id in _recording_thumbnail_cache:
            return _recording_thumbnail_cache[rec.id]

        image_data = await _request_thumbnail(
            api, rec.camera_id, rec.arch_id, rec.mount_id, rec.start_time
        )
        if image_data and generation == _cache_generation:
            _cache_put(_recording_thumbnail_cache, rec.id, image_data, _MAX_THUMBNAIL_CACHE)
        return image_data


async def fetch_camera_thumbnail_at(api: SurveillanceAPI, camera_id: int, timestamp: int) -> bytes:
    """Fetch a thumbnail for *camera_id* at approximately *timestamp*.

    Used by the Live View timeline's hover preview — unlike a specific
    Recording row this is an arbitrary point on the timeline, so it isn't
    cached; the timeline debounces hover events instead to bound request
    volume. Returns b"" if the camera has no recording at that time (e.g.
    it was offline), same as fetch_recording_thumbnail's own failure case.
    """
    async with _thumbnail_semaphore:
        return await _request_thumbnail(
            api, camera_id, arch_id=0, mount_id=0, target_time=timestamp
        )
