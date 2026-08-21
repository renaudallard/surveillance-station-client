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

"""MPV video player widget using GTK4 GLArea + mpv OpenGL render context."""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import logging
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # type: ignore[import-untyped]

log = logging.getLogger(__name__)


def _load_gl_get_proc() -> ctypes.CDLL | None:
    """Load a native GL library that can resolve proc addresses."""
    candidates: list[str] = []
    for name in ("EGL", "GL", "GLX"):
        path = ctypes.util.find_library(name)
        if path:
            candidates.append(path)
    candidates.extend(("libEGL.so.1", "libGL.so.1", "libGLX.so.0"))
    for lib_name in candidates:
        try:
            lib = ctypes.CDLL(lib_name)
            # Verify it has a proc address function
            for fn_name in ("eglGetProcAddress", "glXGetProcAddressARB", "glXGetProcAddress"):
                fn = getattr(lib, fn_name, None)
                if fn is not None:
                    fn.argtypes = [ctypes.c_char_p]
                    fn.restype = ctypes.c_void_p
                    return lib
        except OSError:
            continue
    return None


_gl_lib = _load_gl_get_proc()

# video-zoom is mpv's own log2 scale (0 = 100%, 1 = 200%, ...). Zooming out
# below 100% would just letterbox the video within the widget for no
# benefit, so the range only goes up from there.
_ZOOM_MIN = 0.0
_ZOOM_MAX = 3.0  # 2**3 = 800%
# Clamp so scrolling can't pan the video entirely out of view.
_PAN_MAX = 0.8


def _get_gl_proc_address(_ctx: ctypes.c_void_p, name: bytes) -> int:
    """Get OpenGL procedure address via native GL library.

    Signature matches mpv's MpvGlGetProcAddressFn:
      CFUNCTYPE(c_void_p, c_void_p, c_char_p)
    """
    if _gl_lib is None:
        return 0
    for fn_name in ("eglGetProcAddress", "glXGetProcAddressARB", "glXGetProcAddress"):
        fn = getattr(_gl_lib, fn_name, None)
        if fn is not None:
            addr = fn(name)
            if addr:
                return addr  # type: ignore[no-any-return]
    return 0


