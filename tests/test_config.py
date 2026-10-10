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

"""Tests for configuration management."""

from __future__ import annotations

from pathlib import Path

from surveillance.config import (
    MIN_POLL_INTERVAL,
    AppConfig,
    ConnectionProfile,
    EventTypeHistory,
    _config_from_data,
    _write_config,
    add_profile,
    load_config,
)


class TestConnectionProfile:
    def test_base_url_https(self) -> None:
        p = ConnectionProfile(name="test", host="192.168.1.1", port=5001, https=True)
        assert p.base_url == "https://192.168.1.1:5001"

    def test_base_url_http(self) -> None:
        p = ConnectionProfile(name="test", host="192.168.1.1", port=5000, https=False)
        assert p.base_url == "http://192.168.1.1:5000"

    def test_to_dict(self) -> None:
        p = ConnectionProfile(name="test", host="10.0.0.1", port=5001, https=True, verify_ssl=False)
        d = p.to_dict()
        assert d["host"] == "10.0.0.1"
        assert d["port"] == 5001
        assert d["https"] is True
        assert d["verify_ssl"] is False

    def test_from_dict(self) -> None:
        d = {"host": "10.0.0.1", "port": 5001, "https": True, "verify_ssl": False}
        p = ConnectionProfile.from_dict("mynas", d)
        assert p.name == "mynas"
        assert p.host == "10.0.0.1"
        assert p.base_url == "https://10.0.0.1:5001"

    def test_from_dict_defaults(self) -> None:
        p = ConnectionProfile.from_dict("test", {})
        assert p.host == ""
        assert p.port == 5001
        assert p.https is True
        assert p.verify_ssl is False


class TestAppConfig:
    def test_defaults(self) -> None:
        config = AppConfig()
        assert config.default_profile == ""
        assert config.profiles == {}
        assert config.grid_layout == "2x2"
        assert config.poll_interval_cameras == 30
        assert config.timeline_visible is True

    def test_snapshot_dir_default(self) -> None:
        config = AppConfig()
        assert "snapshots" in config.snapshot_dir

    def test_timeline_visible_defaults_true_when_absent(self) -> None:
        # A config file saved before this setting existed has no such key.
        cfg = _config_from_data({})
        assert cfg.timeline_visible is True

    def test_timeline_visible_false_is_loaded(self) -> None:
        cfg = _config_from_data({"general": {"timeline_visible": False}})
        assert cfg.timeline_visible is False


class TestPollIntervals:
    """A hand-edited interval reaches GLib.timeout_add_seconds() directly, so
    anything it cannot use has to be caught while the config is loaded."""

    def test_zero_is_raised_to_the_minimum(self) -> None:
        cfg = _config_from_data({"general": {"poll_interval_cameras": 0}})
        assert cfg.poll_interval_cameras == MIN_POLL_INTERVAL

    def test_negative_is_raised_to_the_minimum(self) -> None:
        cfg = _config_from_data({"general": {"poll_interval_alerts": -5}})
        assert cfg.poll_interval_alerts == MIN_POLL_INTERVAL

    def test_non_numeric_falls_back_to_the_default(self) -> None:
        cfg = _config_from_data({"general": {"poll_interval_homemode": "often"}})
        assert cfg.poll_interval_homemode == 60

    def test_numeric_string_is_accepted(self) -> None:
        cfg = _config_from_data({"general": {"poll_interval_cameras": "45"}})
        assert cfg.poll_interval_cameras == 45

    def test_a_usable_value_is_left_alone(self) -> None:
        cfg = _config_from_data({"general": {"poll_interval_cameras": 120}})
        assert cfg.poll_interval_cameras == 120


def _in_profile(state: dict[str, object]) -> dict[str, object]:
    """Parsed TOML with *state* under the default profile's own table,
    where camera-keyed settings are written."""
    return {
        "general": {"default_profile": "nas"},
        "profiles": {"nas": {"host": "192.168.1.10", **state}},
    }


