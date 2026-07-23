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

"""Grid layout definitions shared by the live view and the header bar.

Kept in its own module so the header bar can build its layout menu without
importing the live view, which pulls in mpv, OpenGL and websockets.
"""

from __future__ import annotations

DEFAULT_LAYOUT = "2x2"

# Physical slot indices visible in each layout. The internal grid is always
# 4x4 (16 slots) and slots are only shown or hidden, never removed.
LAYOUT_VISIBLE: dict[str, list[int]] = {
    "1x1": [0],
    "2x2": [0, 1, 4, 5],
    "3x3": [0, 1, 2, 4, 5, 6, 8, 9, 10],
    "4x4": list(range(16)),
}


def valid_layout(name: str) -> str:
    """Return *name* if it is a known layout, the default otherwise."""
    return name if name in LAYOUT_VISIBLE else DEFAULT_LAYOUT
