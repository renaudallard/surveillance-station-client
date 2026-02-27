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

import logging
import re
import sys

_REDACT_RE = re.compile(
    r"(passwd|_sid|account)=[^&\s\"]+",
    re.IGNORECASE,
)


class _RedactFilter(logging.Filter):
    """Strip passwords, session IDs, and usernames from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _REDACT_RE.sub(r"\1=***", record.msg)
        return True


def main() -> None:
    debug = "--debug" in sys.argv
    if debug:
        sys.argv.remove("--debug")

    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().addFilter(_RedactFilter())

    # Suppress chatty third-party loggers in debug mode
    for name in ("OpenGL", "websockets", "hpack", "httpcore"):
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    import os
    import signal

    signal.signal(signal.SIGINT, lambda *_: os._exit(0))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))

    from surveillance.app import SurveillanceApp

    # Register AFTER all imports so it runs BEFORE concurrent.futures
    # atexit handler that hangs joining executor threads (LIFO order).
    __import__("atexit").register(os._exit, 0)

    app = SurveillanceApp()
    app.run(sys.argv)
    os._exit(0)


if __name__ == "__main__":
    main()
