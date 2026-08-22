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
        """A requested start before the recording's own start_time (e.g. a
        rounding/clock-skew edge case) must not send a negative offset."""
        from surveillance.services.recording import download_recording_range

        output = tmp_path / "clip.mp4"
        rec = self._rec(start_time=1000)

        with patch.object(api, "stream_download", _stream_mock(b"x")) as mock:
            await download_recording_range(api, rec, 990.0, 995.0, output)
            assert mock.call_args[1]["extra_params"]["offsetTimeMs"] == "0"

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

        config = AppConfig()
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
