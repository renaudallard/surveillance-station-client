# Troubleshooting

Common problems when running the Surveillance Station client and what to do
about them.

## HTTP 502 in the WebSocket live view

```
ERROR surveillance.services.ws_bridge: WebSocket bridge error:
  HTTP 502 (NAS overloaded or camera stream not ready)
```

Surveillance Station's WebSocket proxy returned a 502. Typical causes:

- The camera stream is not ready yet (camera booting, motion event just ended).
- The NAS is under heavy load.
- Too many concurrent streams.

What to try:

1. Wait 10-30 seconds and click the camera tile again.
2. Right-click the camera in the sidebar and switch to MJPEG or RTSP over HTTP.
3. Check *Control Panel > Log Center* on DSM for NAS-side errors.

## A live view slot keeps reconnecting

```
WARNING surveillance.ui.liveview: Stream for Front Door dropped after 12s
  (ConnectionClosedError: no close frame received or sent); reconnecting in 2s
```

The NAS closed the WebSocket streaming session without a closing handshake.
The slot header shows *(reconnecting)* and the stream is restarted with a
growing delay; after five attempts in a row it stops and shows *(stream lost)*.

If a camera does this constantly, right-click it in the sidebar and switch its
protocol to RTSP, which does not go through the WebSocket streaming backend.

## Recording playback never starts

The player dialog opens, the video area stays black, and after seven seconds
an alert appears: *Playback failed*.

Likely causes:

1. **Stream URL expired** - the URL is only valid for a short time.
   Close the dialog and click Play again.
2. **H.265 without hardware decoding** - BC500 records in H.265 by default.
   Run `mpv --hwdec=auto <stream-url>` to verify hardware decoding works,
   or switch the camera to H.264 in Surveillance Station
   (*IP Camera > Edit > Video > Codec*).
3. **Missing codec** - install `libavcodec-extra` (or your distro's equivalent).
4. **Download workaround** - the download button often succeeds when playback
   does not, since the file is decoded locally with the user's full codec set.

## Recording download fails

If a download fails an alert appears naming the camera and the underlying
error. The most common cases:

- **"server returned HTML"** - the DSM session expired and the download was
  redirected to the login page. Quit the app, log back in, retry.
- **"empty response"** - same root cause; nothing was returned.
- **error code 105** - the logged-in account lacks download permission. Grant
  it in *Surveillance Station > User Privilege > [user] > Recording*.

## Segmentation fault

Almost always a mismatch between python-mpv, libmpv, and the OpenGL driver,
or a GPU driver crash (especially NVIDIA on Wayland).

Try:

```sh
GDK_BACKEND=x11 surveillance     # force X11
```

For NVIDIA on Ubuntu, ensure the proprietary driver is installed and current:

```sh
sudo apt install nvidia-driver-550
nvidia-smi
```

## Ubuntu 24.04 / AppImage

- PyGObject >= 3.50 is required for `Gtk.AlertDialog`. Ubuntu 24.04 ships
  3.48 from the system packages. The AppImage bundles a newer version; from
  pip, install into a venv that pulls `PyGObject>=3.50`.
- Do not mix the system `python3-mpv` with the AppImage. The AppImage uses
  its own bundled Python and GTK.

## Collecting debug logs

```sh
surveillance --debug 2>&1 | tee ~/surveillance-debug.log
```

Useful log namespaces:

- `surveillance.services.ws_bridge` - WebSocket bridge errors (classified)
- `surveillance.services.recording` - recording download issues
- `surveillance.ui.mpv_widget` - mpv option / render errors
- `surveillance.ui.player` - playback start failures
