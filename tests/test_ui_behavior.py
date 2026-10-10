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

"""Tests for recording filter, download, and preset logic (no GTK required)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from surveillance.api.client import SurveillanceAPI
from surveillance.api.models import Recording
from surveillance.config import AppConfig, ConnectionProfile


def _stream_mock(data: bytes) -> MagicMock:
    """Stand-in for SurveillanceAPI.stream_download.

    It is an async generator function, not a coroutine, so AsyncMock is the
    wrong shape: calling it must return something `async for` can iterate.
    """

    def _call(**kwargs: object) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            if data:
                yield data

        return _gen()

    return MagicMock(side_effect=_call)


@pytest.fixture
def profile() -> ConnectionProfile:
    return ConnectionProfile(name="test", host="192.168.1.100")


@pytest.fixture
def api(profile: ConnectionProfile) -> SurveillanceAPI:
    client = SurveillanceAPI(profile)
    client.sid = "test-sid"
    return client


class TestSnapshotFilterParams:
    """list_snapshots sends the params the official client uses."""

    @pytest.mark.asyncio
    async def test_camera_filter_uses_cam_id_list(self, api: SurveillanceAPI) -> None:
        from surveillance.services.snapshot import list_snapshots

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"snapshots": [], "total": 0}
        ) as mock:
            await list_snapshots(api, camera_id=5)
            params = mock.call_args[1]["extra_params"]
            assert params["camIdList"] == "5"
            assert "camId" not in params

    @pytest.mark.asyncio
    async def test_no_camera_filter_omits_param(self, api: SurveillanceAPI) -> None:
        from surveillance.services.snapshot import list_snapshots

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"snapshots": [], "total": 0}
        ) as mock:
            await list_snapshots(api)
            params = mock.call_args[1]["extra_params"]
            assert "camIdList" not in params
            assert "camId" not in params


class TestRecordingFilterParams:
    """list_recordings sends the correct query params for filter combinations."""

    @pytest.mark.asyncio
    async def test_no_filters(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"events": [], "total": 0}
        ) as mock:
            await list_recordings(api)
            params = mock.call_args[1]["extra_params"]
            assert "cameraIds" not in params
            assert "fromTime" not in params
            assert "toTime" not in params
            assert params["offset"] == "0"
            assert params["limit"] == "50"

    @pytest.mark.asyncio
    async def test_single_camera_filter(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"events": [], "total": 0}
        ) as mock:
            await list_recordings(api, camera_ids=[5])
            params = mock.call_args[1]["extra_params"]
            assert params["cameraIds"] == "5"

    @pytest.mark.asyncio
    async def test_multi_camera_filter(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"events": [], "total": 0}
        ) as mock:
            await list_recordings(api, camera_ids=[1, 3, 7])
            params = mock.call_args[1]["extra_params"]
            assert params["cameraIds"] == "1,3,7"

    @pytest.mark.asyncio
    async def test_time_range_filter(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"events": [], "total": 0}
        ) as mock:
            await list_recordings(api, from_time=1700000000, to_time=1700086400)
            params = mock.call_args[1]["extra_params"]
            assert params["fromTime"] == "1700000000"
            assert params["toTime"] == "1700086400"

    @pytest.mark.asyncio
    async def test_from_time_only(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"events": [], "total": 0}
        ) as mock:
            await list_recordings(api, from_time=1700000000)
            params = mock.call_args[1]["extra_params"]
            assert params["fromTime"] == "1700000000"
            assert "toTime" not in params

    @pytest.mark.asyncio
    async def test_to_time_only(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"events": [], "total": 0}
        ) as mock:
            await list_recordings(api, to_time=1700086400)
            params = mock.call_args[1]["extra_params"]
            assert "fromTime" not in params
            assert params["toTime"] == "1700086400"

    @pytest.mark.asyncio
    async def test_combined_camera_and_time(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"events": [], "total": 0}
        ) as mock:
            await list_recordings(
                api,
                camera_ids=[2, 4],
                from_time=1700000000,
                to_time=1700086400,
                offset=50,
                limit=25,
            )
            params = mock.call_args[1]["extra_params"]
            assert params["cameraIds"] == "2,4"
            assert params["fromTime"] == "1700000000"
            assert params["toTime"] == "1700086400"
            assert params["offset"] == "50"
            assert params["limit"] == "25"

    @pytest.mark.asyncio
    async def test_pagination_offset(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"events": [], "total": 0}
        ) as mock:
            await list_recordings(api, offset=100, limit=50)
            params = mock.call_args[1]["extra_params"]
            assert params["offset"] == "100"
            assert params["limit"] == "50"

    @pytest.mark.asyncio
    async def test_legacy_camera_id_param(self, api: SurveillanceAPI) -> None:
        """Single camera_id (legacy) is sent as cameraIds."""
        from surveillance.services.recording import list_recordings

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"events": [], "total": 0}
        ) as mock:
            await list_recordings(api, camera_id=9)
            params = mock.call_args[1]["extra_params"]
            assert params["cameraIds"] == "9"

    @pytest.mark.asyncio
    async def test_camera_ids_takes_priority_over_camera_id(self, api: SurveillanceAPI) -> None:
        """camera_ids wins over camera_id when both are provided."""
        from surveillance.services.recording import list_recordings

        with patch.object(
            api, "request", new_callable=AsyncMock, return_value={"events": [], "total": 0}
        ) as mock:
            await list_recordings(api, camera_id=1, camera_ids=[2, 3])
            params = mock.call_args[1]["extra_params"]
            assert params["cameraIds"] == "2,3"


class TestPresetLabels:
    """One mapping for the preset names, shared by every page.

    The three pages each carried their own identical copy until Events
    was found short the 30-day entry and printed a raw "last30d". With a
    single mapping the remaining way back into that state is to add a
    preset and forget to name it, which is what these guard.
    """

    def test_every_preset_has_a_label(self) -> None:
        from surveillance.services import recording

        keys = {
            value
            for name, value in vars(recording).items()
            if name.startswith("PRESET_") and isinstance(value, str)
        }
        assert keys == set(recording.PRESET_LABELS)

    def test_preset_range_serves_every_labelled_preset(self) -> None:
        """A label for a preset preset_range cannot resolve would put the
        name in front of the user and then raise when it is used."""
        from surveillance.services.recording import PRESET_LABELS, preset_range

        for key in PRESET_LABELS:
            from_ts, to_ts = preset_range(key)
            assert from_ts < to_ts, key


class TestPresetRange:
    """preset_range() returns correct (from_time, to_time) windows."""

    def test_today_range_is_full_day(self) -> None:
        """ "to" must be a fixed end-of-day, not "now"."""
        from surveillance.services.recording import preset_range

        from_ts, to_ts = preset_range("today")
        from_dt = datetime.fromtimestamp(from_ts)
        to_dt = datetime.fromtimestamp(to_ts)
        assert from_dt.hour == 0
        assert from_dt.minute == 0
        assert from_dt.second == 0
        assert to_dt.hour == 23
        assert to_dt.minute == 59
        assert to_dt.second == 59
        assert from_dt.date() == to_dt.date()

    def test_yesterday_range_is_full_day(self) -> None:
        from surveillance.services.recording import preset_range

        from_ts, to_ts = preset_range("yesterday")
        from_dt = datetime.fromtimestamp(from_ts)
        to_dt = datetime.fromtimestamp(to_ts)
        assert from_dt.hour == 0
        assert from_dt.minute == 0
        assert to_dt.hour == 23
        assert to_dt.minute == 59
        # Yesterday is the day before today
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        assert from_dt.date() == yesterday_start.date()

    def test_last24h_range_is_24_hours(self) -> None:
        from surveillance.services.recording import preset_range

        from_ts, to_ts = preset_range("last24h")
        diff_hours = (to_ts - from_ts) / 3600
        assert 23.9 <= diff_hours <= 24.1

    def test_last7d_range_is_full_days(self) -> None:
        self._assert_full_days_back("last7d", 7)

    def test_last30d_range_is_full_days(self) -> None:
        self._assert_full_days_back("last30d", 30)

    def _assert_full_days_back(self, preset: str, days: int) -> None:
        """Both ends land on a day boundary, N days apart on the calendar.

        Asserted as wall-clock components and calendar dates rather than as
        an elapsed-seconds width: preset_range does naive local arithmetic,
        so a DST transition inside the window shifts the real elapsed time
        by an hour while the boundaries themselves stay correct.
        """
        from surveillance.services.recording import preset_range

        from_ts, to_ts = preset_range(preset)
        from_dt = datetime.fromtimestamp(from_ts)
        to_dt = datetime.fromtimestamp(to_ts)
        assert (from_dt.hour, from_dt.minute, from_dt.second) == (0, 0, 0)
        assert (to_dt.hour, to_dt.minute, to_dt.second) == (23, 59, 59)
        today = datetime.now().date()
        assert to_dt.date() == today
        assert from_dt.date() == today - timedelta(days=days)

    def test_unknown_preset_raises(self) -> None:
        from surveillance.services.recording import preset_range

        with pytest.raises(ValueError, match="unknown preset"):
            preset_range("last100d")

    def test_today_to_ts_is_after_from_ts(self) -> None:
        from surveillance.services.recording import preset_range

        for preset in ("today", "yesterday", "last24h", "last7d", "last30d"):
            from_ts, to_ts = preset_range(preset)
            assert to_ts > from_ts, f"preset={preset}: to_ts <= from_ts"


class TestRecordingDownloadParams:
    @pytest.mark.asyncio
    async def test_download_sends_correct_recording_id(
        self, api: SurveillanceAPI, tmp_path: Path
    ) -> None:
        from surveillance.services.recording import download_recording

        output = tmp_path / "rec.mp4"
        fake_bytes = b"fake-video-data"

        with patch.object(api, "stream_download", _stream_mock(fake_bytes)) as mock:
            result = await download_recording(api, recording_id=42, output_path=output)
            params = mock.call_args[1]["extra_params"]
            assert params["id"] == "42"
            assert result == output

    @pytest.mark.asyncio
    async def test_download_writes_file(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording

        output = tmp_path / "out.mp4"
        content = b"\x00\x01\x02\x03video"

        with patch.object(api, "stream_download", _stream_mock(content)):
            await download_recording(api, recording_id=1, output_path=output)

        assert output.exists()
        assert output.read_bytes() == content

    @pytest.mark.asyncio
    async def test_download_creates_parent_dirs(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording

        output = tmp_path / "subdir" / "deeper" / "rec.mp4"

        with patch.object(api, "stream_download", _stream_mock(b"data")):
            await download_recording(api, recording_id=7, output_path=output)

        assert output.exists()

    @pytest.mark.asyncio
    async def test_download_uses_correct_api_method(
        self, api: SurveillanceAPI, tmp_path: Path
    ) -> None:
        from surveillance.services.recording import download_recording

        output = tmp_path / "rec.mp4"

        with patch.object(api, "stream_download", _stream_mock(b"x")) as mock:
            await download_recording(api, recording_id=99, output_path=output)
            assert mock.call_args[1]["api"] == "SYNO.SurveillanceStation.Recording"
            assert mock.call_args[1]["method"] == "Download"

    @pytest.mark.asyncio
    async def test_download_returns_path(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording

        output = tmp_path / "video.mp4"

        with patch.object(api, "stream_download", _stream_mock(b"bytes")):
            result = await download_recording(api, recording_id=3, output_path=output)

        assert isinstance(result, Path)
        assert result == output


class TestRecordingDownloadRangeParams:
    """download_recording_range: the version=4 event-based download used by
    the Live View timeline's Download button (see Timeline.set_download_callback),
    distinct from the whole-file id-based download above."""

    def _rec(self, **overrides: object) -> Recording:
        defaults: dict = dict(
            id=4958476,
            camera_id=63,
            camera_name="CAM 63",
            start_time=1000,
            stop_time=2000,
            mount_id=1,
            arch_id=2,
            event_type=3,
        )
        defaults.update(overrides)
        return Recording(**defaults)

    @pytest.mark.asyncio
    async def test_sends_correct_event_params(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording_range

        output = tmp_path / "clip.mp4"
        rec = self._rec()

        with patch.object(api, "stream_download", _stream_mock(b"video")) as mock:
            result = await download_recording_range(api, rec, 1010.0, 1016.0, output)
            params = mock.call_args[1]["extra_params"]
            assert params == {
                "eventId": "4958476",
                "offsetTimeMs": "10000",
                "playTimeMs": "6000",
                "mountId": "1",
                "archId": "2",
                "recEvtType": "3",
            }
            assert mock.call_args[1]["api"] == "SYNO.SurveillanceStation.Recording"
            assert mock.call_args[1]["method"] == "Download"
            assert mock.call_args[1]["version"] == 4
            assert result == output

    @pytest.mark.asyncio
    async def test_offset_clamped_to_zero_before_recording_start(
        self, api: SurveillanceAPI, tmp_path: Path
    ) -> None:
        """A range starting before the recording does not send a negative
        offset, and asks only for what the recording holds of it."""
        from surveillance.services.recording import download_recording_range

        output = tmp_path / "clip.mp4"
        rec = self._rec(start_time=1000)

        with patch.object(api, "stream_download", _stream_mock(b"x")) as mock:
            await download_recording_range(api, rec, 990.0, 1005.0, output)
            params = mock.call_args[1]["extra_params"]
            assert params["offsetTimeMs"] == "0"
            # 1000 to 1005, not 15s from the recording's start, which
            # would run 10s past the end of the range.
            assert params["playTimeMs"] == "5000"

    @pytest.mark.asyncio
    async def test_range_ending_before_recording_start_raises(
        self, api: SurveillanceAPI, tmp_path: Path
    ) -> None:
        """Downloading from offset 0 would save footage from after the
        range under the range's own time."""
        from surveillance.services.recording import download_recording_range

        output = tmp_path / "clip.mp4"
        rec = self._rec(start_time=1000)

        with pytest.raises(ValueError, match="ends before the recording starts"):
            await download_recording_range(api, rec, 990.0, 995.0, output)

    @pytest.mark.asyncio
    async def test_play_time_clamped_to_recording_end(
        self, api: SurveillanceAPI, tmp_path: Path
    ) -> None:
        """A range running past the recording's own stop_time (a selection
        made close to "now", against metadata that was already stale) must
        ask only for what the recording actually holds."""
        from surveillance.services.recording import download_recording_range

        output = tmp_path / "clip.mp4"
        rec = self._rec(start_time=1000, stop_time=2000)

        with patch.object(api, "stream_download", _stream_mock(b"x")) as mock:
            await download_recording_range(api, rec, 1990.0, 2010.0, output)
            params = mock.call_args[1]["extra_params"]
            assert params["offsetTimeMs"] == "990000"
            # 10s left in the recording, not the 20s asked for.
            assert params["playTimeMs"] == "10000"

    @pytest.mark.asyncio
    async def test_range_starting_past_recording_end_raises(
        self, api: SurveillanceAPI, tmp_path: Path
    ) -> None:
        """Nothing of the range is inside the recording, so there is no
        shortened download to fall back to."""
        from surveillance.services.recording import download_recording_range

        output = tmp_path / "clip.mp4"
        rec = self._rec(start_time=1000, stop_time=2000)

        with pytest.raises(ValueError, match="starts after the end"):
            await download_recording_range(api, rec, 2010.0, 2020.0, output)

    @pytest.mark.asyncio
    async def test_end_before_start_raises(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording_range

        output = tmp_path / "clip.mp4"
        rec = self._rec()

        with pytest.raises(ValueError):
            await download_recording_range(api, rec, 1010.0, 1005.0, output)

    @pytest.mark.asyncio
    async def test_end_equal_start_raises(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording_range

        output = tmp_path / "clip.mp4"
        rec = self._rec()

        with pytest.raises(ValueError):
            await download_recording_range(api, rec, 1010.0, 1010.0, output)

    @pytest.mark.asyncio
    async def test_writes_file(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording_range

        output = tmp_path / "clip.mp4"
        content = b"\x00\x01clipdata"
        rec = self._rec()

        with patch.object(api, "stream_download", _stream_mock(content)):
            await download_recording_range(api, rec, 1010.0, 1016.0, output)

        assert output.read_bytes() == content


class TestComputeFocusMarkerUpdate:
    """LiveView._advance_focus_history_position's decision core (ToDo r):
    the shared timeline marker takes whichever active camera delivered
    real data this tick, and only extrapolates from wall clock/speed
    once none of them did -- pulled out as a pure function for the same
    GTK-segfaults-headless reason as TestOrderCamerasFocusFirst below."""

    def test_a_real_tick_wins_outright(self) -> None:
        from surveillance.ui.liveview import compute_focus_marker_update

        position, gap_started_at, _ref = compute_focus_marker_update(
            current_position=1000.0,
            last_set_position=1000.0,
            active_ticks=[1050],
            gap_started_at=None,
            gap_reference_position=0.0,
            now=2000.0,
            speed="1",
            reverse=False,
        )
        assert position == 1050.0
        assert gap_started_at is None

    def test_the_largest_of_several_real_ticks_wins(self) -> None:
        """Doesn't matter which camera it came from -- every active slot
        is playing toward the same target, so the furthest-along one is
        as good an answer as the focus camera's own would be."""
        from surveillance.ui.liveview import compute_focus_marker_update

        position, _gap_started_at, _ref = compute_focus_marker_update(
            current_position=1000.0,
            last_set_position=1000.0,
            active_ticks=[1010, None, 1050, 1030],
            gap_started_at=None,
            gap_reference_position=0.0,
            now=2000.0,
            speed="1",
            reverse=False,
        )
        assert position == 1050.0

    def test_the_smallest_of_several_real_ticks_wins_in_reverse(self) -> None:
        """Reverse playback moves backward, so the camera that has
        progressed furthest reports the *smallest* tick, not the
        largest -- the opposite of forward mode above."""
        from surveillance.ui.liveview import compute_focus_marker_update

        position, _gap_started_at, _ref = compute_focus_marker_update(
            current_position=1100.0,
            last_set_position=1100.0,
            active_ticks=[1010, None, 1050, 1030],
            gap_started_at=None,
            gap_reference_position=0.0,
            now=2000.0,
            speed="1",
            reverse=True,
        )
        assert position == 1010.0

    @pytest.mark.parametrize(("reverse", "ticks"), [(False, [1003, None]), (True, [1007, None])])
    def test_a_slot_falling_quiet_does_not_pull_the_marker_back(
        self, reverse: bool, ticks: list[int | None]
    ) -> None:
        """The slot furthest along has no frame this second; the next one
        is behind where the marker already is."""
        from surveillance.ui.liveview import compute_focus_marker_update

        position, _gap_started_at, _ref = compute_focus_marker_update(
            current_position=1005.0,
            last_set_position=1005.0,
            active_ticks=ticks,
            gap_started_at=None,
            gap_reference_position=0.0,
            now=2000.0,
            speed="1",
            reverse=reverse,
        )
        assert position == 1005.0

    def test_after_a_jump_the_slots_are_taken_as_they_are(self) -> None:
        """A jump back moved the position (current != last set): the
        marker follows the slots there even though it's behind."""
        from surveillance.ui.liveview import compute_focus_marker_update

        position, _gap_started_at, _ref = compute_focus_marker_update(
            current_position=500.0,
            last_set_position=1005.0,
            active_ticks=[501, 502],
            gap_started_at=None,
            gap_reference_position=0.0,
            now=2000.0,
            speed="1",
            reverse=False,
        )
        assert position == 502.0

    def test_a_real_tick_clears_an_in_progress_gap(self) -> None:
        from surveillance.ui.liveview import compute_focus_marker_update

        position, gap_started_at, _ref = compute_focus_marker_update(
            current_position=1005.0,
            last_set_position=1005.0,
            active_ticks=[1200],
            gap_started_at=1990.0,  # was extrapolating
            gap_reference_position=1000.0,
            now=2000.0,
            speed="1",
            reverse=False,
        )
        assert position == 1200.0
        assert gap_started_at is None

    def test_entering_a_gap_starts_extrapolation_at_zero_elapsed(self) -> None:
        from surveillance.ui.liveview import compute_focus_marker_update

        position, gap_started_at, ref = compute_focus_marker_update(
            current_position=1000.0,
            last_set_position=1000.0,  # this method's own last write -- no seek since
            active_ticks=[],
            gap_started_at=None,  # not already tracking a gap
            gap_reference_position=0.0,
            now=2000.0,
            speed="1",
            reverse=False,
        )
        assert position == 1000.0  # zero elapsed on the very first tick of the gap
        assert gap_started_at == 2000.0
        assert ref == 1000.0

    def test_extrapolates_forward_at_1x(self) -> None:
        from surveillance.ui.liveview import compute_focus_marker_update

        position, gap_started_at, ref = compute_focus_marker_update(
            current_position=1005.0,  # this function's own previous output
            last_set_position=1005.0,
            active_ticks=[],
            gap_started_at=1995.0,  # gap has been running 5s of wall clock so far
            gap_reference_position=1000.0,
            now=2000.0,
            speed="1",
            reverse=False,
        )
        assert position == 1005.0
        assert gap_started_at == 1995.0  # unchanged -- still the same gap
        assert ref == 1000.0  # unchanged -- the frozen starting point

    def test_extrapolates_scaled_by_speed(self) -> None:
        from surveillance.ui.liveview import compute_focus_marker_update

        position, _gap_started_at, _ref = compute_focus_marker_update(
            current_position=1020.0,
            last_set_position=1020.0,
            active_ticks=[],
            gap_started_at=1995.0,
            gap_reference_position=1000.0,
            now=2000.0,
            speed="4",
            reverse=False,
        )
        assert position == 1000.0 + 5 * 4  # 5s elapsed at 4x from the frozen reference

    def test_extrapolates_backward_when_reversed(self) -> None:
        from surveillance.ui.liveview import compute_focus_marker_update

        position, _gap_started_at, _ref = compute_focus_marker_update(
            current_position=995.0,
            last_set_position=995.0,
            active_ticks=[],
            gap_started_at=1995.0,
            gap_reference_position=1000.0,
            now=2000.0,
            speed="1",
            reverse=True,
        )
        assert position == 995.0

    def test_a_seek_during_a_gap_restarts_extrapolation_from_the_new_position(self) -> None:
        """current_position != last_set_position means something other
        than this function moved the focus slot since the previous
        tick -- a ruler click, Back/Forward 10s, a focus switch -- so
        the old gap's reference point is stale and must be dropped even
        though we're still (or again) in a gap right now."""
        from surveillance.ui.liveview import compute_focus_marker_update

        position, gap_started_at, ref = compute_focus_marker_update(
            current_position=5000.0,  # a seek landed here, also in a gap
            last_set_position=1005.0,  # what this function last wrote, before the seek
            active_ticks=[],
            gap_started_at=1995.0,  # the OLD gap's tracking -- must not carry over
            gap_reference_position=1000.0,
            now=2000.0,
            speed="1",
            reverse=False,
        )
        assert position == 5000.0  # zero elapsed from the new starting point
        assert gap_started_at == 2000.0
        assert ref == 5000.0

    def test_nothing_to_show_yet_returns_none(self) -> None:
        """History mode was just entered and no real tick has arrived
        for any active camera yet -- there's no prior position to
        extrapolate from either, so there's genuinely nothing to set."""
        from surveillance.ui.liveview import compute_focus_marker_update

        position, gap_started_at, _ref = compute_focus_marker_update(
            current_position=None,
            last_set_position=None,
            active_ticks=[],
            gap_started_at=None,
            gap_reference_position=0.0,
            now=2000.0,
            speed="1",
            reverse=False,
        )
        assert position is None
        assert gap_started_at is None

    def test_a_stale_tick_during_a_gap_is_rejected_in_favor_of_extrapolation(self) -> None:
        """Reproduces the periodic backward jump actually seen live: a
        reconnect while a gap is in progress can resolve back onto the
        recording that just stopped covering the target (absent
        find_covering_recording_at) and get back one real frame at its
        own last clamped moment -- at or before gap_reference_position,
        the marker's own position when the gap began. That must not
        snap the marker backward; it has to keep extrapolating instead."""
        from surveillance.ui.liveview import compute_focus_marker_update

        position, gap_started_at, ref = compute_focus_marker_update(
            current_position=1010.0,  # this function's own last extrapolated output
            last_set_position=1010.0,
            active_ticks=[1000],  # stale: the old recording's own last moment
            gap_started_at=1995.0,
            gap_reference_position=1000.0,  # where the marker was when the gap began
            now=2000.0,
            speed="1",
            reverse=False,
        )
        assert position == 1005.0  # 5s elapsed at 1x, exactly as if no tick had arrived
        assert gap_started_at == 1995.0  # the gap is still considered in progress
        assert ref == 1000.0

    def test_a_stale_tick_exactly_at_the_gap_boundary_is_also_rejected(self) -> None:
        """The clamp that produces the stale frame lands it at exactly
        gap_reference_position, not strictly before it -- the check has
        to be inclusive or this exact, most-likely-in-practice case
        would slip through."""
        from surveillance.ui.liveview import compute_focus_marker_update

        position, _gap_started_at, _ref = compute_focus_marker_update(
            current_position=1010.0,
            last_set_position=1010.0,
            active_ticks=[1000],  # == gap_reference_position, not less than it
            gap_started_at=1995.0,
            gap_reference_position=1000.0,
            now=2000.0,
            speed="1",
            reverse=False,
        )
        assert position == 1005.0  # extrapolated, not snapped to the stale 1000

    def test_a_tick_at_or_after_the_gap_boundary_is_trusted_even_if_behind_the_guess(
        self,
    ) -> None:
        """Real data always wins once it resumes, even if the
        extrapolated guess had already run a little ahead of it --
        exactly the "it will sync again as soon as it starts receiving
        frames" behavior this fallback exists to provide."""
        from surveillance.ui.liveview import compute_focus_marker_update

        position, gap_started_at, _ref = compute_focus_marker_update(
            current_position=1010.0,  # the extrapolated guess has reached this
            last_set_position=1010.0,
            active_ticks=[1002],  # real data, but behind the guess
            gap_started_at=1995.0,
            gap_reference_position=1000.0,
            now=2000.0,
            speed="1",
            reverse=False,
        )
        assert position == 1002.0
        assert gap_started_at is None  # the gap is over

    def test_stale_check_is_flipped_when_playing_in_reverse(self) -> None:
        from surveillance.ui.liveview import compute_focus_marker_update

        position, _gap_started_at, _ref = compute_focus_marker_update(
            current_position=990.0,
            last_set_position=990.0,
            active_ticks=[1000],  # stale: at/after the reference, wrong way for reverse
            gap_started_at=1995.0,
            gap_reference_position=1000.0,
            now=2000.0,
            speed="1",
            reverse=True,
        )
        assert position == 995.0  # extrapolated backward, not snapped to the stale 1000

    def test_stale_check_does_not_apply_outside_an_active_gap(self) -> None:
        """gap_started_at is None means ordinary playback, not a gap in
        progress -- gap_reference_position is stale leftover state from
        some earlier gap and must not reject a perfectly normal tick."""
        from surveillance.ui.liveview import compute_focus_marker_update

        position, gap_started_at, _ref = compute_focus_marker_update(
            current_position=1010.0,
            last_set_position=1010.0,
            active_ticks=[1011],  # ordinary next frame, below old gap_reference_position
            gap_started_at=None,
            gap_reference_position=5000.0,  # leftover from an unrelated earlier gap
            now=2000.0,
            speed="1",
            reverse=False,
        )
        assert position == 1011.0
        assert gap_started_at is None


class TestOrderCamerasFocusFirst:
    """LiveView._download_available_cameras's ordering rule: the Download
    popover's camera dropdown always defaults to position 0, so the
    tracked slot's camera has to be moved there without disturbing the
    rest -- pulled out as a pure function specifically so this doesn't
    need a live CameraSlot/LiveView (GTK widgets segfault without a
    display in this test environment)."""

    def test_focus_camera_moved_to_front(self) -> None:
        from surveillance.ui.liveview import order_cameras_focus_first

        cameras = [(1, "CAM 1"), (2, "CAM 2"), (3, "CAM 3")]
        assert order_cameras_focus_first(cameras, 2) == [
            (2, "CAM 2"),
            (1, "CAM 1"),
            (3, "CAM 3"),
        ]

    def test_focus_camera_already_first_is_unchanged(self) -> None:
        from surveillance.ui.liveview import order_cameras_focus_first

        cameras = [(1, "CAM 1"), (2, "CAM 2")]
        assert order_cameras_focus_first(cameras, 1) == cameras

    def test_none_focus_id_leaves_order_unchanged(self) -> None:
        from surveillance.ui.liveview import order_cameras_focus_first

        cameras = [(1, "CAM 1"), (2, "CAM 2")]
        assert order_cameras_focus_first(cameras, None) == cameras

    def test_focus_id_not_in_list_leaves_order_unchanged(self) -> None:
        """The tracked slot can be empty (no camera assigned) while other
        slots aren't -- the dropdown just keeps its natural order then."""
        from surveillance.ui.liveview import order_cameras_focus_first

        cameras = [(1, "CAM 1"), (2, "CAM 2")]
        assert order_cameras_focus_first(cameras, 99) == cameras

    def test_empty_list(self) -> None:
        from surveillance.ui.liveview import order_cameras_focus_first

        assert order_cameras_focus_first([], 1) == []


class TestRecordingFilterConfig:
    def test_search_camera_ids_round_trip(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg
        from surveillance.config import _write_config, load_config

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        # Camera IDs are kept per profile (see PROFILE_STATE_FIELDS).
        config = AppConfig(default_profile="nas", active_profile="nas")
        config.profiles["nas"] = ConnectionProfile("nas", "192.168.1.10")
        config.search_camera_ids = [1, 5, 9]
        config.search_from_time = "2026-01-01T00:00:00"
        config.search_to_time = "2026-01-07T23:59:59"
        config.search_time_preset = "last7d"

        _write_config(config)
        loaded = load_config()

        assert loaded.search_camera_ids == [1, 5, 9]
        assert loaded.search_from_time == "2026-01-01T00:00:00"
        assert loaded.search_to_time == "2026-01-07T23:59:59"
        assert loaded.search_time_preset == "last7d"

    def test_search_time_preset_default_empty(self) -> None:
        config = AppConfig()
        assert config.search_time_preset == ""

    def test_all_presets_persist(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg
        from surveillance.config import _write_config, load_config

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        for preset in ("today", "yesterday", "last24h", "last7d", ""):
            config = AppConfig(search_time_preset=preset)
            _write_config(config)
            loaded = load_config()
            assert loaded.search_time_preset == preset


class _Visible:
    """Stand-in for the visibility half of a Gtk.Widget, and the active
    state of a nav toggle button."""

    def __init__(self) -> None:
        self.visible = True
        self.active = False

    def set_visible(self, visible: bool) -> None:
        self.visible = visible

    def set_active(self, active: bool) -> None:
        self.active = active


class TestSidebarLoggedOut:
    """Settings is built before any connection exists and works without
    one, and the sidebar's nav list is the only route to it, so a logout
    has to leave that row behind while dropping the rest."""

    _PAGES = (
        "live",
        "recordings",
        "snapshots",
        "events",
        "timelapse",
        "licenses",
        "settings",
        "about",
    )

    def _sidebar(self) -> object:
        from surveillance.ui.sidebar import CameraSidebar

        sidebar = CameraSidebar.__new__(CameraSidebar)
        sidebar._list_header = _Visible()  # type: ignore[assignment]
        sidebar._nav_buttons = {p: _Visible() for p in self._PAGES}  # type: ignore[misc]
        return sidebar

    def test_logout_leaves_only_the_settings_row(self) -> None:
        from surveillance.ui.sidebar import CameraSidebar

        sidebar = self._sidebar()
        CameraSidebar.set_logged_out(sidebar, True)  # type: ignore[arg-type]

        assert sidebar._list_header.visible is False  # type: ignore[attr-defined]
        shown = [p for p, b in sidebar._nav_buttons.items() if b.visible]  # type: ignore[attr-defined]
        assert shown == ["settings"]

    def test_logout_leaves_no_row_highlighted(self) -> None:
        """GTK ignores a click on the active button of a toggle group,
        so logging out from Settings left it impossible to reopen."""
        from surveillance.ui.sidebar import CameraSidebar

        sidebar = self._sidebar()
        sidebar._nav_buttons["settings"].active = True  # type: ignore[attr-defined]
        CameraSidebar.set_logged_out(sidebar, True)  # type: ignore[arg-type]

        assert not any(b.active for b in sidebar._nav_buttons.values())  # type: ignore[attr-defined]

    def test_login_brings_every_row_back(self) -> None:
        from surveillance.ui.sidebar import CameraSidebar

        sidebar = self._sidebar()
        CameraSidebar.set_logged_out(sidebar, True)  # type: ignore[arg-type]
        CameraSidebar.set_logged_out(sidebar, False)  # type: ignore[arg-type]

        assert sidebar._list_header.visible is True  # type: ignore[attr-defined]
        assert all(b.visible for b in sidebar._nav_buttons.values())  # type: ignore[attr-defined]


class TestPlayerSeekHold:
    """The player's slider stops following playback while the user moves
    it, and only then. The click gesture that did this never saw a
    release, since GtkRange claims every press, so one click froze the
    slider and the time label for the rest of the session."""

    class _Recorder:
        def __init__(self) -> None:
            self.values: list[object] = []

        def set_value(self, value: object) -> None:
            self.values.append(value)

        def set_text(self, value: object) -> None:
            self.values.append(value)

    class _Button:
        def __init__(self) -> None:
            self.icon = "media-playback-pause-symbolic"

        def set_icon_name(self, name: str) -> None:
            self.icon = name

    def _dialog(self) -> SimpleNamespace:
        from surveillance.ui.player import PlayerDialog

        player = SimpleNamespace(
            time_pos=30.0, duration=120.0, is_playing=True, seek_absolute=lambda pos: None
        )
        dialog = SimpleNamespace(
            player=player,
            play_btn=self._Button(),
            position_scale=self._Recorder(),
            time_label=self._Recorder(),
            _status_label=self._Recorder(),
            _loading=False,
            _seek_hold_until=0.0,
        )
        dialog._sync_play_icon = lambda: PlayerDialog._sync_play_icon(dialog)  # type: ignore[arg-type]
        return dialog

    def test_the_icon_follows_mpv_pausing_at_the_end(self) -> None:
        """keep-open has mpv pause by itself at the end of a recording,
        which no click reports: the button went on offering Pause."""
        from surveillance.ui.player import PlayerDialog

        dialog = self._dialog()
        dialog.player.is_playing = False
        PlayerDialog._update_position(dialog)  # type: ignore[arg-type]
        assert dialog.play_btn.icon == "media-playback-start-symbolic"

    def test_updates_hold_off_after_a_seek_then_resume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from surveillance.ui import player
        from surveillance.ui.player import PlayerDialog

        clock = [1000.0]
        monkeypatch.setattr(player.time, "monotonic", lambda: clock[0])
        dialog = self._dialog()

        PlayerDialog._on_seek(dialog, None, None, 50.0)  # type: ignore[arg-type]
        PlayerDialog._update_position(dialog)  # type: ignore[arg-type]
        assert dialog.position_scale.values == []

        clock[0] += player._SEEK_HOLD_SECONDS + 0.1
        PlayerDialog._update_position(dialog)  # type: ignore[arg-type]
        assert dialog.position_scale.values == [25.0]
        assert dialog.time_label.values == ["00:30 / 02:00"]


class TestLogoutShowsLoginOnce:
    """The login dialog comes up at once on logout, and only once. Shown
    after the old session's cleanup instead, it left the header's Login
    button live for up to the 30s request timeout, and a login made
    through that was followed by a second dialog over the new session."""

    def test_dialog_shown_before_the_cleanup_and_not_after(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from surveillance.app import SurveillanceApp
        from surveillance.util import async_bridge

        pending: list[object] = []
        monkeypatch.setattr(
            async_bridge, "run_async", lambda coro, **kwargs: pending.append((coro, kwargs))
        )
        shown: list[str] = []
        window = SimpleNamespace(
            on_disconnected=lambda: shown.append("disconnected"),
            show_login=lambda: shown.append("login"),
        )
        app = SimpleNamespace(_window=window, api=SimpleNamespace())

        SurveillanceApp._on_logout(app, None, None)  # type: ignore[arg-type]

        assert shown == ["disconnected", "login"]
        assert app.api is None
        coro, kwargs = pending[0]  # type: ignore[misc]
        assert "callback" not in kwargs, "nothing may show a dialog once the cleanup lands"
        coro.close()  # type: ignore[attr-defined]


class TestSidebarRefreshAfterLogout:
    """A camera list answered after logout, or after logging into another
    NAS, must not land in the sidebar."""

    def test_a_late_answer_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from surveillance.ui import sidebar as sidebar_module
        from surveillance.ui.sidebar import CameraSidebar

        pending: list[object] = []
        monkeypatch.setattr(
            sidebar_module,
            "run_async",
            lambda coro, callback, error_callback: pending.append((coro, callback)),
        )
        updated: list[object] = []
        sidebar = SimpleNamespace(
            app=SimpleNamespace(api=SimpleNamespace()),
            _update_camera_list=updated.append,
            on_cameras_updated=None,
        )
        CameraSidebar.refresh(sidebar)  # type: ignore[arg-type]
        coro, callback = pending[0]  # type: ignore[misc]
        coro.close()  # type: ignore[attr-defined]

        sidebar.app.api = None  # logged out while the request was out
        callback(["cam1"])  # type: ignore[operator]
        assert updated == []


class TestQuickCameraPickIsSaved:
    """Picking one camera drops a prior Advanced Search camera selection.
    Dropped only in memory, a restart brought the old selection back."""

    class _Combo:
        def handler_block_by_func(self, func: object) -> None:
            pass

        def handler_unblock_by_func(self, func: object) -> None:
            pass

        def set_active_id(self, active_id: str) -> None:
            pass

    @pytest.mark.parametrize(
        ("module", "view", "prefix", "load"),
        [
            ("recordings", "RecordingsView", "search", "_load_recordings"),
            ("snapshots", "SnapshotsView", "snapshots_search", "_load_snapshots"),
            ("events", "EventsView", "events_search", "_load_events"),
        ],
    )
    def test_a_sidebar_pick_clears_the_saved_cameras(
        self, monkeypatch: pytest.MonkeyPatch, module: str, view: str, prefix: str, load: str
    ) -> None:
        import importlib

        import surveillance.config as config_module

        page_module = importlib.import_module(f"surveillance.ui.{module}")
        monkeypatch.setattr(config_module, "save_config", lambda cfg: None)
        monkeypatch.setattr(page_module, "save_config", lambda cfg: None, raising=False)
        cls = getattr(page_module, view)
        config = AppConfig()
        setattr(config, f"{prefix}_camera_ids", [1, 2])
        page = SimpleNamespace(
            app=SimpleNamespace(config=config),
            camera_combo=self._Combo(),
            _search_camera_ids=[1, 2],
            _search_from_time=None,
            _search_to_time=None,
            _search_time_preset="",
            _search_event_types=None,
            _search_event_types_match_all=False,
            _ensure_camera_in_combo=lambda camera_id, name: None,
            _on_filter_changed=None,
            **{load: lambda: None},
        )
        page._save_search_to_config = lambda: cls._save_search_to_config(page)

        cls.on_camera_selected(page, SimpleNamespace(id=3, name="cam3"))

        assert getattr(config, f"{prefix}_camera_ids") == []


class TestFailedLoadClearsRows:
    """A load that fails after a filter change must not leave the
    previous filter's rows listed under the new filter summary."""

    class _Box:
        """A row container in both of the shapes the pages use."""

        def __init__(self) -> None:
            self.rows = ["old row", "old row"]

        def get_first_child(self) -> object:
            return self.rows[0] if self.rows else None

        def get_row_at_index(self, index: int) -> object:
            return self.rows[index] if index < len(self.rows) else None

        def remove(self, row: object) -> None:
            self.rows.remove(row)

    class _Widget:
        def __init__(self) -> None:
            self.value: object = None

        def set_sensitive(self, value: object) -> None:
            self.value = value

        def set_text(self, value: object) -> None:
            self.value = value

    def _page(self, **attrs: object) -> SimpleNamespace:
        return SimpleNamespace(
            _loading=True,
            _reload_pending=False,
            prev_btn=self._Widget(),
            next_btn=self._Widget(),
            page_label=self._Widget(),
            **attrs,
        )

    def test_recordings(self) -> None:
        from surveillance.ui.recordings import RecordingsView

        page = self._page(
            row_box=self._Box(), _thumb_futures=[], _thumb_generation=0, _offset=0, _total=120
        )
        page._clear_rows = lambda: RecordingsView._clear_rows(page)
        RecordingsView._on_load_error(page, OSError("down"))
        assert page.row_box.rows == []
        assert page.page_label.value == "Failed to load recordings"

    def test_snapshots(self) -> None:
        from surveillance.ui.snapshots import SnapshotsView

        page = self._page(row_box=self._Box(), _snapshots=["old"], _page=0)
        SnapshotsView._on_load_error(page, OSError("down"))
        assert page.row_box.rows == []
        assert page.page_label.value == "Failed to load snapshots"

    def test_events(self) -> None:
        from surveillance.services.legacy_event import LegacyEventBackend
        from surveillance.ui.events import EventsView

        page = self._page(
            app=SimpleNamespace(event_backend=LegacyEventBackend()),
            listbox=self._Box(),
            _events=["old"],
            _page=0,
            _search_event_types=None,
            _event_type_filter=None,
            _camera_vendor={},
        )
        page._render_events = lambda: EventsView._render_events(page)
        EventsView._on_load_error(page, OSError("down"))
        assert page.listbox.rows == []
        assert page.page_label.value == "Failed to load events"


class TestSnapshotViewerFiles:
    """Quitting goes through os._exit, which skips the viewer's close
    handler, so a viewer open at exit left its image in the shared temp
    directory for good. It is now written to the app's own cache, which
    the next run's first viewer clears out."""

    def test_the_next_run_clears_what_an_exit_left(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from surveillance.ui import snapshots
        from surveillance.ui.snapshots import SnapshotViewerDialog

        viewer_dir = tmp_path / "viewer"
        viewer_dir.mkdir()
        leftover = viewer_dir / "tmpold.jpg"
        leftover.write_bytes(b"from a run that exited")
        monkeypatch.setattr(snapshots, "_VIEWER_DIR", viewer_dir)
        monkeypatch.setattr(snapshots, "_viewer_dir_ready", False)

        played: list[str] = []
        viewer = SimpleNamespace(
            _closed=False,
            _tmp_path=None,
            player=SimpleNamespace(play=played.append, stop=lambda: None),
        )
        SnapshotViewerDialog._on_image_loaded(viewer, b"\xff\xd8 jpeg")  # type: ignore[arg-type]

        assert not leftover.exists()
        assert [Path(p).parent for p in played] == [viewer_dir]
        assert Path(played[0]).read_bytes() == b"\xff\xd8 jpeg"

        SnapshotViewerDialog._on_close(viewer, None)  # type: ignore[arg-type]
        assert list(viewer_dir.iterdir()) == []


class TestEmptyLaterPage:
    """A later page whose rows are all gone, say the last one deleted on
    page 2, steps back to page one rather than showing "Page 2 of 1"."""

    @pytest.mark.parametrize(
        ("module", "view"),
        [("recordings", "RecordingsView"), ("timelapse", "TimeLapseView")],
    )
    def test_steps_back_when_nothing_is_left(self, module: str, view: str) -> None:
        import importlib

        cls = getattr(importlib.import_module(f"surveillance.ui.{module}"), view)
        reloaded: list[int] = []
        page = SimpleNamespace(_loading=True, _reload_pending=False, _offset=50, _total=51)
        page._load_recordings = lambda: reloaded.append(page._offset)
        cls._on_recordings_loaded(page, ([], 0))
        assert reloaded == [0]


class TestEventsSavedTypeKeys:
    """Saved Advanced Search event-type keys from another event backend
    match nothing, so loading drops them instead of emptying the list."""

    def test_keys_from_another_backend_are_dropped(self) -> None:
        from surveillance.config import AppConfig
        from surveillance.services.legacy_event import LegacyEventBackend
        from surveillance.ui.events import EventsView

        config = AppConfig(events_search_event_types=["08", "object:people", "25:hikvision"])
        page = SimpleNamespace(
            app=SimpleNamespace(config=config, event_backend=LegacyEventBackend()),
            _search_event_types=None,
        )
        EventsView._load_search_from_config(page)
        assert page._search_event_types == ["08", "25:hikvision"]

    def test_nothing_left_means_no_type_filter(self) -> None:
        from surveillance.config import AppConfig
        from surveillance.services.legacy_event import LegacyEventBackend
        from surveillance.ui.events import EventsView

        config = AppConfig(events_search_event_types=["object:people"])
        page = SimpleNamespace(
            app=SimpleNamespace(config=config, event_backend=LegacyEventBackend()),
            _search_event_types=None,
        )
        EventsView._load_search_from_config(page)
        assert page._search_event_types is None


class TestFilterPopoverOptions:
    """The Live View Filter-events popover only scans cameras for their
    event types when the event backend can't list them all up front."""

    class _Timeline:
        def __init__(self) -> None:
            self.shown: list[tuple[object, ...]] = []
            self.scanning: list[list[str]] = []

        def show_filter_options(self, *args: object, **kwargs: object) -> None:
            self.shown.append((*args, kwargs))

        def show_filter_scanning(self, names: list[str]) -> None:
            self.scanning.append(names)

    def _page(self, backend: object) -> SimpleNamespace:
        from surveillance.ui.liveview import LiveView

        page = SimpleNamespace(
            app=SimpleNamespace(api=object(), event_backend=backend),
            timeline=self._Timeline(),
            _filter_scan_generation=0,
            _event_filter_keys=None,
            _event_filter_match_all=True,
            _active_timeline_cameras=lambda: (1, [1, 2]),
            _camera_name=str,
            scanned=[],
        )
        page._show_filter_options = lambda options: LiveView._show_filter_options(page, options)
        page._scan_next_camera_for_event_types = lambda *args: page.scanned.append(args)
        return page

    def test_fixed_options_are_shown_without_a_scan(self) -> None:
        from surveillance.ui.liveview import LiveView

        options = [("motion", "Motion", "")]
        backend = SimpleNamespace(fixed_filter_options=lambda: options, supports_match_all=False)
        page = self._page(backend)
        LiveView._on_filter_popover_show(page)
        assert page.scanned == []
        assert page.timeline.scanning == []
        # Any/All hidden and left on Any, whatever was set before.
        assert page.timeline.shown == [(options, None, False, {"show_match_all": False})]

    def test_otherwise_the_cameras_are_scanned(self) -> None:
        from surveillance.services.legacy_event import LegacyEventBackend
        from surveillance.ui.liveview import LiveView

        page = self._page(LegacyEventBackend())
        LiveView._on_filter_popover_show(page)
        assert page.timeline.scanning == [["1", "2"]]
        assert page.scanned == [(1, [1, 2], 0)]


class TestFilterButtonShowsActiveFilter:
    """The timeline's Filter events button is marked while a filter
    narrows the event markers, and unmarked once it's cleared."""

    def _apply(self, selected_keys: set[str] | None) -> list[bool]:
        from surveillance.ui.liveview import LiveView

        marked: list[bool] = []
        page = SimpleNamespace(
            timeline=SimpleNamespace(set_filter_active=marked.append),
            _active_timeline_cameras=lambda: (None, []),
            _apply_timeline_data_to_canvas=lambda *_args: None,
        )
        LiveView._on_filter_apply(page, selected_keys, False)
        return marked

    def test_a_selection_marks_it(self) -> None:
        assert self._apply({"motion"}) == [True]

    def test_all_event_types_unmarks_it(self) -> None:
        assert self._apply(None) == [False]
