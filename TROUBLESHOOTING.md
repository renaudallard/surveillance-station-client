# Troubleshooting Guide

## Synology BC500 / Surveillance Station on Ubuntu 24.04

---

### Recommended Protocols (BC500 Cameras)

| Use Case | Recommended Protocol | Notes |
|---|---|---|
| Live view | **WebSocket (Auto)** | Best latency; falls back automatically |
| Live view (fallback) | MJPEG | Lower quality, very compatible |
| Live view (fallback) | RTSP over HTTP | Good for NAT/firewall traversal |
| Recording playback | Stream URL (automatic) | Handled internally by the app |

**BC500 default codec is H.265 (HEVC).** If mpv cannot decode it in software,
try enabling hardware decoding (the app uses `hwdec=auto` by default). If
hardware decoding fails, see *H.264 vs H.265* below.

---

### HTTP 502 on WebSocket Live View

**Symptom:**
```
websockets.exceptions.InvalidStatus: server rejected WebSocket connection: HTTP 502
ERROR surveillance.services.ws_bridge: WebSocket bridge error
```

**Cause:** Surveillance Station's internal WebSocket proxy returned HTTP 502.
This happens when:
- The camera stream is not yet ready (camera booting, motion event just ended)
- The NAS is under heavy load
- Too many concurrent streams are open

**What to do:**
1. Wait 10–30 seconds and click the camera tile again.
2. Try a different protocol in Settings → Camera → Stream protocol (MJPEG or RTSP over HTTP).
3. Reboot the camera from Surveillance Station if the issue persists.
4. Check *Control Panel → Log Center* on DSM for NAS-side errors.

---

### RTSP / mpv Playback Failure

**Symptom:** Recording opens but stays black, or mpv log shows errors like:
```
TypeError: Tried to get/set mpv property using wrong format, or passed invalid value
options/demuxer-lavf-probesize
value: b'0'
```

**Cause:** A python-mpv version mismatch causes integer option `0` to be sent
as a byte-string, which libmpv rejects for certain INT64 properties.

**Fixed in this release.** The app now uses `safe_set_mpv_option()` which
catches and logs any rejected mpv option without crashing.

If you still see mpv errors:
1. Update python-mpv: `pip install --upgrade python-mpv`
2. Ensure libmpv is installed: `sudo apt install libmpv2` (Ubuntu 24.04)
3. Run the app from a terminal and check the log output.

---

### Recording Playback: Video Never Starts

**Symptom:** Player dialog opens but video area stays black. After ~7 seconds
an error dialog appears: "Playback Failed".

**Likely causes and fixes:**

1. **Stream URL expired** — The URL is valid for a short time.
   Close the dialog and click Play again.

2. **H.265 without hardware decoding** — BC500 records in H.265 by default.
   ```
   # Check if hardware decoding is working:
   mpv --hwdec=auto <stream-url>
   ```
   If it fails, try forcing H.264 in Surveillance Station:
   *Surveillance Station → IP Camera → Edit → Video → Codec → H.264*

3. **mpv cannot find the codec** — Ensure full codec support is installed:
   ```bash
   sudo apt install ubuntu-restricted-extras
   # or specifically:
   sudo apt install libavcodec-extra
   ```

4. **AppImage codec isolation** — The AppImage bundles its own libraries.
   If running via AppImage and codecs are missing, rebuild the AppImage after
   installing the missing libs, or use the pip-installed version instead.

---

### Testing H.264 Instead of H.265 (BC500)

1. In Surveillance Station web UI, go to *IP Camera → [camera name] → Edit*
2. Under *Video* tab, set **Codec** to **H.264**
3. Save and wait for the camera to reconnect
4. Test live view and recording playback in the client

H.264 has wider software decoder support and uses less CPU on older hardware.
H.265 is more efficient at the same quality but requires hardware decoding on
most systems.

---

### Download Fails: "Server returned HTML page"

**Cause:** The session expired between the time you logged in and tried to
download. Synology DSM redirects expired sessions to the login page.

**Fix:** Close the app, reopen it, log in again, then retry the download.

---

### Download Fails: "API returned error code 105"

Error code 105 = *Insufficient user privilege*.

The logged-in account does not have permission to download recordings.
Ask a Surveillance Station administrator to grant the account download access:
*Surveillance Station → User Privilege → [user] → Recording → Allow download*

---

### Segmentation Fault

A segfault in the mpv/OpenGL rendering path usually means:
- A version mismatch between python-mpv, libmpv, and the OpenGL driver
- GPU driver crash (especially with NVIDIA on Wayland)

**Try:**
```bash
# Force X11 backend instead of Wayland
GDK_BACKEND=x11 surveillance

# Disable hardware decoding
# Edit ~/.config/surveillance/config.toml and set hwdec = "no"
# (or add the option manually if not present)
```

For the NVIDIA RTX 3050 on Ubuntu 24.04:
```bash
sudo apt install nvidia-driver-550   # or latest stable
# Check driver version:
nvidia-smi
```

---

### Ubuntu 24.04 / AppImage Notes

- The AppImage uses its own bundled Python and GTK. Do **not** mix system
  `python3-mpv` with the AppImage.
- If GTK theming looks wrong inside the AppImage, set:
  ```bash
  GTK_THEME=Adwaita surveillance.AppImage
  ```
- PyGObject ≥ 3.50 (GTK 4.14) is required for `Gtk.AlertDialog`. Ubuntu 24.04
  ships PyGObject 3.48 from the system packages. The AppImage bundles a newer
  version. If running from pip, ensure your venv has `PyGObject>=3.50`.

---

### Collecting Debug Logs

Run the client with verbose logging:
```bash
surveillance --log-level debug 2>&1 | tee ~/surveillance-debug.log
```

Relevant log namespaces:
- `surveillance.services.ws_bridge` — WebSocket bridge errors
- `surveillance.services.recording` — recording download issues
- `surveillance.ui.mpv_widget` — mpv option / render errors
- `surveillance.ui.player` — playback start failures
