<p align="center">
  <img src="assets/banner.svg" alt="Surveillance Station Client &mdash; native GTK4 desktop client for Synology Surveillance Station on Linux and BSD, showing the Live View grid" width="100%">
</p>

<p align="center">
  <a href="https://github.com/renaudallard/surveillance-station-client/actions/workflows/lint.yml"><img src="https://github.com/renaudallard/surveillance-station-client/actions/workflows/lint.yml/badge.svg" alt="Lint & Type Check"></a>
  <a href="https://github.com/renaudallard/surveillance-station-client/releases/latest"><img src="https://img.shields.io/github/v/release/renaudallard/surveillance-station-client?label=release" alt="Latest Release"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/GTK-4-green" alt="GTK4">
  <img src="https://img.shields.io/github/license/renaudallard/surveillance-station-client" alt="License">
</p>

<p align="center">
  No browser needed. Connect directly to your Synology NAS and get live camera
  feeds, recording playback, PTZ control, snapshots, event alerts, and home mode
  management &mdash; all from a lightweight native desktop application.
</p>

<img src="assets/divider.svg" width="100%" height="8" alt="">

## Features

<details>
<summary><b>Live View</b></summary>

Real-time camera streams in 1&times;1, 2&times;2, 3&times;3, or 4&times;4 grid layouts, selected from the grid button in the header bar. Each layout keeps its own camera arrangement. Clear a single slot from the camera sidebar or the whole layout from the same grid menu, with a confirmation prompt. Scroll to zoom in on a slot (centered on the cursor) and click-and-drag to pan; zoom resets when switching layouts or leaving the page. Streams are muted by default; hover a slot to reveal a toolbar with mute/volume (remembered per camera) and a quick Snapshot button. Audio reaches the player over the RTSP-family protocols (`rtsp`, `rtsp_over_http`, `multicast`, `direct`), or over the default `auto`/`websocket` protocol when the camera's audio codec is PCMU or AAC (muxed in via ffmpeg); otherwise the mute button stays greyed out until the protocol is changed (or DSM reports a different codec) by right-clicking the camera in the sidebar. A camera with no audio track gets no mute button at all. A muxed camera that stops sending audio for three seconds while its video keeps arriving keeps playing without sound until its stream is restarted, rather than holding up its own video. Cameras with a speaker also get a push-to-talk microphone button &mdash; tap to start talking, tap again to stop. Hardware-accelerated rendering via mpv + OpenGL. Works on X11 and Wayland. The client sends a periodic keepalive to hold each WebSocket session open; if a session is ever interrupted anyway (a network blip, a NAS restart), it reconnects on the same pipe transparently, with no visible interruption. A camera the server reports as disabled or disconnected shows an "offline" placeholder instead of freezing, and a WebSocket or RTSP stream that stops responding mid-session shows "stream lost" ("attempting reconnect" while retrying) &mdash; both recover automatically and restore the real feed as soon as the camera is reachable again, with no action needed.
</details>
<details>
<summary><b>Timeline</b></summary>

