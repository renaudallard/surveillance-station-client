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

import contextlib
import logging
import os
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import tomli_w

log = logging.getLogger(__name__)


def _xdg_dir(variable: str, default: Path) -> Path:
    """This app's directory under the XDG base directory *variable*
    names, or under *default* where it is unset."""
    xdg = os.environ.get(variable, "")
    return (Path(xdg) if xdg else default) / "surveillance-station"


CONFIG_DIR = _xdg_dir("XDG_CONFIG_HOME", Path.home() / ".config")
# Actual user content, e.g. snapshots.
DATA_DIR = _xdg_dir("XDG_DATA_HOME", Path.home() / ".local" / "share")
# Logs specifically. XDG_STATE_HOME is where the spec puts state that
# should persist but isn't precious enough for XDG_DATA_HOME.
STATE_DIR = _xdg_dir("XDG_STATE_HOME", Path.home() / ".local" / "state")
# Files only needed while the app runs, e.g. the image a snapshot viewer
# plays.
CACHE_DIR = _xdg_dir("XDG_CACHE_HOME", Path.home() / ".cache")
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class ConnectionProfile:
    """A Synology NAS connection profile."""

    name: str
    host: str
    port: int = 5001
    https: bool = True
    verify_ssl: bool = False
    device_id: str = ""

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "https": self.https,
            "verify_ssl": self.verify_ssl,
        }
        if self.device_id:
            d["device_id"] = self.device_id
        return d

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> ConnectionProfile:
        return cls(
            name=name,
            host=data.get("host", ""),
            port=data.get("port", 5001),
            https=data.get("https", True),
            verify_ssl=data.get("verify_ssl", False),
            device_id=data.get("device_id", ""),
        )


@dataclass
class EventTypeHistory:
    """One camera's event-type discovery cache for the Live View
    timeline's Filter-events popover (see ui.event_type_filter) --
    every distinct type signature (see services.event_backend) an
    event backend has ever produced for this camera, and how far
    forward that scan has been brought up to date. Each backend keeps
    its own, under its own AppConfig field, since one backend's
    signatures mean nothing to another.

    checked_until alone is enough to resume correctly: a camera present
    here at all has already had its full history scanned once (the
    only kind of scan ever done for a camera with no existing entry),
    so there is no separate "have we reached the beginning" flag to
    track -- existence of the entry *is* that fact.
    """

    types: list[tuple[int, int]] = field(default_factory=list)
    checked_until: int = 0