class TestEventTypeHistory:
    def test_defaults_to_empty(self) -> None:
        cfg = _config_from_data({})
        assert cfg.legacy_event_type_history == {}

    def test_loads_types_and_checked_until(self) -> None:
        cfg = _config_from_data(
            _in_profile(
                {
                    "legacy_event_type_history": {
                        "63": {"types": [[513, 0], [257, 1]], "checked_until": 1700000000}
                    }
                }
            )
        )
        assert cfg.legacy_event_type_history[63] == EventTypeHistory(
            types=[(513, 0), (257, 1)], checked_until=1700000000
        )

    def test_malformed_entry_is_dropped_not_fatal(self) -> None:
        cfg = _config_from_data(
            _in_profile(
                {
                    "legacy_event_type_history": {
                        "not-a-number": {"types": [], "checked_until": 0},
                        "63": {"types": [[513, 0]], "checked_until": 5},
                        "64": ["not", "a", "table"],
                    }
                }
            )
        )
        assert list(cfg.legacy_event_type_history.keys()) == [63]

    def test_round_trips_through_save_and_load(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        config_file = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "CONFIG_FILE", config_file)  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = AppConfig(default_profile="nas", active_profile="nas")
        config.profiles["nas"] = ConnectionProfile("nas", "192.168.1.10")
        config.legacy_event_type_history[63] = EventTypeHistory(
            types=[(513, 0), (257, 1)], checked_until=1700000000
        )
        _write_config(config)
        loaded = load_config()
        assert loaded.legacy_event_type_history[63] == EventTypeHistory(
            types=[(513, 0), (257, 1)], checked_until=1700000000
        )

    def test_reads_the_old_key(self) -> None:
        cfg = _config_from_data(
            _in_profile({"event_type_history": {"63": {"types": [[513, 0]], "checked_until": 5}}})
        )
        assert cfg.legacy_event_type_history[63] == EventTypeHistory(
            types=[(513, 0)], checked_until=5
        )

    def test_reads_the_old_key_from_before_profiles(self) -> None:
        cfg = _config_from_data(
            {
                "general": {"default_profile": "nas"},
                "profiles": {"nas": {"host": "192.168.1.10"}},
                "event_type_history": {"63": {"types": [[513, 0]], "checked_until": 5}},
            }
        )
        assert cfg.legacy_event_type_history[63] == EventTypeHistory(
            types=[(513, 0)], checked_until=5
        )

    def test_reads_the_old_key_from_before_profiles_session(self) -> None:
        cfg = _config_from_data(
            {
                "general": {"default_profile": "nas"},
                "profiles": {"nas": {"host": "192.168.1.10"}},
                "session": {
                    "event_type_history": {"63": {"types": [[513, 0]], "checked_until": 5}}
                },
            }
        )
        assert cfg.legacy_event_type_history[63] == EventTypeHistory(
            types=[(513, 0)], checked_until=5
        )

    def test_new_key_wins_per_camera_over_the_old_one(self) -> None:
        cfg = _config_from_data(
            _in_profile(
                {
                    "event_type_history": {
                        "63": {"types": [[513, 0]], "checked_until": 5},
                        "64": {"types": [[257, 0]], "checked_until": 6},
                    },
                    "legacy_event_type_history": {
                        "63": {"types": [[771, 0]], "checked_until": 9},
                    },
                }
            )
        )
        assert cfg.legacy_event_type_history == {
            63: EventTypeHistory(types=[(771, 0)], checked_until=9),
            64: EventTypeHistory(types=[(257, 0)], checked_until=6),
        }

    def test_saving_writes_only_the_new_key(self, tmp_path: Path, monkeypatch: object) -> None:
        import tomllib

        import surveillance.config as cfg

        config_file = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "CONFIG_FILE", config_file)  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]
        config_file.write_text(
            '[general]\ndefault_profile = "nas"\n'
            '[profiles.nas]\nhost = "192.168.1.10"\n'
            "[profiles.nas.event_type_history.63]\ntypes = [[513, 0]]\nchecked_until = 5\n"
        )

        _write_config(load_config())
        saved = tomllib.loads(config_file.read_text())["profiles"]["nas"]
        assert "event_type_history" not in saved
        assert saved["legacy_event_type_history"]["63"] == {
            "types": [[513, 0]],
            "checked_until": 5,
        }

    def test_saving_a_config_from_before_profiles_writes_only_the_new_key(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        import tomllib

        import surveillance.config as cfg

        config_file = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "CONFIG_FILE", config_file)  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]
        config_file.write_text(
            '[general]\ndefault_profile = "nas"\n'
            '[profiles.nas]\nhost = "192.168.1.10"\n'
            "[event_type_history.63]\ntypes = [[513, 0]]\nchecked_until = 5\n"
        )

        _write_config(load_config())
        saved = tomllib.loads(config_file.read_text())
        assert "event_type_history" not in saved
        assert "event_type_history" not in saved["profiles"]["nas"]
        assert saved["profiles"]["nas"]["legacy_event_type_history"]["63"] == {
            "types": [[513, 0]],
            "checked_until": 5,
        }