A shared timeline strip below the video grid, toggled from the header bar (persists across restarts). Scroll or use the zoom buttons to zoom the time scale (centered on the cursor), click-and-drag to pan; a thin blue border marks which slot's camera the timeline is currently tracking &mdash; it follows the last-clicked slot, defaults to the upper-left slot on layout load, and fades after 10 seconds of no activity anywhere in the window. Hovering the ruler shows a small preview thumbnail of that camera at the hovered time, with its date/time overlaid. In History mode, a date/time bubble follows the marker as it moves. Below the ruler, a recording-presence bar shows two rows: where the tracked slot's own camera has a recording, and where any camera in the current layout does. Clicking a point on the ruler switches every slot in the current layout into History mode, each playing its own camera's recorded video from that time; a **Live** button returns every slot to the real-time stream. Picking a different camera into a slot already in History mode keeps playing recorded video, for the newly picked camera, at the same point in time, rather than dropping back to live; switching grid layout always returns to live first. Pan/tilt/zoom, focus, preset, patrol, and push-to-talk are unavailable while a slot shows recorded video (mute/volume and Snapshot remain available). Jump &plusmn;10s buttons seek every slot in the current layout the same way clicking the ruler does, coalescing a burst of rapid clicks into a single request rather than firing one per click. A **Pause** button freezes every active slot in place &mdash; a live slot just freezes locally without leaving Live mode, while a History slot asks DSM to actually stop sending, so the gap behind wall clock grows for as long as it stays paused; resuming a History slot paused for under 10 seconds lands at least 10 seconds behind live rather than right at the edge. A Pause ends by itself wherever the streams behind it are replaced: pressing **Live**, leaving the page and coming back, or switching layout, since each of those starts every slot afresh. A stream that starts while a Pause is on joins it, so a camera picked into a paused layout is frozen with the rest. A playback-speed dropdown (History mode only) offers 1/8x through 100x plus a Fwd/Rev direction toggle, applied to every active History slot at once; it resets to 1x/Fwd whenever a slot returns to Live or the layout switches. The higher speeds are greyed out on the larger layouts, since DSM really does send that many more frames per second and every slot has to decode them: by default, 1&times;1 offers the full range, 2&times;2 stops at 16x, 3&times;3 at 8x, and 4&times;4 at 4x &mdash; adjustable from the Settings page, with 1x always on offer whatever the budget. Real motion/alarm events are overlaid on the same two presence rows as orange markers. **Previous event**/**Next event** jump to the nearest one on either side of the current position, across every camera in the layout; Previous stays available in Live mode (dropping into History first, like Back 10s), while Next is History-only, like Forward 10s. Events within 10 seconds of wall clock are skipped, since a seek that close would just be clamped back to the live edge anyway. A calendar button opens a date/time picker, its own days marked and any day without a recording (across the current layout) refused, and its **Jump** button greyed out until the exact date/time selected falls within a real recording; jumping there re-centers the timeline at its default zoom, while Back/Forward 10s and Previous/Next event instead just pan (at whatever zoom is already set) if their own target would otherwise land off-screen. A **Filter events** button narrows the presence bar's event markers and Previous/Next event navigation down to chosen event types (Any or All of a multi-select, decoded per camera brand &mdash; see `EVENT_BITMASK.md`); opening it scans each layout camera's full recording history for the types it has ever produced (a persisted, incrementally-updated cache, so only the first scan and any time elapsed since the last one cost real seconds), showing a per-camera progress checklist meanwhile. A **Download** button opens a popup with a camera picker (only cameras currently assigned to a slot in the layout, since some may be empty, and defaulting to the tracked slot's own camera), above two tabs: Quick Save, whose three one-click buttons immediately prompt for a save location and download around the tracked slot's current position &mdash; **Download last 1/2/5 min** up to it in Live mode, or **Download &minus;1/2/5 to +1/2/5 min** centered on it in History mode, since both directions are already available once paused on a moment of interest &mdash; and Custom Save, with Start/End date-time fields (defaulting to a short clip ending at the same point) for an exact range.
</details>
<details>
<summary><b>Recordings</b></summary>

Browse, filter by camera, play back with full transport controls (seek, pause, volume, scroll-to-zoom, click-and-drag pan), and download to disk. Quick date presets (Today, Yesterday, Last 24 hrs, Last 7 days) for one-click filtering, plus advanced search by camera(s) and custom time range. Reset button clears all filters at once. Active filter summary always visible. Per-event thumbnails and smart detection labels (person, vehicle, animal, etc.) shown for each recording.
</details>
<details>
<summary><b>PTZ Control</b></summary>

Pan/Tilt, Zoom, Focus, Preset, and Patrol controls for PTZ-capable cameras, in the same per-slot hover toolbar as Live View's audio controls. Picking a patrol asks Surveillance Station to run that saved route; the NAS drives the camera, so it keeps going after you switch cameras or quit. Routes are created and edited in Surveillance Station itself, and there is no stop control, the same as Synology's own clients.
</details>
<details>
<summary><b>Snapshots</b></summary>

