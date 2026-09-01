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

## Known limitations of the Live View timeline

The Live View timeline (Live/History mode, seeking, playback speed, event
markers, downloads) has been tested with many simultaneous cameras over
prolonged periods, but a few rough edges remain, including but not limited
to:

- History-mode playback can stall for several seconds up to a minute or
  more, roughly every 30-40 seconds, when playing near real time — once the
  history buffer runs empty and catches up to what was "now" when History
  mode was entered.
- Playing History faster than 1x can cause playback to periodically jump
  backward before continuing forward, most noticeable at high speeds or
  with multiple cameras active.
- The timeline's position marker can run several seconds ahead of what's
  actually visible on screen during History playback.
- Pausing and resuming a Live camera always reconnects rather than resuming
  instantly — a deliberate tradeoff for now, not a bug.
- Pressing Pause during History playback on a large layout (a full 4x4 grid)
  has been known to cause some cameras, particularly ones with no audio, to
  lose their stream and not recover on their own — leaving and re-entering
  the layout gets them back.

## The Live View timeline's buttons feel sluggish with many cameras

Clicking the ruler, Back/Forward 10s, or Pause/Play seeks every active slot
in the current layout at once. On a 4x4 (or larger) grid, each slot's own
lookup against the NAS adds up, so response can lag noticeably behind a
click — this is more noticeable than on a smaller layout, not a hang.

- Avoid clicking a button again before the previous click has visibly taken
  effect. Rapid repeats are coalesced into a single request rather than
  queued one-by-one, but still wait on the same round trip, so spamming a
  button does not make it respond faster.
- Switch to a smaller layout (2x2 or 1x1) for snappier timeline response,
  especially while scrubbing through History mode.

## High playback speed is demanding, especially on larger layouts

The Live View timeline's speed dropdown (History mode only) asks DSM to
deliver frames that many times faster — at 8x or above, decoding that many
times more video per second, multiplied across every active History slot in
the layout, is a real load on both the NAS and this client's own CPU/memory,
not just a UI setting. Running a high speed across a full 4x4 grid at once
has been observed to crash the app outright, and to be heavy enough on the
system as a whole to affect other running applications too.

The dropdown greys out the speeds a layout cannot afford, on a budget of
(speed × active slots): by default, 1×1 keeps the full range up to 100x,
2×2 stops at 16x, 3×3 at 8x, and 4×4 at 4x. To go faster, switch to a
smaller layout, or raise the budget itself from the Settings page (see
"Tuning playback buffering for your setup" below).

That budget is a guard, not a measurement — high-speed playback under load
is still being characterized, and the default may move once it is. If a
speed the dropdown does allow still causes instability, drop to a smaller
layout (or lower the budget back down) and please report it.

## Tuning playback buffering for your setup

The **Settings** page (Licenses → Settings → About in the sidebar) exposes
the demuxer cache sizes this client uses per streaming profile, plus the
speed×slots budget above — no single set of defaults is right for every
DSM/network/client combination, so these are meant to be tuned rather than
treated as fixed:

- **Cache sizes** (per profile: plain RTSP, WebSocket muxed-audio, silent
  WebSocket) and the **max high-speed History cache** control how much
  video mpv buffers before playback starts adjusting speed to compensate.
  Raise these if you see frequent stutter or the client's own adaptive
  speed correction kicking in a lot (visible via the on-screen readout
  below) on a slow or congested network; lower them for less latency on a
  fast, stable LAN.
- **Demuxer byte cap** is a hard ceiling alongside the cache sizes above —
  raising a cache size has no effect once this cap is reached first, so
  the two need to move together for a high-bitrate camera or a high
  History playback speed.
- **Show stream cache details overlaid on video** draws a small live
  readout (cache depth, target, and effective playback speed) in the
  corner of each video slot, for seeing what the numbers above actually
  do while you tune them, rather than guessing from stutter alone.
- **Max limit for (playback speed × number of slots)** is the same budget
  described above for the History speed dropdown; raise it if your NAS
  and network can handle more concurrent high-speed decoding than the
  default assumes, lower it if the default already causes instability on
  your setup.

Each setting has its own reset-to-default button, plus one that resets
the whole page at once. Changes apply to the next stream that starts (the
on-screen readout takes effect immediately, even on an already-playing
stream) and persist across restarts, in
`~/.config/surveillance-station/config.toml`'s `[setting_overrides]`/
`[setting_overrides_bool]` sections.

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

On the AppImage, read the section below first. A crash that lands the
moment a camera's audio starts has a different cause, and none of this
helps it.

Try:

```sh
GDK_BACKEND=x11 surveillance     # force X11
```

For NVIDIA on Ubuntu, ensure the proprietary driver is installed and current:

```sh
sudo apt install nvidia-driver-550
nvidia-smi
```

## The AppImage dies as soon as a camera's audio starts

The process takes a SIGSEGV the moment a camera with audio comes up. The
kernel log names a PipeWire module:

