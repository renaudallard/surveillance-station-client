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

"""Tests for the E-Map service, models and the Maps page geometry."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from surveillance.api.auth import SessionExpiredError
from surveillance.api.client import ApiError, SurveillanceAPI
from surveillance.api.models import ApiInfo, Camera, CameraStatus, EMap, EMapItem
from surveillance.config import ConnectionProfile
from surveillance.services.emap import (
    EMAP_API,
    EMAP_IMAGE_API,
    EMapService,
    EMapsTooOldError,
    emaps_unavailable,
    marker_to_pixels,
    parse_map,
    parse_maps,
    pixels_to_marker,
)

# Shaped like Surveillance Station 9.3's answers, with placeholder names.
LIST_DATA: dict[str, Any] = {
    "emap": [
        {
            "id": 2,
            "name": "Map B",
            "filename": "emap-2.svg",
            "filesize": 2048,
            "imgWidth": 1600,
            "imgHeight": 1000,
            "itemTotal": 0,
            "privilege": 1,
            "favorViews": [],
        },
        {
            "id": 1,
            "name": "Map A",
            "filename": "emap-1.png",
            "filesize": 4096,
            "imgWidth": 800,
            "imgHeight": 600,
            "itemTotal": 3,
            "privilege": 1,
            "favorViews": [],
        },
        {"name": "no id"},
        "not a map",
    ],
    "total": 4,
}


def _item(item_id: int, ref_type: int, ref_id: int, lng: float, lat: float) -> dict[str, Any]:
    return {
        "id": item_id,
        "emapId": 1,
        "emapType": 1,
        "refItemType": ref_type,
        "refItemIdOnHost": ref_id,
        "refItemIdx": 0,
        "lat": lat,
        "lng": lng,
        "regionType": 1,
        "radius": 20000,
        "direction": 90,
        "viewAngle": 100,
        "vertices": [],
    }


LOAD_DATA: dict[str, Any] = {
    "emap": [
        {
            **LIST_DATA["emap"][1],
            "item": [
                _item(11, 0, 5, 0.4, 0.75),
                _item(12, 0, 7, 1.2, 0.25),
                _item(13, 2, 3, 0.5, 0.5),
                {"id": 14, "refItemType": 0, "refItemIdOnHost": 9, "lat": "x", "lng": 0.1},
                {"id": 15, "refItemType": 0, "lat": 0.1, "lng": 0.1},
            ],
        }
    ],
    "total": 1,
}


class TestCoordinates:
    """Both axes are fractions of the image HEIGHT, origin bottom-left."""

    def test_marker_to_pixels(self) -> None:
        assert marker_to_pixels(0.4, 0.75, 1000) == pytest.approx((400, 250))

    def test_pixels_to_marker(self) -> None:
        assert pixels_to_marker(400, 250, 1000) == pytest.approx((0.4, 0.75))

    def test_lng_past_one_on_a_landscape_image_is_not_clamped(self) -> None:
        # The right edge of a 1600x1000 image.
        assert marker_to_pixels(1.6, 0.0, 1000) == pytest.approx((1600, 1000))
        assert pixels_to_marker(1600, 0, 1000) == pytest.approx((1.6, 1.0))

    def test_round_trip(self) -> None:
        for x, y in [(0, 0), (123.5, 456.25), (1599, 999)]:
            lng, lat = pixels_to_marker(x, y, 1000)
            assert marker_to_pixels(lng, lat, 1000) == pytest.approx((x, y))


class TestWidgetGeometry:
    def test_fit_letterboxes_a_wide_image(self) -> None:
        from surveillance.ui.emaps import fit_image

        # 1600x1000 into 800x800: scale 0.5, 150 px bands top and bottom.
        assert fit_image(1600, 1000, 800, 800) == pytest.approx((0, 150, 0.5))

    def test_fit_of_nothing_is_nothing(self) -> None:
        from surveillance.ui.emaps import fit_image

        assert fit_image(0, 0, 800, 800) == (0.0, 0.0, 0.0)
        assert fit_image(100, 100, 0, 0) == (0.0, 0.0, 0.0)

    def test_marker_scales_with_the_drawn_height(self) -> None:
        from surveillance.ui.emaps import marker_to_widget

        # Drawn at 800x500 in an 800x800 area: y offset 150, scale 0.5.
        assert marker_to_widget(1.6, 0.0, 1600, 1000, 800, 800) == pytest.approx((800, 650))
        assert marker_to_widget(0.4, 0.75, 1600, 1000, 800, 800) == pytest.approx((200, 275))

    def test_marker_at_picks_the_nearest_within_reach(self) -> None:
        from surveillance.ui.emaps import marker_at

        points = [(10.0, 10.0), (20.0, 10.0)]
        assert marker_at(points, 18, 10, 12) == 1
        assert marker_at(points, 12, 10, 12) == 0
        assert marker_at(points, 100, 100, 12) is None
        assert marker_at([], 0, 0, 12) is None

    def test_marker_colour_follows_camera_status(self) -> None:
        from surveillance.ui import emaps

        def cam(status: int) -> Camera:
            return Camera(id=1, name="cam", model="", vendor="", status=CameraStatus(status))

        assert emaps.marker_colour(cam(1)) == emaps._COLOUR_ENABLED
        assert emaps.marker_colour(cam(3)) == emaps._COLOUR_OFFLINE
        assert emaps.marker_colour(cam(7)) == emaps._COLOUR_OFFLINE
        assert emaps.marker_colour(None) == emaps._COLOUR_UNKNOWN


class TestParsing:
    def test_list_keeps_maps_with_an_id(self) -> None:
        maps = parse_maps(LIST_DATA)
        assert [m.id for m in maps] == [2, 1]
        assert maps[0] == EMap(id=2, name="Map B", image_name="emap-2.svg", width=1600, height=1000)

    def test_list_of_nothing(self) -> None:
        assert parse_maps(None) == []
        assert parse_maps({}) == []
        assert parse_maps({"emap": None}) == []

    def test_load_parses_items_and_skips_malformed_ones(self) -> None:
        emap = parse_map(LOAD_DATA)
        assert emap is not None
        assert emap.id == 1
        assert [i.id for i in emap.items] == [11, 12, 13]
        assert [row["id"] for row in emap.extra_rows] == [14, 15]
        assert emap.items[0] == EMapItem(
            id=11,
            emap_id=1,
            emap_type=1,
            ref_item_type=0,
            ref_item_id=5,
            lng=0.4,
            lat=0.75,
            direction=90,
            view_angle=100,
        )

    def test_only_camera_items_are_markers(self) -> None:
        emap = parse_map(LOAD_DATA)
        assert emap is not None
        assert [m.ref_item_id for m in emap.cameras] == [5, 7]

    def test_load_of_no_map(self) -> None:
        assert parse_map({"emap": []}) is None

    def test_image_name_falls_back_to_older_keys(self) -> None:
        assert EMap.from_api({"id": 1, "imgName": "a.png"}).image_name == "a.png"
        assert EMap.from_api({"id": 1, "imageName": "b.png"}).image_name == "b.png"
        assert EMap.from_api({"id": 1}).image_name == ""

    def test_missing_or_bad_size_is_zero(self) -> None:
        emap = EMap.from_api({"id": 1, "imgWidth": "wide"})
        assert (emap.width, emap.height) == (0, 0)


def _api() -> SurveillanceAPI:
    api = SurveillanceAPI(ConnectionProfile(name="t", host="nas", port=5001, https=True))
    api.sid, api.username, api.password = "old", "user", "pw"
    return api


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data})


def _error(code: int) -> httpx.Response:
    return httpx.Response(200, json={"success": False, "error": {"code": code}})


class TestService:
    def test_requests_send_strings_json_quoted(self) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            seen.append(params)
            if params["api"] == EMAP_IMAGE_API:
                return httpx.Response(
                    200, content=b"\x89PNG", headers={"content-type": "image/png"}
                )
            if params["method"] == "List":
                return _ok(LIST_DATA)
            return _ok(LOAD_DATA)

        service = EMapService(_api())
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="nas").mock(side_effect=handler)
            maps = asyncio.run(service.list_maps())
            emap, image = asyncio.run(service.load_map_with_image(1))

        assert [m.id for m in maps] == [2, 1]
        assert emap is not None
        assert image == b"\x89PNG"
        listed, loaded, fetched = seen
        assert (listed["api"], listed["method"], listed["version"]) == (EMAP_API, "List", "1")
        assert (loaded["method"], loaded["version"]) == ("Load", "2")
        assert loaded["emapIds"] == json.dumps("1")
        assert (fetched["method"], fetched["version"]) == ("Load", "1")
        assert fetched["filename"] == '"emap-1.png"'

    def test_maps_and_images_are_cached_until_cleared(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.params["api"])
            if request.url.params["api"] == EMAP_IMAGE_API:
                return httpx.Response(200, content=b"img", headers={"content-type": "image/png"})
            return _ok(LOAD_DATA)

        service = EMapService(_api())
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="nas").mock(side_effect=handler)
            asyncio.run(service.load_map_with_image(1))
            asyncio.run(service.load_map_with_image(1))
            assert calls == [EMAP_API, EMAP_IMAGE_API]
            service.clear()
            asyncio.run(service.load_map_with_image(1))
        assert calls == [EMAP_API, EMAP_IMAGE_API] * 2

    def test_a_map_without_an_image_fetches_none(self) -> None:
        service = EMapService(_api())
        with respx.mock(assert_all_called=False) as mock:
            route = mock.route(host="nas").mock(return_value=_ok({"emap": [{"id": 4}]}))
            emap, image = asyncio.run(service.load_map_with_image(4))
        assert emap is not None
        assert image is None
        assert route.call_count == 1

    def test_an_image_error_is_raised(self) -> None:
        service = EMapService(_api())
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="nas").mock(return_value=_error(400))
            with pytest.raises(ApiError) as exc:
                asyncio.run(service.load_image("emap-1.png"))
        assert exc.value.code == 400

    @pytest.mark.parametrize("code", [102, 103, 104])
    def test_no_emap_api_means_unavailable(self, code: int) -> None:
        service = EMapService(_api())
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="nas").mock(return_value=_error(code))
            with pytest.raises(ApiError) as exc:
                asyncio.run(service.list_maps())
        assert emaps_unavailable(exc.value)

    def test_105_after_a_fresh_login_means_unavailable(self) -> None:
        logins: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if request.method == "POST":
                params.update(dict(httpx.QueryParams(request.content.decode())))
            if params.get("api") == "SYNO.API.Auth":
                logins.append(params.get("method", ""))
                return _ok({"sid": "new"})
            return _error(105)

        service = EMapService(_api())
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="nas").mock(side_effect=handler)
            with pytest.raises(ApiError) as exc:
                asyncio.run(service.list_maps())
        assert exc.value.code == 105
        assert logins == ["Login"]
        assert emaps_unavailable(exc.value)

    def test_other_errors_are_not_unavailable(self) -> None:
        assert not emaps_unavailable(ApiError(400))
        assert not emaps_unavailable(ApiError(119))
        assert not emaps_unavailable(httpx.ConnectError("down"))
        assert emaps_unavailable(SessionExpiredError("relogin failed"))


class TestLoadErrorMessage:
    @staticmethod
    def _page() -> SimpleNamespace:
        shown: list[str] = []
        return SimpleNamespace(
            _refresh_btn=MagicMock(),
            listbox=MagicMock(),
            _unavailable=False,
            _listed=True,
            shown=shown,
            _show_message=shown.append,
            # Losing the maps also ends an edit in progress.
            _emap=None,
            _end_edit=MagicMock(),
            _update_edit_controls=MagicMock(),
        )

    def test_a_failed_relogin_says_so(self) -> None:
        from surveillance.ui.emaps import EMapsView

        page = self._page()
        EMapsView._on_load_error(page, SessionExpiredError("relogin failed"))  # type: ignore[arg-type]
        assert page._unavailable
        assert "logging in to the NAS again failed" in page.shown[0]

    def test_an_old_emap_api_says_so(self) -> None:
        from surveillance.ui.emaps import EMapsView

        page = self._page()
        EMapsView._on_load_error(page, EMapsTooOldError("v1"))  # type: ignore[arg-type]
        assert page._unavailable
        assert "older than the one the Maps page reads" in page.shown[0]

    def test_no_privilege_says_unavailable(self) -> None:
        from surveillance.ui.emaps import EMapsView

        page = self._page()
        EMapsView._on_load_error(page, ApiError(105))  # type: ignore[arg-type]
        assert page._unavailable
        assert page.shown[0].startswith("E-Maps are not available")


class TestEmapVersion:
    def test_a_nas_without_load_v2_is_refused_before_any_request(self) -> None:
        api = _api()
        api._api_info[EMAP_API] = ApiInfo(path="entry.cgi", max_version=1)
        with respx.mock(assert_all_called=False) as mock:
            route = mock.route(host="nas").respond(200)
            with pytest.raises(EMapsTooOldError) as exc:
                asyncio.run(EMapService(api).list_maps())
        assert not route.called
        assert emaps_unavailable(exc.value)

    def test_load_v2_is_enough(self) -> None:
        api = _api()
        api._api_info[EMAP_API] = ApiInfo(path="entry.cgi", max_version=2)
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="nas").mock(return_value=_ok(LIST_DATA))
            maps = asyncio.run(EMapService(api).list_maps())
        assert maps