Browse saved snapshots, filter by camera and time range, view, download, or delete. Take a snapshot straight from a Live View slot's right-click menu, which saves it to the snapshot database and offers a local copy. The full-size viewer supports scroll-to-zoom and click-and-drag panning.
</details>
<details>
<summary><b>Time Lapse</b></summary>

Browse, play back, download, lock/unlock, and delete Smart Time Lapse recordings. Filter by time lapse task.
</details>
<details>
<summary><b>Events & Alerts</b></summary>

Browse real events decoded from each camera's own detected categories (motion, audio, tampering, person/vehicle/pet, and more, brand-dependent &mdash; see `EVENT_BITMASK.md`) with their type and time, filter by event type (quick filter plus a multi-select in advanced search, matching Any or All of the selected types) and by camera. Notification bell with unread badge and alert popover, polled every 30 seconds.
</details>
<details>
<summary><b>Home Mode</b></summary>

Toggle Surveillance Station home mode directly from the header bar.
</details>
<details>
<summary><b>License Management</b></summary>

View, add, and delete camera licenses. Online and offline activation.
</details>
<details>
<summary><b>Settings</b></summary>

Tune the demuxer cache sizes used for each streaming profile (plain RTSP, WebSocket muxed-audio, and silent WebSocket), the extra buffer added automatically at high History playback speeds, the shared demuxer byte cap, and the speed&times;slots budget the Live View timeline's History speed dropdown enforces, plus an on-screen readout of cache depth/target/effective speed for diagnosing buffering. Each setting has its own reset-to-default button, plus one that resets everything on the page at once. Changes apply to the next stream that starts (the on-screen readout takes effect immediately) and persist across restarts.
</details>
<details>
<summary><b>Session Persistence</b></summary>

Grid layout, active page, camera assignments, sidebar and timeline visibility, per-camera volume, mute and stream protocol, and the search filters of each browser page (including time presets) are restored on restart. Critical changes are flushed to disk immediately for crash resilience.
</details>
<details>
<summary><b>Two-Factor Authentication</b></summary>

MFA/OTP login support. When 2FA is enabled on your Synology account, the client prompts for a 6-digit authenticator code and optionally registers as a trusted device to skip OTP on future logins.
</details>
<details>
<summary><b>Multi-Profile</b></summary>

Save multiple NAS connection profiles and switch between them from the login screen.
</details>
<details>
<summary><b>Secure Credentials</b></summary>

Passwords stored in your system keyring (GNOME Keyring, KWallet, macOS Keychain).
</details>
<details>
<summary><b>Theming</b></summary>

Auto (follow OS), dark, or light theme selectable from the header bar.
</details>
<details>
<summary><b>About & Updates</b></summary>

An About page shows the version, license, and repository links. On login the client checks the GitHub releases page once for a newer version and, if one exists, marks the About entry until you have seen it.
</details>

<img src="assets/divider.svg" width="100%" height="8" alt="">

## Quick Start (Linux, no install needed)