class TestSettingOverrides:
    """setting_overrides holds Settings-page overrides keyed by
    Setting.key (see surveillance.settings_registry) — a generic
    str -> float map, unlike every other AppConfig field, so a new
    Setting needs no new AppConfig field or load/save code of its own.
    """

    def test_defaults_to_empty(self) -> None:
        cfg = _config_from_data({})
        assert cfg.setting_overrides == {}

    def test_loads_values(self) -> None:
        cfg = _config_from_data({"setting_overrides": {"cache_seconds_default": 3.3}})
        assert cfg.setting_overrides == {"cache_seconds_default": 3.3}

    def test_malformed_entry_is_dropped_not_fatal(self) -> None:
        cfg = _config_from_data(
            {"setting_overrides": {"cache_seconds_default": "not-a-number", "other_key": 1.5}}
        )
        assert cfg.setting_overrides == {"other_key": 1.5}

    def test_round_trips_through_save_and_load(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        config_file = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "CONFIG_FILE", config_file)  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = AppConfig()
        config.setting_overrides["cache_seconds_default"] = 3.3
        _write_config(config)
        loaded = load_config()
        assert loaded.setting_overrides == {"cache_seconds_default": 3.3}


class TestBoolSettingOverrides:
    """setting_overrides_bool is setting_overrides' bool-valued twin, for
    the Settings page's on/off toggles (BoolSetting.key -> value) —
    separate since TOML (and this dataclass) distinguishes bool from
    float."""

    def test_defaults_to_empty(self) -> None:
        cfg = _config_from_data({})
        assert cfg.setting_overrides_bool == {}

    def test_loads_values(self) -> None:
        cfg = _config_from_data({"setting_overrides_bool": {"osd_enabled": True}})
        assert cfg.setting_overrides_bool == {"osd_enabled": True}

    def test_round_trips_through_save_and_load(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        config_file = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "CONFIG_FILE", config_file)  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = AppConfig()
        config.setting_overrides_bool["osd_enabled"] = True
        _write_config(config)
        loaded = load_config()
        assert loaded.setting_overrides_bool == {"osd_enabled": True}


