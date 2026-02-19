<h1 align="center">Surveillance Station Client</h1>

<p align="center">
  <strong>Native GTK4 desktop client for Synology Surveillance Station</strong>
</p>

<p align="center">
  <a href="https://github.com/renaudallard/synology-surveillance-station-client/actions/workflows/lint.yml"><img src="https://github.com/renaudallard/synology-surveillance-station-client/actions/workflows/lint.yml/badge.svg" alt="Lint & Type Check"></a>
  <a href="https://github.com/renaudallard/synology-surveillance-station-client/releases/latest"><img src="https://img.shields.io/github/v/release/renaudallard/synology-surveillance-station-client?label=release" alt="Latest Release"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/GTK-4-green" alt="GTK4">
  <img src="https://img.shields.io/github/license/renaudallard/synology-surveillance-station-client" alt="License">
</p>

<p align="center">
  No browser needed. Connect directly to your Synology NAS and get live camera
  feeds, recording playback, PTZ control, snapshots, event alerts, and home mode
  management &mdash; all from a lightweight native desktop application.
</p>

---

## Features

- **Live View** &mdash; Real-time camera streams in 1&times;1, 2&times;2, 3&times;3, or 4&times;4 grid layouts. Hardware-accelerated rendering via mpv + OpenGL. Works on X11 and Wayland.
- **Recordings** &mdash; Browse, filter by camera, play back with full transport controls (seek, pause, volume), and download to disk. Search by camera(s) and time range. Snapshot thumbnails and smart detection labels (person, vehicle, animal, etc.) shown for each recording.
- **PTZ Control** &mdash; Direction pad, zoom in/out, preset positions, and patrol routes. Appears automatically below the live view when a PTZ-capable camera is active.
- **Snapshots** &mdash; Take live snapshots from any camera, browse saved snapshots, download or delete.
- **Time Lapse** &mdash; Browse, play back, download, lock/unlock, and delete Smart Time Lapse recordings. Filter by time lapse task.
- **Events & Alerts** &mdash; View motion detection and alarm events with smart detection labels. Notification bell with unread badge and alert popover, polled every 30 seconds.
- **Home Mode** &mdash; Toggle Surveillance Station home mode directly from the header bar.
- **License Management** &mdash; View, add, and delete camera licenses. Online and offline activation.
- **Session Persistence** &mdash; Grid layout, active page, and camera assignments are restored on restart.
- **Multi-Profile** &mdash; Save multiple NAS connection profiles and switch between them from the login screen.
- **Secure Credentials** &mdash; Passwords stored in your system keyring (GNOME Keyring, KWallet, macOS Keychain).
- **Theming** &mdash; Auto (follow OS), dark, or light theme selectable from the header bar.

---

## Quick Start

### AppImage (Linux, no install needed)