Download the latest AppImage for your architecture from the
[Releases](https://github.com/renaudallard/surveillance-station-client/releases/latest)
page:

```sh
chmod +x Surveillance-*-x86_64.AppImage
./Surveillance-*-x86_64.AppImage
```

Available for **x86_64** and **aarch64**. A new release with AppImages is built
automatically every time the version is bumped.

The AppImage carries its own Python, GTK and mpv, but uses the host's
PipeWire, ALSA and JACK libraries wherever the host has them. Those load
plugins from fixed system directories, so a bundled copy picks up the
host's plugins and crashes on them. Nothing to install for that, it is
what the bundle does on its own. Where it does have to fall back to its
own PipeWire, it sends mpv to PulseAudio for the same reason; set
`SURVEILLANCE_AO` to any driver name `mpv --ao` takes to choose yourself.

<img src="assets/divider.svg" width="100%" height="8" alt="">

## Usage

<details>
<summary><b>Application startup arguments</b></summary>

```sh
surveillance                                  # launch the application
surveillance --debug                          # enable debug logging to stderr
surveillance --log-file                       # log to an auto-named file that will be preserved for sessions that do not exit normally
surveillance --debug --log-file=/tmp/run.log  # log to a file of your own choosing
python -m surveillance                        # run directly from the source tree
```

Debug logs automatically redact passwords, session tokens, and usernames.

`--log-file` writes at the same level as stderr (WARNING, or DEBUG with
`--debug`), and additionally captures the traceback of an uncaught exception
and the messages GTK and GLib print themselves, neither of which reaches
stderr through the log. Given a path, it writes there. Without one, it
writes to a fresh, timestamped file under
`$XDG_STATE_HOME/surveillance-station/logs/` (or
`~/.local/state/surveillance-station/logs/` if `$XDG_STATE_HOME` isn't
set) and marks it complete on a clean exit. Files marked complete are
deleted by the next run that also passes a bare `--log-file`. Unmarked
files left over from crashes are kept for inspection.

Two environment variables reach the embedded mpv player. `SURVEILLANCE_AO`
picks its audio output driver (see the AppImage note above), and
`SURVEILLANCE_MPV_OPTS` passes any other mpv options, whitespace-separated
and written as `name=value` the way `mpv.conf` spells them, or a bare
`name` for a flag. They are applied after the client's own options, so
they override them:

```sh
SURVEILLANCE_MPV_OPTS="hwdec=nvdec hwdec-extra-frames=12" surveillance
```

Quote a value that contains whitespace as in a shell. A name mpv does not
know stops the player from starting, and the log names it. See
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) for when this is needed.
</details>

<details>
<summary><b>Login to your Surveillance Station (DSM)</b></summary>

On launch, a login dialog asks for your NAS connection details:

| Field | Description | Default |
|---|---|---|
| **Profile name** | Label for this connection (e.g. `home-nas`) | hostname |
| **Host** | NAS IP address or hostname | &mdash; |
| **Port** | DSM port | `5001` |
| **Use HTTPS** | Enable HTTPS (recommended) | on |
| **Verify SSL** | Validate the SSL certificate (disable for self-signed) | off |
| **Username** | DSM user with Surveillance Station permissions | &mdash; |
| **Password** | DSM password | &mdash; |
| **Remember credentials** | Store in system keyring | on |
</details>

<details>
<summary><b>Basic usage</b></summary>

After connecting, the camera list appears in the sidebar. Click a camera to
start its live stream. Use the navigation buttons at the bottom of the sidebar
to switch between **Live View**, **Recordings**, **Snapshots**, **Events**,
**Time Lapse**, **Licenses**, **Settings**, and **About**; the button for the
current page stays highlighted. The header bar shows the current page name and holds the panel
toggle on the left, which hides or shows the whole sidebar.

On **Live View**, the grid button in the header bar selects the layout and can
clear the current one. Click a slot to select it, then click a camera to fill
it, or click **Empty Slot** at the bottom of the camera list to empty it again.
Clicking a camera with no slot selected switches to 1&times;1 and shows only
that camera. Right-click a slot for **Take Snapshot**, **Open in 1x1 Layout**,
and **Clear Slot**.
</details>

<details>
<summary><b>Keyboard shortcuts</b></summary>

| Key | Action |
|---|---|
| `Ctrl+Q` | Quit |
</details>


<details>
<summary><b>Configuration</b></summary>

Configuration is stored in TOML format following the XDG base directory
specification:

```
~/.config/surveillance-station/config.toml
```

#### Example configuration

