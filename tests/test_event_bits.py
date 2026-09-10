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

"""Tests for services.event_bits — the event_map bit decoder (no GTK
required, pure functions only). See EVENT_BITMASK.md for the underlying
reverse-engineering and event_bits.json for the actual bit table these
tests exercise."""

from __future__ import annotations

import pytest

from surveillance.services.event_bits import (
    BitVariant,
    build_filter_options,
    decode_flag,
    event_matches_key,
    event_matches_keys,
    filter_key,
    format_filter_label,
    load_bit_table,
    normalize_brand,
)


class TestNormalizeBrand:
    def test_known_brands_case_insensitive(self) -> None:
        assert normalize_brand("HIKVISION") == "hikvision"
        assert normalize_brand("Hikvision") == "hikvision"
        assert normalize_brand("Reolink") == "reolink"
        assert normalize_brand("  reolink  ") == "reolink"

    def test_unknown_brand_falls_back_to_universal(self) -> None:
        assert normalize_brand("D-Link") == "*"
        assert normalize_brand("Vivotek") == "*"
        assert normalize_brand("") == "*"

    def test_brand_added_only_in_json_decodes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # EVENT_BITMASK.md's Contributing section promises that editing
        # event_bits.json is sufficient to add a brand, with no code
        # change. Pin that: a brand existing only in the table must
        # decode, not fall back to an unrecognized-vendor Unknown.
        table = dict(load_bit_table())
        table["25"] = (
            *table.get("25", ()),
            BitVariant(brands=("dahua",), label="Human Detection", confirmed=True, notes=""),
        )
        monkeypatch.setattr("surveillance.services.event_bits.load_bit_table", lambda: table)
        assert normalize_brand("Dahua") == "dahua"
        decoded = decode_flag(1 | (1 << 25), 0, "Dahua")
        assert [d.label for d in decoded] == ["Human Detection"]
        assert decoded[0].key == "25:dahua"
        assert decoded[0].confirmed is True


class TestDecodeFlag:
    def test_universal_only_flag(self) -> None:
        # 513 = bits {0, 9} = Audio detected, brand-independent
        decoded = decode_flag(513, 0, "hikvision")
        assert [d.label for d in decoded] == ["Audio detected"]
        assert decoded[0].brand is None
        assert decoded[0].key == "09"

    def test_modifier_bits_never_appear(self) -> None:
        # 259 = bits {0, 1, 8} — bits 0/1 must be excluded, only bit 8 shown
        decoded = decode_flag(259, 0, "hikvision")
        assert [d.bit for d in decoded] == [8]

    def test_brand_collision_bit_27(self) -> None:
        # 134217731 = bits {0, 1, 27}
        reolink = decode_flag(134217731, 0, "reolink")
        hikvision = decode_flag(134217731, 0, "hikvision")
        assert [d.label for d in reolink] == ["Pet Detect"]
        assert [d.label for d in hikvision] == ["Intrusion Detection"]
        assert reolink[0].key == "27:reolink"
        assert hikvision[0].key == "27:hikvision"

    def test_unrecognized_brand_on_a_brand_only_bit_is_unknown(self) -> None:
        # Bit 27 has no "*" fallback variant — an unrecognized brand must
        # not silently inherit either brand's meaning.
        decoded = decode_flag(134217731, 0, "*")
        assert [d.label for d in decoded] == ["Unknown"]
        assert decoded[0].brand is None
        assert decoded[0].confirmed is False

    def test_unmapped_bit_produces_synthetic_unknown(self) -> None:
        # Bit 13 has no entry in event_bits.json at all — a known brand
        # still carries its own key (see test_known_brand_unmapped_bit_
        # carries_brand below for why).
        decoded = decode_flag(1 << 13, 0, "hikvision")
        assert len(decoded) == 1
        assert decoded[0].label == "Unknown"
        assert decoded[0].key == "13:hikvision"

    def test_known_brand_unmapped_bit_carries_brand(self) -> None:
        # Bit 28 only has a Hikvision variant — a Reolink camera firing it
        # is a known brand hitting a bit with no variant for it (and no "*"
        # fallback), distinct from an unrecognized-vendor camera (brand
        # "*"). Must not collapse to the same brand-neutral "Unknown" key/
        # label as a "*" decode — see event_matches_key leak this pins in
        # TestBuildFilterOptions below.
        decoded = decode_flag(1 << 28, 0, "reolink")
        assert [d.label for d in decoded] == ["Unknown"]
        assert decoded[0].brand == "reolink"
        assert decoded[0].key == "28:reolink"

    def test_unrecognized_vendor_unmapped_bit_carries_raw_vendor_name(self) -> None:
        # Bit 15 has no entry in event_bits.json at all, decoded for a
        # vendor DSM reports that isn't Hikvision or Reolink — must not
        # collapse to a bare, unidentifiable "Unknown" (the pre-fix
        # behavior for every untested vendor at once); the camera's own
        # vendor name is the only way to tell this source apart from
        # another untested vendor's Unknown on the same bit.
        decoded = decode_flag(1 << 15, 0, "Vivotek")
        assert [d.label for d in decoded] == ["Unknown"]
        assert decoded[0].brand == "vivotek"
        assert decoded[0].key == "15:vivotek"

    def test_unrecognized_vendor_name_is_normalized_for_grouping(self) -> None:
        # Case/whitespace variations of the same vendor string (as DSM
        # might report it differently across models) must still collapse
        # to one filter option, same as normalize_brand() does for the
        # known brands.
        decoded = decode_flag(1 << 15, 0, "  VIVOTEK  ")
        assert decoded[0].key == "15:vivotek"

    def test_reserved_field_decodes_as_pseudo_bit(self) -> None:
        decoded = decode_flag(1, 1, "hikvision")
        assert [d.label for d in decoded] == ["Object Removal Detection"]
        assert decoded[0].bit is None
        # Brand-scoped (Hikvision-only in the current data), so the key
        # carries the brand suffix just like any other brand-scoped bit.
        assert decoded[0].key == "R0:hikvision"

    def test_reserved_field_zero_produces_nothing(self) -> None:
        decoded = decode_flag(1, 0, "hikvision")
        assert decoded == ()

    def test_negative_flag_recovers_sign_bit(self) -> None:
        # -2147483647 -> unsigned bits {0, 31} -> Unattended Baggage Detection
        decoded = decode_flag(-2147483647, 0, "hikvision")
        assert [d.label for d in decoded] == ["Unattended Baggage Detection"]