```
segfault at 10 ip ... error 4 in libpipewire-module-client-node.so
```

and `coredumpctl` puts that module under the copy of libpipewire inside
the bundle:

```
#0  libpipewire-module-client-node.so + 0x2ba1e
#1  libpipewire-module-client-node.so + 0x13a86
#2  .../_internal/libpipewire-0.3.so.0 + 0x3d0c0
```

Under `--debug` the last thing mpv reports is its PipeWire output opening,
with a version gap that looks like the cause and is not:

```
[ao/pipewire] Library version: 1.4.2
[ao/pipewire] Core version: 1.0.5
[ao/pipewire] Stream state changed: old_state=unconnected state=connecting
```

"Library version" is the libpipewire loaded into this process and "Core
version" is the daemon, so the daemon really is older. The daemon is not
what crashes, though. Every frame in that backtrace is in code this
process loaded itself, reached before anything goes on the wire.

The mismatch is inside the one process. libmpv links against libpipewire,
so PyInstaller collects it into the bundle along with everything else
libmpv needs, and what lands there is whatever the build container had.
PipeWire then loads its own modules by dlopen at run time, from a
directory compiled into the library:

```sh
strings -a _internal/libpipewire-0.3.so.0 | grep 'pipewire-0.3$'
/usr/lib/x86_64-linux-gnu/pipewire-0.3
```

No modules are bundled, and Debian and Ubuntu spell that directory the
same way, so the bundled core finds the host's modules where it expects
its own and loads them. They call back into the core already in the
process, which is the other release, and the first structure whose layout
moved between the two is then read at the wrong offset. libpipewire
carries no symbol versioning and no module ABI check, so nothing catches
it. Running from source does not crash because nothing is bundled: the
host's libmpv loads the host's libpipewire, which loads the host's
modules.

`libasound.so.2` was bundled the same way and looks for its plugins in
`/usr/lib/x86_64-linux-gnu/alsa-lib`, so the same fault was waiting on the
ALSA route, which on a PipeWire desktop leads straight back into
libpipewire through `pcm.!default`.

The AppImage now uses the host's own `libpipewire-0.3.so.0`,
`libasound.so.2` and `libjack.so.0`, and falls back to the copies it
carries only where the host has none. On 0.9.0 and earlier, take the
bundled copy away by hand:

```sh
./Surveillance-0.9.0-x86_64.AppImage --appimage-extract
rm squashfs-root/usr/lib/Surveillance/_internal/libpipewire-0.3.so.0
./squashfs-root/AppRun
```

Any machine that hits this crash has PipeWire installed by definition, and
the 36 symbols libmpv needs from it are all present in the 1.0.5 Ubuntu
24.04 ships. A host still on PipeWire 0.3 (Ubuntu 22.04, Debian 12, Mint
21) is missing one or two of them, so there the bundled copy is kept:
preferring the host's would leave libmpv unable to load at all, costing
video as well as audio. On those hosts the AppImage sends mpv to
PulseAudio, which both PulseAudio and pipewire-pulse answer over a
negotiated protocol. Its ALSA output would not do, since a PipeWire
desktop routes that straight back into the same library.

`SURVEILLANCE_AO` picks the output driver by hand, taking any name mpv's
`--ao` accepts, on the AppImage or from source:

```sh
SURVEILLANCE_AO=pulse surveillance
```

Set explicitly it overrides what the AppImage would have chosen; unset,
mpv picks for itself. Push-to-talk is not covered either way, since it
goes through libportaudio and the host's ALSA library rather than mpv.

## Ubuntu 24.04 / AppImage

- PyGObject >= 3.50 is required for `Gtk.AlertDialog`. Ubuntu 24.04 ships
  3.48 from the system packages. The AppImage bundles a newer version; from
  pip, install into a venv that pulls `PyGObject>=3.50`.
- Do not mix the system `python3-mpv` with the AppImage. The AppImage uses
  its own bundled Python and GTK.
- The AppImage uses the host's PipeWire, ALSA and JACK libraries wherever
  the host has them. Carrying its own over a host that already had them is
  what caused the crash in the section above.

## Collecting debug logs

```sh
surveillance --debug --log-file=~/surveillance-debug.log
```

Useful log namespaces:

- `surveillance.services.ws_bridge` - WebSocket bridge errors (classified),
  which audio framing a camera was given and why it lost its audio if it did
- `surveillance.services.recording` - recording download issues
- `surveillance.ui.mpv_widget` - mpv option / render errors, and mpv's own
  messages, which it reports only through this logger
- `surveillance.ui.player` - playback start failures
- `surveillance.crash` - uncaught exception
- `surveillance.glib` - GTK/GLib/GIO

On a debug run the bridge also pipes the muxing ffmpeg's stderr and
re-logs each line under `surveillance.services.ws_bridge` as
`ffmpeg for <camera>: ...`, so it lands in the capture with everything
else rather than on the terminal by itself.
