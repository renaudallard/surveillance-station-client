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

"""A periodic resource line for debug runs.

Extended unattended runs grow in native memory until the app is
OOM-killed, after roughly ten to twelve hours of a busy 16-camera
layout. That was reproduced on main and predates the timeline work, but
nothing in the app has ever measured anything, so every account of it is
an anecdote about where it ended up rather than how it got there. One
line per interval turns a long run into a trend.

Debug runs only, and Linux only: it reads two small files under /proc
and says nothing an ordinary run needs. Where /proc is absent it simply
does not start.
"""

from __future__ import annotations

import logging
import os
import threading

from gi.repository import GLib  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

# Long enough that a twelve-hour run is a few hundred lines, short enough
# to show the shape of the growth rather than just its endpoints.
_INTERVAL_SECONDS = 60


def rss_kb() -> int | None:
    """Resident set size, which is the number the OOM killer acts on.

    Read from /proc rather than resource.getrusage: ru_maxrss is a
    high-water mark and never comes down, so it cannot show a plateau,
    and a plateau is exactly what tells ordinary warm-up apart from a
    leak.
    """
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
    except (OSError, IndexError, ValueError):
        return None


def open_fds() -> int | None:
    """Open descriptors. Each muxed camera holds three 1 MiB pipes, which
    is kernel memory the Python heap never shows, so a climbing count here
    accounts for growth nothing else would explain."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def log_snapshot() -> bool:
    """Write one resource line. False (which ends the timer) if there is
    nothing to read, since that will not change later in the run."""
    rss = rss_kb()
    if rss is None:
        return False
    log.debug(
        "resources: rss=%dkB fds=%s threads=%d",
        rss,
        open_fds(),
        threading.active_count(),
    )
    return True


def start_if_debugging() -> None:
    """Begin the heartbeat when debug logging is on, otherwise do
    nothing. Never stopped: it is one timer for the life of the
    process, and the run it exists to describe is a long one."""
    if not log.isEnabledFor(logging.DEBUG) or rss_kb() is None:
        return
    GLib.timeout_add_seconds(_INTERVAL_SECONDS, log_snapshot)
