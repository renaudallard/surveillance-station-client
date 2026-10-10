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

"""Tests for the camera settings dialog's direct-URL validation."""

from __future__ import annotations

import pytest

from surveillance.ui.camera_settings import validate_rtsp_url


@pytest.mark.parametrize(
    "url",
    [
        "rtsp://user:pass@192.168.1.10:554/stream",
        "rtsps://camera.local/stream",
        "http://camera.local/video.mjpg",
    ],
)
def test_accepts_a_stream_url(url: str) -> None:
    assert validate_rtsp_url(url) is None


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("", "must not be empty"),
        ("ftp://camera.local/stream", "Unsupported scheme"),
        ("rtsp:///stream", "hostname"),
    ],
)
def test_rejects_a_bad_url(url: str, message: str) -> None:
    error = validate_rtsp_url(url)
    assert error is not None
    assert message in error
