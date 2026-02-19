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

"""Time lapse recording management service."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from surveillance.api.models import Recording, TimeLapseRecording, TimeLapseTask

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI

log = logging.getLogger(__name__)


async def list_tasks(api: SurveillanceAPI) -> list[TimeLapseTask]:
    """List time lapse tasks."""
    data = await api.request(
        api="SYNO.SurveillanceStation.TimeLapse",
        method="ListTask",
        version=1,
    )

    raw = data.get("task", [])
    if raw:
        log.debug("TimeLapse ListTask first item keys: %s", list(raw[0].keys()))
    return [TimeLapseTask.from_api(t) for t in raw]


async def list_recordings(
    api: SurveillanceAPI,
    task_id: int = -1,
    offset: int = 0,
    limit: int = 100,
    from_time: int = 0,
    to_time: int = 0,
    locked: int = 0,
) -> tuple[list[TimeLapseRecording], int]:
    """List time lapse recordings.

    Returns (recordings, total_count).

    Args:
        task_id: Task ID to filter by (-1 = all tasks).
        offset: Pagination offset.
        limit: Maximum number of results (max 100).
        from_time: Start of time range (unix seconds, 0 = no filter).
        to_time: End of time range (unix seconds, 0 = no filter).
        locked: 0 = all, 1 = locked only, 2 = unlocked only.
    """
    params: dict[str, str] = {
        "lapseId": str(task_id),
        "start": str(offset),
        "limit": str(limit),
        "locked": str(locked),
        "blIncludeSnapshot": "false",
    }
    if from_time:
        params["fromTime"] = str(from_time)
    if to_time:
        params["toTime"] = str(to_time)

    data = await api.request(
        api="SYNO.SurveillanceStation.TimeLapse.Recording",
        method="List",
        version=1,
        extra_params=params,
    )

    raw = data.get("events", [])
    if raw:
        log.debug("TimeLapse Recording List first item keys: %s", list(raw[0].keys()))
    recordings = [TimeLapseRecording.from_api(r) for r in raw]
    total = data.get("total", len(recordings))
    return recordings, total


async def delete_recordings(api: SurveillanceAPI, ids: list[int]) -> None:
    """Delete time lapse recordings."""
    await api.request(
        api="SYNO.SurveillanceStation.TimeLapse.Recording",
        method="Delete",
        version=1,
        extra_params={"idList": ",".join(str(i) for i in ids)},
    )


async def lock_recordings(api: SurveillanceAPI, ids: list[int]) -> None:
    """Lock time lapse recordings."""
    await api.request(
        api="SYNO.SurveillanceStation.TimeLapse.Recording",
        method="Lock",
        version=1,
        extra_params={"idList": ",".join(str(i) for i in ids)},
    )


async def unlock_recordings(api: SurveillanceAPI, ids: list[int]) -> None:
    """Unlock time lapse recordings."""
    await api.request(
        api="SYNO.SurveillanceStation.TimeLapse.Recording",
        method="Unlock",
        version=1,
        extra_params={"idList": ",".join(str(i) for i in ids)},
    )


def to_recording(rec: TimeLapseRecording) -> Recording:
    """Convert a TimeLapseRecording to a Recording for playback."""
    return Recording(
        id=rec.id,
        camera_id=rec.camera_id,
        camera_name=rec.camera_name,
        start_time=rec.start_time,
        stop_time=rec.stop_time,
        event_type=3,  # time lapse
        mount_id=rec.mount_id,
        arch_id=rec.arch_id,
    )


async def download_recording(
    api: SurveillanceAPI,
    recording_id: int,
    output_path: Path,
) -> Path:
    """Download a time lapse recording to disk."""
    data = await api.download(
        api="SYNO.SurveillanceStation.Recording",
        method="Download",
        version=5,
        extra_params={"id": str(recording_id), "recEvtType": "3"},
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return output_path
