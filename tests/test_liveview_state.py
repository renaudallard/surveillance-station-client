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

"""Tests for LiveView's own state handling, driven on stand-ins: a real
LiveView needs a display, and these methods read only what the stand-in
carries (the same technique as test_mpv_profiles' _applied)."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from surveillance.ui import liveview
from surveillance.ui.liveview import LiveView


class _FakeGLib:
    """Records timeout sources the way GLib hands out ids for them."""

    def __init__(self) -> None:
        self.added: list[tuple[int, Callable[[], bool]]] = []
        self.removed: list[int] = []

    def timeout_add(self, interval: int, callback: Callable[[], bool]) -> int:
        self.added.append((interval, callback))
        return len(self.added)

    def source_remove(self, source_id: int) -> None:
        self.removed.append(source_id)


class TestPageTickers:
    """The two one-second tickers follow the page on and off screen.

    Started on map and stopped on unmap: a page the stack covers stays
    realized, and a login replaces the whole LiveView, so a ticker bound
    to neither would keep an old page alive and ticking forever."""

    @pytest.fixture
    def glib(self, monkeypatch: pytest.MonkeyPatch) -> _FakeGLib:
        fake = _FakeGLib()
        monkeypatch.setattr(liveview, "GLib", fake)
        return fake

    @staticmethod
    def _page() -> SimpleNamespace:
        return SimpleNamespace(
            _focus_idle_id=0,
            _history_tick_id=0,
            _check_timeline_focus_idle=lambda: True,
            _tick_history_positions=lambda: True,
        )

    def test_map_starts_both_once(self, glib: _FakeGLib) -> None:
        page = self._page()
        LiveView._on_map(page, None)  # type: ignore[arg-type]
        LiveView._on_map(page, None)  # type: ignore[arg-type]
        assert [interval for interval, _ in glib.added] == [1000, 1000]
        assert {callback for _, callback in glib.added} == {
            page._check_timeline_focus_idle,
            page._tick_history_positions,
        }

    def test_unmap_removes_both_and_only_once(self, glib: _FakeGLib) -> None:
        page = self._page()
        LiveView._on_map(page, None)  # type: ignore[arg-type]
        LiveView._on_unmap(page, None)  # type: ignore[arg-type]
        LiveView._on_unmap(page, None)  # type: ignore[arg-type]
        assert sorted(glib.removed) == [1, 2]
        assert (page._focus_idle_id, page._history_tick_id) == (0, 0)

    def test_remap_starts_again(self, glib: _FakeGLib) -> None:
        page = self._page()
        LiveView._on_map(page, None)  # type: ignore[arg-type]
        LiveView._on_unmap(page, None)  # type: ignore[arg-type]
        LiveView._on_map(page, None)  # type: ignore[arg-type]
        assert len(glib.added) == 4
        assert page._focus_idle_id and page._history_tick_id
