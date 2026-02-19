# Surveillance Station Client

> Native GTK4 desktop client for **Synology Surveillance Station**

No browser needed. Connect directly to your Synology NAS and get live camera
feeds, recording playback, PTZ control, snapshots, event alerts, and home mode
management -- all from a lightweight native desktop application.

---

## Features

| Feature | Description |
|---|---|
| **Live View** | Real-time camera streams in 1x1, 2x2, 3x3, or 4x4 grid layouts. Hardware-accelerated rendering via mpv + OpenGL. Works on X11 and Wayland. |
| **Recordings** | Browse, filter by camera, play back with full transport controls (seek, pause, volume), and download to disk. Camera snapshot thumbnails and smart detection labels (person, vehicle, animal, etc.) shown for each recording. |
| **PTZ Control** | 8-direction pad, zoom in/out, speed slider, preset positions, and patrol routes for PTZ-capable cameras. |
| **Snapshots** | Take live snapshots from any camera, browse saved snapshots, download or delete. |
| **Time Lapse** | Browse, play back, download, lock/unlock, and delete Smart Time Lapse recordings. Filter by time lapse task. |
| **Events & Alerts** | View motion detection and alarm events with smart detection labels. Notification bell with unread badge and alert popover, polled every 30 seconds. |
| **Home Mode** | Toggle Surveillance Station home mode directly from the header bar. |
| **License Management** | View, add, and delete Surveillance Station camera licenses. Supports both online activation (via NAS) and offline activation (direct to Synology). |
| **Session Persistence** | Grid layout, active page, and camera assignments are restored on restart. |
| **Multi-Profile** | Save multiple NAS connection profiles. Switch between them from the login screen. |
| **Secure Credentials** | Passwords stored in your system keyring (GNOME Keyring, KWallet, macOS Keychain). |

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

### Python

**Python 3.11** or later is required.

The following Python packages are installed automatically by `pip`:

| Package | Version | Purpose |
|---|---|---|
| `PyGObject` | >= 3.50 | GTK4 bindings with native asyncio integration |
| `httpx[http2]` | >= 0.27 | Async HTTP/2 client for Synology REST API |
| `python-mpv` | >= 1.0 | libmpv bindings for video rendering |
| `PyOpenGL` | >= 3.1 | OpenGL context for mpv render in GTK4 GLArea |
| `keyring` | >= 25.0 | Secure credential storage |
| `tomli-w` | >= 1.0 | TOML config writing |

---

## Installation

### From source (recommended)

```sh
git clone https://github.com/renaudallard/synology-surveillance-station-client.git
cd synology-surveillance-station-client
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install .
```

> `--system-site-packages` is required so the venv can access the system-installed
> PyGObject and cairo bindings, which cannot be built via pip without extensive
> C development headers.

### Editable install (for development)

```sh
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

---

## Usage

### Launch

```sh
# If installed via pip
surveillance

# Or run directly from the source tree
python -m surveillance

# Enable debug logging
surveillance --debug
```

### First connection

On launch, a login dialog appears:

| Field | Description | Default |
|---|---|---|
| **Profile name** | A label for this connection (e.g. `home-nas`) | hostname |
| **Host** | NAS IP address or hostname | -- |
| **Port** | DSM port | `5001` |
| **Use HTTPS** | Enable HTTPS (recommended) | on |
| **Verify SSL** | Validate the SSL certificate (disable for self-signed) | off |
| **Username** | DSM user with Surveillance Station permissions | -- |
| **Password** | DSM password | -- |
| **Remember credentials** | Store in system keyring | on |

After connecting you will see the camera list in the sidebar. Click a camera
to start its live stream. Use the navigation buttons at the bottom of the
sidebar to switch between Live View, Recordings, Snapshots, Events, Time Lapse, and Licenses.

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

Example:

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
last_page = "live"             # last active page (live, recordings, snapshots, events)

[session.layout_cameras]
# Camera IDs per layout (0 = empty slot).  Each layout remembers its
# own assignment independently so switching between layouts restores
# the previous arrangement.
"1x1" = [1]
"2x2" = [1, 3, 0, 5]
"3x3" = [1, 3, 7, 0, 5, 8, 2, 0, 0]

[camera_overrides]
# Direct RTSP URLs keyed by Surveillance Station camera ID.
# Use this when Synology's RTSP proxy corrupts a camera's stream
# (e.g. Reolink Duo 3 PoE h265).  The camera ID can be found in
# the sidebar tooltip or the debug log.
# 5 = "rtsp://admin:password@192.168.1.50:554/h265Preview_01_main"

[camera_protocols]
# Stream protocol per camera ID.  Supported values:
# auto, rtsp, rtsp_over_http, mjpeg, multicast, direct
# When set to "direct", the URL from [camera_overrides] is used.
# 5 = "direct"

[profiles.home-nas]
host = "192.168.1.100"
port = 5001
https = true
verify_ssl = false
```

The `[session]` section is managed automatically. On each restart the application
restores the grid layout, active page, and camera assignments from the previous
session.

The `[camera_overrides]` and `[camera_protocols]` sections can also be
configured from the UI: right-click a camera in the sidebar to choose the
stream protocol.  Available protocols: Auto, RTSP, RTSP over HTTP, MJPEG,
Multicast, and Direct RTSP URL (bypasses Synology's proxy entirely — useful
for cameras whose stream Synology corrupts, e.g. Reolink Duo 3 PoE).

Credentials are **never** stored in the config file. They are kept in the
system keyring under the service name `surveillance-station`.

---

## Architecture

```
┌─────────────────────────────────────────┐
│  UI Layer          GTK4 widgets         │
│  window, sidebar, liveview, recordings, │
│  player, ptz, snapshots, events,        │
│  timelapse, licenses                    │
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

### Project structure

```
surveillance/
├── pyproject.toml
├── README.md
├── surveillance.1                      man page
├── data/
│   ├── org.surveillance.desktop
│   └── style.css
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
    ├── test_api_client.py
    ├── test_models.py
    ├── test_config.py
    └── test_services.py
```

---

## Development

```sh
# activate the venv
source .venv/bin/activate

# lint
ruff check src/ tests/

# format
ruff format src/ tests/

# type check
mypy src/surveillance/

# run tests
pytest tests/ -v
```

---

## Synology API Reference

This client uses the following Synology Web API endpoints:

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

---

## Disclaimer

This project is **not affiliated with, endorsed by, or sponsored by Synology Inc.**
Synology, Surveillance Station, and DiskStation Manager (DSM) are trademarks of
Synology Inc. This software is an independent, third-party client that interacts
with the publicly documented Synology Web API. Use it at your own risk.

---

## License

BSD-2-Clause

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
