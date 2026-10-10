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

"""LegacyEventBackend: events decoded from EnumInterval's event_map bitmask.

The only event source on a Surveillance Station without the Event Center
API (see services.event_backend). Each event is a run of event_map
buckets, classified bit by bit and per camera brand by
services.legacy_event_bits; LEGACY_EVENT_BITMASK.md documents how each
bit was derived. Event.legacy_flag/legacy_reserved keep the raw values,
never a guess.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from surveillance.api.models import Event
from surveillance.services.event import (
    ENUM_INTERVAL_SEC,
    EventKind,
    decode_camera_presence,
    fetch_enum_interval,
    find_parent_recording,
)
from surveillance.services.legacy_event_bits import (
    LegacyDecodedBit,
    build_legacy_filter_options,
    decode_legacy_flag,
    format_legacy_filter_label,
    legacy_event_matches_keys,
)

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI
    from surveillance.config import AppConfig, EventTypeHistory
    from surveillance.services.event_backend import TypeSignature

# RecordingPicker::EnumInterval's event_map is a run-length-encoded bitmap:
# each [value, flag, reserved] entry means "value * ENUM_INTERVAL_SEC
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
_EVENT_MAP_NON_EVENT_FLAGS = {0, 1}

_MOTION_BIT = 8

# legacy_filter_key's format: a two-digit bit or the R0 reserved
# pseudo-bit, optionally scoped to a brand ("08", "25:hikvision", "R0").
_FILTER_KEY_RE = re.compile(r"^(\d{2}|R0)(:.+)?$")


def _decode_legacy_camera_events(
    cam: dict[str, Any], camera_id: int, camera_name: str, from_time: int
) -> list[Event]:
    """One camera's real, short-duration events, decoded from
    EnumInterval's event_map -- see LegacyEventBackend.list_events."""
    recordings = cam.get("event", [])
    events: list[Event] = []
    t = from_time
    parent_idx = 0
    for value, flag, reserved in cam.get("event_map", []):
        duration = value * ENUM_INTERVAL_SEC
        run_start, run_stop = t, t + duration
        t = run_stop
        # A flag of 0/1 alone means "nothing happened" — but not if
        # `reserved` is set: before 9.3 that's Object Removal Detection
        # firing via overflow with nothing else in this bucket (see
        # LEGACY_EVENT_BITMASK.md), a real event that must not be dropped.
        # On 9.3 a nonzero reserved can be other detection types too.
        if flag in _EVENT_MAP_NON_EVENT_FLAGS and not reserved:
            continue

        parent, parent_idx = find_parent_recording(recordings, parent_idx, run_start)
        if parent is None:
            continue

        events.append(
            Event(
                id=parent.get("id", 0),
                camera_id=camera_id,
                camera_name=camera_name,
                event_type=0,
                start_time=run_start,
                stop_time=run_stop,
                # mountId/archId sit on the camera object, not the
                # individual event entry, so they must come from cam.
                mount_id=cam.get("mountId", 0),
                arch_id=cam.get("archId", 0),
                seek_offset=max(0, run_start - parent.get("start", run_start)),
                legacy_flag=flag,
                legacy_reserved=reserved,
            )
        )
    return events


def _legacy_kind(decoded: LegacyDecodedBit) -> EventKind:
    return EventKind(
        key=decoded.key,
        label=decoded.label,
        filter_label=format_legacy_filter_label(decoded.bit, decoded.label, decoded.brand),
        notes=decoded.notes,
        is_motion=decoded.bit == _MOTION_BIT,
    )


@functools.lru_cache(maxsize=1024)
def _classify_legacy(flag: int, reserved: int, vendor: str) -> tuple[EventKind, ...]:
    """Memoised for the same reason decode_legacy_flag is: a NAS produces
    a handful of distinct flags, classified once per event row."""
    return tuple(_legacy_kind(d) for d in decode_legacy_flag(flag, reserved, vendor))


