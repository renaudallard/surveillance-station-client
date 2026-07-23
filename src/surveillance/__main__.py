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

_LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"

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


def main() -> None:
    debug = "--debug" in sys.argv
    if debug:
        sys.argv.remove("--debug")

    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=level, format=_LOG_FORMAT)
    # On the handler, not the root logger: a logger's own filters never see
    # records propagated up from the module loggers the application uses.
    for handler in logging.getLogger().handlers:
        handler.setFormatter(_RedactFormatter(_LOG_FORMAT))

    # Suppress chatty third-party loggers in debug mode
    for name in ("OpenGL", "websockets", "hpack", "httpcore", "httpx"):
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
