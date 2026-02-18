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


async def get_stream_url(api: SurveillanceAPI, recording_id: int) -> str:
    """Get playback URL for a recording."""
    data = await api.request(
        api="SYNO.SurveillanceStation.Recording",
        method="Stream",
        version=5,
        extra_params={"id": str(recording_id), "offsetTimeMs": "0"},
    )
    # Build full URL from response
    path = data.get("uri", "")
    if path:
        return f"{api.base_url}{path}"
    # Fallback: construct URL directly
    return api.get_stream_url(
        "entry.cgi",
        {
            "api": "SYNO.SurveillanceStation.Recording",
            "method": "Stream",
            "version": "5",
            "id": str(recording_id),
            "offsetTimeMs": "0",
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


async def fetch_recording_thumbnail(
    api: SurveillanceAPI,
    rec: Recording,
) -> bytes:
    """Extract a single JPEG frame from the middle of a recording via ffmpeg."""
    mid_offset_ms = (rec.stop_time - rec.start_time) * 500
    stream_url = api.get_stream_url(
        "entry.cgi",
        {
            "api": "SYNO.SurveillanceStation.Recording",
            "method": "Stream",
            "version": "5",
            "id": str(rec.id),
            "offsetTimeMs": str(mid_offset_ms),
        },
    )

    async with _thumbnail_semaphore:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            stream_url,
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "5",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)

    if proc.returncode != 0 or not stdout:
        msg = stderr.decode(errors="replace").strip() if stderr else "no output"
        log.warning("Thumbnail failed for recording %d: %s", rec.id, msg)
        return b""
    return stdout
