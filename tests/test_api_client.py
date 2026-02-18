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

"""Tests for API client."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from surveillance.api.client import ApiError, SurveillanceAPI
from surveillance.config import ConnectionProfile


@pytest.fixture
def profile() -> ConnectionProfile:
    return ConnectionProfile(
        name="test", host="192.168.1.100", port=5001, https=True, verify_ssl=False
    )


@pytest.fixture
def api(profile: ConnectionProfile) -> SurveillanceAPI:
    client = SurveillanceAPI(profile)
    client.sid = "test-sid"
    return client


class TestSurveillanceAPI:
    def test_init(self, profile: ConnectionProfile) -> None:
        api = SurveillanceAPI(profile)
        assert api.base_url == "https://192.168.1.100:5001"
        assert api.sid == ""

    @pytest.mark.asyncio
    @respx.mock
    async def test_discover_apis(self, api: SurveillanceAPI) -> None:
        respx.get(
            "https://192.168.1.100:5001/webapi/query.cgi",
        ).mock(
            return_value=Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "SYNO.API.Auth": {
                            "path": "entry.cgi",
                            "minVersion": 1,
                            "maxVersion": 7,
                        },
                        "SYNO.SurveillanceStation.Camera": {
                            "path": "entry.cgi",
                            "minVersion": 1,
                            "maxVersion": 9,
                        },
                    },
                },
            )
        )

        await api.discover_apis()
        assert "SYNO.API.Auth" in api._api_info
        assert api._api_info["SYNO.API.Auth"].max_version == 7

    @pytest.mark.asyncio
    @respx.mock
    async def test_raw_request_success(self, api: SurveillanceAPI) -> None:
        respx.get(
            "https://192.168.1.100:5001/webapi/entry.cgi",
        ).mock(
            return_value=Response(
                200,
                json={
                    "success": True,
                    "data": {"cameras": [{"id": 1, "name": "Test"}]},
                },
            )
        )

        data = await api.raw_request(
            api="SYNO.SurveillanceStation.Camera",
            method="List",
            version=9,
        )
        assert "cameras" in data
        assert data["cameras"][0]["id"] == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_raw_request_error(self, api: SurveillanceAPI) -> None:
        respx.get(
            "https://192.168.1.100:5001/webapi/entry.cgi",
        ).mock(
            return_value=Response(
                200,
                json={"success": False, "error": {"code": 102}},
            )
        )

        with pytest.raises(ApiError) as exc_info:
            await api.raw_request(
                api="SYNO.SurveillanceStation.Camera",
                method="List",
            )
        assert exc_info.value.code == 102

    def test_get_stream_url(self, api: SurveillanceAPI) -> None:
        url = api.get_stream_url("entry.cgi", {"api": "SYNO.Test", "method": "Stream"})
        assert "192.168.1.100:5001" in url
        assert "_sid=test-sid" in url
        assert "api=SYNO.Test" in url

    @pytest.mark.asyncio
    async def test_close(self, api: SurveillanceAPI) -> None:
        # Access client to create it
        _ = api.client
        await api.close()
        assert api._client is None
