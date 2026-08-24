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

"""Entry point for surveillance application."""

from __future__ import annotations

import contextlib
import logging
import re
import sys
from pathlib import Path

# Timestamped so a bug report can be lined up against the timestamps the
# libraries this app drives print on the same stderr (mpv, ffmpeg,
# PipeWire, GTK), which is usually the only way to tell whether a stream
# failed before or after the thing being blamed for it.
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Credentials the client sends as query parameters.
_REDACT_PARAMS = re.compile(
    r"\b(passwd|password|account|otp_code|device_id|_sid)=[^&\s\"']+",
    re.IGNORECASE,
)

# Credentials embedded in a stream URL, as in the rtsp://user:pass@host
# overrides from [camera_overrides].
_REDACT_USERINFO = re.compile(r"(\w+://)[^/\s@]+@")


class _RedactFormatter(logging.Formatter):
    """Strip credentials from log output.

    Redacting the formatted text rather than the record covers exception
    tracebacks too, which quote request URLs with the session id in them.
    """

    def format(self, record: logging.LogRecord) -> str:
        text = _REDACT_PARAMS.sub(r"\1=***", super().format(record))
        return _REDACT_USERINFO.sub(r"\1***@", text)


# Set only for --log-file's auto-named case (never for an explicit
# --log-file=PATH, which the caller named themselves and owns overwriting).
# Read by _mark_log_complete().
_log_complete_path: Path | None = None


def _mark_log_complete() -> None:
    """Touch the auto-named log's completion sentinel on a graceful exit.

    Only ever reached via a clean shutdown (SIGINT/SIGTERM, or app.run()
    returning) -- an OOM SIGKILL or a segfault skips this entirely, which
    is the point: the sentinel's absence is what tells a later session the
    previous log ended abnormally, not any marker the crash itself would
    have had to write (it can't -- it's dead before it gets the chance).
    """
    if _log_complete_path is not None:
        with contextlib.suppress(OSError):
            _log_complete_path.touch()


def _clean_completed_logs(log_dir: Path) -> None:
    """Remove every previous auto-named log whose session shut down
    cleanly (its .complete sentinel is present) -- a crash log (no
    sentinel) is left alone for inspection.

    Run once per startup, before this session's own log/sentinel exist,
    so disk usage only grows across a run of sessions that keep
    crashing, never just from ordinary, uneventful use.
    """
    for sentinel in log_dir.glob("debug-*.log.complete"):
        log_file = sentinel.with_name(sentinel.name.removesuffix(".complete"))
        with contextlib.suppress(OSError):
            log_file.unlink()
        with contextlib.suppress(OSError):
            sentinel.unlink()


def _parse_log_file_arg(argv: list[str]) -> tuple[str | None, list[str]]:
    """Extract --log-file/--log-file=PATH from *argv*, if present.

    Returns (value, remaining_argv). value is None if the flag was not
    given at all, "" if given bare (--log-file's auto-named case), or
    the path if given with one (--log-file=PATH). Every matching entry
    is removed from remaining_argv -- what is left goes on to
    Gio.Application, which rejects a flag it does not know -- with the
    last occurrence winning if the flag was given more than once.
    """
    log_file_arg: str | None = None
    remaining_argv: list[str] = []
    for arg in argv:
        if arg == "--log-file" or arg.startswith("--log-file="):
            log_file_arg = arg.split("=", 1)[1] if "=" in arg else ""
        else:
            remaining_argv.append(arg)
    return log_file_arg, remaining_argv


def main() -> None:
    # Every occurrence, not just the first: what is left of argv goes to
    # Gio.Application, which rejects a flag it does not know.
    debug = "--debug" in sys.argv
    while "--debug" in sys.argv:
        sys.argv.remove("--debug")

    # --log-file alone falls back to an auto-named file under
    # STATE_DIR/logs, one per run, paired with a completion sentinel so a
    # later session can tell a crashed run's log from a finished one --
    # and clean up every earlier one that shut down normally (see
    # _clean_completed_logs), so this never grows unbounded from ordinary
    # use, only across crashes. --log-file=PATH instead writes to exactly
    # that path, truncated every run -- once the caller has named it
    # themselves, that's their call, not something to protect or clean up.
    log_file_arg, remaining_argv = _parse_log_file_arg(sys.argv)
    sys.argv[:] = remaining_argv

    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=level, format=_LOG_FORMAT)
    # On the handler, not the root logger: a logger's own filters never see
    # records propagated up from the module loggers the application uses.
    for handler in logging.getLogger().handlers:
        handler.setFormatter(_RedactFormatter(_LOG_FORMAT))

    if log_file_arg is not None:
        # expanduser() because no shell expands a tilde after the = in an
        # option word, so --log-file=~/x.log arrives here literally.
        # Everything that can fail on the way to an open file is one
        # diagnostic rather than a traceback: this runs before the window
        # exists, and a log destination the user got wrong should not read
        # like a crash in the app they were trying to record.
        try:
            if log_file_arg:
                log_path = Path(log_file_arg).expanduser()
            else:
                from datetime import datetime

                from surveillance.config import STATE_DIR

                log_dir = STATE_DIR / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                _clean_completed_logs(log_dir)
                log_path = log_dir / f"debug-{datetime.now():%Y%m%dT%H%M%S}.log"
                global _log_complete_path
                _log_complete_path = log_path.with_name(log_path.name + ".complete")
            file_handler = logging.FileHandler(log_path, mode="w")
        except OSError as e:
            sys.exit(f"surveillance: cannot open log file: {e}")
        file_handler.setFormatter(_RedactFormatter(_LOG_FORMAT))
        logging.getLogger().addHandler(file_handler)
        print(f"Logging to {log_path}")

    # Suppress chatty third-party loggers in debug mode
    for name in ("OpenGL", "websockets", "hpack", "httpcore", "httpx"):
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    import os
    import signal

    def _graceful_exit(*_args: object) -> None:
        _mark_log_complete()
        os._exit(0)

    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)

    from surveillance.app import SurveillanceApp

    # Register AFTER all imports so it runs BEFORE concurrent.futures
    # atexit handler that hangs joining executor threads (LIFO order).
    __import__("atexit").register(os._exit, 0)

    app = SurveillanceApp()
    app.run(sys.argv)
    _mark_log_complete()
    os._exit(0)


if __name__ == "__main__":
    main()
