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

import ctypes
import ctypes.util
import logging
from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # type: ignore[import-untyped]

log = logging.getLogger(__name__)


def _load_gl_get_proc() -> ctypes.CDLL | None:
    """Load a native GL library that can resolve proc addresses."""
    for lib_name in ("libEGL.so.1", "libGL.so.1", "libGLX.so.0"):
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

    def __init__(self) -> None:
        super().__init__()
        self._mpv: Any = None
        self._ctx: Any = None
        self._url: str = ""
        self._initialized = False
        self._render_pending = False

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
                hwdec="vaapi",
                keep_open="yes",
                idle="yes",
                input_default_bindings=False,
                input_vo_keyboard=False,
                log_handler=self._mpv_log,
                loglevel="warn",
                demuxer_lavf_o="rtsp_transport=tcp,fflags=+genpts",
                demuxer_lavf_probesize=10000000,
                demuxer_lavf_analyzeduration=10000000,
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

            # If URL was set before realization, start playing
            if self._url:
                self._mpv.play(self._url)

        except Exception as e:
            log.error("Failed to initialize mpv: %s", e)
            self._initialized = False

    def _on_unrealize(self, widget: Gtk.GLArea) -> None:
        """Clean up mpv when widget is unrealized."""
        self.stop()
        if self._ctx:
            try:
                self._ctx.free()
            except Exception:
                pass
            self._ctx = None
        if self._mpv:
            try:
                self._mpv.terminate()
            except Exception:
                pass
            self._mpv = None
        self._initialized = False

    def _on_render(self, area: Gtk.GLArea, ctx: Any) -> bool:
        """Render callback - called by GTK when the area needs to be redrawn."""
        self._render_pending = False

        if not self._initialized or not self._ctx:
            return True

        try:
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
        """
        if not self._render_pending:
            self._render_pending = True
            GLib.idle_add(self._do_queue_render)

    def _do_queue_render(self) -> bool:
        """Queue a render on the main thread. Returns False to remove idle source."""
        if self._initialized:
            self.queue_render()
        return False

    def _mpv_log(self, loglevel: str, component: str, message: str) -> None:
        """Handle mpv log messages."""
        if loglevel in ("error", "fatal"):
            log.error("mpv [%s]: %s", component, message.strip())
        elif loglevel == "warn":
            log.warning("mpv [%s]: %s", component, message.strip())

    def play(self, url: str) -> None:
        """Start playing a stream URL."""
        self._url = url
        if self._initialized and self._mpv:
            try:
                self._mpv.play(url)
            except Exception as e:
                log.error("Failed to play %s: %s", url, e)

    def stop(self) -> None:
        """Stop playback."""
        self._url = ""
        if self._mpv:
            try:
                self._mpv.command("stop")
            except Exception:
                pass

    def pause(self) -> None:
        """Toggle pause."""
        if self._mpv:
            try:
                self._mpv.pause = not self._mpv.pause
            except Exception:
                pass

    def set_volume(self, volume: int) -> None:
        """Set volume (0-100)."""
        if self._mpv:
            try:
                self._mpv.volume = volume
            except Exception:
                pass

    def seek(self, seconds: float) -> None:
        """Seek relative to current position."""
        if self._mpv:
            try:
                self._mpv.seek(seconds)
            except Exception:
                pass

    def seek_absolute(self, seconds: float) -> None:
        """Seek to absolute position."""
        if self._mpv:
            try:
                self._mpv.seek(seconds, reference="absolute")
            except Exception:
                pass

    @property
    def duration(self) -> float | None:
        """Get duration of current media."""
        if self._mpv:
            try:
                result: float | None = self._mpv.duration
                return result
            except Exception:
                return None
        return None

    @property
    def time_pos(self) -> float | None:
        """Get current playback position."""
        if self._mpv:
            try:
                result: float | None = self._mpv.time_pos
                return result
            except Exception:
                return None
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
