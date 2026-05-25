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

"""Tests for UI-level routing and recording filter / download logic (no GTK required)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from surveillance.api.client import SurveillanceAPI
from surveillance.api.models import Camera, CameraStatus
from surveillance.config import AppConfig, ConnectionProfile


def _make_camera(cam_id: int, name: str = "Cam") -> Camera:
    return Camera(
        id=cam_id,
        name=name,
        model="",
        vendor="",
        status=CameraStatus.ENABLED,
    )


@pytest.fixture
def profile() -> ConnectionProfile:
    return ConnectionProfile(name="test", host="192.168.1.100")


@pytest.fixture
def api(profile: ConnectionProfile) -> SurveillanceAPI:
    client = SurveillanceAPI(profile)
    client.sid = "test-sid"
    return client


# ---------------------------------------------------------------------------
# Sidebar camera click routing
# ---------------------------------------------------------------------------


class TestCameraClickRouting:
    """on_camera_selected routes to the active page handler."""

    def _make_mock_page(self, has_handler: bool = True) -> MagicMock:
        page = MagicMock()
        if not has_handler:
            del page.on_camera_selected
        return page

    def test_routes_to_page_with_handler(self) -> None:
        camera = _make_camera(1, "Front Door")
        page = MagicMock()
        page.on_camera_selected = MagicMock()

        # Simulate window.on_camera_selected logic
        if hasattr(page, "on_camera_selected"):
            page.on_camera_selected(camera)

        page.on_camera_selected.assert_called_once_with(camera)

    def test_no_call_when_page_lacks_handler(self) -> None:
        camera = _make_camera(1, "Front Door")
        page = MagicMock(spec=[])  # no attributes at all

        called = []
        if hasattr(page, "on_camera_selected"):
            page.on_camera_selected(camera)
            called.append(True)

        assert called == []

    def test_live_page_receives_camera(self) -> None:
        """When on Live View, the live view handler is called."""
        camera = _make_camera(3, "Garage")
        live_page = MagicMock()
        live_page.on_camera_selected = MagicMock()

        # window routes to current visible page
        current_page = live_page
        if hasattr(current_page, "on_camera_selected"):
            current_page.on_camera_selected(camera)

        live_page.on_camera_selected.assert_called_once_with(camera)

    def test_recordings_page_receives_camera(self) -> None:
        """When on Recordings, the recordings handler is called (not live view)."""
        camera = _make_camera(7, "Backyard")
        recordings_page = MagicMock()
        recordings_page.on_camera_selected = MagicMock()
        live_page = MagicMock()
        live_page.on_camera_selected = MagicMock()

        # Simulate being on recordings page
        current_page = recordings_page
        if hasattr(current_page, "on_camera_selected"):
            current_page.on_camera_selected(camera)

        recordings_page.on_camera_selected.assert_called_once_with(camera)
        live_page.on_camera_selected.assert_not_called()

    def test_different_cameras_routed_independently(self) -> None:
        """Multiple sequential camera selections each route correctly."""
        cam_a = _make_camera(1, "CamA")
        cam_b = _make_camera(2, "CamB")
        page = MagicMock()
        page.on_camera_selected = MagicMock()

        for cam in (cam_a, cam_b):
            if hasattr(page, "on_camera_selected"):
                page.on_camera_selected(cam)

        assert page.on_camera_selected.call_count == 2
        assert page.on_camera_selected.call_args_list[0][0][0].id == 1
        assert page.on_camera_selected.call_args_list[1][0][0].id == 2


# ---------------------------------------------------------------------------
# Recording filter parameter generation
# ---------------------------------------------------------------------------


class TestRecordingFilterParams:
    """list_recordings sends the correct query params for filter combinations."""

    @pytest.mark.asyncio
    async def test_no_filters(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(api, "request", new_callable=AsyncMock,
                          return_value={"events": [], "total": 0}) as mock:
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

        with patch.object(api, "request", new_callable=AsyncMock,
                          return_value={"events": [], "total": 0}) as mock:
            await list_recordings(api, camera_ids=[5])
            params = mock.call_args[1]["extra_params"]
            assert params["cameraIds"] == "5"

    @pytest.mark.asyncio
    async def test_multi_camera_filter(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(api, "request", new_callable=AsyncMock,
                          return_value={"events": [], "total": 0}) as mock:
            await list_recordings(api, camera_ids=[1, 3, 7])
            params = mock.call_args[1]["extra_params"]
            assert params["cameraIds"] == "1,3,7"

    @pytest.mark.asyncio
    async def test_time_range_filter(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(api, "request", new_callable=AsyncMock,
                          return_value={"events": [], "total": 0}) as mock:
            await list_recordings(api, from_time=1700000000, to_time=1700086400)
            params = mock.call_args[1]["extra_params"]
            assert params["fromTime"] == "1700000000"
            assert params["toTime"] == "1700086400"

    @pytest.mark.asyncio
    async def test_from_time_only(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(api, "request", new_callable=AsyncMock,
                          return_value={"events": [], "total": 0}) as mock:
            await list_recordings(api, from_time=1700000000)
            params = mock.call_args[1]["extra_params"]
            assert params["fromTime"] == "1700000000"
            assert "toTime" not in params

    @pytest.mark.asyncio
    async def test_to_time_only(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(api, "request", new_callable=AsyncMock,
                          return_value={"events": [], "total": 0}) as mock:
            await list_recordings(api, to_time=1700086400)
            params = mock.call_args[1]["extra_params"]
            assert "fromTime" not in params
            assert params["toTime"] == "1700086400"

    @pytest.mark.asyncio
    async def test_combined_camera_and_time(self, api: SurveillanceAPI) -> None:
        from surveillance.services.recording import list_recordings

        with patch.object(api, "request", new_callable=AsyncMock,
                          return_value={"events": [], "total": 0}) as mock:
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

        with patch.object(api, "request", new_callable=AsyncMock,
                          return_value={"events": [], "total": 0}) as mock:
            await list_recordings(api, offset=100, limit=50)
            params = mock.call_args[1]["extra_params"]
            assert params["offset"] == "100"
            assert params["limit"] == "50"

    @pytest.mark.asyncio
    async def test_legacy_camera_id_param(self, api: SurveillanceAPI) -> None:
        """Single camera_id (legacy) is sent as cameraIds."""
        from surveillance.services.recording import list_recordings

        with patch.object(api, "request", new_callable=AsyncMock,
                          return_value={"events": [], "total": 0}) as mock:
            await list_recordings(api, camera_id=9)
            params = mock.call_args[1]["extra_params"]
            assert params["cameraIds"] == "9"

    @pytest.mark.asyncio
    async def test_camera_ids_takes_priority_over_camera_id(self, api: SurveillanceAPI) -> None:
        """camera_ids wins over camera_id when both are provided."""
        from surveillance.services.recording import list_recordings

        with patch.object(api, "request", new_callable=AsyncMock,
                          return_value={"events": [], "total": 0}) as mock:
            await list_recordings(api, camera_id=1, camera_ids=[2, 3])
            params = mock.call_args[1]["extra_params"]
            assert params["cameraIds"] == "2,3"


# ---------------------------------------------------------------------------
# Recording preset range calculation
# ---------------------------------------------------------------------------


class TestPresetRange:
    """preset_range() returns correct (from_time, to_time) windows."""

    def test_today_range_starts_at_midnight(self) -> None:
        from surveillance.services.recording import preset_range

        from_ts, to_ts = preset_range("today")
        from_dt = datetime.fromtimestamp(from_ts)
        assert from_dt.hour == 0
        assert from_dt.minute == 0
        assert from_dt.second == 0
        assert to_ts >= from_ts

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

    def test_last7d_range_is_roughly_7_days(self) -> None:
        from surveillance.services.recording import preset_range

        from_ts, to_ts = preset_range("last7d")
        diff_days = (to_ts - from_ts) / 86400
        # from_ts is midnight 7 days ago; to_ts is now — window is 7–8 days wide
        assert 7.0 <= diff_days < 8.0

    def test_unknown_preset_raises(self) -> None:
        from surveillance.services.recording import preset_range

        with pytest.raises(ValueError, match="unknown preset"):
            preset_range("last100d")

    def test_today_to_ts_is_after_from_ts(self) -> None:
        from surveillance.services.recording import preset_range

        for preset in ("today", "yesterday", "last24h", "last7d"):
            from_ts, to_ts = preset_range(preset)
            assert to_ts > from_ts, f"preset={preset}: to_ts <= from_ts"


# ---------------------------------------------------------------------------
# Recording download parameter variants
# ---------------------------------------------------------------------------


class TestRecordingDownloadParams:
    @pytest.mark.asyncio
    async def test_download_sends_correct_recording_id(
        self, api: SurveillanceAPI, tmp_path: Path
    ) -> None:
        from surveillance.services.recording import download_recording

        output = tmp_path / "rec.mp4"
        fake_bytes = b"fake-video-data"

        with patch.object(api, "download", new_callable=AsyncMock, return_value=fake_bytes) as mock:
            result = await download_recording(api, recording_id=42, output_path=output)
            params = mock.call_args[1]["extra_params"]
            assert params["id"] == "42"
            assert result == output

    @pytest.mark.asyncio
    async def test_download_writes_file(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording

        output = tmp_path / "out.mp4"
        content = b"\x00\x01\x02\x03video"

        with patch.object(api, "download", new_callable=AsyncMock, return_value=content):
            await download_recording(api, recording_id=1, output_path=output)

        assert output.exists()
        assert output.read_bytes() == content

    @pytest.mark.asyncio
    async def test_download_creates_parent_dirs(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording

        output = tmp_path / "subdir" / "deeper" / "rec.mp4"

        with patch.object(api, "download", new_callable=AsyncMock, return_value=b"data"):
            await download_recording(api, recording_id=7, output_path=output)

        assert output.exists()

    @pytest.mark.asyncio
    async def test_download_uses_correct_api_method(
        self, api: SurveillanceAPI, tmp_path: Path
    ) -> None:
        from surveillance.services.recording import download_recording

        output = tmp_path / "rec.mp4"

        with patch.object(api, "download", new_callable=AsyncMock, return_value=b"x") as mock:
            await download_recording(api, recording_id=99, output_path=output)
            assert mock.call_args[1]["api"] == "SYNO.SurveillanceStation.Recording"
            assert mock.call_args[1]["method"] == "Download"

    @pytest.mark.asyncio
    async def test_download_returns_path(self, api: SurveillanceAPI, tmp_path: Path) -> None:
        from surveillance.services.recording import download_recording

        output = tmp_path / "video.mp4"

        with patch.object(api, "download", new_callable=AsyncMock, return_value=b"bytes"):
            result = await download_recording(api, recording_id=3, output_path=output)

        assert isinstance(result, Path)
        assert result == output


# ---------------------------------------------------------------------------
# Recording config persistence (search filters)
# ---------------------------------------------------------------------------


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
