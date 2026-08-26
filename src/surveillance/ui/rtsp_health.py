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

"""Health monitoring for plain RTSP live view streams.

A WebSocket stream is bridged through WebSocketBridge (see
services/ws_bridge.py), which already detects a dead connection and gives
up after repeated failures. A plain RTSP stream has no such bridge — mpv
talks to the camera directly — so nothing watches for the demuxer
silently dying mid-stream, which can leave a slot's mpv render context
permanently wedged on a single stale frame, with no way back (confirmed
by reproducing that exact failure mode this investigation).

This fills that gap for RTSP: watch mpv's own time_pos to confirm a
stream is actually advancing, retry play() on the same URL if it isn't,
and give up (report back) after too many failures in a row so a slot
doesn't sit silently wedged forever.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from gi.repository import GLib  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from surveillance.ui.mpv_widget import MpvGLArea

log = logging.getLogger(__name__)

_CHECK_INTERVAL_SECS = 5
_MAX_CONSECUTIVE_STALLS = 3
# How long to wait for the first frame before deciding the stream will
# never start. Cameras over a slow link can take well over one interval to
# produce their first frame, so give a generous window rather than aborting
# an in-progress connection.
_MAX_STARTUP_CHECKS = 6


class RtspHealthMonitor:
    """Watches an MpvGLArea playing a plain RTSP URL and retries on stall."""

    def __init__(
        self,
        player: MpvGLArea,
        url: str,
        label: str,
        on_gave_up: Callable[[str], None],
        on_recovered: Callable[[], None] | None = None,
    ) -> None:
        self._player = player
        self._url = url
        self._label = label or url
        self._on_gave_up = on_gave_up
        self._on_recovered = on_recovered
        self._reported_recovered = False
        self._advancing = False  # has the current attempt produced a moving frame
        self._startup_checks = 0
        self._last_time_pos: float | None = None
        self._consecutive_stalls = 0
        # Set by Live View's timeline Pause button -- see set_paused.
        self._paused = False
        self._timer_id: int = GLib.timeout_add_seconds(_CHECK_INTERVAL_SECS, self._check)

    def stop(self) -> None:
        """Stop monitoring — the slot has moved on to something else."""
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0

    def set_paused(self, paused: bool) -> None:
        """Suspend stall detection while deliberately paused (see
        _check's own guard) -- mpv.pause naturally stops time_pos from
        advancing, which this monitor would otherwise misread as the
        RTSP stream itself having died: retrying play() (a visible
        black flash) after ~15s, then giving up on the stream entirely
        after ~30s more, purely from a deliberate pause rather than
        anything wrong with the camera.

        On resume, the baseline is cleared rather than left to
        immediately read as zero progress across the paused gap --
        the next _check() then treats it like a fresh startup window
        instead of an instant stall/timeout.
        """
        self._paused = paused
        if paused:
            return
        self._advancing = False
        self._startup_checks = 0
        self._consecutive_stalls = 0
        self._last_time_pos = None

    def _check(self) -> bool:
        if self._paused:
            return True
        pos = self._player.time_pos
        advancing = (
            pos is not None and self._last_time_pos is not None and pos > self._last_time_pos
        )
        self._last_time_pos = pos

        if advancing:
            self._advancing = True
            self._startup_checks = 0
            self._consecutive_stalls = 0
            if not self._reported_recovered and self._on_recovered is not None:
                self._reported_recovered = True
                self._on_recovered()
            return True

        if not self._advancing:
            # Still connecting / buffering the first frame. Do not restart
            # the stream here: that would abort a slow-but-working connect.
            # Only give up if no frame ever arrives within the startup window.
            self._startup_checks += 1
            if self._startup_checks >= _MAX_STARTUP_CHECKS:
                log.error(
                    "RTSP stream for %s produced no video within ~%ds — giving up",
                    self._label,
                    _CHECK_INTERVAL_SECS * _MAX_STARTUP_CHECKS,
                )
                self._timer_id = 0
                self._on_gave_up("no video")
                return False
            return True

        # The stream advanced before and has now frozen: a real mid-stream
        # stall, so retry play() on the same URL.
        self._consecutive_stalls += 1
        if self._consecutive_stalls >= _MAX_CONSECUTIVE_STALLS:
            log.error(
                "RTSP stream for %s stalled (no progress for ~%ds) — giving up",
                self._label,
                _CHECK_INTERVAL_SECS * _MAX_CONSECUTIVE_STALLS,
            )
            self._timer_id = 0
            self._on_gave_up("stalled: no progress")
            return False

        log.warning(
            "RTSP stream for %s stalled — retrying play() (%d/%d)",
            self._label,
            self._consecutive_stalls,
            _MAX_CONSECUTIVE_STALLS,
        )
        self._player.stop()
        self._player.play(self._url)
        # Fresh attempt: wait for its first frame again rather than reading
        # the old baseline as progress, and do not treat its startup buffering
        # as another stall.
        self._advancing = False
        self._startup_checks = 0
        self._last_time_pos = None
        return True
