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

"""Tests for the Advanced Search dialog's own parsing, run on stand-ins
for its calendar and entry (a real dialog needs a display)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from surveillance.ui.advanced_search import AdvancedSearchDialog


def _parse(text: str, default_time: str = "00:00:00") -> datetime:
    calendar = SimpleNamespace(
        get_date=lambda: SimpleNamespace(
            get_year=lambda: 2026, get_month=lambda: 9, get_day_of_month=lambda: 10
        )
    )
    entry = SimpleNamespace(get_text=lambda: text)
    return AdvancedSearchDialog._get_datetime(None, calendar, entry, default_time)  # type: ignore[arg-type]


class TestTimeEntry:
    def test_a_valid_time_is_kept(self) -> None:
        assert _parse("13:45:10") == datetime(2026, 9, 10, 13, 45, 10)

    def test_an_empty_entry_takes_the_default(self) -> None:
        assert _parse("", "23:59:59") == datetime(2026, 9, 10, 23, 59, 59)

    @pytest.mark.parametrize("text", ["24:00:00", "23:60:00", "12:00:99", "abc", "9:5"])
    def test_garbage_and_out_of_range_fields_mean_midnight(self, text: str) -> None:
        """A field outside its range used to raise out of the Search
        button; it means midnight now, the same as garbage always has."""
        assert _parse(text) == datetime(2026, 9, 10)