class TestChoiceSettingOverrides:
    """setting_overrides_choice is the text-valued twin, for the Settings
    page's dropdowns (ChoiceSetting.key -> value)."""

    def test_defaults_to_empty(self) -> None:
        cfg = _config_from_data({})
        assert cfg.setting_overrides_choice == {}

    def test_a_non_text_value_is_dropped(self) -> None:
        cfg = _config_from_data(
            {"setting_overrides_choice": {"live_view_stream_profile": "low", "other": 3}}
        )
        assert cfg.setting_overrides_choice == {"live_view_stream_profile": "low"}

    def test_round_trips_through_save_and_load(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = AppConfig()
        config.setting_overrides_choice["live_view_stream_profile"] = "low"
        _write_config(config)
        assert load_config().setting_overrides_choice == {"live_view_stream_profile": "low"}


class TestEventsSearchEventTypesMigration:
    """events_search_event_types switched from raw int flag values to
    string filter keys (see services.legacy_event_bits) — a config saved before
    that change has int entries, which must be dropped rather than
    crashing or being misinterpreted as filter keys."""

    def test_stale_int_entries_are_dropped(self) -> None:
        cfg = _config_from_data({"session": {"events_search_event_types": [513, 257]}})
        assert cfg.events_search_event_types == []

    def test_string_entries_pass_through(self) -> None:
        cfg = _config_from_data({"session": {"events_search_event_types": ["08", "25:hikvision"]}})
        assert cfg.events_search_event_types == ["08", "25:hikvision"]

    def test_mixed_entries_keep_only_strings(self) -> None:
        cfg = _config_from_data({"session": {"events_search_event_types": ["08", 513]}})
        assert cfg.events_search_event_types == ["08"]

    def test_match_all_defaults_to_false(self) -> None:
        cfg = _config_from_data({})
        assert cfg.events_search_event_types_match_all is False

    def test_match_all_round_trips(self) -> None:
        cfg = _config_from_data({"session": {"events_search_event_types_match_all": True}})
        assert cfg.events_search_event_types_match_all is True


class TestSaveLoadConfig:
    def test_round_trip(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        config_file = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "CONFIG_FILE", config_file)  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = AppConfig(
            default_profile="mynas",
            grid_layout="3x3",
            poll_interval_cameras=15,
            timeline_visible=False,
        )
        profile = ConnectionProfile(
            name="mynas", host="192.168.1.100", port=5001, https=True, verify_ssl=False
        )
        config.profiles["mynas"] = profile

        _write_config(config)
        assert config_file.exists()

        loaded = load_config()
        assert loaded.default_profile == "mynas"
        assert loaded.grid_layout == "3x3"
        assert loaded.poll_interval_cameras == 15
        assert loaded.timeline_visible is False
        assert "mynas" in loaded.profiles
        assert loaded.profiles["mynas"].host == "192.168.1.100"


class TestAddRemoveProfile:
    def test_add_profile_sets_default(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = AppConfig()
        profile = ConnectionProfile(name="nas1", host="10.0.0.1")
        add_profile(config, profile)

        assert config.default_profile == "nas1"
        assert "nas1" in config.profiles


class TestPerProfileCameraSettings:
    """Camera-keyed settings belong to one NAS: every NAS numbers its
    cameras from 1, so shared between profiles one's direct RTSP URL,
    protocol, volume or layout landed on another's camera of the same
    number."""

    @staticmethod
    def _two_profiles() -> AppConfig:
        config = AppConfig(default_profile="home", active_profile="home")
        config.profiles["home"] = ConnectionProfile("home", "192.168.1.10")
        config.profiles["office"] = ConnectionProfile("office", "office.example.com")
        return config

    def test_switching_profile_switches_the_settings(self) -> None:
        config = self._two_profiles()
        config.camera_overrides[1] = "rtsp://home-cam"
        config.layout_cameras["2x2"] = [1, 2, 0, 0]
        config.search_camera_ids = [1]

        config.activate_profile("office")
        assert config.camera_overrides == {}
        assert config.layout_cameras == {}
        assert config.search_camera_ids == []
        config.camera_overrides[1] = "rtsp://office-cam"

        config.activate_profile("home")
        assert config.camera_overrides == {1: "rtsp://home-cam"}
        assert config.layout_cameras == {"2x2": [1, 2, 0, 0]}
        assert config.search_camera_ids == [1]

    def test_every_profile_round_trips(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]
        config = self._two_profiles()
        config.camera_volume[1] = 40
        config.activate_profile("office")
        config.camera_volume[1] = 90
        config.activate_profile("home")
        _write_config(config)

        loaded = load_config()
        assert loaded.active_profile == "home"
        assert loaded.camera_volume == {1: 40}
        loaded.activate_profile("office")
        assert loaded.camera_volume == {1: 90}

    def test_written_under_each_profile_table(self, tmp_path: Path, monkeypatch: object) -> None:
        import tomllib

        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]
        config = self._two_profiles()
        config.camera_protocols[5] = "direct"
        config.camera_live_view_stream_profiles[5] = "low"
        _write_config(config)

        data = tomllib.loads((tmp_path / "config.toml").read_text())
        assert data["profiles"]["home"]["camera_protocols"] == {"5": "direct"}
        assert data["profiles"]["home"]["camera_live_view_stream_profiles"] == {"5": "low"}
        assert load_config().camera_live_view_stream_profiles == {5: "low"}
        assert "camera_protocols" not in data
        assert "camera_protocols" not in data["profiles"]["office"]

    def test_an_older_shared_config_goes_to_the_default_profile(self) -> None:
        cfg = _config_from_data(
            {
                "general": {"default_profile": "home"},
                "session": {"layout_cameras": {"2x2": [1, 2, 0, 0]}, "search_camera_ids": [1]},
                "camera_overrides": {"5": "rtsp://cam"},
                "profiles": {"home": {"host": "a"}, "office": {"host": "b"}},
            }
        )
        assert cfg.active_profile == "home"
        assert cfg.camera_overrides == {5: "rtsp://cam"}
        assert cfg.layout_cameras == {"2x2": [1, 2, 0, 0]}
        assert cfg.search_camera_ids == [1]
        cfg.activate_profile("office")
        assert cfg.camera_overrides == {}


class TestSearchTimesAsText:
    """The README's commented example search time, enabled without its
    quotes, is a TOML datetime rather than text. Passed on as one, every
    browser page raised building itself and login never completed, and
    the next save wrote it back."""

    def test_an_unquoted_datetime_becomes_its_text(self) -> None:
        from datetime import date, datetime

        cfg = _config_from_data(
            {
                "session": {
                    "search_from_time": datetime(2026, 2, 1, 0, 0),
                    "events_search_to_time": date(2026, 2, 19),
                    "snapshots_search_from_time": 5,
                }
            }
        )
        assert cfg.search_from_time == "2026-02-01T00:00:00"
        assert cfg.events_search_to_time == "2026-02-19"
        assert cfg.snapshots_search_from_time == ""

    def test_the_text_form_is_kept(self) -> None:
        cfg = _config_from_data({"session": {"search_to_time": "2026-02-19T23:59:59"}})
        assert cfg.search_to_time == "2026-02-19T23:59:59"


class TestSearchPresetValidation:
    """A hand-edited preset the pages do not know made every load raise,
    and Recordings and Snapshots stayed marked loading for the session."""

    def test_an_unknown_preset_is_dropped(self) -> None:
        from surveillance.config import load_search_filters

        cfg = AppConfig(search_time_preset="last7days")
        assert load_search_filters(cfg, "search")[3] == ""

    def test_a_known_preset_is_kept(self) -> None:
        from surveillance.config import load_search_filters

        cfg = AppConfig(snapshots_search_time_preset="last7d")
        assert load_search_filters(cfg, "snapshots_search")[3] == "last7d"


class TestCameraVolumeRange:
    def test_a_hand_edited_volume_is_held_to_the_slider_range(self) -> None:
        cfg = _config_from_data(
            _in_profile({"camera_volume": {"1": 250, "2": -20, "3": 40, "4": "loud"}})
        )
        assert cfg.camera_volume == {1: 100, 2: 0, 3: 40}
