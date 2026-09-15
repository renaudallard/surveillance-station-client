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

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from surveillance.services import ffmpeg_check
from surveillance.services.ffmpeg_check import _parse_major_version, ffmpeg_version_is_affected


class TestParseMajorVersion:
    def test_plain_release(self) -> None:
        assert _parse_major_version(b"ffmpeg version 8.0.1 Copyright (c) 2000-2025") == 8

    def test_static_build_suffix(self) -> None:
        assert _parse_major_version(b"ffmpeg version 6.0.1-static https://...") == 6

    def test_distro_patch_suffix(self) -> None:
        assert _parse_major_version(b"ffmpeg version 7.1.5-0+deb13u1 Copyright") == 7

    def test_git_snapshot_n_prefix(self) -> None:
        assert _parse_major_version(b"ffmpeg version n7.1.5 Copyright") == 7

    def test_unrecognized_output_is_none(self) -> None:
        assert _parse_major_version(b"not ffmpeg at all") is None

    def test_empty_output_is_none(self) -> None:
        assert _parse_major_version(b"") is None


class _FakeVersionProc:
    """Stand-in for asyncio.subprocess.Process. Avoids spawning a real
    ffmpeg, the same way test_ws_bridge.py's _FakeValidationProc does for
    its own throwaway ffmpeg probe."""

    def __init__(self, stdout: bytes) -> None:
        self._stdout = stdout

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, b""


class TestFfmpegVersionIsAffected:
    @pytest.mark.asyncio
    async def test_affected_version_is_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_exec(*args: Any, **kwargs: Any) -> Any:
            return _FakeVersionProc(b"ffmpeg version 8.0.1 Copyright")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        assert await ffmpeg_version_is_affected() is True

    @pytest.mark.asyncio
    async def test_unaffected_version_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_exec(*args: Any, **kwargs: Any) -> Any:
            return _FakeVersionProc(b"ffmpeg version 6.1.1 Copyright")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        assert await ffmpeg_version_is_affected() is False

    @pytest.mark.asyncio
    async def test_missing_ffmpeg_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_exec(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("no such file: ffmpeg")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        assert await ffmpeg_version_is_affected() is None

    @pytest.mark.asyncio
    async def test_unrecognized_output_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_exec(*args: Any, **kwargs: Any) -> Any:
            return _FakeVersionProc(b"")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        assert await ffmpeg_version_is_affected() is None

    @pytest.mark.asyncio
    async def test_spawns_bare_ffmpeg_relying_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Must resolve the same binary ws_bridge.py's real muxing ffmpeg
        would (bare "ffmpeg" on PATH), so a PATH-pinned workaround build
        is what gets checked, not some other fixed location."""
        seen: list[tuple[Any, ...]] = []

        async def _fake_exec(*args: Any, **kwargs: Any) -> Any:
            seen.append(args)
            return _FakeVersionProc(b"ffmpeg version 6.1.1 Copyright")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        await ffmpeg_version_is_affected()
        assert seen == [("ffmpeg", "-version")]


class _HangingVersionProc:
    """An ffmpeg that never answers. Records whether it was killed, since
    walking away without killing it leaves the process behind."""

    def __init__(self) -> None:
        self.killed = False

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    def kill(self) -> None:
        self.killed = True


class TestVersionProbeTimeout:
    """The check runs at startup off the main loop, so a hung probe never
    freezes the UI, but it would leave the notice pending for the life of
    the session and the process behind it running."""

    @pytest.mark.asyncio
    async def test_a_hung_probe_gives_up_and_kills_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _HangingVersionProc()

        async def _fake_exec(*args: Any, **kwargs: Any) -> Any:
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        monkeypatch.setattr(ffmpeg_check, "_VERSION_PROBE_TIMEOUT", 0.05)

        assert await ffmpeg_version_is_affected() is None
        assert proc.killed, "a probe left running is a process nothing will reap"


class TestDocAgreesWithTheConstant:
    """The notice tells the user to read TROUBLESHOOTING.md, so the two
    have to name the same version. An earlier version of this test only
    compared the constant with the literal it is defined as, which meant
    editing one required editing the other and nothing was cross-checked;
    this one reads the document.
    """

    def _doc(self) -> str:
        path = Path(__file__).resolve().parents[1] / "TROUBLESHOOTING.md"
        return path.read_text(encoding="utf-8")

    def test_the_first_affected_major_is_the_one_documented(self) -> None:
        match = re.search(r"On ffmpeg (\d+)\.0 and higher", self._doc())
        assert match is not None, (
            "TROUBLESHOOTING.md no longer says which ffmpeg major is affected; "
            "the notice links users to a document that stopped answering that"
        )
        assert int(match.group(1)) == ffmpeg_check._FIRST_AFFECTED_MAJOR

    def test_the_version_called_unaffected_really_is(self) -> None:
        """The doc tells users to pin that build, so the check must agree
        it is safe or the notice fires on the very fix it recommends."""
        match = re.search(r"ffmpeg (\d+)\.[\d.]+ is unaffected", self._doc())
        assert match is not None, "TROUBLESHOOTING.md no longer names a known-good build"
        assert int(match.group(1)) < ffmpeg_check._FIRST_AFFECTED_MAJOR
