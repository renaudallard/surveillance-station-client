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

"""Check whether the ffmpeg this app would actually use is a version known
to stall/deadlock muxing a camera's audio (see TROUBLESHOOTING.md).

Spawns bare "ffmpeg", the same way ws_bridge.py resolves the real muxing
ffmpeg via PATH, so this reports on whatever binary the app itself would
use, including a PATH-pinned workaround build, which correctly reads as
unaffected. Failures (ffmpeg missing, unrecognized output) are a
different, already-surfaced problem, not this check's job, so they
return None rather than True/False (this should never raise).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import subprocess

log = logging.getLogger(__name__)

TROUBLESHOOTING_URL = (
    "https://github.com/renaudallard/surveillance-station-client/blob/main/"
    "TROUBLESHOOTING.md#a-camera-with-audio-repeatedly-stalls-or-loses-its-websocket-stream"
)

# ffmpeg 7.0 and higher are known to stall/deadlock muxing live piped
# H.264/HEVC video with PCMU or AAC audio; 6.1.1 is confirmed unaffected.
# See TROUBLESHOOTING.md's own section (linked above) for the upstream
# issue and workarounds.
_FIRST_AFFECTED_MAJOR = 7

# `ffmpeg -version` prints and exits; it does not read stdin or touch a
# camera. A build that hangs here is broken in a way this check cannot
# diagnose, so give up rather than leave the notice pending forever.
_VERSION_PROBE_TIMEOUT = 5.0  # seconds

# Matches ffmpeg's own version banner ("ffmpeg version 8.0.1 Copyright...",
# "... version n7.1.5 ...", a git snapshot's "n" prefix, "... version
# 6.0.1-static ...", a static build's suffix, or "... version
# 7.1.5-0+deb13u1 ..." a distro's patch suffix) -- just the leading major
# number, whatever comes after it. A build straight from master carries no
# major number at all ("ffmpeg version N-121055-g1a2b3c4d"), so it does not
# match and the check stays quiet rather than guessing at one.
_VERSION_RE = re.compile(rb"ffmpeg version n?(\d+)\.")


def _parse_major_version(version_output: bytes) -> int | None:
    """The major version number out of `ffmpeg -version`'s own banner, or
    None if *version_output* doesn't look like it at all."""
    match = _VERSION_RE.search(version_output)
    return int(match.group(1)) if match else None


async def ffmpeg_version_is_affected() -> bool | None:
    """True if the resolved ffmpeg is a known-affected version, False if
    it's a known-safe one, None if that couldn't be determined (ffmpeg
    missing from PATH, output this doesn't recognize, or a probe that
    never finished)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-version",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        log.debug("ffmpeg version check failed (non-fatal): %s", e)
        return None

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_VERSION_PROBE_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        log.debug(
            "ffmpeg version check did not finish in %.0fs (non-fatal)", _VERSION_PROBE_TIMEOUT
        )
        return None

    major = _parse_major_version(stdout)
    if major is None:
        return None
    return major >= _FIRST_AFFECTED_MAJOR