class MpvGLArea(Gtk.GLArea):
    """GTK4 GLArea widget that renders mpv video via OpenGL.

    Each instance has its own mpv player and render context.
    Works on both X11 and Wayland without wid embedding.
    """

    def __init__(self, tls_verify: bool = True) -> None:
        super().__init__()
        self._mpv: Any = None
        self._ctx: Any = None
        self._url: str = ""
        self._initialized = False
        self._render_pending = False
        self._tls_verify = tls_verify
        self._low_latency = False
        self._muxed_audio = False
        self._start_offset: float = 0
        self._zoom: float = 0.0
        self._pan_x: float = 0.0
        self._pan_y: float = 0.0
        self._muted = False
        self._volume = 100

        self.set_auto_render(False)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        self.connect("render", self._on_render)

    def _on_realize(self, widget: Gtk.GLArea) -> None:
        """Initialize mpv and OpenGL render context when widget is realized."""
        self.make_current()

        if self.get_error():
            log.error("GLArea has error: %s", self.get_error())
            return

        try:
            import mpv

            self._mpv = mpv.MPV(
                vo="libmpv",
                hwdec="auto",
                keep_open="yes",
                idle="yes",
                input_default_bindings=False,
                input_vo_keyboard=False,
                log_handler=self._mpv_log,
                # A minimum level, not a filter: at "fatal" libmpv delivers
                # nothing else, so _mpv_log's error and warn branches could
                # never run and mpv was silent about every problem short of
                # a fatal one. It writes nothing to stderr either, terminal
                # being off, so this handler is the only way its diagnostics
                # reach a log at all.
                loglevel="debug" if log.isEnabledFor(logging.DEBUG) else "warn",
                demuxer_lavf_o="rtsp_transport=tcp",
                tls_verify=self._tls_verify,
                mute=self._muted,
                volume=self._volume,
            )

            # Wrap with mpv's own CFUNCTYPE so ctypes type identity matches
            self._proc_addr_fn = mpv.MpvGlGetProcAddressFn(_get_gl_proc_address)

            # Set up OpenGL render context
            self._ctx = mpv.MpvRenderContext(
                self._mpv,
                "opengl",
                opengl_init_params={
                    "get_proc_address": self._proc_addr_fn,
                },
            )

            self._ctx.update_cb = self._mpv_update_cb
            self._initialized = True

            # If URL was set before realization, apply options and start playing
            if self._url:
                self._apply_playback_options()
                self._mpv["start"] = str(self._start_offset) if self._start_offset else "0"
                self._mpv.play(self._url)

        except Exception:
            log.exception("Failed to initialize mpv")
            self._initialized = False

    def _on_unrealize(self, widget: Gtk.GLArea) -> None:
        """Clean up mpv when widget is unrealized."""
        self.stop()
        if self._ctx:
            with contextlib.suppress(Exception):
                self._ctx.free()
            self._ctx = None
        if self._mpv:
            with contextlib.suppress(Exception):
                self._mpv.terminate()
            self._mpv = None
        self._initialized = False

    def _on_render(self, area: Gtk.GLArea, ctx: Any) -> bool:
        """Render callback - called by GTK when the area needs to be redrawn."""
        self._render_pending = False

        if not self._initialized or not self._ctx:
            return True

        try:
            if not self._url:
                # No active stream: paint black instead of asking mpv's render
                # context to redraw, since libmpv keeps repainting the last
                # decoded frame it has buffered until a new one arrives.
                from OpenGL.GL import GL_COLOR_BUFFER_BIT, glClear, glClearColor

                glClearColor(0.0, 0.0, 0.0, 1.0)
                glClear(GL_COLOR_BUFFER_BIT)
                return True

            width = self.get_width()
            height = self.get_height()
            scale = self.get_scale_factor()

            from OpenGL.GL import GL_FRAMEBUFFER_BINDING, glGetIntegerv

            fbo = int(glGetIntegerv(GL_FRAMEBUFFER_BINDING))

            self._ctx.render(
                flip_y=True,
                opengl_fbo={
                    "w": width * scale,
                    "h": height * scale,
                    "fbo": fbo,
                },
            )
            self._ctx.report_swap()
        except Exception as e:
            log.debug("Render error: %s", e)

        return True

    def _mpv_update_cb(self) -> None:
        """Called by mpv from its thread when a new frame is available.

        Coalesces multiple updates into a single render to avoid flooding
        the GTK main loop when many streams are active (e.g. 3x3 grid).
        Skips scheduling when stopped (no URL) to prevent idle render loops.
        """
        if self._url and not self._render_pending:
            self._render_pending = True
            GLib.idle_add(self._do_queue_render)

    def _do_queue_render(self) -> bool:
        """Queue a render on the main thread. Returns False to remove idle source."""
        if self._initialized:
            self.queue_render()
        return False

    def _mpv_log(self, loglevel: str, component: str, message: str) -> None:
        """Handle mpv log messages.

        mpv routes libavcodec and libavformat through the same client log
        under components named ffmpeg/*, and maps AV_LOG_ERROR to its own
        "error". Those are routine on a live stream: a decoder started
        mid-GOP reports a missing PPS and a failed slice header for every
        frame until the next keyframe, which is what a WebSocket bridge
        reconnecting onto the same pipe hands it. Reporting that as an
        application error would call a healthy stream broken, several
        hundred lines at a time and once per slot, so the decoder's own
        complaints stay at debug and only mpv's own components speak up.
        """
        text = message.strip()
        if component.startswith("ffmpeg"):
            log.debug("mpv [%s] %s: %s", component, loglevel, text)
        elif loglevel in ("error", "fatal"):
            log.error("mpv [%s]: %s", component, text)
        elif loglevel == "warn":
            log.warning("mpv [%s]: %s", component, text)
        else:
            # Only reachable on a debug run, which is the only time libmpv
            # is asked for these. Dropping them instead would mean paying
            # to carry every message across the C boundary for nothing.
            log.debug("mpv [%s] %s: %s", component, loglevel, text)

    def _apply_playback_options(self) -> None:
        """Apply buffering and timing options for the current playback profile."""
        if not self._mpv:
            return
        if self._muxed_audio:
            # A live-piped Matroska stream from our own ffmpeg mux (see
            # ws_bridge.py's audio muxing) -- unlike the raw-NAL
            # low_latency profile below, mpv is demuxing a properly
            # headered container here, so it doesn't need probesize
            # tuned for raw H.264/H.265 stream sync. It does still need a
            # live-stream-appropriate (bounded, non-file-sized) max byte
            # cap, and a small but nonzero readahead buffer to absorb
            # normal scheduling jitter that would otherwise show up as
            # audible micro-cuts with zero buffer margin.
            self._mpv["cache"] = "yes"
            self._mpv["demuxer-max-bytes"] = "32MiB"
            self._mpv["demuxer-readahead-secs"] = 2
            self._mpv["cache-secs"] = 2
            # This widget's other profile's analyzeduration=0 + large
            # probesize (tuned for RTSP/local files, below) causes a
            # real, reproducible multi-second demuxer stall on a live
            # pipe: a never-ending source can leave libavformat waiting
            # well past any reasonable duration for "enough" data to
            # feel confident, unlike a normal file/RTSP source. A
            # well-formed MKV header (ffmpeg has already resolved
            # codec/timing info by the time mpv sees it) needs nowhere
            # near this much probing margin.
            self._mpv["demuxer-lavf-analyzeduration"] = 0.3
            self._mpv["demuxer-lavf-probesize"] = 32768
            self._mpv["correct-pts"] = True
            self._mpv["untimed"] = False
            self._mpv["container-fps-override"] = 0
        elif self._low_latency:
            self._mpv["cache"] = "no"
            self._mpv["demuxer-max-bytes"] = "512KiB"
            self._mpv["demuxer-readahead-secs"] = 0
            # Inert while the cache is off (mpv only raises the readahead
            # to cache-secs when the cache is on), but written anyway so
            # every profile states the same set of options.
            self._mpv["cache-secs"] = 0
            self._mpv["demuxer-lavf-analyzeduration"] = 0
            self._mpv["demuxer-lavf-probesize"] = 32
            self._mpv["correct-pts"] = False
            self._mpv["untimed"] = True
            self._mpv["container-fps-override"] = 25
        else:
            # Explicit mpv defaults so a widget that previously played a
            # low-latency stream does not inherit probesize=32 and
            # container-fps-override=25 here.
            self._mpv["cache"] = "auto"
            self._mpv["demuxer-max-bytes"] = "150MiB"
            self._mpv["demuxer-readahead-secs"] = 1
            # mpv's own default. With the cache on, mpv reads ahead by
            # whichever of this and demuxer-readahead-secs is larger, so
            # leaving it at the muxed profile's 2 would quietly cap an
            # RTSP stream's buffer at two seconds instead of letting
            # demuxer-max-bytes above govern it.
            self._mpv["cache-secs"] = 1000 * 60 * 60
            self._mpv["demuxer-lavf-analyzeduration"] = 0
            self._mpv["demuxer-lavf-probesize"] = 5000000
            self._mpv["correct-pts"] = True
            self._mpv["untimed"] = False
            self._mpv["container-fps-override"] = 0

    def play(
        self,
        url: str,
        *,
        low_latency: bool = False,
        muxed_audio: bool = False,
        start_offset: float = 0,
    ) -> None:
        """Start playing a stream URL.

        When *low_latency* is True, disable caching and read-ahead so the
        stream plays in near real-time (used for WebSocket pipe bridges
        without a usable audio track, piping raw video only).

        When *muxed_audio* is True, use the buffering profile tuned for a
        live-piped Matroska stream instead (used for WebSocket pipe
        bridges that muxed real audio in via ffmpeg — see ws_bridge.py).
        Takes precedence over *low_latency* if both are set.

        *start_offset* seeks to that position (seconds) as the file loads,
        for playing a moment within a much longer recording file.
        """
        self._url = url
        self._low_latency = low_latency
        self._muxed_audio = muxed_audio
        self._start_offset = start_offset
        if self._initialized and self._mpv:
            try:
                self._apply_playback_options()
                self._mpv["start"] = str(start_offset) if start_offset else "0"
                self._mpv.play(url)
            except Exception:
                log.exception("Failed to play %s", url)

    def stop(self) -> None:
        """Stop playback."""
        self._url = ""
        if self._mpv:
            with contextlib.suppress(Exception):
                self._mpv.command("stop")
        if self._initialized:
            self.queue_render()

    def pause(self) -> None:
        """Toggle pause."""
        if self._mpv:
            with contextlib.suppress(Exception):
                self._mpv.pause = not self._mpv.pause

    def set_paused(self, paused: bool) -> None:
        """Explicitly pause or resume, unlike pause()'s toggle -- for a
        caller tracking its own paused state (Live View's timeline
        Pause/Play button) where a toggle could drift out of sync with
        it if this and some other trigger ever raced."""
        if self._mpv:
            with contextlib.suppress(Exception):
                self._mpv.pause = paused

    def _letterbox_fraction(self, width: int, height: int) -> tuple[float, float]:
        """Fraction of the widget's width/height the fitted (unzoomed)
        video actually occupies — 1.0 on an axis mpv doesn't letterbox to
        preserve the camera's aspect ratio, less than 1.0 on one it does.
        Falls back to (1.0, 1.0) if mpv hasn't reported a video size yet.
        """
        dwidth = getattr(self._mpv, "dwidth", None)
        dheight = getattr(self._mpv, "dheight", None)
        if not dwidth or not dheight or width <= 0 or height <= 0:
            return 1.0, 1.0
        fit_scale = min(width / dwidth, height / dheight)
        return (dwidth * fit_scale) / width, (dheight * fit_scale) / height

    def zoom_at(self, delta: float, cursor_x: float, cursor_y: float) -> None:
        """Zoom in/out by *delta* (mpv's own log2 video-zoom units — e.g.
        0.15 per scroll tick), keeping whatever's under the cursor
        (cursor_x, cursor_y — widget-relative pixel coordinates) fixed on
        screen — the standard "zoom to point" behavior of image viewers,
        maps, and graphics editors.

        mpv's video-pan-x/y are fractions of the *scaled* video size, not
        the widget, so a screen position maps to a video-space point via
        `screen = scale * (point + pan)`. Solving for the pan that keeps a
        screen position mapped to the same point across a scale change
        gives `pan' = pan + cursor_offset * (1/scale' - 1/scale)` — exact
        at every step either direction, and self-limiting at high zoom
        since 1/scale shrinks. cursor_offset is relative to the actual
        displayed video rect (see _letterbox_fraction), not the raw
        widget.
        """
        if not self._mpv or not self._initialized:
            return

        width = self.get_width()
        height = self.get_height()
        if width <= 0 or height <= 0:
            return

        new_zoom = max(_ZOOM_MIN, min(_ZOOM_MAX, self._zoom + delta))

        if new_zoom <= _ZOOM_MIN:
            # Back at (or already at) 1:1 — pan is meaningless with no
            # extra zoomed content to shift around, and leaving it nonzero
            # would show the video pushed off into a corner instead of
            # filling the slot. Reset it here unconditionally, even if the
            # zoom level itself isn't changing — e.g. scrolling out
            # further while already at the floor after panning around at
            # 1:1, which would otherwise never reach this branch at all.
            if self._zoom == new_zoom and self._pan_x == 0.0 and self._pan_y == 0.0:
                return  # genuinely nothing to do
            self._zoom = new_zoom
            self._pan_x = 0.0
            self._pan_y = 0.0
        else:
            if new_zoom == self._zoom:
                return
            scale_old = 2**self._zoom
            scale_new = 2**new_zoom
            inv_delta = 1 / scale_new - 1 / scale_old
            letterbox_x, letterbox_y = self._letterbox_fraction(width, height)
            # Cursor position relative to widget center, normalized to
            # [-0.5, 0.5], then rescaled from widget-relative to
            # actual-video-rect-relative.
            nx = (cursor_x / width - 0.5) / letterbox_x
            ny = (cursor_y / height - 0.5) / letterbox_y
            self._pan_x = max(-_PAN_MAX, min(_PAN_MAX, self._pan_x + nx * inv_delta))
            self._pan_y = max(-_PAN_MAX, min(_PAN_MAX, self._pan_y + ny * inv_delta))
            self._zoom = new_zoom

        with contextlib.suppress(Exception):
            self._mpv.video_zoom = self._zoom
            self._mpv.video_pan_x = self._pan_x
            self._mpv.video_pan_y = self._pan_y

    def pan_by(self, dx_fraction: float, dy_fraction: float) -> None:
        """Pan by an incremental amount, as fractions of the widget's own
        size (same units zoom_at() takes a cursor offset in). Divided by
        the current scale and the letterbox fraction to convert to mpv's
        own pan units, so a drag tracks the cursor at a constant
        screen-space rate regardless of zoom level or the camera's aspect
        ratio. Allowed even at 1:1 zoom (scrolling back out already
        re-centers via reset-on-floor in zoom_at(), so panning off-center
        at 1:1 is always one scroll-out away from being undone)."""
        if not self._mpv or not self._initialized:
            return
        scale = 2**self._zoom
        letterbox_x, letterbox_y = self._letterbox_fraction(self.get_width(), self.get_height())
        self._pan_x = max(
            -_PAN_MAX, min(_PAN_MAX, self._pan_x + dx_fraction / (scale * letterbox_x))
        )
        self._pan_y = max(
            -_PAN_MAX, min(_PAN_MAX, self._pan_y + dy_fraction / (scale * letterbox_y))
        )
        with contextlib.suppress(Exception):
            self._mpv.video_pan_x = self._pan_x
            self._mpv.video_pan_y = self._pan_y

    def reset_zoom(self) -> None:
        """Reset zoom/pan to the default 1:1 view."""
        self._zoom = 0.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        if self._mpv:
            with contextlib.suppress(Exception):
                self._mpv.video_zoom = 0.0
                self._mpv.video_pan_x = 0.0
                self._mpv.video_pan_y = 0.0

    def set_volume(self, volume: int) -> None:
        """Set volume (0-100). Independent of mute — the level set here is
        what's restored on unmute, silent or not in the meantime. Stored
        whether or not mpv has been realized yet, same as set_mute()."""
        self._volume = volume
        if self._mpv:
            with contextlib.suppress(Exception):
                self._mpv.volume = volume

    @property
    def muted(self) -> bool:
        return self._muted

    def set_mute(self, muted: bool) -> None:
        """Mute/unmute without touching the volume level (mpv's own `mute`
        property, not zeroing volume) — stored regardless of whether mpv
        has been realized yet, same as _low_latency/_start_offset, and
        applied immediately if it has."""
        self._muted = muted
        if self._mpv:
            with contextlib.suppress(Exception):
                self._mpv.mute = muted

    def seek(self, seconds: float) -> None:
        """Seek relative to current position."""
        if self._mpv:
            with contextlib.suppress(Exception):
                self._mpv.seek(seconds)

    def seek_absolute(self, seconds: float) -> None:
        """Seek to absolute position."""
        if self._mpv:
            with contextlib.suppress(Exception):
                self._mpv.seek(seconds, reference="absolute")

    @property
    def duration(self) -> float | None:
        """Get duration of current media."""
        if self._mpv:
            try:
                result: float | None = self._mpv.duration
            except Exception:
                return None
            else:
                return result
        return None

    @property
    def time_pos(self) -> float | None:
        """Get current playback position."""
        if self._mpv:
            try:
                result: float | None = self._mpv.time_pos
            except Exception:
                return None
            else:
                return result
        return None

    @property
    def is_playing(self) -> bool:
        """Check if currently playing."""
        if self._mpv:
            try:
                return not self._mpv.pause and self._mpv.time_pos is not None
            except Exception:
                return False
        return False


