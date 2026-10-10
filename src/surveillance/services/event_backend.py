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

"""The EventBackend protocol, and picking one for a connected NAS.

Surveillance Station 9.3 added the Event Center, with its own
SYNO.SurveillanceStation.EventCenter.Event API returning typed events.
Before that, the only source of short, classified events was
RecordingPicker::EnumInterval's event_map bitmask, which
LegacyEventBackend decodes. The Events page and the Live View timeline
only ever go through an EventBackend, so neither depends on which one
is in use.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

from surveillance.services.event_center import EVENT_CENTER_API, EventCenterBackend
from surveillance.services.legacy_event import LegacyEventBackend

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI
    from surveillance.api.models import Event
    from surveillance.config import AppConfig, EventTypeHistory
    from surveillance.services.event import EventKind

log = logging.getLogger(__name__)

# A small, hashable summary of an event's classification -- see EventBackend.
TypeSignature = tuple[int, int]

# Use LegacyEventBackend even where the NAS offers the Event Center API,
# to try the pre-9.3 path on a newer NAS. Read when a backend is picked,
# at login, so a change takes effect at the next login (see
# settings_registry).
_FORCE_LEGACY_EVENTS = False


def set_force_legacy_events(value: bool) -> None:
    global _FORCE_LEGACY_EVENTS
    _FORCE_LEGACY_EVENTS = value


class EventBackend(Protocol):
    """Where events come from, and how they're classified and filtered.

    *vendor* is always the camera's raw DSM vendor string. A type
    signature is a small, hashable summary of an event's classification
    (whatever the backend needs to classify it again later without the
    event itself), persisted per camera in the backend's own
    type_history so the Live View Filter-events popover can list every
    type a camera has ever produced.
    """

    name: str
    # False when every event has exactly one type, so "all of" two
    # different types could never match: the UI then hides Any/All.
    supports_match_all: bool

    async def list_events(
        self,
        api: SurveillanceAPI,
        camera_ids: list[int],
        camera_names: dict[int, str],
        from_time: int,
        to_time: int,
    ) -> list[Event]:
        """Every event within [from_time, to_time], newest first."""
        ...

    async def list_presence_and_events(
        self,
        api: SurveillanceAPI,
        camera_ids: list[int],
        camera_names: dict[int, str],
        from_time: int,
        to_time: int,
    ) -> tuple[dict[int, list[tuple[int, int]]], list[Event]]:
        """services.event.list_recording_presence and list_events together."""
        ...

    async def list_type_signatures(
        self, api: SurveillanceAPI, camera_id: int, from_time: int, to_time: int
    ) -> set[TypeSignature]:
        """Every type signature *camera_id* produced within [from_time,
        to_time], for type_history. Over a camera's whole history, so a
        backend should answer it more cheaply than list_events when it
        can."""
        ...

    def classify(self, event: Event, vendor: str) -> tuple[EventKind, ...]:
        """The detected categories *event* carries, possibly none."""
        ...

    def matches(self, event: Event, vendor: str, keys: Iterable[str], match_all: bool) -> bool:
        """True if *event* matches any (*match_all* False) or every
        (*match_all* True) one of the filter *keys*."""
        ...

    def is_filter_key(self, key: str) -> bool:
        """True if *key* is in this backend's own filter-key format, so a
        saved key from another backend can be dropped rather than
        silently matching nothing."""
        ...

    def fixed_filter_options(self) -> list[tuple[str, str, str]] | None:
        """Every (key, filter_label, notes) filter option this backend can
        produce, when it knows them all up front. None when types are
        only known from the events themselves: filter menus are then
        built with filter_options from what list_events and
        list_type_signatures have seen, and only then are type_signature,
        list_type_signatures and type_history used."""
        ...

    def type_signature(self, event: Event) -> TypeSignature:
        """*event*'s type signature, for type_history."""
        ...

    def filter_options(
        self, occurrences: Iterable[tuple[TypeSignature, str]]
    ) -> list[tuple[str, str, str]]:
        """Sorted, deduplicated (key, filter_label, notes) filter options
        for a set of (type signature, vendor) occurrences."""
        ...

    def type_history(self, config: AppConfig) -> dict[int, EventTypeHistory]:
        """This backend's own per-camera type-signature cache in *config*."""
        ...


async def select_event_backend(api: SurveillanceAPI) -> EventBackend:
    """The EventBackend for the NAS *api* is connected to, ready to use.

    Picked by whether the NAS offers the Event Center API at all, not by
    its Surveillance Station version number, unless the Settings page
    forces the legacy one. *api* must be logged in.
    Async so that a backend can ask the NAS what it needs before the
    pages are built.
    """
    backend: EventBackend
    if api.has_api(EVENT_CENTER_API) and not _FORCE_LEGACY_EVENTS:
        backend = await EventCenterBackend.connect(api)
    else:
        backend = LegacyEventBackend()
    log.info("Event backend: %s", backend.name)
    return backend