class LegacyEventBackend:
    """EventBackend over RecordingPicker::EnumInterval's event_map."""

    name = "legacy"
    # One event_map bucket can carry several detected categories at once.
    supports_match_all = True

    async def list_events(
        self,
        api: SurveillanceAPI,
        camera_ids: list[int],
        camera_names: dict[int, str],
        from_time: int,
        to_time: int,
    ) -> list[Event]:
        """List real, short-duration events decoded from event_map.

        Unlike SYNO.SurveillanceStation.Event::List, which only exposes
        coarse ~30-minute recording-file segments, this decodes
        RecordingPicker::EnumInterval's event_map to recover the actual
        irregular motion/alarm windows shown in DSM's own Monitor Center
        timeline. Returns every event within [from_time, to_time], newest
        first — deliberately uncapped, since silently dropping
        older-but-in-range events would make the time-range filter
        (Today/Yesterday/Last 7 days/...) lie about what it's actually
        showing.
        """
        _presence, events = await self.list_presence_and_events(
            api, camera_ids, camera_names, from_time, to_time
        )
        return events

    async def list_presence_and_events(
        self,
        api: SurveillanceAPI,
        camera_ids: list[int],
        camera_names: dict[int, str],
        from_time: int,
        to_time: int,
    ) -> tuple[dict[int, list[tuple[int, int]]], list[Event]]:
        """services.event.list_recording_presence and list_events
        together, off a single EnumInterval call -- for a caller needing
        both (the Live View timeline, which shows presence and event
        markers on the same bar and refreshes them on the same
        pan/zoom/live-tick cadence), fetching this same per-camera data
        twice would double the request rate for no benefit.

        Returns (presence, events) exactly as the two would individually.
        """
        if not camera_ids:
            return {}, []

        cameras = await fetch_enum_interval(api, camera_ids, from_time, to_time)
        presence: dict[int, list[tuple[int, int]]] = {}
        events: list[Event] = []
        for cam in cameras:
            camera_id = cam.get("camera_id", 0)
            spans = decode_camera_presence(cam)
            if spans:
                presence[camera_id] = spans
            camera_name = camera_names.get(camera_id, str(camera_id))
            events.extend(_decode_legacy_camera_events(cam, camera_id, camera_name, from_time))

        events.sort(key=lambda e: e.start_time, reverse=True)
        return presence, events

    async def list_type_signatures(
        self, api: SurveillanceAPI, camera_id: int, from_time: int, to_time: int
    ) -> set[TypeSignature]:
        # event_map is the only place a type shows up, so this costs a
        # full list_events.
        events = await self.list_events(
            api, [camera_id], {camera_id: str(camera_id)}, from_time, to_time
        )
        return {self.type_signature(e) for e in events}

    def classify(self, event: Event, vendor: str) -> tuple[EventKind, ...]:
        return _classify_legacy(event.legacy_flag, event.legacy_reserved, vendor)

    def matches(self, event: Event, vendor: str, keys: Iterable[str], match_all: bool) -> bool:
        return legacy_event_matches_keys(
            event.legacy_flag, event.legacy_reserved, vendor, keys, match_all
        )

    def fixed_filter_options(self) -> list[tuple[str, str, str]] | None:
        # Which bits a camera sets is only known from its event_map.
        return None

    def is_filter_key(self, key: str) -> bool:
        return _FILTER_KEY_RE.match(key) is not None

    def type_signature(self, event: Event) -> TypeSignature:
        return (event.legacy_flag, event.legacy_reserved)

    def filter_options(
        self, occurrences: Iterable[tuple[TypeSignature, str]]
    ) -> list[tuple[str, str, str]]:
        return build_legacy_filter_options(
            (flag, reserved, vendor) for (flag, reserved), vendor in occurrences
        )

    def type_history(self, config: AppConfig) -> dict[int, EventTypeHistory]:
        return config.legacy_event_type_history
