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
import contextlib
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from surveillance.api.models import Recording

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI

log = logging.getLogger(__name__)

_thumbnail_semaphore = asyncio.Semaphore(8)

PRESET_TODAY = "today"
PRESET_YESTERDAY = "yesterday"
PRESET_LAST24H = "last24h"
PRESET_LAST7D = "last7d"


def preset_range(preset: str) -> tuple[int, int]:
    """Return (from_time, to_time) unix timestamps for a named time preset."""
    now = datetime.now()
    if preset == PRESET_TODAY:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp()), int(now.timestamp())
    if preset == PRESET_YESTERDAY:
        yesterday = now - timedelta(days=1)
        start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)
        return int(start.timestamp()), int(end.timestamp())
    if preset == PRESET_LAST24H:
        return int((now - timedelta(hours=24)).timestamp()), int(now.timestamp())
    if preset == PRESET_LAST7D:
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp()), int(now.timestamp())
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


def _check_download_content(data: bytes, recording_id: int) -> None:
    """Raise ValueError if *data* looks like an error response rather than a video file.

    Synology DSM may return:
    - An empty body when the session has expired and no redirect is possible.
    - An HTML login page (text/html) when the reverse proxy redirects instead
      of returning a JSON error.
    - A JSON error body when the content-type header was missed by the client.
    Any of these would silently produce a corrupt or empty file without this check.
    """
    if not data:
        raise ValueError(
            f"Recording {recording_id}: server returned an empty response. "
            "The session may have expired — try logging out and back in."
        )

    # Detect HTML responses (login redirect, DSM error page).
    stripped = data[:100].lstrip()
    if stripped[:9].lower() == b"<!doctype" or stripped[:6].lower() == b"<html>":
        raise ValueError(
            f"Recording {recording_id}: server returned an HTML page instead of a video file. "
            "This usually means the session expired or the request was rejected. "
            "Log out and log back in, then try again."
        )

    # Detect a bare JSON error that slipped past the content-type check.
    if stripped[:1] == b"{":
        import json as _json  # noqa: PLC0415

        try:
            obj = _json.loads(data)
        except Exception:
            obj = None
        if isinstance(obj, dict) and not obj.get("success", True):
            code = obj.get("error", {}).get("code", 0)
            msg = obj.get("error", {}).get("message", "")
            raise ValueError(
                f"Recording {recording_id}: API returned error code {code}"
                + (f" — {msg}" if msg else "")
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
    data = await api.download(
        api="SYNO.SurveillanceStation.Recording",
        method="Download",
        # Confirmed against Synology's official Web API reference: this API
        # only ever shipped versions 1/3/4/6 (never 5) and Download's id
        # param is documented as "6 and onward" — the previous version=5
        # here silently produced a server-side "Execution failed" (code
        # 400) for every download instead of an actual video file.
        version=6,
        extra_params={"id": str(recording_id)},
    )

    _check_download_content(data, recording_id)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_path.write_bytes(data)
    except Exception:
        # Remove any partial file so the user is not left with a 0-byte placeholder.
        with contextlib.suppress(OSError):
            output_path.unlink()
        raise

    log.info(
        "Recording %d downloaded: %s (%d bytes)",
        recording_id,
        output_path,
        len(data),
    )
    return output_path


_recording_thumbnail_cache: collections.OrderedDict[int, bytes] = collections.OrderedDict()

_MAX_THUMBNAIL_CACHE = 128


def _cache_put(
    cache: collections.OrderedDict[int, bytes], key: int, value: bytes, limit: int
) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def clear_snapshot_cache() -> None:
    """Clear the thumbnail cache."""
    _recording_thumbnail_cache.clear()


async def fetch_recording_thumbnail(
    api: SurveillanceAPI,
    rec: Recording,
) -> bytes:
    """Fetch a thumbnail for a recording.

    Uses Recording.GetThumbnail with eventInfo array matching the APK format.
    """
    if rec.id in _recording_thumbnail_cache:
        return _recording_thumbnail_cache[rec.id]

    async with _thumbnail_semaphore:
        if rec.id in _recording_thumbnail_cache:
            return _recording_thumbnail_cache[rec.id]

        # Recording.GetThumbnail — eventInfo must be a JSON array of objects
        # matching the APK format (dsId + blFallbackByLoadEvt + eventInfo only).
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
                                "cameraId": rec.camera_id,
                                "archId": rec.arch_id,
                                "mountId": rec.mount_id,
                                "rec_group": 0,
                                "targetTime": rec.start_time,
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
                        _cache_put(
                            _recording_thumbnail_cache,
                            rec.id,
                            image_data,
                            _MAX_THUMBNAIL_CACHE,
                        )
                        return image_data
        except Exception as exc:
            log.debug(
                "Recording thumbnail failed for %d: %s",
                rec.id,
                exc,
            )

    return b""