```toml
[general]
default_profile = "home-nas"
theme = "auto"                  # "auto" (follow OS), "dark", or "light"
sidebar_visible = true          # camera sidebar shown at startup
timeline_visible = true         # Live View timeline strip shown at startup
dismissed_update_version = ""   # release tag whose update notice was dismissed
poll_interval_cameras = 30      # seconds, minimum 5; also how often a lost stream is retried
poll_interval_alerts = 30
poll_interval_homemode = 60
snapshot_dir = "/home/user/.local/share/surveillance-station/snapshots"  # folder the Save dialog opens in

[session]
grid_layout = "2x2"            # "1x1", "2x2", "3x3", or "4x4"
last_page = "live"             # last active page

[session.layout_cameras]
# Camera IDs per layout (0 = empty slot).  Each layout remembers its
# own assignment independently.
"1x1" = [1]
"2x2" = [1, 3, 0, 5]
"3x3" = [1, 3, 7, 0, 5, 8, 2, 0, 0]

# Recording search filters (persisted from last search)
# search_camera_ids = [1, 3]
# search_from_time = "2026-02-01T00:00:00"
# search_to_time = "2026-02-19T23:59:59"
# search_time_preset = "today"  # "today", "yesterday", "last24h", "last7d", "last30d", or ""

[camera_overrides]
# Direct RTSP URLs keyed by camera ID.
# Use when Synology's RTSP proxy corrupts a stream (e.g. Reolink Duo 3 PoE h265).
# 5 = "rtsp://admin:password@192.168.1.50:554/h265Preview_01_main"

[camera_volume]
# Live View volume per camera ID, 0-100. Set from the slot's hover toolbar.
# 5 = 40

[camera_muted]
# Live View mute state per camera ID. Cameras start muted.
# 5 = false

[event_type_history]
# Event types each camera has ever produced, discovered by the Live View
# timeline's "Filter events" popover and kept so a later open only has to
# scan forward from checked_until instead of the whole history again.
# Written automatically; delete a camera's entry to force a full rescan.
# [event_type_history.5]
# types = [[3, 0], [33554435, 2]]  # (event type flag, reserved) pairs
# checked_until = 1771200000       # unix time this camera was scanned up to

[camera_protocols]
# Stream protocol per camera ID:
# auto, websocket, mjpeg, rtsp_over_http, rtsp, multicast, direct
# "auto" is the same as "websocket"; there is no fallback between
# protocols, so pick one explicitly if WebSocket does not work.
# "websocket" uses a WebSocket stream bridged to mpv via an in-memory pipe,
# muxing in real audio via ffmpeg when the camera's audio codec is PCMU or AAC.
# "direct" uses the URL from [camera_overrides].
# 5 = "direct"

[setting_overrides]
# Settings page tuning, numeric values (see the Settings page itself for
# what each one does). Present only for settings actually changed from
# their default; use the page's own reset button to remove one.
# cache_seconds_default = 1.0
# demuxer_max_bytes_mib = 64.0

[setting_overrides_bool]
# Same, for the Settings page's on/off toggles.
# osd_enabled = true

[profiles.home-nas]
host = "192.168.1.100"
port = 5001
https = true
verify_ssl = false
```

The `[session]` section is managed automatically &mdash; the application
restores the grid layout, active page, and camera assignments from the previous
session on restart.

Stream protocols and direct RTSP overrides can also be configured from the UI:
right-click a camera in the sidebar to choose the protocol.

`[setting_overrides]`/`[setting_overrides_bool]` are written by the
**Settings** page (Licenses &rarr; Settings &rarr; About in the sidebar), not
meant for hand-editing &mdash; use the page's own reset buttons instead.

Credentials are **never** stored in the config file. They are kept in the
system keyring under the service name `surveillance-station`.

When two-factor authentication (2FA/MFA) is enabled on the Synology account, the
client will prompt for a 6-digit OTP code after entering credentials. Checking
"Trust this device" stores a device token in the profile so subsequent logins
skip the OTP step. If the trust is revoked on the NAS, the client will prompt
for OTP again automatically.
</details>

<img src="assets/divider.svg" width="100%" height="8" alt="">

## Development

<a id="install-dependencies"></a>
<details>
<summary><b>Install dependencies</b></summary>

### System packages

Identify, download and install the packages that match your distro. These
must be installed **before** the Python dependencies.

#### Debian / Ubuntu

```sh
sudo apt install \
    gir1.2-gtk-4.0 \
    libgtk-4-dev \
    libmpv-dev \
    libmpv2 \
    libportaudio2 \
    ffmpeg \
    python3-gi \
    python3-gi-cairo \
    python3-cairo
```
#### Arch Linux

```sh
sudo pacman -S gtk4 mpv portaudio ffmpeg python-gobject python-cairo
```
#### Fedora