class TestFilterKeyAndLabel:
    def test_universal_key_and_label(self) -> None:
        assert filter_key(8, None) == "08"
        assert format_filter_label(8, "Motion detected", None) == "08 - Motion detected"

    def test_brand_scoped_key_and_label(self) -> None:
        assert filter_key(25, "hikvision") == "25:hikvision"
        assert (
            format_filter_label(25, "Scene Change Detection", "hikvision")
            == "25 - Scene Change Detection (Hikvision)"
        )

    def test_reserved_key_and_label(self) -> None:
        assert filter_key(None, "hikvision") == "R0:hikvision"
        assert (
            format_filter_label(None, "Object Removal Detection", "hikvision")
            == "R0 - Object Removal Detection (Hikvision)"
        )

    def test_universal_brand_marker_produces_no_suffix(self) -> None:
        assert filter_key(8, "*") == "08"
        assert format_filter_label(8, "Motion detected", "*") == "08 - Motion detected"


class TestEventMatchesKey:
    def test_universal_key_matches_regardless_of_brand(self) -> None:
        assert event_matches_key(513, 0, "hikvision", "09")
        assert event_matches_key(513, 0, "reolink", "09")

    def test_brand_scoped_key_requires_matching_brand(self) -> None:
        key = filter_key(27, "hikvision")
        assert event_matches_key(134217731, 0, "hikvision", key)
        assert not event_matches_key(134217731, 0, "reolink", key)

    def test_reserved_key(self) -> None:
        key = filter_key(None, "hikvision")
        assert event_matches_key(1, 1, "hikvision", key)
        assert not event_matches_key(1, 0, "hikvision", key)

    def test_bit_not_set_does_not_match(self) -> None:
        assert not event_matches_key(513, 0, "hikvision", "08")

    def test_unrecognized_vendor_unknown_key_does_not_leak_into_known_brand(self) -> None:
        # An unrecognized-vendor ("*") camera's "27 - Unknown" filter option
        # must not also match a Reolink camera's real, confirmed "Pet
        # Detect" event just because both happen to set bit 27 — they're
        # different categories with different keys ("27" vs "27:reolink").
        assert event_matches_key(134217731, 0, "*", "27")
        assert not event_matches_key(134217731, 0, "reolink", "27")


