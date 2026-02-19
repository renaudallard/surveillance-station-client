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

"""XDG-compliant TOML configuration management."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

import tomli_w


def _config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "surveillance-station"


def _data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "surveillance-station"


CONFIG_DIR = _config_dir()
DATA_DIR = _data_dir()
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class ConnectionProfile:
    """A Synology NAS connection profile."""

    name: str
    host: str
    port: int = 5001
    https: bool = True
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "https": self.https,
            "verify_ssl": self.verify_ssl,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> ConnectionProfile:
        return cls(
            name=name,
            host=data.get("host", ""),
            port=data.get("port", 5001),
            https=data.get("https", True),
            verify_ssl=data.get("verify_ssl", False),
        )


@dataclass
class AppConfig:
    """Application configuration."""

    default_profile: str = ""
    profiles: dict[str, ConnectionProfile] = field(default_factory=dict)
    theme: str = "auto"  # "auto", "dark", "light"
    grid_layout: str = "2x2"
    last_page: str = "live"
    layout_cameras: dict[str, list[int]] = field(default_factory=dict)
    poll_interval_cameras: int = 30
    poll_interval_alerts: int = 30
    poll_interval_homemode: int = 60
    snapshot_dir: str = ""
    camera_overrides: dict[int, str] = field(default_factory=dict)
    camera_protocols: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.snapshot_dir:
            self.snapshot_dir = str(DATA_DIR / "snapshots")


def _load_theme(general: dict[str, Any]) -> str:
    """Read theme setting with backward compat for old dark_theme bool."""
    theme = general.get("theme")
    if isinstance(theme, str) and theme in ("auto", "dark", "light"):
        return theme
    # Migrate old dark_theme boolean
    dark = general.get("dark_theme")
    if isinstance(dark, bool):
        return "dark" if dark else "light"
    return "auto"


def load_config() -> AppConfig:
    """Load configuration from TOML file."""
    if not CONFIG_FILE.exists():
        return AppConfig()

    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)

    profiles: dict[str, ConnectionProfile] = {}
    for name, pdata in data.get("profiles", {}).items():
        profiles[name] = ConnectionProfile.from_dict(name, pdata)

    general = data.get("general", {})
    session = data.get("session", {})

    # camera_overrides: maps camera ID (int) -> direct RTSP URL
    overrides: dict[int, str] = {}
    for cam_id_str, url in data.get("camera_overrides", {}).items():
        try:
            overrides[int(cam_id_str)] = str(url)
        except (ValueError, TypeError):
            pass

    # camera_protocols: maps camera ID (int) -> protocol name
    protocols: dict[int, str] = {}
    for cam_id_str, proto in data.get("camera_protocols", {}).items():
        try:
            protocols[int(cam_id_str)] = str(proto)
        except (ValueError, TypeError):
            pass

    return AppConfig(
        default_profile=general.get("default_profile", ""),
        profiles=profiles,
        theme=_load_theme(general),
        grid_layout=session.get("grid_layout", general.get("grid_layout", "2x2")),
        last_page=session.get("last_page", "live"),
        layout_cameras=session.get("layout_cameras", {}),
        poll_interval_cameras=general.get("poll_interval_cameras", 30),
        poll_interval_alerts=general.get("poll_interval_alerts", 30),
        poll_interval_homemode=general.get("poll_interval_homemode", 60),
        snapshot_dir=general.get("snapshot_dir", str(DATA_DIR / "snapshots")),
        camera_overrides=overrides,
        camera_protocols=protocols,
    )


_save_pending: int = 0


def save_config(config: AppConfig) -> None:
    """Schedule a debounced config save (writes at most once per second)."""
    global _save_pending

    if _save_pending:
        return  # already scheduled

    from gi.repository import GLib  # type: ignore[import-untyped]

    def _do_save() -> bool:
        global _save_pending
        _save_pending = 0
        _write_config(config)
        return False  # one-shot

    _save_pending = GLib.timeout_add(1000, _do_save)


def save_config_now(config: AppConfig) -> None:
    """Write config immediately (for use at shutdown)."""
    global _save_pending

    if _save_pending:
        from gi.repository import GLib  # type: ignore[import-untyped]

        GLib.source_remove(_save_pending)
        _save_pending = 0

    _write_config(config)


def _write_config(config: AppConfig) -> None:
    """Write configuration to TOML file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {
        "general": {
            "default_profile": config.default_profile,
            "poll_interval_cameras": config.poll_interval_cameras,
            "poll_interval_alerts": config.poll_interval_alerts,
            "poll_interval_homemode": config.poll_interval_homemode,
            "theme": config.theme,
            "snapshot_dir": config.snapshot_dir,
        },
        "session": {
            "grid_layout": config.grid_layout,
            "last_page": config.last_page,
            "layout_cameras": config.layout_cameras,
        },
        "camera_overrides": {str(cam_id): url for cam_id, url in config.camera_overrides.items()},
        "camera_protocols": {
            str(cam_id): proto for cam_id, proto in config.camera_protocols.items()
        },
        "profiles": {},
    }

    for name, profile in config.profiles.items():
        data["profiles"][name] = profile.to_dict()

    with open(CONFIG_FILE, "wb") as f:
        tomli_w.dump(data, f)


def add_profile(config: AppConfig, profile: ConnectionProfile) -> None:
    """Add or update a connection profile."""
    config.profiles[profile.name] = profile
    if not config.default_profile:
        config.default_profile = profile.name
    save_config_now(config)


def remove_profile(config: AppConfig, name: str) -> None:
    """Remove a connection profile."""
    config.profiles.pop(name, None)
    if config.default_profile == name:
        config.default_profile = next(iter(config.profiles), "")
    save_config_now(config)
