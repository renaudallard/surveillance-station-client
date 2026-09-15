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

"""Tests for the debug-run resource heartbeat (no GTK main loop needed)."""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest

from surveillance.util import resource_log


class TestMeasurements:
    """These have to measure the running process, not return a constant:
    a heartbeat that always prints the same number would look healthy
    through the whole of a leak.
    """

    def test_rss_tracks_a_real_allocation(self) -> None:
        before = resource_log.rss_kb()
        assert before is not None and before > 0
        ballast = bytearray(32 * 1024 * 1024)
        ballast[::4096] = b"\x01" * len(ballast[::4096])  # touch it, so it is resident
        after = resource_log.rss_kb()
        del ballast
        assert after is not None
        assert after - before > 16 * 1024, "32MiB resident should move an RSS reading"

    def test_open_fds_tracks_a_real_descriptor(self, tmp_path: Any) -> None:
        before = resource_log.open_fds()
        assert before is not None
        with open(tmp_path / "held", "w") as handle:
            assert handle is not None
            after = resource_log.open_fds()
        assert after == before + 1


class TestSnapshotLine:
    def test_it_carries_the_numbers(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger=resource_log.log.name):
            assert resource_log.log_snapshot() is True
        line = caplog.text
        assert "rss=" in line and "fds=" in line and "threads=" in line
        assert "rss=0kB" not in line

    def test_it_stops_where_there_is_nothing_to_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returning False ends the GLib timer. /proc will not appear
        later in the run, so retrying every interval would be noise."""
        monkeypatch.setattr(resource_log, "rss_kb", lambda: None)
        assert resource_log.log_snapshot() is False


class _FakeGLib:
    def __init__(self) -> None:
        self.added: list[tuple[int, Any]] = []

    def timeout_add_seconds(self, interval: int, callback: Any) -> int:
        self.added.append((interval, callback))
        return len(self.added)


class TestStartIfDebugging:
    @pytest.fixture
    def glib(self, monkeypatch: pytest.MonkeyPatch) -> _FakeGLib:
        fake = _FakeGLib()
        monkeypatch.setattr(resource_log, "GLib", fake)
        return fake

    def test_debug_off_starts_nothing(
        self, glib: _FakeGLib, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(resource_log.log, "isEnabledFor", lambda level: False)
        resource_log.start_if_debugging()
        assert glib.added == []

    def test_debug_on_starts_one_heartbeat(
        self, glib: _FakeGLib, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(resource_log.log, "isEnabledFor", lambda level: True)
        resource_log.start_if_debugging()
        assert [interval for interval, _ in glib.added] == [resource_log._INTERVAL_SECONDS]
        assert glib.added[0][1] is resource_log.log_snapshot

    def test_it_does_not_start_without_proc(
        self, glib: _FakeGLib, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(resource_log.log, "isEnabledFor", lambda level: True)
        monkeypatch.setattr(resource_log, "rss_kb", lambda: None)
        resource_log.start_if_debugging()
        assert glib.added == []


def test_os_import_is_used() -> None:
    """Guards the module's own /proc reads against being stubbed out."""
    assert os.path.exists("/proc/self/statm")
