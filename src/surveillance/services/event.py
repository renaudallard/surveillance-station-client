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

"""Event and alert management service."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from surveillance.api.models import Alert, Event

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI

log = logging.getLogger(__name__)

# RecordingPicker::EnumInterval's event_map is a run-length-encoded bitmap:
# each [value, flag, reserved] entry means "value * _EVENT_MAP_INTERVAL_SEC
# seconds in state `flag`". flag == 1 means "recording, nothing detected";
# any other known flag value is a real, short motion/alarm event, confirmed
# by decoding the exact requests DSM's own Monitor Center web UI makes
# (interval=5) and by watching the source video at several decoded event
# windows. flag == 0 is a separate, non-event state: it only ever appears
# as the last few minutes of the *currently still-recording* (not yet
# closed) segment, i.e. "not processed yet" rather than "something
# happened" — confirmed by re-querying minutes later and finding it had
# resolved to flag 1 once that segment closed. Treating it as an event
# produced a phantom event that didn't match DSM's own timeline.
_EVENT_MAP_INTERVAL_SEC = 5
_EVENT_MAP_NON_EVENT_FLAGS = {0, 1}

# EnumInterval over a wide range (e.g. Last 7 days) with many cameras selected
# has been observed to exceed the API client's default 30s timeout and fail
# outright (httpx.ReadTimeout) rather than just being slow — confirmed with
# a 7-day/22-camera query against the real NAS. This doesn't speed up the
# request, it just gives DSM enough room to actually finish it.
_EVENT_MAP_REQUEST_TIMEOUT = 120.0

# Bit 0 is set on every real event flag we've seen (257, 513, 33554689, ...)
# and looks like a generic "something happened here" marker. Confirmed flag
# values so far, each checked against the actual recorded video (not just
# inferred from camera settings):
#   513  (bit0|bit9)        plain Motion Detection, no smart detector
#   257  (bit0|bit8)        "enhanced"/dynamic motion, not a person — often
#                            environmental (e.g. a lamp switching on/off).
#                            Consistent with CAM 67, which produces this flag
#                            with Person Detection disabled: bit8 alone does
#                            NOT mean a person was classified.
#   33554689 (bit0|bit8|bit25)  person detected — bit25 is what actually
#                            carries the classification; bit8 alone doesn't.
# Any other flag value is unconfirmed. Event.event_type is always the *raw*
# flag value, never a guess beyond what's listed here — see EVENT_TYPES in
# ui/events.py, which only names these three and shows everything else as a
# bare numeric type.
MOTION_EVENT_FLAG = 513
UNKNOWN_MOTION_FLAG = 257
PERSON_DETECTED_FLAG = 33554689


def _advance_to_parent(
    recordings: list[dict[str, Any]], idx: int, timestamp: int
) -> tuple[dict[str, Any] | None, int]:
    """Find the coarse recording-file entry containing *timestamp*, resuming
    the scan from *idx* rather than restarting at the beginning each time.

    event_map covers the whole queried [from, to] range, spanning possibly
    several (or zero, in a real gap) underlying recording files. Playback
    needs that file's id/mountId/archId; the precise moment is reached via
    Event.seek_offset instead. Both `recordings` (blStartTimeAsc=true) and
    the timestamps this is called with (event_map decoded in order) are
    chronological, so a rescan-from-scratch per event — O(events *
    recordings) — was pure waste on a large result set; this is O(events +
    recordings) per camera.
    """
    while idx < len(recordings) and timestamp >= recordings[idx].get(
        "stop", recordings[idx].get("start", 0)
    ):
        idx += 1
    if idx < len(recordings) and recordings[idx].get("start", 0) <= timestamp:
        return recordings[idx], idx
    return None, idx


async def list_granular_events(
    api: SurveillanceAPI,
    camera_ids: list[int],
    camera_names: dict[int, str],
    from_time: int,
    to_time: int,
) -> list[Event]:
    """List real, short-duration events decoded from event_map.

    Unlike list_events() (SYNO.SurveillanceStation.Event::List), which only
    exposes coarse ~30-minute recording-file segments, this decodes
    RecordingPicker::EnumInterval's event_map to recover the actual
    irregular motion/alarm windows shown in DSM's own Monitor Center
    timeline. Returns every event within [from_time, to_time], newest first —
    deliberately uncapped, since silently dropping older-but-in-range events
    would make the time-range filter (Today/Yesterday/Last 7 days/...) lie
    about what it's actually showing.
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
            "interval": str(_EVENT_MAP_INTERVAL_SEC),
            "blExcludeC2": "true",
        },
        timeout=_EVENT_MAP_REQUEST_TIMEOUT,
    )

    events: list[Event] = []
    for entry in data.get("cameras", []):
        for cam in entry:
            camera_id = cam.get("camera_id", 0)
            camera_name = camera_names.get(camera_id, str(camera_id))
            recordings = cam.get("event", [])

            t = from_time
            parent_idx = 0
            for value, flag, _reserved in cam.get("event_map", []):
                duration = value * _EVENT_MAP_INTERVAL_SEC
                run_start, run_stop = t, t + duration
                t = run_stop
                if flag in _EVENT_MAP_NON_EVENT_FLAGS:
                    continue

                parent, parent_idx = _advance_to_parent(recordings, parent_idx, run_start)
                if parent is None:
                    continue

                events.append(
                    Event(
                        id=parent.get("id", 0),
                        camera_id=camera_id,
                        camera_name=camera_name,
                        event_type=flag,
                        start_time=run_start,
                        stop_time=run_stop,
                        mount_id=parent.get("mountId", 0),
                        arch_id=parent.get("archId", 0),
                        seek_offset=max(0, run_start - parent.get("start", run_start)),
                    )
                )

    events.sort(key=lambda e: e.start_time, reverse=True)
    return events


async def list_events(
    api: SurveillanceAPI,
    camera_id: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Event], int]:
    """List motion/alarm events.

    Tries Event.List first, falls back to Event.Query on older NAS versions.
    Returns (events, total_count).
    """
    params: dict[str, str] = {
        "offset": str(offset),
        "limit": str(limit),
    }
    if camera_id is not None:
        params["cameraIds"] = str(camera_id)

    # Try List first (modern), fall back to Query (legacy)
    last_exc: Exception | None = None
    for method in ("List", "Query"):
        try:
            data = await api.request(
                api="SYNO.SurveillanceStation.Event",
                method=method,
                version=5,
                extra_params=params,
            )
        except Exception as exc:
            last_exc = exc
            if method == "List":
                log.debug("Event.List not available, trying Event.Query")
                continue
            raise
        else:
            events = [Event.from_api(e) for e in data.get("events", [])]
            total = data.get("total", len(events))
            return events, total
    raise last_exc  # type: ignore[misc]


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
