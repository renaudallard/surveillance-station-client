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

"""Registry of runtime-tunable constants exposed on the Settings page.

Each Setting bridges a plain module-level constant elsewhere in the app
(read fresh on every use there, so reassigning it here takes effect
immediately, no restart needed) to a label/tooltip/default, so the
Settings page can render and reset it generically without knowing
anything about what the constant actually does. Grow the page by adding
a Setting to an existing SettingSection, or a new SettingSection for a
new named subsection.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from surveillance.ui import mpv_widget, timeline

if TYPE_CHECKING:
    from surveillance.config import AppConfig


@dataclass(frozen=True)
class Setting:
    """One user-configurable numeric constant.

    *get*/*set* read and write the actual live value; *default* is a
    snapshot of the constant's own hardcoded value, captured at import
    time before any persisted override is applied: what "Reset to
    default" restores.
    """

    key: str
    label: str
    tooltip: str
    default: float
    get: Callable[[], float]
    set: Callable[[float], None]
    minimum: float = 0.0
    maximum: float = 100.0
    step: float = 0.1


@dataclass(frozen=True)
class BoolSetting:
    """One user-configurable on/off constant; same *get*/*set*/*default*
    shape as Setting, just bool-valued (rendered as a switch, not a
    spinner)."""

    key: str
    label: str
    tooltip: str
    default: bool
    get: Callable[[], bool]
    set: Callable[[bool], None]


@dataclass(frozen=True)
class SettingSection:
    """A named group of settings, e.g. "Media player settings"."""

    title: str
    settings: list[Setting] = field(default_factory=list)
    bool_settings: list[BoolSetting] = field(default_factory=list)


def _player_settings() -> list[Setting]:
    return [
        Setting(
            key="cache_seconds_default",
            label="Default camera cache size (seconds)",
            tooltip=(
                "Demuxer cache target for a plain RTSP camera stream. "
                "Keep small but not zero to absorb normal "
                "scheduling jitter without audible micro-cuts."
            ),
            default=mpv_widget._CACHE_SECONDS_DEFAULT,
            get=lambda: mpv_widget._CACHE_SECONDS_DEFAULT,
            set=mpv_widget.set_cache_seconds_default,
            maximum=10.0,
        ),
        Setting(
            key="cache_seconds_muxed_audio",
            label="Audio-camera cache size (seconds)",
            tooltip=(
                "Demuxer cache target for a live-piped stream muxed with "
                "real audio. Keep small but not zero to absorb normal "
                "scheduling jitter without audible micro-cuts."
            ),
            default=mpv_widget._CACHE_SECONDS_MUXED_AUDIO,
            get=lambda: mpv_widget._CACHE_SECONDS_MUXED_AUDIO,
            set=mpv_widget.set_cache_seconds_muxed_audio,
            maximum=10.0,
        ),
        Setting(
            key="cache_seconds_low_latency",
            label="Silent-camera cache size (seconds)",
            tooltip=(
                "Demuxer cache target for WebSocket pipes used by cameras "
                "without audio track. Will provide least possible latency "
                "when set to 0."
            ),
            default=mpv_widget._CACHE_SECONDS_LOW_LATENCY,
            get=lambda: mpv_widget._CACHE_SECONDS_LOW_LATENCY,
            set=mpv_widget.set_cache_seconds_low_latency,
            maximum=10.0,
        ),
        Setting(
            key="cache_high_speed_max_seconds",
            label="Max high-speed History cache (seconds)",
            tooltip=(
                "Extra cache on top of a streaming profile's own baseline "
                "at History's fastest (100x) playback speed. DSM delivers "
                "frames proportionally faster at high speed, so a bigger "
                "buffer here keeps a fast rewind from outrunning its "
                "cache. Cache growth between 2x and 100x is exponential."
            ),
            default=mpv_widget._CACHE_HIGH_SPEED_MAX_SECONDS,
            get=lambda: mpv_widget._CACHE_HIGH_SPEED_MAX_SECONDS,
            set=mpv_widget.set_high_speed_cache_max_seconds,
            minimum=1.0,
            maximum=60.0,
            step=1.0,
        ),
        Setting(
            key="demuxer_max_bytes_mib",
            label="Demuxer byte cap (MiB)",
            tooltip=(
                "Byte-size cap on the demuxer cache, same for all "
                "three streaming profiles above. Player reads ahead by "
                "whichever of this and the seconds-based cache targets "
                "above is larger, so too small a value here can "
                "bottleneck a cache otherwise sized generously (e.g. the "
                "silent-camera cache at a high History speed)."
            ),
            default=mpv_widget._DEMUXER_MAX_BYTES_MIB,
            get=lambda: mpv_widget._DEMUXER_MAX_BYTES_MIB,
            set=mpv_widget.set_demuxer_max_bytes_mib,
            minimum=1.0,
            maximum=512.0,
            step=1.0,
        ),
    ]


def _player_bool_settings() -> list[BoolSetting]:
    return [
        BoolSetting(
            key="osd_enabled",
            label="Show stream cache details overlaid on video",
            tooltip=(
                "Draws a small live readout (cache depth, target, and "
                "effective playback speed) in the corner of each video: "
                "useful for diagnosing buffering or speed-correction "
                "behavior, not needed for normal use."
            ),
            default=mpv_widget._OSD_ENABLED,
            get=lambda: mpv_widget._OSD_ENABLED,
            set=mpv_widget.set_osd_enabled,
        ),
    ]


def _timeline_settings() -> list[Setting]:
    return [
        Setting(
            key="max_speed_slot_product",
            label="Max limit for (playback speed X number of slots)",
            tooltip=(
                "Ceiling on (playback speed X active grid slots) offered "
                "in the History speed dropdown. DSM delivers that many "
                "times more video per second, on every slot at once, so "
                "too high a product across a full grid can overload the "
                "app. Higher speeds are greyed out on layouts with more "
                "slots to stay under this budget."
            ),
            default=timeline._MAX_SPEED_SLOT_PRODUCT,
            get=lambda: timeline._MAX_SPEED_SLOT_PRODUCT,
            set=timeline.set_max_speed_slot_product,
            minimum=1.0,
            maximum=1000.0,
            step=10.0,
        ),
    ]


SECTIONS: list[SettingSection] = [
    SettingSection(
        title="Media player settings",
        settings=_player_settings(),
        bool_settings=_player_bool_settings(),
    ),
    SettingSection(title="Timeline settings", settings=_timeline_settings()),
]


def apply_persisted_settings(config: AppConfig) -> None:
    """Push every persisted override in *config.setting_overrides* /
    *config.setting_overrides_bool* onto its live constant. Called once
    at startup, before any stream can start, so a saved value is in
    effect from the very first camera played."""
    for section in SECTIONS:
        for setting in section.settings:
            if setting.key in config.setting_overrides:
                setting.set(config.setting_overrides[setting.key])
        for bool_setting in section.bool_settings:
            if bool_setting.key in config.setting_overrides_bool:
                bool_setting.set(config.setting_overrides_bool[bool_setting.key])


def update_setting(config: AppConfig, setting: Setting, value: float) -> None:
    """Apply and persist a new value for one setting."""
    setting.set(value)
    config.setting_overrides[setting.key] = value


def update_bool_setting(config: AppConfig, setting: BoolSetting, value: bool) -> None:
    """Apply and persist a new value for one on/off setting."""
    setting.set(value)
    config.setting_overrides_bool[setting.key] = value


def reset_setting(config: AppConfig, setting: Setting) -> None:
    """Apply one setting's default and drop its override.

    Dropped rather than saved back as an explicit value: writing the
    default in would pin today's number in the config file for good, so
    a later release that retunes the constant would never reach anyone
    who had ever pressed Reset.
    """
    setting.set(setting.default)
    config.setting_overrides.pop(setting.key, None)


def reset_bool_setting(config: AppConfig, setting: BoolSetting) -> None:
    """Apply one on/off setting's default and drop its override, for
    the same reason as reset_setting."""
    setting.set(setting.default)
    config.setting_overrides_bool.pop(setting.key, None)


def reset_all_settings(config: AppConfig) -> None:
    """Reset every registered setting to its default and persist that."""
    for section in SECTIONS:
        for setting in section.settings:
            reset_setting(config, setting)
        for bool_setting in section.bool_settings:
            reset_bool_setting(config, bool_setting)