@dataclass
class AppConfig:
    """Application configuration."""

    default_profile: str = ""
    profiles: dict[str, ConnectionProfile] = field(default_factory=dict)
    theme: str = "auto"  # "auto", "dark", "light"
    sidebar_visible: bool = True
    timeline_visible: bool = True
    # Version of the last "new release" notice the user has seen (by
    # visiting the About page) — suppresses the update indicator for that
    # same release without needing to re-check it against the live tag.
    dismissed_update_version: str = ""
    grid_layout: str = "2x2"
    last_page: str = "live"
    layout_cameras: dict[str, list[int]] = field(default_factory=dict)
    poll_interval_cameras: int = 30
    poll_interval_alerts: int = 30
    poll_interval_homemode: int = 60
    snapshot_dir: str = ""
    camera_overrides: dict[int, str] = field(default_factory=dict)
    camera_protocols: dict[int, str] = field(default_factory=dict)
    # Camera ID -> StreamProfile value ("high", "balanced", "low"); a
    # camera with no entry follows its Live View setting in Surveillance
    # Station. The app-wide setting overrides it (services.live).
    camera_live_view_stream_profiles: dict[int, str] = field(default_factory=dict)
    camera_volume: dict[int, int] = field(default_factory=dict)
    camera_muted: dict[int, bool] = field(default_factory=dict)
    search_camera_ids: list[int] = field(default_factory=list)
    search_from_time: str = ""
    search_to_time: str = ""
    # "today", "yesterday", "last24h", "last7d", "last30d", or "". Advanced
    # Search offers one preset more than the quick filter bar, so "last30d"
    # can only arrive from there.
    search_time_preset: str = ""
    events_search_camera_ids: list[int] = field(default_factory=list)
    events_search_from_time: str = ""
    events_search_to_time: str = ""
    # Events stops at "last24h": neither its quick filter nor its Advanced
    # Search offers the longer ranges.
    events_search_time_preset: str = "today"
    # Filter keys (e.g. "08", "25:hikvision"), not raw event_map flag
    # values — see services.legacy_event_bits. A config saved before that switch
    # has int-typed entries here; _config_from_data() drops them on load
    # rather than misinterpreting them as filter keys.
    events_search_event_types: list[str] = field(default_factory=list)
    # False ("Any"/OR, the default) or True ("All"/AND) — see
    # ui.events.EventsView._search_event_types_match_all.
    events_search_event_types_match_all: bool = False
    snapshots_search_camera_ids: list[int] = field(default_factory=list)
    snapshots_search_from_time: str = ""
    snapshots_search_to_time: str = ""
    snapshots_search_time_preset: str = ""  # same values as search_time_preset
    # camera ID -> discovered event types + scan progress, see
    # EventTypeHistory, for LegacyEventBackend. Deliberately
    # session/config-persisted rather than re-scanned every launch: a
    # full-history scan costs real seconds per camera (~7s/camera, ~90s
    # for a 20-camera NAS if done all at once), which is why it's never
    # done all at once; see ui.event_type_filter. Saved as
    # legacy_event_type_history, read from there or from the
    # event_type_history it was saved as before the rename.
    legacy_event_type_history: dict[int, EventTypeHistory] = field(default_factory=dict)
    # Runtime-tunable constants overridden from the Settings page, keyed by
    # Setting.key (see surveillance.settings_registry): generic, so a new
    # setting added there needs no new AppConfig field of its own.
    setting_overrides: dict[str, float] = field(default_factory=dict)
    # Same, for the Settings page's on/off toggles (BoolSetting.key ->
    # overridden value); kept apart from setting_overrides since TOML
    # (and this dataclass) distinguishes bool from float.
    setting_overrides_bool: dict[str, bool] = field(default_factory=dict)
    # Same, for the Settings page's dropdowns (ChoiceSetting.key ->
    # overridden value).
    setting_overrides_choice: dict[str, str] = field(default_factory=dict)
    # The profile whose camera-keyed settings are the ones held in the
    # fields above, and every other profile's, waiting their turn (see
    # PROFILE_STATE_FIELDS and activate_profile).
    active_profile: str = ""
    profile_state: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.snapshot_dir:
            self.snapshot_dir = str(DATA_DIR / "snapshots")

    def activate_profile(self, name: str) -> None:
        """Make *name*'s camera-keyed settings the ones in the fields
        PROFILE_STATE_FIELDS names, putting the current profile's away.

        Copied in and out rather than shared: some of these fields are
        reassigned rather than changed in place, which would cut a
        shared reference loose from the profile it belongs to.
        """
        if name == self.active_profile:
            return
        if self.active_profile:
            self.profile_state[self.active_profile] = {
                f: getattr(self, f) for f in PROFILE_STATE_FIELDS
            }
        state = self.profile_state.pop(name, {})
        for f in PROFILE_STATE_FIELDS:
            setattr(self, f, state.get(f, _FIELD_DEFAULTS[f]()))
        self.active_profile = name


# Settings keyed by camera ID. Every NAS numbers its cameras from 1, so
# shared between profiles one NAS's direct RTSP URL, protocol, stream
# profile, volume or layout landed on another's camera of the same number. Each profile
# keeps its own, written under its [profiles.<name>] table.
PROFILE_STATE_FIELDS = (
    "layout_cameras",
    "camera_overrides",
    "camera_protocols",
    "camera_live_view_stream_profiles",
    "camera_volume",
    "camera_muted",
    "legacy_event_type_history",
    "search_camera_ids",
    "events_search_camera_ids",
    "snapshots_search_camera_ids",
)

# What legacy_event_type_history was saved as before it was renamed.
# Only ever read: saving writes the current name, so a save after
# loading drops the old one.
_OLD_EVENT_TYPE_HISTORY_KEY = "event_type_history"

_FIELD_DEFAULTS: dict[str, Any] = {
    f: AppConfig.__dataclass_fields__[f].default_factory  # type: ignore[misc]
    for f in PROFILE_STATE_FIELDS
}


# Floor for the poll_interval_* settings. They are only reachable by hand
# editing the file, and they go straight to GLib.timeout_add_seconds(), where
# 0 is not "off" but a source that fires as fast as the main loop can run it.
MIN_POLL_INTERVAL = 5


