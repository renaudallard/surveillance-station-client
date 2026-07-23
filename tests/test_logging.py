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

"""Tests for debug-log credential redaction."""

from __future__ import annotations

import io
import logging

from surveillance.__main__ import _LOG_FORMAT, _RedactFormatter


def _emit(msg: str, *args: object, exc: BaseException | None = None) -> str:
    """Log through a module logger the way the application does, return output."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_RedactFormatter(_LOG_FORMAT))
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    try:
        log = logging.getLogger("surveillance.api.client")
        if exc is not None:
            try:
                raise exc
            except type(exc):
                log.exception(msg, *args)
        else:
            log.debug(msg, *args)
    finally:
        root.handlers, root.level = old_handlers, old_level
    return stream.getvalue()


class TestRedaction:
    def test_module_logger_is_redacted(self) -> None:
        """The application never logs through the root logger itself."""
        out = _emit("request %s", "https://nas/webapi/entry.cgi?passwd=hunter2&_sid=ABCDEF")
        assert "hunter2" not in out
        assert "ABCDEF" not in out
        assert "passwd=***" in out
        assert "_sid=***" in out

    def test_login_parameters(self) -> None:
        out = _emit("params: account=admin&passwd=s3cret&otp_code=123456&device_id=tok")
        for secret in ("admin", "s3cret", "123456", "tok"):
            assert secret not in out
        assert out.count("=***") == 4

    def test_stream_url_credentials(self) -> None:
        out = _emit("Starting stream: %s", "rtsp://admin:letmein@192.168.1.50:554/h265")
        assert "admin" not in out
        assert "letmein" not in out
        assert "rtsp://***@192.168.1.50:554/h265" in out

    def test_traceback_is_redacted(self) -> None:
        out = _emit("stream failed", exc=ValueError("GET https://nas/x.cgi?_sid=LEAKED failed"))
        assert "LEAKED" not in out
        assert "Traceback" in out

    def test_ordinary_fields_survive(self) -> None:
        out = _emit("query: api=SYNO.SurveillanceStation.Camera&cameraId=5&version=9")
        assert "api=SYNO.SurveillanceStation.Camera" in out
        assert "cameraId=5" in out

    def test_plain_url_keeps_host(self) -> None:
        out = _emit("connecting to %s", "wss://nas:5001/webman/3rdparty/x?api=y")
        assert "wss://nas:5001/webman/3rdparty/x?api=y" in out