```sh
sudo dnf install \
    gtk4-devel \
    mpv-devel \
    portaudio \
    ffmpeg \
    python3-gobject \
    python3-cairo
```
#### openSUSE

```sh
sudo zypper install \
    gtk4-devel \
    mpv-devel \
    libportaudio2 \
    ffmpeg \
    python3-gobject \
    python3-gobject-cairo
```
#### FreeBSD

```sh
pkg install gtk4 mpv portaudio ffmpeg py311-gobject3 py311-cairo
```
#### OpenBSD

```sh
pkg_add gtk4 mpv portaudio ffmpeg py3-gobject3 py3-cairo
```

### Python packages

**Python 3.11** or later is required. These are installed automatically by `pip`:

| Package | Purpose |
|---|---|
| `PyGObject` >= 3.50 | GTK4 bindings with native asyncio integration |
| `pycairo` >= 1.20 | Cairo bindings, for the timeline ruler and presence bar |
| `httpx[http2]` >= 0.27 | Async HTTP/2 client for Synology REST API |
| `python-mpv` >= 1.0 | libmpv bindings for video rendering |
| `PyOpenGL` >= 3.1 | OpenGL context for mpv render in GTK4 GLArea |
| `keyring` >= 25.0 | Secure credential storage |
| `tomli-w` >= 1.0 | TOML config writing |
| `websockets` >= 13.0 | WebSocket stream bridge for live view |
| `sounddevice` >= 0.5 | PortAudio bindings for push-to-talk mic capture |
| `cryptography` >= 42.0 | AES for offline license activation |
</details>

<details>
<summary><b>Install source code</b></summary>

