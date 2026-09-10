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

"""Decode RecordingPicker::EnumInterval's event_map bitmask into labels.

The bit -> label table lives in data/event_bits.json, transcribed from
EVENT_BITMASK.md (the reverse-engineering writeup) and kept as the single
source of truth for both. See that doc for how each bit was confirmed and
its "Contributing" section for how to add new brands/bits.

Bit meanings are brand-dependent for the advanced/AI class bits (confirmed
collisions on bits 25 and 27 — DSM assigns those bit positions per camera
vendor, not globally), so every decode here takes a camera's raw DSM
`vendor` string alongside the raw flag and normalizes it internally
(normalize_brand()) — kept raw rather than pre-normalized by the caller so
an unrecognized vendor's Unknown bits can still be identified by their own
name instead of collapsing into one shared bucket (see _decode_bit).
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources

log = logging.getLogger(__name__)

# Bits 0 (always set) and 1 (unresolved, doesn't reliably track anything —
# see EVENT_BITMASK.md) qualify other bits rather than representing a
# detected category of their own, so they're excluded from decoding output
# and must never surface as a filter-menu entry.
_MODIFIER_BITS = {0, 1}
_MAX_BIT = 31
_UNSIGNED_MASK = 0xFFFFFFFF

# The reserved RLE field is only ever a single 0/1 flag so far (see
# EVENT_BITMASK.md's Object Removal Detection / Temperature Measurement
# discussion) — modeled as a pseudo-bit with bit=None and this JSON key.
_RESERVED_KEY = "reserved:0"
_RESERVED_LABEL = "R0"


@dataclass(frozen=True)
class BitVariant:
    """One brand's meaning for a bit, as loaded from event_bits.json."""

    brands: tuple[str, ...]
    label: str
    confirmed: bool
    notes: str


@dataclass(frozen=True)
class DecodedBit:
    """A single decoded bit (or the reserved pseudo-bit) from a flag."""

    key: str
    bit: int | None
    label: str
    brand: str | None
    confirmed: bool
    notes: str


@functools.lru_cache(maxsize=1)
def load_bit_table() -> dict[str, tuple[BitVariant, ...]]:
    """Load and cache event_bits.json's bit -> variant-list table."""
    raw = resources.files("surveillance").joinpath("data", "event_bits.json").read_text("utf-8")
    data = json.loads(raw)
    table: dict[str, tuple[BitVariant, ...]] = {}
    for key, variants in data.get("bits", {}).items():
        table[key] = tuple(
            BitVariant(
                brands=tuple(v.get("brands", [])),
                label=v.get("label", ""),
                confirmed=bool(v.get("confirmed", False)),
                notes=v.get("notes", ""),
            )
            for v in variants
        )
    return table


def _table_brands() -> set[str]:
    """Brand keys the bit table maps, derived from the loaded table so a
    JSON-only brand addition decodes without any code change (the promise
    EVENT_BITMASK.md's Contributing section makes)."""
    return {
        brand
        for variants in load_bit_table().values()
        for variant in variants
        for brand in variant.brands
        if brand != "*"
    }


def normalize_brand(vendor: str) -> str:
    """Map a camera's raw DSM `vendor` string to a bit-table brand key.

    Falls back to "*" (universal-only decoding) for any brand the bit
    table doesn't map: untested/unrecognized vendors only ever get the
    shared, brand-independent bits.
    """
    candidate = vendor.strip().lower()
    return candidate if candidate in _table_brands() else "*"


def _lookup_variant(bit_key: str, brand: str) -> BitVariant | None:
    variants = load_bit_table().get(bit_key, ())
    for variant in variants:
        if brand in variant.brands:
            return variant
    for variant in variants:
        if "*" in variant.brands:
            return variant
    return None


def filter_key(bit: int | None, brand: str | None) -> str:
    """Canonical filter key for a bit (or the reserved pseudo-bit) and an
    optional brand — e.g. "08", "25:hikvision", "R0"."""
    base = f"{bit:02d}" if bit is not None else _RESERVED_LABEL
    if brand and brand != "*":
        return f"{base}:{brand}"
    return base


def format_filter_label(bit: int | None, label: str, brand: str | None) -> str:
    """Locked display format: "NN - Label" or "NN - Label (Brand)"."""
    base = f"{bit:02d}" if bit is not None else _RESERVED_LABEL
    text = f"{base} - {label}"
    if brand and brand != "*":
        return f"{text} ({brand.title()})"
    return text