Download the latest AppImage for your architecture from the
[Releases](https://github.com/renaudallard/synology-surveillance-station-client/releases/latest)
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
git clone https://github.com/renaudallard/synology-surveillance-station-client.git
cd synology-surveillance-station-client
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
**Time Lapse**, and **Licenses**.

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
poll_interval_cameras = 30      # seconds
poll_interval_alerts = 30
poll_interval_homemode = 60
snapshot_dir = "/home/user/.local/share/surveillance-station/snapshots"

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

[camera_overrides]
# Direct RTSP URLs keyed by camera ID.
# Use when Synology's RTSP proxy corrupts a stream (e.g. Reolink Duo 3 PoE h265).
# 5 = "rtsp://admin:password@192.168.1.50:554/h265Preview_01_main"

[camera_protocols]
# Stream protocol per camera ID:
# auto, rtsp, rtsp_over_http, mjpeg, multicast, direct
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
    python3-gi \
    python3-gi-cairo \
    python3-cairo
```
</details>

<details>
<summary><b>Arch Linux</b></summary>

```sh
sudo pacman -S gtk4 mpv python-gobject python-cairo
```
</details>

<details>
<summary><b>Fedora</b></summary>

```sh
sudo dnf install \
    gtk4-devel \
    mpv-devel \
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
    python3-gobject \
    python3-gobject-cairo
```
</details>

<details>
<summary><b>FreeBSD</b></summary>

```sh
pkg install gtk4 mpv py311-gobject3 py311-cairo
```
</details>

<details>
<summary><b>OpenBSD</b></summary>

```sh
pkg_add gtk4 mpv py3-gobject3 py3-cairo
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

---

## Architecture

```
┌─────────────────────────────────────────┐
│  UI Layer          GTK4 widgets         │
│  window, sidebar, liveview, recordings, │
│  player, ptz, snapshots, events,        │
│  timelapse, licenses, notifications     │
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
- **asyncio** is integrated into GLib via PyGObject 3.50+ native support
- **mpv threads** bridge back to the main thread via `GLib.idle_add()`

Video is rendered through mpv's OpenGL render API into a `Gtk.GLArea` widget,
which works on both X11 and Wayland without window ID embedding.

<details>
<summary><b>Project structure</b></summary>

```
synology-surveillance-station-client/
├── pyproject.toml
├── README.md
├── surveillance.1                      man page
├── build-appimage.sh                   AppImage build script
├── appimage_entry.py                   PyInstaller entry point
├── data/
│   ├── org.surveillance.desktop
│   └── style.css
├── .github/workflows/
│   ├── lint.yml                        CI: ruff + mypy
│   └── release.yml                     AppImage build + GitHub release
├── src/surveillance/
│   ├── __main__.py                     entry point
│   ├── app.py                          Gtk.Application
│   ├── config.py                       TOML config + XDG paths
│   ├── credentials.py                  keyring wrapper
│   ├── api/
│   │   ├── client.py                   SurveillanceAPI (httpx)
│   │   ├── auth.py                     login / logout / SID
│   │   └── models.py                   dataclasses
│   ├── services/
│   │   ├── camera.py                   camera list + status
│   │   ├── live.py                     RTSP URL resolution
│   │   ├── recording.py               recording management
│   │   ├── ptz.py                      PTZ commands
│   │   ├── snapshot.py                 snapshot management
│   │   ├── event.py                    events + alerts
│   │   ├── homemode.py                 home mode toggle
│   │   ├── license.py                  license management
│   │   └── timelapse.py                time lapse management
│   ├── ui/
│   │   ├── window.py                   main window
│   │   ├── login.py                    login dialog
│   │   ├── headerbar.py                header bar controls
│   │   ├── sidebar.py                  camera list sidebar
│   │   ├── liveview.py                 live stream grid
│   │   ├── mpv_widget.py               GLArea + mpv render
│   │   ├── recordings.py               recording browser
│   │   ├── recording_search.py         recording search dialog
│   │   ├── player.py                   playback controls
│   │   ├── ptz_controls.py             PTZ direction pad
│   │   ├── snapshots.py                snapshot browser
│   │   ├── events.py                   event list
│   │   ├── licenses.py                 license management
│   │   ├── timelapse.py                time lapse browser
│   │   └── notifications.py            alert popover
│   └── util/
│       └── async_bridge.py             GLib + asyncio bridge
└── tests/
    ├── conftest.py
    ├── test_api_client.py
    ├── test_models.py
    ├── test_config.py
    └── test_services.py
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
Requires `libmpv`, GTK4 development files, and `libfuse2` on the build machine.

---

## Synology API Reference

<details>
<summary><b>Endpoints used by this client</b></summary>

| API | Purpose |
|---|---|
| `SYNO.API.Info` | Discover available APIs and CGI paths |
| `SYNO.API.Auth` | Login / logout / session management |
| `SYNO.SurveillanceStation.Camera` | Camera list, info, enable/disable, snapshots, live view paths |
| `SYNO.SurveillanceStation.PTZ` | Pan, tilt, zoom, presets, patrols |
| `SYNO.SurveillanceStation.Recording` | List, stream, download, delete recordings |
| `SYNO.SurveillanceStation.SnapShot` | List, download, delete snapshots |
| `SYNO.SurveillanceStation.TimeLapse` | Time lapse task listing |
| `SYNO.SurveillanceStation.TimeLapse.Recording` | Time lapse recording management |
| `SYNO.SurveillanceStation.Event` | Motion and alarm event history |
| `SYNO.SurveillanceStation.Notification` | Alert list, unread count, mark read |
| `SYNO.SurveillanceStation.HomeMode` | Get/set home mode status |
| `SYNO.SurveillanceStation.License` | License management |
| `SYNO.SurveillanceStation.Info` | NAS device info |

</details>

---

## Disclaimer

This project is **not affiliated with, endorsed by, or sponsored by Synology Inc.**
Synology, Surveillance Station, and DiskStation Manager (DSM) are trademarks of
Synology Inc. This software is an independent, third-party client that interacts
with the publicly documented Synology Web API. Use it at your own risk.

---

## License

BSD-2-Clause &mdash; see [LICENSE](https://github.com/renaudallard/synology-surveillance-station-client/blob/main/pyproject.toml) for details.

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