# video-zoom units (mpv's own log2 scale) applied per scroll wheel tick.
_ZOOM_STEP = 0.15
# A drag shorter than this (pixels, either axis) is still treated as a
# plain click rather than a pan.
_DRAG_CLICK_THRESHOLD = 4


def attach_zoom_pan_controls(player: MpvGLArea, on_click: Callable[[], None] | None = None) -> None:
    """Wire up scroll-to-zoom (centered on the cursor) and click-and-drag
    panning on an MpvGLArea. Shared by Live View slots and the recording
    player dialog so both behave identically.

    If `on_click` is given, a drag shorter than _DRAG_CLICK_THRESHOLD is
    treated as a plain click and calls it with no arguments — used by Live
    View slots, which select the slot on click. Pass None (the default)
    where the video area has no click behavior to preserve, so every drag
    is treated as a pan from the first pixel.
    """
    # EventControllerScroll's "scroll" signal has no position, so a motion
    # controller tracks the last-known pointer position for it to use.
    pointer = {"x": 0.0, "y": 0.0}

    def on_motion(controller: Gtk.EventControllerMotion, x: float, y: float) -> None:
        pointer["x"] = x
        pointer["y"] = y

    motion = Gtk.EventControllerMotion()
    motion.connect("motion", on_motion)
    player.add_controller(motion)

    def on_scroll(controller: Gtk.EventControllerScroll, dx: float, dy: float) -> bool:
        # Scroll up (dy negative — "away from the user") zooms in, matching
        # the near-universal convention (maps, image viewers, etc.).
        player.zoom_at(-dy * _ZOOM_STEP, pointer["x"], pointer["y"])
        return True  # handled — don't let it bubble past the widget

    scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
    scroll.connect("scroll", on_scroll)
    player.add_controller(scroll)

    # drag-update/drag-end report the offset cumulative from drag-begin,
    # not incrementally — pan_by() wants an increment, so track how much
    # of that cumulative offset has already been applied.
    drag_last = {"x": 0.0, "y": 0.0}

    def on_drag_begin(gesture: Gtk.GestureDrag, start_x: float, start_y: float) -> None:
        drag_last["x"] = 0.0
        drag_last["y"] = 0.0

    def on_drag_update(gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        width = player.get_width()
        height = player.get_height()
        if width <= 0 or height <= 0:
            return
        dx = (offset_x - drag_last["x"]) / width
        dy = (offset_y - drag_last["y"]) / height
        drag_last["x"] = offset_x
        drag_last["y"] = offset_y
        # Content should follow the cursor (grab-and-drag feel): dragging
        # right/down reveals what was previously off to the left/top.
        player.pan_by(dx, dy)

    def on_drag_end(gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        moved = abs(offset_x) >= _DRAG_CLICK_THRESHOLD or abs(offset_y) >= _DRAG_CLICK_THRESHOLD
        if not moved and on_click is not None:
            on_click()

    drag = Gtk.GestureDrag(button=1)
    drag.connect("drag-begin", on_drag_begin)
    drag.connect("drag-update", on_drag_update)
    drag.connect("drag-end", on_drag_end)
    player.add_controller(drag)
