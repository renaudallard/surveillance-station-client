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

"""Event-related code shared by every event backend, plus alerts.

What the Events page and the Live View timeline list as events, and how
they classify them, depends on the Surveillance Station version: see
services.event_backend for the EventBackend protocol and how one is
picked. This module holds what doesn't: the RecordingPicker::EnumInterval
request (recording presence and the recording files events play back
from), the EventKind every backend classifies into, and alerts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from surveillance.api.models import Alert

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI

log = logging.getLogger(__name__)

# EnumInterval's `interval`: the bucket size, in seconds, of the event_map
# it returns alongside the per-file recording list. 5 is what DSM's own
# Monitor Center web UI asks for.
ENUM_INTERVAL_SEC = 5

# EnumInterval over a wide range (e.g. Last 7 days) with many cameras selected
# has been observed to exceed the API client's default 30s timeout and fail
# outright (httpx.ReadTimeout) rather than just being slow — confirmed with
# a 7-day/22-camera query against the real NAS. This doesn't speed up the
# request, it just gives DSM enough room to actually finish it.
_ENUM_INTERVAL_REQUEST_TIMEOUT = 120.0


@dataclass(frozen=True)
class EventKind:
    """One detected category of an event, as an EventBackend classifies it.

    An event can carry several at once (e.g. motion + audio). *key* is
    the filter key the type filters select and persist; each backend
    keeps its keys in a format of its own, so a key saved under one
    backend can never match an event from another. *filter_label* is
    the menu text for a filter entry, *label* the shorter text an event
    row shows, and *notes* contributor-facing caveats about how sure
    the classification is (never shown to end users).
    """

    key: str
    label: str
    filter_label: str
    notes: str = ""
    is_motion: bool = False


def find_parent_recording(
    recordings: list[dict[str, Any]], idx: int, timestamp: int
) -> tuple[dict[str, Any] | None, int]:
    """Find the coarse recording-file entry containing *timestamp*, resuming
    the scan from *idx* rather than restarting at the beginning each time.

    An event backend's events span possibly several (or zero, in a real
    gap) underlying recording files of the queried range. Playback needs
    that file's id/mountId/archId; the precise moment is reached via
    Event.seek_offset instead. Both `recordings` (blStartTimeAsc=true)
    and the timestamps this is called with must be chronological: a
    rescan-from-scratch per event, O(events * recordings), was pure
    waste on a large result set; this is O(events + recordings) per
    camera.
    """
    while idx < len(recordings) and timestamp >= recordings[idx].get(
        "stop", recordings[idx].get("start", 0)
    ):
        idx += 1
    if idx < len(recordings) and recordings[idx].get("start", 0) <= timestamp:
        return recordings[idx], idx
    return None, idx


async def fetch_enum_interval(
    api: SurveillanceAPI, camera_ids: list[int], from_time: int, to_time: int
) -> list[dict[str, Any]]:
    """The RecordingPicker::EnumInterval request behind both
    list_recording_presence (each camera's per-file `event` list) and
    LegacyEventBackend's events (the same response's `event_map`) --
    one call, two different fields of the same per-camera result.
    """
    if not camera_ids:
        return []

    content = [{"dsId": 0, "archId": 0, "mountId": 0, "camList": camera_ids}]
    data = await api.request(
        api="SYNO.SurveillanceStation.RecordingPicker",
        method="EnumInterval",
        version=1,
        extra_params={
            "from": str(from_time),
            "to": str(to_time),
            "content": json.dumps(content),
            "recording": "true",
            "blStartTimeAsc": "true",
            "blGetMetaMap": "true",
            "interval": str(ENUM_INTERVAL_SEC),
            "blExcludeC2": "true",
        },
        timeout=_ENUM_INTERVAL_REQUEST_TIMEOUT,
    )
    cameras: list[dict[str, Any]] = []
    for entry in data.get("cameras", []):
        cameras.extend(entry)
    return cameras


def merge_intervals(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort *spans* and merge every overlapping/adjacent pair.

    Shared by decode_camera_presence's per-camera merge and the Live
    View timeline's own cross-camera OR union (see
    LiveView._apply_timeline_data_to_canvas) -- same operation either way.
    """
    merged: list[tuple[int, int]] = []
    for start, stop in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((start, stop))
    return merged


def decode_camera_presence(cam: dict[str, Any]) -> list[tuple[int, int]]:
    """One camera's recording-presence spans, from EnumInterval's own
    per-file `event` list -- see list_recording_presence."""
    spans = [
        (rec["start"], rec["stop"])
        for rec in cam.get("event", [])
        if "start" in rec and "stop" in rec
    ]
    return merge_intervals(spans)


async def list_recording_presence(
    api: SurveillanceAPI, camera_ids: list[int], from_time: int, to_time: int
) -> dict[int, list[tuple[int, int]]]:
    """Per-camera recording-presence spans for the Live View timeline's
    presence bar, read from RecordingPicker::EnumInterval's own per-file
    `event` list -- the same underlying recording-file segments an event
    backend resolves an event's parent file from, used directly here
    instead: each entry's start/stop already marks exactly where a
    recording exists.

    Returns {camera_id: [(start, stop), ...]}, merged and sorted; a
    camera with nothing recorded in range is simply absent from the
    result.
    """
    cameras = await fetch_enum_interval(api, camera_ids, from_time, to_time)
    result: dict[int, list[tuple[int, int]]] = {}
    for cam in cameras:
        spans = decode_camera_presence(cam)
        if spans:
            result[cam.get("camera_id", 0)] = spans
    return result


async def list_alerts(
    api: SurveillanceAPI,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Alert], int]:
    """List alerts/notifications.

    Returns (alerts, total_count).
    """
    data = await api.request(
        api="SYNO.SurveillanceStation.Notification",
        method="List",
        version=1,
        extra_params={
            "offset": str(offset),
            "limit": str(limit),
        },
    )

    alerts = [Alert.from_api(a) for a in data.get("notifications", data.get("alerts", []))]
    total = data.get("total", len(alerts))
    return alerts, total


async def count_unread_alerts(api: SurveillanceAPI) -> int:
    """Get count of unread alerts."""
    data = await api.request(
        api="SYNO.SurveillanceStation.Notification",
        method="GetUnreadCount",
        version=1,
    )
    count: int = data.get("unread", 0)
    return count


async def mark_alerts_read(api: SurveillanceAPI, alert_ids: list[int]) -> None:
    """Mark multiple alerts as read in a single call."""
    if not alert_ids:
        return
    await api.request(
        api="SYNO.SurveillanceStation.Notification",
        method="SetRead",
        version=1,
        extra_params={"idList": ",".join(str(i) for i in alert_ids)},
    )