def _decode_bit(bit: int | None, bit_key: str, brand: str, vendor: str) -> DecodedBit:
    variant = _lookup_variant(bit_key, brand)
    if variant is None:
        # A known brand with no variant for this bit still needs its own
        # key/label, or its "Unknown" collides with every other brand's
        # (and the universal) "Unknown" for the same bit. An unrecognized
        # vendor is identified by its own raw name instead of collapsing
        # into one shared bucket — helps users/contributors tell "my
        # D-Link cam did X" from "my Vivotek cam did Y" rather than both
        # showing up as a bare "Unknown". filter_key/format_filter_label
        # already treat a falsy or "*" brand as no-suffix, so an empty or
        # literal "*" vendor string still degrades to brand-neutral.
        unknown_brand = brand if brand != "*" else vendor.strip().lower()
        display_brand = unknown_brand if unknown_brand and unknown_brand != "*" else None
        return DecodedBit(
            key=filter_key(bit, unknown_brand),
            bit=bit,
            label="Unknown",
            brand=display_brand,
            confirmed=False,
            notes="",
        )
    # A "*" variant applies regardless of brand, so its filter key/label
    # stay brand-neutral even when decoding for a specific camera.
    variant_brand = None if "*" in variant.brands else brand
    return DecodedBit(
        key=filter_key(bit, variant_brand),
        bit=bit,
        label=variant.label,
        brand=variant_brand,
        confirmed=variant.confirmed,
        notes=variant.notes,
    )


@functools.lru_cache(maxsize=1024)
def decode_flag(flag: int, reserved: int, vendor: str) -> tuple[DecodedBit, ...]:
    """Decode a raw event_map flag (+ its RLE reserved field) into the set
    of DecodedBit entries it represents, for the given camera's raw DSM
    `vendor` string (normalized internally for variant matching, but kept
    raw for identifying an unrecognized vendor's Unknown bits — see
    _decode_bit).

    Modifier bits 0 and 1 are always excluded — see _MODIFIER_BITS.

    Memoised: a NAS produces a handful of distinct flags, while the
    Events page and the timeline decode one per event per filter key,
    which at well over a hundred microseconds a decode came to seconds
    on the GTK thread for a large result. The result is a tuple, so
    handing the same one to every caller is safe.
    """
    brand = normalize_brand(vendor)
    unsigned = flag & _UNSIGNED_MASK
    decoded: list[DecodedBit] = []
    for bit in range(2, _MAX_BIT + 1):
        if not (unsigned >> bit) & 1:
            continue
        decoded.append(_decode_bit(bit, str(bit), brand, vendor))
    if reserved:
        decoded.append(_decode_bit(None, _RESERVED_KEY, brand, vendor))
    return tuple(decoded)


def event_matches_key(flag: int, reserved: int, vendor: str, key: str) -> bool:
    """True if *key* (as produced by filter_key/build_filter_options)
    matches this event's decoded bits and (for brand-scoped keys) brand.

    Delegates to decode_flag() rather than re-deriving bit/brand matching
    separately — a raw bit-test alone isn't enough: the same bit can be a
    confirmed brand-neutral category for one brand and an unmapped
    "Unknown" for another, which decode_flag's variant lookup already
    knows how to tell apart (see _decode_bit)."""
    return any(d.key == key for d in decode_flag(flag, reserved, vendor))


def event_matches_keys(
    flag: int, reserved: int, vendor: str, keys: Iterable[str], match_all: bool
) -> bool:
    """True if this event matches the given filter keys — *match_all=False*
    (the default/"Any") for any one key, *match_all=True* ("All") only if
    every key matches. Purely a local combination of event_matches_key();
    DSM has no concept of these decoded categories to filter on server-side."""
    tests = (event_matches_key(flag, reserved, vendor, key) for key in keys)
    return all(tests) if match_all else any(tests)


def build_filter_options(
    occurrences: Iterable[tuple[int, int, str]],
) -> list[tuple[str, str, str]]:
    """Sorted, deduplicated (key, display_label, tooltip_notes) filter
    options decoded from a set of (flag, reserved, vendor) occurrences —
    the single source both the quick combo and the advanced-search
    checklist build their options from, so they never drift apart."""
    options: dict[str, tuple[str, str]] = {}
    for flag, reserved, vendor in occurrences:
        for decoded in decode_flag(flag, reserved, vendor):
            options[decoded.key] = (
                format_filter_label(decoded.bit, decoded.label, decoded.brand),
                decoded.notes,
            )
    return sorted((key, label, notes) for key, (label, notes) in options.items())