1. Make sure you have installed [system dependencies](#install-dependencies) for your distro
2. Clone and install:

```sh
git clone https://github.com/renaudallard/surveillance-station-client.git
cd surveillance-station-client
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install .
```

> **Note:** `--system-site-packages` is required so the venv can access the
> system-installed PyGObject and cairo bindings, which cannot be built via pip
> without extensive C development headers.

3. Run:

```sh
surveillance
```
</details>

<details>
<summary><b>Building, testing and CI</b></summary>

### Building an AppImage locally

```sh
./build-appimage.sh
```

This produces `Surveillance-<version>-<arch>.AppImage` in the project root.
Requires `libmpv`, `libportaudio2`, `ffmpeg`, GTK4 development files, and
`libfuse2` on the build machine.

After PyInstaller runs, `libpipewire-0.3.so.0`, `libasound.so.2` and
`libjack.so.0` are moved out of `_internal/` into
`_internal/host-libs/<soname>/`, one directory each, and AppRun puts one
back on `LD_LIBRARY_PATH` only where the host has no copy of that library.
They dlopen their plugins from paths compiled into them at build time, so
a bundled copy running on another distribution loads that distribution's
plugins into itself and crashes. Keep them out of `_internal/`.

### Running checks locally

```sh
pip install -e ".[dev]"

ruff check src/ tests/       # lint (rules: E, F, W, I, B, S, SIM, RET, PLR, PLW, PLC, TRY, RUF)
ruff format src/ tests/       # format
mypy src/surveillance/        # type check
pytest tests/ -v              # tests
```

### Automated build pipeline

CI runs automatically on push and pull requests to `main`:

| Workflow | Trigger | What it does |
|---|---|---|
| [`lint.yml`](.github/workflows/lint.yml) | push / PR to `main` | ruff check, ruff format, mypy |
| [`release.yml`](.github/workflows/release.yml) | version bump on `main` | Build AppImages (x86_64 + aarch64), create GitHub release |

</details>

<details>
<summary><b>Architecture</b></summary>

### Design overview

```
┌─────────────────────────────────────────┐
│  UI Layer          GTK4 widgets         │
│  window, sidebar, liveview, timeline,   │
│  recordings, player, slot_toolbar,      │
│  snapshots, events, timelapse,          │
│  licenses, settings, notifications      │
├─────────────────────────────────────────┤
│  Service Layer     domain logic         │
│  camera, live, ws_bridge, recording,    │
│  ptz, ptt, snapshot, event, homemode,   │
│  license, timelapse                     │
├─────────────────────────────────────────┤
│  API Layer         httpx (async)        │
│  client, auth, models                   │
└─────────────────────────────────────────┘
```

Three event systems are integrated:

- **GLib main loop** drives the GTK4 UI
- **asyncio** runs in a background thread, bridged to GLib via `GLib.idle_add()`
- **mpv threads** bridge back to the main thread via `GLib.idle_add()`

Video is rendered through mpv's OpenGL render API into a `Gtk.GLArea` widget,
which works on both X11 and Wayland without window ID embedding.

### Project structure

```
surveillance-station-client/
├── pyproject.toml
├── README.md
├── LICENSE
├── TROUBLESHOOTING.md                   troubleshooting guide
├── surveillance.1                       man page
├── EVENT_BITMASK.md                     event_map bitmask reverse-engineering reference
├── build-appimage.sh                    AppImage build script
├── appimage_entry.py                    PyInstaller entry point
├── assets/
│   ├── banner.svg                       README banner image
│   └── divider.svg                      README section divider
├── scripts/
│   └── dump_event_map.py                diagnostic tool for extending EVENT_BITMASK.md
├── data/
│   └── org.surveillance.desktop
├── .github/ISSUE_TEMPLATE/
│   ├── bug_report.yml                   bug report form
│   └── config.yml                       issue template config
├── .github/workflows/
│   ├── lint.yml                         CI: ruff + mypy
│   ├── check-debug-logs.yml             CI: validates bug-report debug logs
│   ├── stale-issues.yml                 CI: closes stale issues
│   └── release.yml                      AppImage build + GitHub release
├── src/surveillance/
│   ├── __main__.py                      entry point
│   ├── app.py                           Gtk.Application
│   ├── config.py                        TOML config + XDG paths
│   ├── credentials.py                   keyring wrapper
│   ├── logfile.py                       --log-file handling + redaction
│   ├── settings_registry.py             tunable constants behind the Settings page
│   ├── data/
│   │   ├── style.css
│   │   └── event_bits.json              event_map bit -> label table (see EVENT_BITMASK.md)
│   ├── api/
│   │   ├── client.py                    SurveillanceAPI (httpx)
│   │   ├── auth.py                      login / logout / SID
│   │   └── models.py                    dataclasses
│   ├── services/
│   │   ├── camera.py                    camera list
│   │   ├── live.py                      stream URL resolution
│   │   ├── ws_bridge.py                 WebSocket-to-pipe bridge
│   │   ├── recording.py                 recording management
│   │   ├── download.py                  recording/snapshot download helpers
│   │   ├── ptz.py                       PTZ commands
│   │   ├── ptt.py                       push-to-talk session (AudioOut WebSocket)
│   │   ├── g711.py                      G.711 mu-law encoder
│   │   ├── aac.py                       AAC frame recovery + ADTS headers
│   │   ├── snapshot.py                  snapshot management
│   │   ├── event.py                     events + alerts
│   │   ├── event_bits.py                event_map bitmask decoder (see EVENT_BITMASK.md)
│   │   ├── homemode.py                  home mode toggle
│   │   ├── license.py                   license management
│   │   ├── timelapse.py                 time lapse management
│   │   └── update_check.py              GitHub release version check
│   ├── ui/
│   │   ├── window.py                    main window
│   │   ├── about.py                     About page
│   │   ├── login.py                     login dialog
│   │   ├── headerbar.py                 header bar controls
│   │   ├── sidebar.py                   camera list sidebar
│   │   ├── liveview.py                  live stream grid
│   │   ├── layouts.py                   grid layout definitions
│   │   ├── timeline.py                  Live View timeline strip
│   │   ├── date_time_picker.py          calendar date/time picker
│   │   ├── event_type_filter.py         Filter-events popover
│   │   ├── slot_toolbar.py              per-slot hover toolbar (audio, PTT, PTZ, snapshot)
│   │   ├── mpv_widget.py                GLArea + mpv render
│   │   ├── rtsp_health.py               RTSP stream stall/health monitor
│   │   ├── recordings.py                recording browser
│   │   ├── advanced_search.py           advanced search dialog (shared by Recordings/Snapshots/Events)
│   │   ├── player.py                    playback controls
│   │   ├── snapshots.py                 snapshot browser
│   │   ├── events.py                    event list
│   │   ├── licenses.py                  license management
│   │   ├── timelapse.py                 time lapse browser
│   │   ├── notifications.py             alert popover
│   │   ├── settings.py                  Settings page
│   │   ├── icons.py                     icon loading helpers
│   │   └── labels.py                    combo label helpers shared by the browser pages
│   └── util/
│       └── async_bridge.py              GLib + asyncio bridge
└── tests/
    ├── conftest.py
    ├── test_aac.py
    ├── test_advanced_search.py
    ├── test_api_client.py
    ├── test_config.py
    ├── test_event_bits.py
    ├── test_liveview_persistence.py
    ├── test_liveview_state.py
    ├── test_logging.py
    ├── test_models.py
    ├── test_mpv_env_options.py
    ├── test_mpv_profiles.py
    ├── test_rtsp_health.py
    ├── test_services.py
    ├── test_settings_registry.py
    ├── test_timeline.py
    ├── test_ui_behavior.py
    ├── test_update_check.py
    └── test_ws_bridge.py
```
</details>

<details>
<summary><b>Synology API Reference</b></summary>

### Endpoints used by this client

| API | Purpose |
|---|---|
| `SYNO.API.Info` | Discover available APIs and CGI paths |
| `SYNO.API.Auth` | Login / logout / session management |
| `SYNO.SurveillanceStation.Camera` | Camera list, snapshots, live view paths |
| `SYNO.SurveillanceStation.PTZ` | Pan, tilt, zoom, presets, patrols |
| `SYNO.SurveillanceStation.AudioOut` | Push-to-talk busy-check (CheckOccupied) — the actual audio upload is a raw WebSocket, not a REST call |
| `SYNO.SurveillanceStation.Recording` | List, stream, download recordings |
| `SYNO.SurveillanceStation.RecordingPicker` | Per-camera event intervals behind the Events page and the Live View timeline: recording presence and the motion/alarm history, decoded bit-by-bit into brand-aware categories &mdash; see `EVENT_BITMASK.md` |
| `SYNO.SurveillanceStation.Stream` | Recording playback URL (`EventStream`), with `SYNO.SurveillanceStation.Streaming` as the legacy fallback |
| `SYNO.SurveillanceStation.SnapShot` | List, take, download, delete snapshots |
| `SYNO.SurveillanceStation.TimeLapse` | Time lapse task listing |
| `SYNO.SurveillanceStation.TimeLapse.Recording` | Time lapse recording management |
| `SYNO.SurveillanceStation.Notification` | Alert list, unread count, mark read |
| `SYNO.SurveillanceStation.HomeMode` | Get/set home mode status |
| `SYNO.SurveillanceStation.License` | License management |
| `SYNO.SurveillanceStation.Info` | NAS device info |

</details>

<img src="assets/divider.svg" width="100%" height="8" alt="">

## [Troubleshooting](TROUBLESHOOTING.md)

<img src="assets/divider.svg" width="100%" height="8" alt="">

## Support

If you find this project useful, you can support its development:

[![PayPal](https://img.shields.io/badge/PayPal-Donate-blue?logo=paypal)](https://www.paypal.me/RenaudAllard)

<img src="assets/divider.svg" width="100%" height="8" alt="">

## Disclaimer

This project is **not affiliated with, endorsed by, or sponsored by Synology Inc.**
Synology, Surveillance Station, and DiskStation Manager (DSM) are trademarks of
Synology Inc. This software is an independent, third-party client that interacts
with the publicly documented Synology Web API. Use it at your own risk.

<img src="assets/divider.svg" width="100%" height="8" alt="">

## License

BSD-2-Clause &mdash; see [LICENSE](https://github.com/renaudallard/surveillance-station-client/blob/main/LICENSE) for details.

```
Copyright (c) 2026, Renaud Allard <renaud@allard.it>
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```