class TestEventMatchesKeys:
    # 771 = bits {0, 1, 8, 9} = Motion detected + Audio detected
    def test_any_matches_with_one_hit(self) -> None:
        assert event_matches_keys(771, 0, "hikvision", ["08", "10"], match_all=False)

    def test_any_fails_with_no_hits(self) -> None:
        assert not event_matches_keys(771, 0, "hikvision", ["10", "12"], match_all=False)

    def test_all_requires_every_key(self) -> None:
        assert event_matches_keys(771, 0, "hikvision", ["08", "09"], match_all=True)
        assert not event_matches_keys(771, 0, "hikvision", ["08", "10"], match_all=True)

    def test_all_respects_brand_scoping(self) -> None:
        # 234881283 = bits {0,1,8,25,26,27} = Person + Vehicle + Pet (Reolink) + Motion
        flag = 234881283
        reolink_keys = [filter_key(25, "reolink"), filter_key(27, "reolink")]
        hikvision_keys = [filter_key(25, "hikvision"), filter_key(27, "hikvision")]
        # A Reolink event's bits satisfy Reolink-scoped keys...
        assert event_matches_keys(flag, 0, "reolink", reolink_keys, match_all=True)
        # ...but not Hikvision-scoped keys, even though the same bits are
        # numerically set — the camera's actual brand doesn't match.
        assert not event_matches_keys(flag, 0, "reolink", hikvision_keys, match_all=True)

    def test_empty_keys_any_is_false(self) -> None:
        assert not event_matches_keys(771, 0, "hikvision", [], match_all=False)

    def test_empty_keys_all_is_true(self) -> None:
        # all() of an empty iterable is vacuously True — matches Python's
        # own semantics, and an empty key list should never reach this
        # function in practice (callers only filter when keys is non-empty).
        assert event_matches_keys(771, 0, "hikvision", [], match_all=True)


class TestBuildFilterOptions:
    def test_dedup_and_sort(self) -> None:
        occurrences = [
            (134217731, 0, "reolink"),
            (134217731, 0, "hikvision"),
            (134217731, 0, "reolink"),  # duplicate, must collapse
            (513, 0, "hikvision"),
        ]
        options = build_filter_options(occurrences)
        keys = [key for key, _label, _notes in options]
        assert keys == sorted(keys)
        assert keys == ["09", "27:hikvision", "27:reolink"]

    def test_empty_occurrences_produce_no_options(self) -> None:
        assert build_filter_options([]) == []

    def test_unmapped_bit_unknown_option_is_brand_scoped(self) -> None:
        # Regression test for a brand leak: an unrecognized-vendor camera's
        # ("*") Unknown bit 28 and a known-brand (Reolink) camera's Unknown
        # bit 28 (no variant defined for Reolink) must produce two distinct
        # filter options, and neither's key may match the other brand's
        # events — see _decode_bit's brand handling for the "no variant"
        # case.
        occurrences = [(1 << 28, 0, "*"), (1 << 28, 0, "reolink")]
        options = build_filter_options(occurrences)
        keys = [key for key, _label, _notes in options]
        assert keys == ["28", "28:reolink"]
        labels = dict((key, label) for key, label, _notes in options)
        assert labels["28"] == "28 - Unknown"
        assert labels["28:reolink"] == "28 - Unknown (Reolink)"

        assert event_matches_key(1 << 28, 0, "*", "28")
        assert event_matches_key(1 << 28, 0, "reolink", "28:reolink")
        assert not event_matches_key(1 << 28, 0, "reolink", "28")

    def test_unmapped_bit_gets_one_option_per_unrecognized_vendor(self) -> None:
        # Two different untested vendors hitting the same unmapped bit must
        # not collapse into one indistinguishable "Unknown" — each vendor's
        # own name identifies its source, one filter option per vendor.
        occurrences = [
            (1 << 15, 0, "Vivotek"),
            (1 << 15, 0, "D-Link"),
            (1 << 15, 0, "Vivotek"),  # duplicate, must collapse
        ]
        options = build_filter_options(occurrences)
        keys = [key for key, _label, _notes in options]
        assert keys == ["15:d-link", "15:vivotek"]
        labels = dict((key, label) for key, label, _notes in options)
        assert labels["15:vivotek"] == "15 - Unknown (Vivotek)"
        assert labels["15:d-link"] == "15 - Unknown (D-Link)"

    def test_confirmed_universal_bit_stays_unsuffixed_for_any_vendor(self) -> None:
        # A bit registered with "*" as its brand in event_bits.json (a
        # confirmed, brand-independent category) omits the brand suffix
        # regardless of which vendor's camera produced it — only an
        # *unmapped* bit gets brand-scoped.
        occurrences = [(513, 0, "Vivotek"), (513, 0, "hikvision")]
        options = build_filter_options(occurrences)
        assert [key for key, _label, _notes in options] == ["09"]


class TestDecodeCache:
    def test_the_same_flag_decodes_once(self) -> None:
        """The Events page and the timeline decode one flag per event per
        filter key; a NAS produces a handful of distinct flags, so the
        decoder hands back the same tuple rather than working again."""
        first = decode_flag(259, 0, "hikvision")
        assert isinstance(first, tuple)
        assert decode_flag(259, 0, "hikvision") is first
        assert decode_flag(259, 0, "reolink") is not first
