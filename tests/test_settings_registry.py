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

"""Tests for the Settings page's registry (no GTK required): applying
persisted overrides, updating/resetting one or all settings, and that
each Setting's get/set actually round-trips through its live constant.
"""

from __future__ import annotations

from surveillance.config import AppConfig
from surveillance.settings_registry import (
    SECTIONS,
    BoolSetting,
    Setting,
    apply_persisted_settings,
    reset_all_settings,
    reset_bool_setting,
    reset_setting,
    update_bool_setting,
    update_setting,
)


def _find(key: str) -> Setting:
    for section in SECTIONS:
        for setting in section.settings:
            if setting.key == key:
                return setting
    raise AssertionError(f"no such setting: {key}")


def _find_bool(key: str) -> BoolSetting:
    for section in SECTIONS:
        for setting in section.bool_settings:
            if setting.key == key:
                return setting
    raise AssertionError(f"no such bool setting: {key}")


class TestRegistryShape:
    def test_media_player_section_has_the_expected_keys(self) -> None:
        section = next(s for s in SECTIONS if s.title == "Media player settings")
        assert {setting.key for setting in section.settings} == {
            "cache_seconds_default",
            "cache_seconds_muxed_audio",
            "cache_seconds_low_latency",
            "cache_high_speed_max_seconds",
            "demuxer_max_bytes_mib",
        }

    def test_timeline_section_has_the_expected_keys(self) -> None:
        section = next(s for s in SECTIONS if s.title == "Timeline settings")
        assert {setting.key for setting in section.settings} == {"max_speed_slot_product"}

    def test_media_player_section_has_the_expected_bool_keys(self) -> None:
        section = next(s for s in SECTIONS if s.title == "Media player settings")
        assert {setting.key for setting in section.bool_settings} == {"osd_enabled"}

    def test_every_setting_has_a_tooltip(self) -> None:
        for section in SECTIONS:
            for setting in section.settings:
                assert setting.tooltip.strip()
            for bool_setting in section.bool_settings:
                assert bool_setting.tooltip.strip()


class TestGetSetRoundTrip:
    def test_set_then_get_reflects_the_new_value(self) -> None:
        setting = _find("cache_seconds_default")
        original = setting.get()
        try:
            setting.set(4.2)
            assert setting.get() == 4.2
        finally:
            setting.set(original)

    def test_bool_set_then_get_reflects_the_new_value(self) -> None:
        setting = _find_bool("osd_enabled")
        original = setting.get()
        try:
            setting.set(not original)
            assert setting.get() is (not original)
        finally:
            setting.set(original)

    def test_high_speed_setting_recomputes_its_derived_rate(self) -> None:
        """cache_high_speed_max_seconds isn't read fresh everywhere the
        way the other three are: _CACHE_HIGH_SPEED_GROWTH is derived
        from it once, so set() must recompute that too (see
        mpv_widget.set_high_speed_cache_max_seconds)."""
        from surveillance.ui import mpv_widget

        setting = _find("cache_high_speed_max_seconds")
        original = setting.get()
        try:
            setting.set(20.0)
            assert mpv_widget._high_speed_cache_seconds(100.0) > 15.0
        finally:
            setting.set(original)


class TestUpdateAndReset:
    def test_update_setting_applies_and_persists(self) -> None:
        setting = _find("cache_seconds_default")
        config = AppConfig()
        try:
            update_setting(config, setting, 3.3)
            assert setting.get() == 3.3
            assert config.setting_overrides["cache_seconds_default"] == 3.3
        finally:
            setting.set(setting.default)

    def test_reset_setting_drops_the_override(self) -> None:
        """Reset applies the default and removes the key, rather than
        saving the default back as an explicit override."""
        setting = _find("cache_seconds_default")
        config = AppConfig()
        try:
            update_setting(config, setting, 3.3)
            reset_setting(config, setting)
            assert setting.get() == setting.default
            assert "cache_seconds_default" not in config.setting_overrides
        finally:
            setting.set(setting.default)

    def test_update_bool_setting_applies_and_persists(self) -> None:
        setting = _find_bool("osd_enabled")
        config = AppConfig()
        try:
            update_bool_setting(config, setting, not setting.default)
            assert setting.get() is (not setting.default)
            assert config.setting_overrides_bool["osd_enabled"] is (not setting.default)
        finally:
            setting.set(setting.default)

    def test_reset_bool_setting_drops_the_override(self) -> None:
        """Same as test_reset_setting_drops_the_override, for a
        switch."""
        setting = _find_bool("osd_enabled")
        config = AppConfig()
        try:
            update_bool_setting(config, setting, not setting.default)
            reset_bool_setting(config, setting)
            assert setting.get() is setting.default
            assert "osd_enabled" not in config.setting_overrides_bool
        finally:
            setting.set(setting.default)

    def test_reset_all_settings_drops_every_override(self) -> None:
        config = AppConfig()
        originals: dict[str, float] = {}
        bool_originals: dict[str, bool] = {}
        try:
            for section in SECTIONS:
                for setting in section.settings:
                    originals[setting.key] = setting.get()
                    update_setting(config, setting, setting.default + 1.0)
                for bool_setting in section.bool_settings:
                    bool_originals[bool_setting.key] = bool_setting.get()
                    update_bool_setting(config, bool_setting, not bool_setting.default)
            reset_all_settings(config)
            for section in SECTIONS:
                for setting in section.settings:
                    assert setting.get() == setting.default
                    assert setting.key not in config.setting_overrides
                for bool_setting in section.bool_settings:
                    assert bool_setting.get() is bool_setting.default
                    assert bool_setting.key not in config.setting_overrides_bool
        finally:
            for section in SECTIONS:
                for setting in section.settings:
                    setting.set(originals[setting.key])
                for bool_setting in section.bool_settings:
                    bool_setting.set(bool_originals[bool_setting.key])


class TestApplyPersistedSettings:
    def test_applies_a_saved_override(self) -> None:
        setting = _find("cache_seconds_default")
        original = setting.get()
        config = AppConfig()
        config.setting_overrides["cache_seconds_default"] = 3.3
        try:
            apply_persisted_settings(config)
            assert setting.get() == 3.3
        finally:
            setting.set(original)

    def test_leaves_settings_with_no_saved_override_untouched(self) -> None:
        setting = _find("cache_seconds_muxed_audio")
        original = setting.get()
        config = AppConfig()
        try:
            apply_persisted_settings(config)
            assert setting.get() == original
        finally:
            setting.set(original)

    def test_applies_a_saved_bool_override(self) -> None:
        setting = _find_bool("osd_enabled")
        original = setting.get()
        config = AppConfig()
        config.setting_overrides_bool["osd_enabled"] = not original
        try:
            apply_persisted_settings(config)
            assert setting.get() is (not original)
        finally:
            setting.set(original)

    def test_leaves_bool_settings_with_no_saved_override_untouched(self) -> None:
        setting = _find_bool("osd_enabled")
        original = setting.get()
        config = AppConfig()
        try:
            apply_persisted_settings(config)
            assert setting.get() is original
        finally:
            setting.set(original)