def _poll_interval(general: dict[str, Any], key: str, default: int) -> int:
    """Read a poll interval, falling back on anything GLib cannot use.

    timeout_add_seconds() raises TypeError on a string and OverflowError on a
    negative, and busy loops on 0, so a typo in the config file would either
    break the connect path or hammer the NAS.
    """
    try:
        value = int(general.get(key, default))
    except (TypeError, ValueError):
        log.warning("Config: %s is not a number, using %ds", key, default)
        return default
    if value < MIN_POLL_INTERVAL:
        log.warning("Config: %s of %ds is below the %ds minimum", key, value, MIN_POLL_INTERVAL)
        return MIN_POLL_INTERVAL
    return value


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
    """Load configuration from TOML file.

    A file we cannot parse is moved to config.toml.bad rather than left in
    place: the next save would otherwise write defaults straight over
    whatever was still recoverable in it.
    """
    if not CONFIG_FILE.exists():
        return AppConfig()

    try:
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
        return _config_from_data(data)
    except (OSError, tomllib.TOMLDecodeError, AttributeError, TypeError, ValueError):
        salvaged = CONFIG_FILE.with_suffix(".toml.bad")
        with contextlib.suppress(OSError):
            os.replace(CONFIG_FILE, salvaged)
        log.exception("Unreadable config, starting with defaults; kept a copy at %s", salvaged)
        return AppConfig()


def _int_keyed(table: Any, convert: Any) -> dict[int, Any]:
    """A TOML table keyed by camera ID, with *convert* applied to each
    value, dropping any entry that does not convert."""
    result: dict[int, Any] = {}
    if isinstance(table, dict):
        for key, value in table.items():
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                result[int(key)] = convert(value)
    return result


def _event_type_history(entry: Any) -> EventTypeHistory:
    types = [
        (int(pair[0]), int(pair[1]))
        for pair in entry.get("types", [])
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    ]
    return EventTypeHistory(types=types, checked_until=int(entry.get("checked_until", 0)))


def _volume(value: Any) -> int:
    """A saved slot volume, held to the 0-100 the slider offers. Edited by
    hand past it, mpv played up to ten times louder than the slider then
    showed, and refused values below zero outright."""
    return max(0, min(100, int(value)))


def _profile_state_from(data: dict[str, Any]) -> dict[str, Any]:
    """One profile's PROFILE_STATE_FIELDS out of *data*, which holds
    them under their own names."""
    return {
        "layout_cameras": data.get("layout_cameras", {}),
        "camera_overrides": _int_keyed(data.get("camera_overrides"), str),
        "camera_protocols": _int_keyed(data.get("camera_protocols"), str),
        "camera_live_view_stream_profiles": _int_keyed(
            data.get("camera_live_view_stream_profiles"), str
        ),
        "camera_volume": _int_keyed(data.get("camera_volume"), _volume),
        "camera_muted": _int_keyed(data.get("camera_muted"), bool),
        "legacy_event_type_history": {
            # The current key wins per camera over the old one, as the
            # one saving writes.
            **_int_keyed(data.get(_OLD_EVENT_TYPE_HISTORY_KEY), _event_type_history),
            **_int_keyed(data.get("legacy_event_type_history"), _event_type_history),
        },
        "search_camera_ids": data.get("search_camera_ids", []),
        "events_search_camera_ids": data.get("events_search_camera_ids", []),
        "snapshots_search_camera_ids": data.get("snapshots_search_camera_ids", []),
    }


def _profile_state_to(state: dict[str, Any]) -> dict[str, Any]:
    """The TOML form of one profile's PROFILE_STATE_FIELDS, leaving out
    what is empty so a profile never used keeps a short table."""
    tables: dict[str, Any] = {
        "layout_cameras": state["layout_cameras"],
        "camera_overrides": {str(k): v for k, v in state["camera_overrides"].items()},
        "camera_protocols": {str(k): v for k, v in state["camera_protocols"].items()},
        "camera_live_view_stream_profiles": {
            str(k): v for k, v in state["camera_live_view_stream_profiles"].items()
        },
        "camera_volume": {str(k): v for k, v in state["camera_volume"].items()},
        "camera_muted": {str(k): v for k, v in state["camera_muted"].items()},
        "legacy_event_type_history": {
            str(cam_id): {
                "types": [list(pair) for pair in hist.types],
                "checked_until": hist.checked_until,
            }
            for cam_id, hist in state["legacy_event_type_history"].items()
        },
        "search_camera_ids": state["search_camera_ids"],
        "events_search_camera_ids": state["events_search_camera_ids"],
        "snapshots_search_camera_ids": state["snapshots_search_camera_ids"],
    }
    return {key: value for key, value in tables.items() if value}


