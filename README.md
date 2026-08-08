<h1 align="center">Surveillance Station Client</h1>

<p align="center">
  <strong>Native GTK4 desktop client for Synology Surveillance Station</strong>
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

---

## Features

- **Live View** &mdash; Real-time camera streams in 1&times;1, 2&times;2, 3&times;3, or 4&times;4 grid layouts, selected from the grid button in the header bar. Each layout keeps its own camera arrangement. Clear a single slot from the camera sidebar or the whole layout from the same grid menu, with a confirmation prompt. Scroll to zoom in on a slot (centered on the cursor) and click-and-drag to pan; zoom resets when switching layouts or leaving the page. Streams are muted by default; hover a slot to reveal a toolbar with mute/volume (remembered per camera) and a quick Snapshot button. Audio reaches the player over the RTSP-family protocols (`rtsp`, `rtsp_over_http`, `multicast`, `direct`), or over the default `auto`/`websocket` protocol when the camera's audio codec is PCMU or AAC (muxed in via ffmpeg); otherwise the mute button stays greyed out until the protocol is changed (or DSM reports a different codec) by right-clicking the camera in the sidebar. A camera with no audio track gets no mute button at all. Cameras with a speaker also get a push-to-talk microphone button &mdash; tap to start talking, tap again to stop. Hardware-accelerated rendering via mpv + OpenGL. Works on X11 and Wayland. The client sends a periodic keepalive to hold each WebSocket session open; if a session is ever interrupted anyway (a network blip, a NAS restart), it reconnects on the same pipe transparently, with no visible interruption. A camera the server reports as disabled or disconnected shows an "offline" placeholder instead of freezing, and a WebSocket or RTSP stream that stops responding mid-session shows "stream lost" ("attempting reconnect" while retrying) &mdash; both recover automatically and restore the real feed as soon as the camera is reachable again, with no action needed.
- **Recordings** &mdash; Browse, filter by camera, play back with full transport controls (seek, pause, volume, scroll-to-zoom, click-and-drag pan), and download to disk. Quick date presets (Today, Yesterday, Last 24 h, Last 7 days) for one-click filtering, plus advanced search by camera(s) and custom time range. Reset button clears all filters at once. Active filter summary always visible. Per-event thumbnails and smart detection labels (person, vehicle, animal, etc.) shown for each recording.
- **PTZ Control** &mdash; Pan/Tilt, Zoom, Focus, Preset, and Patrol controls for PTZ-capable cameras, in the same per-slot hover toolbar as Live View's audio controls. Picking a patrol asks Surveillance Station to run that saved route; the NAS drives the camera, so it keeps going after you switch cameras or quit. Routes are created and edited in Surveillance Station itself, and there is no stop control, the same as Synology's own clients.
- **Snapshots** &mdash; Browse saved snapshots, filter by camera and time range, view, download, or delete. Take a snapshot straight from a Live View slot's right-click menu, which saves it to the snapshot database and offers a local copy. The full-size viewer supports scroll-to-zoom and click-and-drag panning.
- **Time Lapse** &mdash; Browse, play back, download, lock/unlock, and delete Smart Time Lapse recordings. Filter by time lapse task.
- **Events & Alerts** &mdash; Browse real events decoded from each camera's own detected categories (motion, audio, tampering, person/vehicle/pet, and more, brand-dependent &mdash; see `EVENT_BITMASK.md`) with their type and time, filter by event type (quick filter plus a multi-select in advanced search, matching Any or All of the selected types) and by camera. Notification bell with unread badge and alert popover, polled every 30 seconds.
- **Home Mode** &mdash; Toggle Surveillance Station home mode directly from the header bar.
- **License Management** &mdash; View, add, and delete camera licenses. Online and offline activation.
- **Session Persistence** &mdash; Grid layout, active page, camera assignments, sidebar visibility, and recording search filters (including time presets) are restored on restart. Critical changes are flushed to disk immediately for crash resilience.
- **Two-Factor Authentication** &mdash; MFA/OTP login support. When 2FA is enabled on your Synology account, the client prompts for a 6-digit authenticator code and optionally registers as a trusted device to skip OTP on future logins.
- **Multi-Profile** &mdash; Save multiple NAS connection profiles and switch between them from the login screen.
- **Secure Credentials** &mdash; Passwords stored in your system keyring (GNOME Keyring, KWallet, macOS Keychain).
- **Theming** &mdash; Auto (follow OS), dark, or light theme selectable from the header bar.
- **About & Updates** &mdash; An About page shows the version, license, and repository links. On login the client checks the GitHub releases page once for a newer version and, if one exists, marks the About entry until you have seen it.

