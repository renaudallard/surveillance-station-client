# Troubleshooting

Common problems when running the Surveillance Station client and what to do
about them.

Log excerpts below are quoted without the timestamp every line starts with,
and wrapped to fit. See "Collecting debug logs" at the end for how to capture
one.

## HTTP 502 in the WebSocket live view

```
ERROR surveillance.services.ws_bridge: WebSocket for entree failed to
  establish 5 times in a row — giving up: HTTP 502 (NAS overloaded or
  camera stream not ready)
```

Surveillance Station's WebSocket proxy returned a 502. Typical causes:

- The camera stream is not ready yet (camera booting, motion event just ended).
- The NAS is under heavy load.
- Too many concurrent streams.

What to try:

1. Wait 10-30 seconds and click the camera tile again.
2. Right-click the camera in the sidebar and switch to MJPEG or RTSP over HTTP.
3. Check *Control Panel > Log Center* on DSM for NAS-side errors.

## A live view slot's log shows "reconnecting on the same pipe"

```
WARNING surveillance.services.ws_bridge: WebSocket for entree dropped
  after 18s (ConnectionClosedError: no close frame received or sent) —
  reconnecting on the same pipe
```

The client sends a periodic keepalive to hold each WebSocket session open,
so this should be rare — typically a real network interruption or a NAS
restart. The client reconnects on the same pipe without ever stopping
playback, so there is no visible interruption. If it repeats constantly for
one camera, check that camera's network path to the NAS.

## A live view slot shows "(offline)"

The server reports this camera as disabled or disconnected. The slot shows
a local "Camera offline" placeholder instead of a frozen frame or a black
screen — this is deliberate: handing mpv a real stream URL for a camera
that can't be reached is what used to wedge the slot permanently. Nothing
to do here; the sidebar polls camera status periodically (30s by default),
and the real feed comes back on its own once the camera is reachable again.

## A live view slot shows "(stream lost)" or "(attempting reconnect)"

This means the stream stopped responding mid-session and didn't recover on
its own: for WebSocket, either the bridge tried several times in a row and
never re-established a connection, or a pipe write stalled, which gives up
on the first occurrence (see the stalled-pipe section below); for RTSP,
mpv's demuxer stopped decoding without the camera's status necessarily
changing. Either way the slot
falls back to the same "Camera offline" placeholder rather than a frozen
frame. "(attempting reconnect)" means it's currently retrying the real
stream after such a failure.

This also recovers automatically, no action needed &mdash; both transports
retry on every camera-status poll while the camera is still reported
enabled, so the wait between one attempt and the next is
`poll_interval_cameras` (30s by default). If a specific camera never
recovers on its own after a couple of minutes, right-click it in the
sidebar and try switching protocol (e.g. RTSP instead of WebSocket, or
vice versa) as a workaround.

## A camera with audio repeatedly stalls or loses its WebSocket stream

The symptom in the log is a slot giving up with a stalled pipe write:

```
ERROR surveillance.ui.liveview: Stream for entree gave up (video pipe
  write stalled for 5s with 61440 of 65536 bytes left, downstream reader
  stopped draining)
```

This affects WebSocket cameras whose audio this client muxes, which means
any camera DSM reports as PCMU or AAC. It was first reported with PCMU on
Hikvision and Reolink models, but the two codecs differ only in how the
input is opened: past that, both are decoded to PCM under
`-use_wallclock_as_timestamps` and reach the muxer identically.

Note that a *video* pipe write stalls in both modes, so that half of the
message does not by itself mean the camera's audio is being muxed. An
*audio* pipe write can only stall on a muxed camera. If the slot has no
muxed audio, the cause is elsewhere: the reader is mpv rather than
ffmpeg, and this section does not apply.

**The camera's audio stopping is handled, and is no longer this.** A live
mux holds its video input while any of its inputs has nothing to deliver:
measured on ffmpeg 7.1.5, video stops being drained 0.7s after audio goes
quiet and never resumes. The client watches for a camera whose audio has
stopped while its video keeps arriving, and ends the audio stream after
three seconds of that. You will see this instead, once per affected
camera, and the slot stays up:

```
WARNING surveillance.services.ws_bridge: WebSocket bridge for entree: no
  audio for 3s, ending the audio stream so it stops holding up the video
```

The picture pauses for those three seconds and then catches up. That
camera plays without sound for the rest of the session; to get it back,
restart its stream by switching page and back, or by right-clicking it in
the sidebar, opening Stream Protocol and clicking Apply. Nothing else is
wrong with it, and cameras with silence suppression or an intermittent
microphone will do this routinely.

Both conditions matter. A session the NAS drops silently delivers neither
stream for up to ten seconds before reconnecting, and its audio is
deliberately kept: nothing is being held up while both inputs are quiet.

What remains, if a slot still gives up with a stalled pipe write, is **an
ffmpeg regression**. On ffmpeg 7.0 and higher (all versions released at
least until 2026-08-08), muxing live piped H.264/HEVC video with PCMU
audio under `-use_wallclock_as_timestamps` can stall or fully deadlock
ffmpeg's own pipe writes. ffmpeg 6.1.1 is unaffected. Filed upstream:
https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/24053

The app detects the stall and retries, but the retry rebuilds the same
pipeline and meets the same cause, so this shows up as a slot that
recovers and gives up again once per camera poll (30s by default) rather
than a one-off recovery. Workarounds:

- Point the app at a known-good ffmpeg build: put an ffmpeg 6.1.1 binary in
  its own directory and launch with `PATH=/path/to/ffmpeg-6.1.1:$PATH
  surveillance`. This only affects this app's process, not the rest of the
  system. It works the same way on the AppImage, whose bundled ffmpeg is a
  fallback for systems that have none rather than an override (AppImages
  0.4.0 through 0.7.1 put the bundled copy first on PATH, so this has no
  effect on those; earlier ones bundled no ffmpeg at all).
- Switch that camera to RTSP instead of WebSocket (right-click it in the
  sidebar). RTSP streams go straight to mpv and never go through this
  app's ffmpeg muxing path, so this does not apply to them.
- Turn the camera's audio off in Surveillance Station. With no audio
  codec to mux, the stream is piped straight to mpv, so this does not
  apply to it either. Keeps WebSocket, at the price of the audio.

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

`--log-file` writes the same records to a file without the redirection,
which helps when a session may end before you get to save the terminal.
Keep stderr as well where you can: crash tracebacks and GTK's own
warnings go straight there and never reach the log file.
```sh
surveillance --debug --log-file 2>&1 | tee ~/surveillance-debug.log
```

Useful log namespaces:

- `surveillance.services.ws_bridge` - WebSocket bridge errors (classified),
  which audio framing a camera was given and why it lost its audio if it did
- `surveillance.services.recording` - recording download issues
- `surveillance.ui.mpv_widget` - mpv option / render errors, and mpv's own
  messages, which it reports only through this logger
- `surveillance.ui.player` - playback start failures

A debug run also lets the muxing ffmpeg write its complaints straight to
stderr, unprefixed by any of the namespaces above.