def _time_text(value: Any) -> str:
    """A saved search time as the ISO text the pages parse. Written
    without quotes, as when the README's commented example is enabled by
    hand, TOML reads it as a datetime rather than text; passed on as one,
    every page raised building itself and login never completed. Anything
    else that is not text means no time."""
    if isinstance(value, (date, dt_time)):
        return value.isoformat()
    return value if isinstance(value, str) else ""


def _config_from_data(data: dict[str, Any]) -> AppConfig:
    """Build an AppConfig from already-parsed TOML."""

    profiles: dict[str, ConnectionProfile] = {}
    for name, pdata in data.get("profiles", {}).items():
        profiles[name] = ConnectionProfile.from_dict(name, pdata)

    general = data.get("general", {})
    session = data.get("session", {})

    # Each profile's camera-keyed settings. A config written before they
    # went per profile kept one shared set, in top-level sections and in
    # [session]: it goes to the default profile, the one logged into.
    states = {name: _profile_state_from(pdata) for name, pdata in data.get("profiles", {}).items()}
    active = general.get("default_profile", "")
    state_keys = (*PROFILE_STATE_FIELDS, _OLD_EVENT_TYPE_HISTORY_KEY)
    legacy = {
        **{key: data[key] for key in state_keys if key in data},
        **{key: session[key] for key in state_keys if key in session},
    }
    if legacy and active and not any(states.get(active, {}).values()):
        states[active] = _profile_state_from(legacy)
    active_state = states.pop(active, None) or _profile_state_from({})

    # setting_overrides: maps Setting.key (str) -> overridden value
    setting_overrides: dict[str, float] = {}
    for key, value in data.get("setting_overrides", {}).items():
        with contextlib.suppress(ValueError, TypeError):
            setting_overrides[str(key)] = float(value)

    # setting_overrides_bool: maps BoolSetting.key (str) -> overridden value
    setting_overrides_bool: dict[str, bool] = {}
    for key, value in data.get("setting_overrides_bool", {}).items():
        with contextlib.suppress(ValueError, TypeError):
            setting_overrides_bool[str(key)] = bool(value)

    # setting_overrides_choice: maps ChoiceSetting.key (str) -> overridden value
    setting_overrides_choice: dict[str, str] = {
        str(key): value
        for key, value in data.get("setting_overrides_choice", {}).items()
        if isinstance(value, str)
    }

    return AppConfig(
        **active_state,
        active_profile=active,
        profile_state=states,
        default_profile=active,
        profiles=profiles,
        theme=_load_theme(general),
        sidebar_visible=general.get("sidebar_visible", True),
        timeline_visible=general.get("timeline_visible", True),
        dismissed_update_version=general.get("dismissed_update_version", ""),
        grid_layout=session.get("grid_layout", general.get("grid_layout", "2x2")),
        last_page=session.get("last_page", "live"),
        poll_interval_cameras=_poll_interval(general, "poll_interval_cameras", 30),
        poll_interval_alerts=_poll_interval(general, "poll_interval_alerts", 30),
        poll_interval_homemode=_poll_interval(general, "poll_interval_homemode", 60),
        snapshot_dir=general.get("snapshot_dir", str(DATA_DIR / "snapshots")),
        search_from_time=_time_text(session.get("search_from_time")),
        search_to_time=_time_text(session.get("search_to_time")),
        search_time_preset=session.get("search_time_preset", ""),
        events_search_from_time=_time_text(session.get("events_search_from_time")),
        events_search_to_time=_time_text(session.get("events_search_to_time")),
        events_search_time_preset=session.get("events_search_time_preset", "today"),
        events_search_event_types=[
            v for v in session.get("events_search_event_types", []) if isinstance(v, str)
        ],
        events_search_event_types_match_all=session.get(
            "events_search_event_types_match_all", False
        ),
        snapshots_search_from_time=_time_text(session.get("snapshots_search_from_time")),
        snapshots_search_to_time=_time_text(session.get("snapshots_search_to_time")),
        snapshots_search_time_preset=session.get("snapshots_search_time_preset", ""),
        setting_overrides=setting_overrides,
        setting_overrides_bool=setting_overrides_bool,
        setting_overrides_choice=setting_overrides_choice,
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
            "sidebar_visible": config.sidebar_visible,
            "timeline_visible": config.timeline_visible,
            "dismissed_update_version": config.dismissed_update_version,
            "snapshot_dir": config.snapshot_dir,
        },
        "session": {
            "grid_layout": config.grid_layout,
            "last_page": config.last_page,
            "search_from_time": config.search_from_time,
            "search_to_time": config.search_to_time,
            "search_time_preset": config.search_time_preset,
            "events_search_from_time": config.events_search_from_time,
            "events_search_to_time": config.events_search_to_time,
            "events_search_time_preset": config.events_search_time_preset,
            "events_search_event_types": config.events_search_event_types,
            "events_search_event_types_match_all": config.events_search_event_types_match_all,
            "snapshots_search_from_time": config.snapshots_search_from_time,
            "snapshots_search_to_time": config.snapshots_search_to_time,
            "snapshots_search_time_preset": config.snapshots_search_time_preset,
        },
        "setting_overrides": dict(config.setting_overrides),
        "setting_overrides_bool": dict(config.setting_overrides_bool),
        "setting_overrides_choice": dict(config.setting_overrides_choice),
        "profiles": {},
    }

    states = dict(config.profile_state)
    states[config.active_profile] = {f: getattr(config, f) for f in PROFILE_STATE_FIELDS}
    for name, profile in config.profiles.items():
        data["profiles"][name] = profile.to_dict()
        if name in states:
            data["profiles"][name].update(_profile_state_to(states[name]))

    # Write a sibling temp file and rename over the real one. os.replace()
    # is atomic within a filesystem, so an interrupted save leaves the
    # previous config intact instead of a truncated or empty one — the
    # SIGINT/SIGTERM handlers in __main__ are os._exit(), which would
    # otherwise skip the flush and lose every profile.
    # Per-process temp name: a fixed one is the same inode for every
    # writer, so two instances sharing a $HOME could interleave their
    # writes into it and rename a spliced file over the real config.
    tmp = CONFIG_FILE.with_suffix(f".toml.{os.getpid()}.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with open(fd, "wb") as f:
            tomli_w.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
    except BaseException:
        # Cleanup only — the failure is always re-raised. BaseException
        # because KeyboardInterrupt is exactly the case this guards.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def add_profile(config: AppConfig, profile: ConnectionProfile) -> None:
    """Add or update a connection profile."""
    config.profiles[profile.name] = profile
    if not config.default_profile:
        config.default_profile = profile.name
    save_config_now(config)


def load_search_filters(
    cfg: AppConfig, prefix: str
) -> tuple[list[int] | None, int | None, int | None, str]:
    """Load persisted advanced-search filters (camera IDs, time range, preset)
    from the `{prefix}_camera_ids`/`{prefix}_from_time`/`{prefix}_to_time`/
    `{prefix}_time_preset` fields on AppConfig — e.g. prefix="search" for
    Recordings, "events_search" for Events. Shared by each page's own
    _load_search_from_config() so the parsing logic isn't tripled."""
    camera_ids: list[int] | None = getattr(cfg, f"{prefix}_camera_ids") or None
    from_time = None
    from_str = getattr(cfg, f"{prefix}_from_time")
    if from_str:
        with contextlib.suppress(ValueError):
            from_time = int(datetime.fromisoformat(from_str).timestamp())
    to_time = None
    to_str = getattr(cfg, f"{prefix}_to_time")
    if to_str:
        with contextlib.suppress(ValueError):
            to_time = int(datetime.fromisoformat(to_str).timestamp())
    time_preset: str = getattr(cfg, f"{prefix}_time_preset")
    # Hand-edited and mistyped, a preset made every load of the page raise
    # in preset_range, and Recordings and Snapshots stayed marked loading
    # for the rest of the session.
    from surveillance.services.recording import PRESET_LABELS

    if time_preset not in PRESET_LABELS:
        time_preset = ""
    return camera_ids, from_time, to_time, time_preset


def save_search_filters(
    cfg: AppConfig,
    prefix: str,
    camera_ids: list[int] | None,
    from_time: int | None,
    to_time: int | None,
    time_preset: str,
) -> None:
    """Persist advanced-search filters to the same `{prefix}_*` AppConfig
    fields load_search_filters() reads, and write the config to disk."""
    setattr(cfg, f"{prefix}_camera_ids", camera_ids or [])
    setattr(
        cfg,
        f"{prefix}_from_time",
        datetime.fromtimestamp(from_time).isoformat() if from_time else "",
    )
    setattr(
        cfg,
        f"{prefix}_to_time",
        datetime.fromtimestamp(to_time).isoformat() if to_time else "",
    )
    setattr(cfg, f"{prefix}_time_preset", time_preset)
    save_config(cfg)