---

## Quick Start

### AppImage (Linux, no install needed)

Download the latest AppImage for your architecture from the
[Releases](https://github.com/renaudallard/surveillance-station-client/releases/latest)
page:

```sh
chmod +x Surveillance-*-x86_64.AppImage
./Surveillance-*-x86_64.AppImage
```

Available for **x86_64** and **aarch64**. A new release with AppImages is built
automatically every time the version is bumped.

### From source

1. Install [system dependencies](#system-packages) for your distro
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

---

## Usage

```sh
surveillance            # launch the application
surveillance --debug    # enable debug logging to stderr
python -m surveillance  # run directly from the source tree
```

Debug logs automatically redact passwords, session tokens, and usernames.

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

After connecting, the camera list appears in the sidebar. Click a camera to
start its live stream. Use the navigation buttons at the bottom of the sidebar
to switch between **Live View**, **Recordings**, **Snapshots**, **Events**,
**Time Lapse**, and **Licenses**; the button for the current page stays
highlighted. The header bar shows the current page name and holds the panel
toggle on the left, which hides or shows the whole sidebar.

On **Live View**, the grid button in the header bar selects the layout and can
clear the current one. Click a slot to select it, then click a camera to fill
it, or click **Empty Slot** at the bottom of the camera list to empty it again.
Clicking a camera with no slot selected switches to 1&times;1 and shows only
that camera. Right-click a slot for **Take Snapshot**, **Open in 1x1 Layout**,
and **Clear Slot**.

### Keyboard shortcuts

| Key | Action |
|---|---|
| `Ctrl+Q` | Quit |

---

## Configuration

Configuration is stored in TOML format following the XDG base directory
specification:

```
~/.config/surveillance-station/config.toml
```

<details>
<summary><b>Example configuration</b></summary>

```toml
[general]
default_profile = "home-nas"
theme = "auto"                  # "auto" (follow OS), "dark", or "light"
sidebar_visible = true          # camera sidebar shown at startup
dismissed_update_version = ""   # release tag whose update notice was dismissed
poll_interval_cameras = 30      # seconds, minimum 5
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
# search_time_preset = "today"  # "today", "yesterday", "last24h", "last7d", or ""

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

[camera_protocols]
# Stream protocol per camera ID:
# auto, websocket, mjpeg, rtsp_over_http, rtsp, multicast, direct
# "auto" is the same as "websocket"; there is no fallback between
# protocols, so pick one explicitly if WebSocket does not work.
# "websocket" uses a WebSocket stream bridged to mpv via an in-memory pipe,
# muxing in real audio via ffmpeg when the camera's audio codec is PCMU or AAC.
# "direct" uses the URL from [camera_overrides].
# 5 = "direct"

[profiles.home-nas]
host = "192.168.1.100"
port = 5001
https = true
verify_ssl = false
```
</details>

The `[session]` section is managed automatically &mdash; the application
restores the grid layout, active page, and camera assignments from the previous
session on restart.

Stream protocols and direct RTSP overrides can also be configured from the UI:
right-click a camera in the sidebar to choose the protocol.

Credentials are **never** stored in the config file. They are kept in the
system keyring under the service name `surveillance-station`.

When two-factor authentication (2FA/MFA) is enabled on the Synology account, the
client will prompt for a 6-digit OTP code after entering credentials. Checking
"Trust this device" stores a device token in the profile so subsequent logins
skip the OTP step. If the trust is revoked on the NAS, the client will prompt
for OTP again automatically.

---

## Dependencies

### System packages

These must be installed **before** the Python dependencies.

<details>
<summary><b>Debian / Ubuntu</b></summary>

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
</details>

<details>
<summary><b>Arch Linux</b></summary>

```sh
sudo pacman -S gtk4 mpv portaudio ffmpeg python-gobject python-cairo
```
</details>

<details>
<summary><b>Fedora</b></summary>

```sh
sudo dnf install \
    gtk4-devel \
    mpv-devel \
    portaudio \
    ffmpeg \
    python3-gobject \
    python3-cairo
```
</details>

<details>
<summary><b>openSUSE</b></summary>

```sh
sudo zypper install \
    gtk4-devel \
    mpv-devel \
    libportaudio2 \
    ffmpeg \
    python3-gobject \
    python3-gobject-cairo
```
</details>

<details>
<summary><b>FreeBSD</b></summary>

```sh
pkg install gtk4 mpv portaudio ffmpeg py311-gobject3 py311-cairo
```
</details>

<details>
<summary><b>OpenBSD</b></summary>

```sh
pkg_add gtk4 mpv portaudio ffmpeg py3-gobject3 py3-cairo
```
</details>

### Python packages

**Python 3.11** or later is required. These are installed automatically by `pip`:

| Package | Purpose |
|---|---|
| `PyGObject` >= 3.50 | GTK4 bindings with native asyncio integration |
| `httpx[http2]` >= 0.27 | Async HTTP/2 client for Synology REST API |
| `python-mpv` >= 1.0 | libmpv bindings for video rendering |
| `PyOpenGL` >= 3.1 | OpenGL context for mpv render in GTK4 GLArea |
| `keyring` >= 25.0 | Secure credential storage |
| `tomli-w` >= 1.0 | TOML config writing |
| `websockets` >= 13.0 | WebSocket stream bridge for live view |
| `sounddevice` >= 0.5 | PortAudio bindings for push-to-talk mic capture |
| `cryptography` >= 42.0 | AES for offline license activation |

---

## Architecture

```
┌─────────────────────────────────────────┐
│  UI Layer          GTK4 widgets         │
│  window, sidebar, liveview, recordings, │
│  player, slot_toolbar, snapshots,       │
│  events, timelapse, licenses,           │
│  notifications                          │
├─────────────────────────────────────────┤
│  Service Layer     domain logic         │
│  camera, live, recording, ptz,          │
│  snapshot, event, homemode, license,    │
│  timelapse                              │
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

<details>
<summary><b>Project structure</b></summary>

```
surveillance-station-client/
├── pyproject.toml
├── README.md
├── surveillance.1                      man page
├── EVENT_BITMASK.md                    event_map bitmask reverse-engineering reference
├── build-appimage.sh                   AppImage build script
├── appimage_entry.py                   PyInstaller entry point
├── scripts/
│   └── dump_event_map.py               diagnostic tool for extending EVENT_BITMASK.md
├── data/
│   └── org.surveillance.desktop
├── .github/workflows/
│   ├── lint.yml                        CI: ruff + mypy
│   └── release.yml                     AppImage build + GitHub release
├── src/surveillance/
│   ├── __main__.py                     entry point
│   ├── app.py                          Gtk.Application
│   ├── config.py                       TOML config + XDG paths
│   ├── credentials.py                  keyring wrapper
│   ├── data/
│   │   ├── style.css
│   │   └── event_bits.json             event_map bit -> label table (see EVENT_BITMASK.md)
│   ├── api/
│   │   ├── client.py                   SurveillanceAPI (httpx)
│   │   ├── auth.py                     login / logout / SID
│   │   └── models.py                   dataclasses
│   ├── services/
│   │   ├── camera.py                   camera list
│   │   ├── live.py                     stream URL resolution
│   │   ├── ws_bridge.py               WebSocket-to-pipe bridge
│   │   ├── recording.py               recording management
│   │   ├── ptz.py                      PTZ commands
│   │   ├── ptt.py                      push-to-talk session (AudioOut WebSocket)
│   │   ├── g711.py                     G.711 mu-law encoder
│   │   ├── snapshot.py                 snapshot management
│   │   ├── event.py                    events + alerts
│   │   ├── event_bits.py               event_map bitmask decoder (see EVENT_BITMASK.md)
│   │   ├── homemode.py                 home mode toggle
│   │   ├── license.py                  license management
│   │   └── timelapse.py                time lapse management
│   ├── ui/
│   │   ├── window.py                   main window
│   │   ├── login.py                    login dialog
│   │   ├── headerbar.py                header bar controls
│   │   ├── sidebar.py                  camera list sidebar
│   │   ├── liveview.py                 live stream grid
│   │   ├── slot_toolbar.py             per-slot hover toolbar (audio, PTT, PTZ, snapshot)
│   │   ├── mpv_widget.py               GLArea + mpv render
│   │   ├── recordings.py               recording browser
│   │   ├── advanced_search.py          advanced search dialog (shared by Recordings/Snapshots/Events)
│   │   ├── player.py                   playback controls
│   │   ├── snapshots.py                snapshot browser
│   │   ├── events.py                   event list
│   │   ├── licenses.py                 license management
│   │   ├── timelapse.py                time lapse browser
│   │   ├── notifications.py            alert popover
│   │   └── labels.py                   combo label helpers shared by the browser pages
│   └── util/
│       └── async_bridge.py             GLib + asyncio bridge
└── tests/
    ├── conftest.py
    ├── test_api_client.py
    ├── test_config.py
    ├── test_event_bits.py
    ├── test_liveview_persistence.py
    ├── test_models.py
    ├── test_services.py
    └── test_ui_behavior.py
```
</details>

---

## Development

CI runs automatically on push and pull requests to `main`:

| Workflow | Trigger | What it does |
|---|---|---|
| [`lint.yml`](.github/workflows/lint.yml) | push / PR to `main` | ruff check, ruff format, mypy |
| [`release.yml`](.github/workflows/release.yml) | version bump on `main` | Build AppImages (x86_64 + aarch64), create GitHub release |

### Running checks locally

```sh
pip install -e ".[dev]"

ruff check src/ tests/       # lint (rules: E, F, W, I, B, S, SIM, RET, PLR, PLW, PLC, TRY, RUF)
ruff format src/ tests/       # format
mypy src/surveillance/        # type check
pytest tests/ -v              # tests
```

### Building an AppImage locally

```sh
./build-appimage.sh
```

This produces `Surveillance-<version>-<arch>.AppImage` in the project root.
Requires `libmpv`, `libportaudio2`, `ffmpeg`, GTK4 development files, and
`libfuse2` on the build machine.

---

## Synology API Reference

<details>
<summary><b>Endpoints used by this client</b></summary>

| API | Purpose |
|---|---|
| `SYNO.API.Info` | Discover available APIs and CGI paths |
| `SYNO.API.Auth` | Login / logout / session management |
| `SYNO.SurveillanceStation.Camera` | Camera list, snapshots, live view paths |
| `SYNO.SurveillanceStation.PTZ` | Pan, tilt, zoom, presets, patrols |
| `SYNO.SurveillanceStation.AudioOut` | Push-to-talk busy-check (CheckOccupied) — the actual audio upload is a raw WebSocket, not a REST call |
| `SYNO.SurveillanceStation.Recording` | List, stream, download recordings |
| `SYNO.SurveillanceStation.RecordingPicker` | Per-camera event intervals for the Events page, decoded bit-by-bit into brand-aware categories &mdash; see `EVENT_BITMASK.md` |
| `SYNO.SurveillanceStation.SnapShot` | List, take, download, delete snapshots |
| `SYNO.SurveillanceStation.TimeLapse` | Time lapse task listing |
| `SYNO.SurveillanceStation.TimeLapse.Recording` | Time lapse recording management |
| `SYNO.SurveillanceStation.Event` | Motion and alarm event history |
| `SYNO.SurveillanceStation.Notification` | Alert list, unread count, mark read |
| `SYNO.SurveillanceStation.HomeMode` | Get/set home mode status |
| `SYNO.SurveillanceStation.License` | License management |
| `SYNO.SurveillanceStation.Info` | NAS device info |

</details>

---

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for common problems
(HTTP 502, recording playback never starts, download failures, segfaults,
Ubuntu 24.04 / AppImage notes).

---

## Support

If you find this project useful, you can support its development:

[![PayPal](https://img.shields.io/badge/PayPal-Donate-blue?logo=paypal)](https://www.paypal.me/RenaudAllard)

---

## Disclaimer

This project is **not affiliated with, endorsed by, or sponsored by Synology Inc.**
Synology, Surveillance Station, and DiskStation Manager (DSM) are trademarks of
Synology Inc. This software is an independent, third-party client that interacts
with the publicly documented Synology Web API. Use it at your own risk.

---

## License

BSD-2-Clause &mdash; see [LICENSE](https://github.com/renaudallard/surveillance-station-client/blob/main/pyproject.toml) for details.

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
