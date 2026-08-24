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

"""The --log-file destination and its completion sentinel.

Kept out of __main__ deliberately. This state has to be reachable from
the quit paths in app.py and ui/window.py, and __main__ is not
importable from them: under python -m surveillance the interpreter runs
it as the module __main__, so importing surveillance.__main__ builds a
second copy with its own globals, and a sentinel set in one is invisible
to the other.
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import datetime
from pathlib import Path

from surveillance.config import STATE_DIR

# Set only for --log-file's auto-named case, never for an explicit
# --log-file=PATH, which the caller named themselves and owns
# overwriting. Read by mark_complete().
_complete_path: Path | None = None


def parse_arg(argv: list[str]) -> tuple[str | None, list[str]]:
    """Extract --log-file/--log-file=PATH from *argv*, if present.

    Returns (value, remaining_argv). value is None if the flag was not
    given at all, "" if given bare (the auto-named case), or the path if
    given with one. Every matching entry is removed from remaining_argv,
    because what is left goes on to Gio.Application, which rejects a flag
    it does not know. The last occurrence wins.
    """
    log_file_arg: str | None = None
    remaining_argv: list[str] = []
    for arg in argv:
        if arg == "--log-file" or arg.startswith("--log-file="):
            log_file_arg = arg.split("=", 1)[1] if "=" in arg else ""
        else:
            remaining_argv.append(arg)
    return log_file_arg, remaining_argv


def mark_complete() -> None:
    """Touch the auto-named log's completion sentinel on a graceful exit.

    Reached only from a clean shutdown. An OOM kill or a segfault skips
    it entirely, which is the point: the sentinel's absence is what tells
    a later session the previous log ended abnormally, since a process
    that died that way never got the chance to write a marker of its own.
    """
    if _complete_path is not None:
        with contextlib.suppress(OSError):
            _complete_path.touch()


def _clean_completed(log_dir: Path) -> None:
    """Remove every previous auto-named log whose session shut down
    cleanly, leaving a crash log (no sentinel) alone for inspection.

    Run once per startup, before this session's own log and sentinel
    exist, so disk usage only grows across sessions that keep crashing
    and not from ordinary, uneventful use.
    """
    for sentinel in log_dir.glob("debug-*.log.complete"):
        log_file = sentinel.with_name(sentinel.name.removesuffix(".complete"))
        with contextlib.suppress(OSError):
            log_file.unlink()
        with contextlib.suppress(OSError):
            sentinel.unlink()


def install(log_file_arg: str, formatter: logging.Formatter) -> Path:
    """Add a file handler for --log-file and return where it writes.

    Raises OSError if the destination cannot be opened, which the caller
    turns into a diagnostic. expanduser() because no shell expands a
    tilde after the = in an option word, so --log-file=~/x.log arrives
    here literally.
    """
    global _complete_path

    if log_file_arg:
        log_path = Path(log_file_arg).expanduser()
    else:
        log_dir = STATE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _clean_completed(log_dir)
        # Per-process name, for the same reason config.py qualifies its
        # temp file: the timestamp only resolves to the second, so two
        # instances started inside one second would open the same path
        # with mode="w" and write into it at independent offsets, which
        # loses one session's log and splices a fragment into the other.
        log_path = log_dir / f"debug-{datetime.now():%Y%m%dT%H%M%S}-{os.getpid()}.log"
        _complete_path = log_path.with_name(log_path.name + ".complete")
    handler = logging.FileHandler(log_path, mode="w")
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)
    return log_path
