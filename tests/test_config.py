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
    AppConfig,
    ConnectionProfile,
    _write_config,
    add_profile,
    load_config,
    remove_profile,
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

    def test_snapshot_dir_default(self) -> None:
        config = AppConfig()
        assert "snapshots" in config.snapshot_dir


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

    def test_remove_profile_updates_default(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = AppConfig(default_profile="nas1")
        config.profiles["nas1"] = ConnectionProfile(name="nas1", host="10.0.0.1")
        config.profiles["nas2"] = ConnectionProfile(name="nas2", host="10.0.0.2")

        remove_profile(config, "nas1")
        assert "nas1" not in config.profiles
        assert config.default_profile == "nas2"
