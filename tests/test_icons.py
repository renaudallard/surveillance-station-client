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

"""Every app icon the code names has an SVG, and every SVG is used."""

from __future__ import annotations

import re
from pathlib import Path

import surveillance

_PACKAGE = Path(surveillance.__file__).parent
_ICON_DIR = _PACKAGE / "data" / "icons" / "hicolor" / "scalable" / "actions"
_NAME = re.compile(r"surveillance-[a-z0-9-]+-symbolic")


def _names_in_code() -> set[str]:
    return {name for path in _PACKAGE.rglob("*.py") for name in _NAME.findall(path.read_text())}


def _names_on_disk() -> set[str]:
    return {path.stem for path in _ICON_DIR.glob("*.svg")}


def test_every_icon_named_in_the_code_has_an_svg() -> None:
    assert _names_in_code() <= _names_on_disk()


def test_every_svg_is_used() -> None:
    assert _names_on_disk() <= _names_in_code()


def test_no_svg_uses_a_transform() -> None:
    """GTK's own symbolic-icon renderer ignored a transform: a mirrored
    reverse icon came out identical to the forward one. Draw the final
    coordinates instead."""
    for path in _ICON_DIR.glob("*.svg"):
        assert "transform" not in path.read_text(), path.name
