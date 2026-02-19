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

"""Async HTTP client for Synology Surveillance Station API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from surveillance.api.auth import AuthError, SessionExpiredError, login
from surveillance.api.models import ApiInfo
from surveillance.config import ConnectionProfile

log = logging.getLogger(__name__)

# Synology API error codes
ERRORS: dict[int, str] = {
    100: "Unknown error",
    101: "Invalid parameters",
    102: "API does not exist",
    103: "Method does not exist",
    104: "This API version is not supported",
    105: "Insufficient user privilege",
    106: "Connection time out",
    107: "Multiple login detected",
    119: "SID not found",
    400: "Execution failed",
    401: "Parameter invalid",
    402: "Camera disabled",
    407: "CMS closed",
    412: "Need to run as admin",
    413: "Need to enable home mode first",
}

SESSION_ERRORS = {105, 106, 107, 119}


class ApiError(Exception):
    """API call failed."""

    def __init__(self, code: int, message: str = "") -> None:
        self.code = code
        self.message = message or ERRORS.get(code, f"Unknown error ({code})")
        super().__init__(self.message)


class SurveillanceAPI:
    """Async client for Synology Surveillance Station REST API."""

    def __init__(self, profile: ConnectionProfile) -> None:
        self.profile = profile
        self.base_url = profile.base_url
        self.sid = ""
        self.username = ""
        self.password = ""
        self._api_info: dict[str, ApiInfo] = {}
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                verify=self.profile.verify_ssl,
                timeout=30.0,
                http2=True,
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20,
                ),
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def discover_apis(self) -> None:
        """Discover available APIs via SYNO.API.Info."""
        resp = await self.client.get(
            "/webapi/query.cgi",
            params={
                "api": "SYNO.API.Info",
                "version": 1,
                "method": "Query",
                "query": "all",
            },
        )
        resp.raise_for_status()
        result = resp.json()

        if not result.get("success"):
            raise ApiError(result.get("error", {}).get("code", 100))

        for name, info in result.get("data", {}).items():
            self._api_info[name] = ApiInfo.from_api(info)

        log.debug("Discovered %d APIs", len(self._api_info))

    def _get_api_path(self, api_name: str) -> str:
        """Get CGI path for an API, falling back to entry.cgi."""
        info = self._api_info.get(api_name)
        if info:
            return f"/webapi/{info.path}"
        return "/webapi/entry.cgi"

    def _get_api_version(self, api_name: str, requested: int | None = None) -> int:
        """Get version to use for an API call."""
        info = self._api_info.get(api_name)
        if info and requested:
            return min(requested, info.max_version)
        if info:
            return info.max_version
        return requested or 1

    async def raw_request(
        self,
        api: str,
        method: str,
        version: int = 1,
        extra_params: dict[str, Any] | None = None,
    ) -> Any:
        """Make a raw API request without session error handling.

        Returns the 'data' field from the response (dict or list).
        """
        path = self._get_api_path(api)
        ver = self._get_api_version(api, version)

        params: dict[str, Any] = {
            "api": api,
            "version": ver,
            "method": method,
        }
        if self.sid:
            params["_sid"] = self.sid
        if extra_params:
            params.update(extra_params)

        resp = await self.client.get(path, params=params)
        resp.raise_for_status()
        result = resp.json()

        if not result.get("success"):
            code = result.get("error", {}).get("code", 100)
            raise ApiError(code)

        data: Any = result.get("data", {})
        return data

    async def request(
        self,
        api: str,
        method: str,
        version: int = 1,
        extra_params: dict[str, Any] | None = None,
    ) -> Any:
        """Make an API request with auto-reconnect on session errors.

        Returns the 'data' field from the response (dict or list).
        """
        try:
            return await self.raw_request(api, method, version, extra_params)
        except ApiError as e:
            if e.code in SESSION_ERRORS and self.username and self.password:
                log.info("Session error %d, attempting re-login", e.code)
                try:
                    await login(self, self.username, self.password)
                except AuthError:
                    raise SessionExpiredError("Re-login failed") from e
                return await self.raw_request(api, method, version, extra_params)
            raise

    async def download(
        self,
        api: str,
        method: str,
        version: int = 1,
        extra_params: dict[str, Any] | None = None,
    ) -> bytes:
        """Download binary data from an API endpoint."""
        path = self._get_api_path(api)
        ver = self._get_api_version(api, version)

        params: dict[str, Any] = {
            "api": api,
            "version": ver,
            "method": method,
        }
        if self.sid:
            params["_sid"] = self.sid
        if extra_params:
            params.update(extra_params)

        resp = await self.client.get(path, params=params)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            result = resp.json()
            if not result.get("success"):
                code = result.get("error", {}).get("code", 100)
                raise ApiError(code)

        content: bytes = resp.content
        return content

    def get_stream_url(self, path: str, extra_params: dict[str, Any] | None = None) -> str:
        """Build a full URL for streaming endpoints."""
        url = f"{self.base_url}/webapi/{path}"
        params = []
        if self.sid:
            params.append(f"_sid={self.sid}")
        if extra_params:
            for k, v in extra_params.items():
                params.append(f"{k}={v}")
        if params:
            url += "?" + "&".join(params)
        return url
