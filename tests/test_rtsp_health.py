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

"""Tests for the RTSP stream health monitor's stall/recover logic."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from surveillance.ui.mpv_widget import MpvGLArea
from surveillance.ui.rtsp_health import RtspHealthMonitor


class _FakePlayer:
    def __init__(self, positions: Sequence[float | None]) -> None:
        self._positions = positions
        self._i = 0
        self.play_calls = 0

    @property
    def time_pos(self) -> float | None:
        p = self._positions[min(self._i, len(self._positions) - 1)]
        self._i += 1
        return p

    def stop(self) -> None:
        pass

    def play(self, url: str, **kwargs: object) -> None:
        self.play_calls += 1


def _monitor(positions: Sequence[float | None]) -> tuple[RtspHealthMonitor, _FakePlayer, list]:
    gave_up: list[str] = []
    recovered: list[bool] = []
    player = _FakePlayer(positions)
    # The monitor only ever reads time_pos and calls stop()/play(), which is
    # all _FakePlayer provides, so it stands in for the real widget here.
    mon = RtspHealthMonitor(
        cast("MpvGLArea", player),
        "rtsp://cam/live",
        "Front",
        gave_up.append,
        lambda: recovered.append(True),
    )
    mon.stop()  # cancel the real GLib timer; we drive _check() manually
    return mon, player, [gave_up, recovered]


class TestRtspHealth:
    def test_slow_first_frame_is_not_killed(self) -> None:
        """A camera that takes several intervals to produce its first frame
        must NOT be restarted or given up on: buffering is not a stall."""
        # No time_pos for four checks (still connecting), then it advances.
        mon, player, (gave_up, recovered) = _monitor([None, None, None, None, 1.0, 2.0])
        for _ in range(6):
            mon._check()
        assert player.play_calls == 0, "must not abort an in-progress connect"
        assert not gave_up, f"a slow-but-working connect must not give up: {gave_up}"
        assert recovered, "should recover once the first frames arrive"

    def test_never_produces_video_gives_up_without_restarting(self) -> None:
        """A stream that never produces a frame gives up after the startup
        window, without the destructive stop()/play() retry."""
        mon, player, (gave_up, _recovered) = _monitor([None] * 20)
        for _ in range(20):
            if not mon._check():
                break
        assert gave_up == ["no video"], gave_up
        assert player.play_calls == 0, "must not restart a stream that never started"

    def test_recovers_after_a_mid_stream_stall_and_replay(self) -> None:
        """A stream that advanced, then froze, is replayed and recovers when
        the fresh feed advances from a low time_pos (not read as a stall)."""
        # advance 10->11, freeze at 11 (stall -> replay), fresh feed 2->3.
        mon, player, (gave_up, recovered) = _monitor([10.0, 11.0, 11.0, 11.0, 2.0, 3.0])
        for _ in range(6):
            mon._check()
        assert player.play_calls >= 1, "should have retried on a real stall"
        assert not gave_up, f"should not give up on a recoverable stream: {gave_up}"
        assert recovered, "should report recovery once the replayed stream advances"

    def test_a_stream_that_freezes_for_good_gives_up(self) -> None:
        """A stream that advances once then freezes forever gives up in a
        bounded number of checks rather than retrying indefinitely."""
        # advance 10->11, then frozen at 11 forever across replays.
        mon, _player, (gave_up, _recovered) = _monitor([10.0, 11.0] + [11.0] * 20)
        for _ in range(20):
            if not mon._check():
                break
        assert gave_up, "a permanently frozen stream must give up"

    def test_set_paused_suspends_stall_detection(self) -> None:
        """A frozen time_pos while deliberately paused (Live View's
        timeline Pause button) must not be read as a dead stream --
        see set_paused's own docstring. Checks while paused read
        time_pos not at all, so two positions cover an advancing
        stream followed by any number of paused checks."""
        mon, player, (gave_up, _recovered) = _monitor([10.0, 11.0])
        mon._check()
        mon._check()
        mon.set_paused(True)
        for _ in range(10):
            mon._check()
        assert not gave_up, "a deliberate pause must not be read as a dead stream"
        assert player.play_calls == 0, "must not retry play() while deliberately paused"

    def test_resume_clears_the_stall_baseline(self) -> None:
        """Resuming must not immediately read the paused gap as zero
        progress -- the first post-resume check should read like a
        fresh startup window, not an instant stall (see set_paused's
        own docstring)."""
        mon, player, (gave_up, recovered) = _monitor([10.0, 11.0, 11.0, 12.0])
        mon._check()
        mon._check()
        mon.set_paused(True)
        for _ in range(10):
            mon._check()
        mon.set_paused(False)
        mon._check()  # first tick after resume: startup-like, not a stall
        mon._check()  # advances again -> recovered
        assert not gave_up
        assert player.play_calls == 0
        assert recovered
