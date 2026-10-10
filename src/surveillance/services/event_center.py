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

"""EventCenterBackend: typed events from Surveillance Station 9.3's Event Center.

SYNO.SurveillanceStation.EventCenter.Event is what DSM's own Event Center
web app is built on. Unlike LegacyEventBackend's event_map, each event
comes with exactly one explicit type (EventCenterType below, Synology's
own enum) and, for object detection, an object class, and the NAS reports
which of them it supports (Capability), so every type an event can have
is known up front. Its events carry no recording file, so playback still
resolves each one against EnumInterval's per-file recording list, the
same as the legacy backend does.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from surveillance.api.models import Event
from surveillance.services.event import (
    ENUM_INTERVAL_SEC,
    EventKind,
    decode_camera_presence,
    fetch_enum_interval,
    find_parent_recording,
)

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI
    from surveillance.config import AppConfig, EventTypeHistory
    from surveillance.services.event_backend import TypeSignature

log = logging.getLogger(__name__)

EVENT_CENTER_API = "SYNO.SurveillanceStation.EventCenter.Event"

# 1000 per page took ~0.3s on a real NAS; a busy camera produces tens
# of thousands of events a month.
_LIST_PAGE_SIZE = 1000
# Only this NAS's own cameras, the same as the EnumInterval request.
_LOCAL_DS_ID = "0"
_SQL_ORDER_DESC = 1


class EventCenterType(IntEnum):
    """EventCenter.Event's event_type: Synology's own EVENT_TYPE enum
    (event_center_vue.js). The API only ever sends the number."""

    INTRUSION = 1
    LOITERING = 2
    LPR = 3
    CROWD = 4
    VACANCY = 5
    TAILGATING = 6
    RUNNING = 7
    FACE_DETECTION = 8
    FACE_RECOGNITION = 9
    MOTION = 10
    OBJECT_DETECTION = 11
    TAMPERING = 12
    FACE_TEMPERATURE = 13
    TEMPERATURE = 14
    HIGH_TEMPERATURE = 15
    LOW_TEMPERATURE = 16
    SMOKE = 17
    AUDIO = 18
    DI = 19
    EXTERNAL_DI = 20
    INTERCOM = 21
    PIR = 22
    UNATTENDED_OBJECT = 23
    MISSING_OBJECT = 24
    ENTER = 25
    EXIT = 26


class EventCenterObjectType(IntEnum):
    """EventCenter.Event's object_type, for OBJECT_DETECTION: Synology's
    own OBJECT_TYPE enum. NONE when the detection has no class."""

    NONE = 0
    ANIMAL = 1
    PEOPLE = 2
    VEHICLE = 3
    PEOPLE_VEHICLE = 4


@dataclass(frozen=True)
class _TypeInfo:
    key: str
    label: str


# Every event type, in the order the Event Center's own filter menu
# lists them (event_center_vue.js), which is also the filter order here.
_EVENT_TYPES: dict[int, _TypeInfo] = {
    EventCenterType.OBJECT_DETECTION: _TypeInfo("object", "Object detection"),
    EventCenterType.INTRUSION: _TypeInfo("intrusion", "Intrusion"),
    EventCenterType.LOITERING: _TypeInfo("loitering", "Loitering"),
    EventCenterType.LPR: _TypeInfo("lpr", "License plate recognition"),
    EventCenterType.CROWD: _TypeInfo("crowd", "Crowd"),
    EventCenterType.VACANCY: _TypeInfo("vacancy", "Vacancy"),
    EventCenterType.SMOKE: _TypeInfo("smoke", "Smoke"),
    EventCenterType.MOTION: _TypeInfo("motion", "Motion"),
    EventCenterType.TAMPERING: _TypeInfo("tampering", "Tampering"),
    EventCenterType.AUDIO: _TypeInfo("audio", "Audio"),
    EventCenterType.ENTER: _TypeInfo("enter", "Enter"),
    EventCenterType.EXIT: _TypeInfo("exit", "Exit"),
    EventCenterType.FACE_DETECTION: _TypeInfo("face_detection", "Face detection"),
    EventCenterType.TAILGATING: _TypeInfo("tailgating", "Tailgating"),
    EventCenterType.RUNNING: _TypeInfo("running", "Running"),
    EventCenterType.UNATTENDED_OBJECT: _TypeInfo("unattended_object", "Unattended object"),
    EventCenterType.MISSING_OBJECT: _TypeInfo("missing_object", "Missing object"),
    EventCenterType.DI: _TypeInfo("di", "Digital input"),
    EventCenterType.EXTERNAL_DI: _TypeInfo("external_di", "External digital input"),
    EventCenterType.INTERCOM: _TypeInfo("intercom", "Intercom"),
    EventCenterType.PIR: _TypeInfo("pir", "PIR"),
    EventCenterType.FACE_TEMPERATURE: _TypeInfo("face_temperature", "Face temperature"),
    EventCenterType.TEMPERATURE: _TypeInfo("temperature", "Temperature"),
    EventCenterType.HIGH_TEMPERATURE: _TypeInfo("high_temperature", "High temperature"),
    EventCenterType.LOW_TEMPERATURE: _TypeInfo("low_temperature", "Low temperature"),
    EventCenterType.FACE_RECOGNITION: _TypeInfo("face_recognition", "Face recognition"),
}
_TYPE_ORDER = {event_type: i for i, event_type in enumerate(_EVENT_TYPES)}

# Object classes object detection can report, in filter order.
_OBJECT_TYPES: dict[int, _TypeInfo] = {
    EventCenterObjectType.ANIMAL: _TypeInfo("animal", "Animal"),
    EventCenterObjectType.PEOPLE: _TypeInfo("people", "People"),
    EventCenterObjectType.VEHICLE: _TypeInfo("vehicle", "Vehicle"),
    EventCenterObjectType.PEOPLE_VEHICLE: _TypeInfo("people_vehicle", "People and vehicle"),
}

_UNKNOWN_KEY_PREFIX = "type:"


def _signatures(event_types: Iterable[int], object_types: Iterable[int]) -> list[tuple[int, int]]:
    """Every (event_type, object_type) an event can have, in filter order:
    object detection both without a class and with each of its own."""
    wanted, objects = set(event_types), [o for o in _OBJECT_TYPES if o in set(object_types)]
    return [
        (int(event_type), int(object_type))
        for event_type in _EVENT_TYPES
        if event_type in wanted
        for object_type in (
            [EventCenterObjectType.NONE, *objects]
            if event_type == EventCenterType.OBJECT_DETECTION
            else [EventCenterObjectType.NONE]
        )
    ]


_ALL_SIGNATURES = _signatures(_EVENT_TYPES, _OBJECT_TYPES)


def _kind(event_type: int, object_type: int) -> EventKind:
    info = _EVENT_TYPES.get(event_type)
    if info is None:
        label = f"Unknown type {event_type}"
        return EventKind(key=f"{_UNKNOWN_KEY_PREFIX}{event_type}", label=label, filter_label=label)
    if event_type == EventCenterType.OBJECT_DETECTION and object_type in _OBJECT_TYPES:
        obj = _OBJECT_TYPES[object_type]
        return EventKind(
            key=f"{info.key}:{obj.key}",
            label=obj.label,
            filter_label=f"{info.label} ({obj.label})",
        )
    return EventKind(
        key=info.key,
        label=info.label,
        filter_label=info.label,
        is_motion=event_type == EventCenterType.MOTION,
    )


_FILTER_KEYS = frozenset(_kind(*signature).key for signature in _ALL_SIGNATURES)


def _signature_order(signature: TypeSignature) -> tuple[int, int]:
    event_type, object_type = signature
    return (_TYPE_ORDER.get(event_type, len(_TYPE_ORDER) + event_type), object_type)


async def _list_event_center(
    api: SurveillanceAPI, camera_ids: list[int], from_time: int, to_time: int
) -> list[dict[str, Any]]:
    """Every EventCenter.Event::List entry for *camera_ids* within
    [from_time, to_time], paged through newest first."""
    entries: list[dict[str, Any]] = []
    offset = {"start_time": 0, "id": 0}
    while True:
        data = await api.request(
            api=EVENT_CENTER_API,
            method="List",
            version=1,
            extra_params={
                "camera_ids": ",".join(str(c) for c in camera_ids),
                "start_time": str(from_time),
                "end_time": str(to_time),
                "order_by_start_time": str(_SQL_ORDER_DESC),
                # Keyed by DS id; the next page starts after this one's
                # last (start_time, id).
                "start_time_offset": json.dumps({_LOCAL_DS_ID: offset}),
                "limit": str(_LIST_PAGE_SIZE),
            },
        )
        page = data.get(_LOCAL_DS_ID, [])
        # Done only on an empty page: List's own cap on limit is unknown,
        # and stopping at a short page would lose the rest under a lower
        # one.
        if not page:
            return entries
        entries.extend(page)
        offset = {"start_time": page[-1]["start_time"], "id": page[-1]["id"]}


def _resolve_camera_events(
    cam: dict[str, Any], entries: list[dict[str, Any]], camera_name: str
) -> list[Event]:
    """*entries* (one camera's) as Events, each resolved to the recording
    file it plays back from. An entry outside every recording file, e.g.
    older than the recordings kept, can't be played and is left out."""
    recordings = cam.get("event", [])
    events: list[Event] = []
    parent_idx = 0
    # find_parent_recording needs chronological input; List is newest first.
    for entry in sorted(entries, key=lambda e: (e.get("start_time", 0), e.get("id", 0))):
        start = entry.get("start_time", 0)
        parent, parent_idx = find_parent_recording(recordings, parent_idx, start)
        if parent is None:
            continue
        # duration is 0 while an event is still going on. Never shorter
        # than an event_map bucket, so a timeline marker stays visible.
        duration = max(entry.get("duration", 0), ENUM_INTERVAL_SEC)
        events.append(
            Event(
                id=parent.get("id", 0),
                camera_id=entry.get("camera_id", 0),
                camera_name=camera_name,
                event_type=0,
                start_time=start,
                stop_time=start + duration,
                mount_id=cam.get("mountId", 0),
                arch_id=cam.get("archId", 0),
                seek_offset=max(0, start - parent.get("start", start)),
                event_center_type=entry.get("event_type", 0),
                object_type=entry.get("object_type", 0),
            )
        )
    return events


class EventCenterBackend:
    """EventBackend over SYNO.SurveillanceStation.EventCenter.Event."""

    name = "event-center"
    supports_match_all = False

    def __init__(self, signatures: list[tuple[int, int]] | None = None) -> None:
        """*signatures*: the (event_type, object_type) pairs the NAS
        supports, in filter order; every defined one if None."""
        self._signatures = _ALL_SIGNATURES if signatures is None else signatures

    @classmethod
    async def connect(cls, api: SurveillanceAPI) -> EventCenterBackend:
        """A backend listing only the types *api*'s NAS supports, as its
        Capability reports them -- the same list DSM's own Event Center
        offers. Every defined type if that can't be read."""
        try:
            data = await api.request(api=EVENT_CENTER_API, method="Capability", version=1)
        except Exception as exc:  # any failure: fall back rather than block login
            log.warning("Event Center Capability failed, listing every type: %s", exc)
            return cls()

        def _ints(text: Any) -> list[int]:
            return [int(v) for v in str(text or "").split(",") if v.strip().isdigit()]

        signatures = _signatures(_ints(data.get("event_types")), _ints(data.get("object_types")))
        log.debug("Event Center types: %s", signatures)
        return cls(signatures)

    async def list_events(
        self,
        api: SurveillanceAPI,
        camera_ids: list[int],
        camera_names: dict[int, str],
        from_time: int,
        to_time: int,
    ) -> list[Event]:
        """Every Event Center event within [from_time, to_time] that has a
        recording to play back, newest first."""
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
        if not camera_ids:
            return {}, []

        cameras, entries = await asyncio.gather(
            fetch_enum_interval(api, camera_ids, from_time, to_time),
            _list_event_center(api, camera_ids, from_time, to_time),
        )
        by_camera: dict[int, list[dict[str, Any]]] = {}
        for entry in entries:
            by_camera.setdefault(entry.get("camera_id", 0), []).append(entry)

        presence: dict[int, list[tuple[int, int]]] = {}
        events: list[Event] = []
        for cam in cameras:
            camera_id = cam.get("camera_id", 0)
            spans = decode_camera_presence(cam)
            if spans:
                presence[camera_id] = spans
            camera_name = camera_names.get(camera_id, str(camera_id))
            events.extend(_resolve_camera_events(cam, by_camera.get(camera_id, []), camera_name))

        events.sort(key=lambda e: e.start_time, reverse=True)
        return presence, events

    async def list_type_signatures(
        self, api: SurveillanceAPI, camera_id: int, from_time: int, to_time: int
    ) -> set[TypeSignature]:
        # Never called: fixed_filter_options lists every type.
        return set()

    def classify(self, event: Event, vendor: str) -> tuple[EventKind, ...]:
        return (_kind(event.event_center_type, event.object_type),)

    def matches(self, event: Event, vendor: str, keys: Iterable[str], match_all: bool) -> bool:
        # One type per event, so "all of" two different keys never matches
        # (supports_match_all is False, so the UI never asks for it).
        key = _kind(event.event_center_type, event.object_type).key
        tests = (key == k for k in keys)
        return all(tests) if match_all else any(tests)

    def fixed_filter_options(self) -> list[tuple[str, str, str]] | None:
        return self.filter_options((signature, "") for signature in self._signatures)

    def is_filter_key(self, key: str) -> bool:
        return key in _FILTER_KEYS or key.startswith(_UNKNOWN_KEY_PREFIX)

    def type_signature(self, event: Event) -> TypeSignature:
        return (event.event_center_type, event.object_type)

    def filter_options(
        self, occurrences: Iterable[tuple[TypeSignature, str]]
    ) -> list[tuple[str, str, str]]:
        signatures = sorted({signature for signature, _vendor in occurrences}, key=_signature_order)
        options: dict[str, tuple[str, str, str]] = {}
        for event_type, object_type in signatures:
            kind = _kind(event_type, object_type)
            options.setdefault(kind.key, (kind.key, kind.filter_label, kind.notes))
        return list(options.values())

    def type_history(self, config: AppConfig) -> dict[int, EventTypeHistory]:
        # Never used: fixed_filter_options lists every type, so there's
        # nothing to discover or keep.
        return {}
