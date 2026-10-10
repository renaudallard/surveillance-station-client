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
import platform
from collections.abc import AsyncIterator
from typing import Any

import httpx

from surveillance.api.auth import AuthError, SessionExpiredError, login
from surveillance.api.models import ApiInfo
from surveillance.config import ConnectionProfile

log = logging.getLogger(__name__)

# Big enough that a multi-hundred-MB recording does not cost one thread
# hop per 64 KiB of it, small enough to keep memory flat.
_DOWNLOAD_CHUNK_SIZE = 1 << 20

# Codes 100-119 are shared by every Synology API. Above that the
# namespaces diverge: SYNO.API.Auth and Surveillance Station both define
# 400-407 and mean entirely different things by them, so they need
# separate tables (see _error_table).
COMMON_ERRORS: dict[int, str] = {
    100: "Unknown error",
    101: "Invalid parameters",
    102: "API does not exist",
    103: "Method does not exist",
    104: "This API version is not supported",
    105: "Insufficient user privilege",
    106: "Connection time out",
    107: "Multiple login detected",
    119: "SID not found",
}

ERRORS: dict[int, str] = {
    **COMMON_ERRORS,
    400: "Execution failed",
    401: "Parameter invalid",
    402: "Camera disabled",
    407: "CMS closed",
    412: "Need to run as admin",
    413: "Need to enable home mode first",
}

AUTH_ERRORS: dict[int, str] = {
    **COMMON_ERRORS,
    400: "No such account or incorrect password",
    401: "Account disabled",
    402: "Permission denied",
    403: "Two-factor authentication required",
    404: "Invalid OTP code",
    406: "Two-factor authentication enforced",
    407: "Account blocked after too many failed attempts",
}

AUTH_API = "SYNO.API.Auth"
SESSION_ERRORS = {105, 106, 107, 119}
OTP_ERRORS = {403, 404, 406}


def _error_table(api: str) -> dict[int, str]:
    """Pick the code table for *api*'s namespace."""
    return AUTH_ERRORS if api.startswith(AUTH_API) else ERRORS


class ApiError(Exception):
    """API call failed.

    *table* selects the code namespace; it defaults to Surveillance
    Station's, since that is all but a handful of this client's calls.
    """

    def __init__(self, code: int, message: str = "", table: dict[int, str] | None = None) -> None:
        self.code = code
        self.message = message or (table or ERRORS).get(code, f"Unknown error ({code})")
        super().__init__(self.message)


class HttpStatusError(Exception):
    """The server answered with a non-2xx HTTP status.

    Deliberately not an ApiError and deliberately carrying no URL: every
    request but the login puts the session id in the query string, and
    httpx's own HTTPStatusError quotes the whole URL in its message, which
    then reaches the user's screen.
    HTTP status numbers also collide with Synology's own codes (403 and
    404 mean two-factor prompts there), so this cannot share ApiError.
    """

    def __init__(self, status_code: int, reason: str = "") -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"Server returned HTTP {status_code}{f' {reason}' if reason else ''}")


class OtpRequiredError(ApiError):
    """Two-factor authentication code required."""


def _raise_for_status(resp: httpx.Response) -> None:
    """httpx's raise_for_status() puts the full request URL in the message.

    These URLs carry _sid in their query string and the message ends up
    in user-facing dialogs, so raise our own error with just the status
    instead.
    """
    if resp.is_success:
        return
    raise HttpStatusError(resp.status_code, resp.reason_phrase)


def _json_or_raise(resp: httpx.Response) -> Any:
    """Decode a response body, turning a non-JSON one into an ApiError.

    A reverse proxy in front of DSM answers a request it will not forward
    with an HTML login page under a 200, and httpx then raises ValueError
    from resp.json(). Callers only handle ApiError, so it has to become one.
    Code 119 is what _raw_download already reports for the same page, and it
    is in SESSION_ERRORS, so request() retries it once after a re-login.
    """
    try:
        return resp.json()
    except ValueError as exc:
        raise ApiError(
            119, "Server returned a non-JSON response (session expired or blocked)"
        ) from exc


class SurveillanceAPI:
    """Async client for Synology Surveillance Station REST API."""

    def __init__(self, profile: ConnectionProfile) -> None:
        self.profile = profile
        self.base_url = profile.base_url
        self.sid = ""
        self.username = ""
        self.password = ""
        # Seeded from the profile so the automatic re-login in request()
        # and download() can present the trusted-device token minted by an
        # earlier run. Left empty it would re-login without one, which the
        # NAS answers by demanding an OTP code no background retry can
        # supply. login() overwrites it whenever the server issues a new one.
        self.device_id = profile.device_id
        self._api_info: dict[str, ApiInfo] = {}
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    @property
    def client(self) -> httpx.AsyncClient:
        if self._closed:
            # Logout drops this object and closes it while polls and
            # downloads may still be in flight. Rebuilding the pool here
            # would hand those coroutines a connection nobody owns or will
            # ever close, so make it a plain failure they already handle.
            raise ApiError(0, "API client is closed")
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
        """Close the HTTP client permanently.

        The object is not reusable afterwards: the client property refuses
        to build a replacement pool rather than resurrecting one behind a
        caller that has already been dropped.
        """
        self._closed = True
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
        _raise_for_status(resp)
        result = _json_or_raise(resp)

        if not result.get("success"):
            raise ApiError(result.get("error", {}).get("code", 100), table=COMMON_ERRORS)

        for name, info in result.get("data", {}).items():
            self._api_info[name] = ApiInfo.from_api(info)

        log.debug("Discovered %d APIs", len(self._api_info))

    def has_api(self, api_name: str) -> bool:
        """True if discover_apis() found *api_name* on this NAS."""
        return api_name in self._api_info

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
        timeout: float | None = None,
        post: bool = False,
    ) -> Any:
        """Make a raw API request without session error handling.

        *timeout* overrides the client's default (30s) for this call only —
        for endpoints that can legitimately take longer, e.g. RecordingPicker
        ::EnumInterval over a wide time range with many cameras. Left unset,
        the client default applies.

        *post* sends the parameters as a form body rather than in the URL,
        for the ones that must not end up in a proxy's or DSM's access log
        (see auth.login).

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

        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if post:
            resp = await self.client.post(path, data=params, **kwargs)
        else:
            resp = await self.client.get(path, params=params, **kwargs)
        _raise_for_status(resp)
        result = _json_or_raise(resp)

        if not result.get("success"):
            code = result.get("error", {}).get("code", 100)
            syno_msg = result.get("error", {}).get("message", "")
            # OTP codes only mean two-factor under SYNO.API.Auth; 403 and
            # 404 mean something else entirely to Surveillance Station.
            if api.startswith(AUTH_API) and code in OTP_ERRORS:
                raise OtpRequiredError(code, syno_msg, AUTH_ERRORS)
            raise ApiError(code, syno_msg, _error_table(api))

        data: Any = result.get("data", {})
        return data

    async def request(
        self,
        api: str,
        method: str,
        version: int = 1,
        extra_params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Make an API request with auto-reconnect on session errors.

        Returns the 'data' field from the response (dict or list).
        """
        try:
            return await self.raw_request(api, method, version, extra_params, timeout=timeout)
        except ApiError as e:
            if e.code in SESSION_ERRORS and self.username and self.password:
                log.info("Session error %d, attempting re-login", e.code)
                try:
                    await login(
                        self,
                        self.username,
                        self.password,
                        device_id=self.device_id,
                        device_name=platform.node(),
                    )
                except (AuthError, ApiError) as relogin_exc:
                    # login() reports a rejected password as ApiError, not
                    # AuthError, so catching only the latter let a stale
                    # stored password surface as whatever the failing data
                    # call happened to be.
                    raise SessionExpiredError(str(relogin_exc)) from e
                return await self.raw_request(api, method, version, extra_params, timeout=timeout)
            raise

    async def _raw_download(
        self,
        api: str,
        method: str,
        version: int = 1,
        extra_params: dict[str, Any] | None = None,
    ) -> bytes:
        """Download binary data from an API endpoint (no auto-reconnect)."""
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
        _raise_for_status(resp)

        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            result = _json_or_raise(resp)
            if not result.get("success"):
                code = result.get("error", {}).get("code", 100)
                syno_msg = result.get("error", {}).get("message", "")
                raise ApiError(code, syno_msg)
        elif "text/html" in content_type:
            # DSM redirected to the login page — the session has expired.
            log.warning(
                "Download endpoint returned HTML (content-type=%s); session may have expired",
                content_type,
            )
            raise ApiError(
                119,
                "Server returned an HTML page — session expired or access denied",
            )

        content: bytes = resp.content
        if not content:
            raise ApiError(100, "Server returned an empty response body")

        return content

    async def _raw_stream_download(
        self,
        api: str,
        method: str,
        version: int = 1,
        extra_params: dict[str, Any] | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream binary data from an API endpoint (no auto-reconnect).

        Everything DSM can report instead of a file — an HTTP status, a
        JSON error, an HTML login page — is raised before the first chunk
        is yielded, so a caller that has started receiving data knows the
        response really is the file.
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

        async with self.client.stream("GET", path, params=params) as resp:
            if not resp.is_success:
                await resp.aread()
            _raise_for_status(resp)

            content_type = resp.headers.get("content-type", "")
            if "json" in content_type:
                await resp.aread()
                result = _json_or_raise(resp)
                code = result.get("error", {}).get("code", 100)
                syno_msg = result.get("error", {}).get("message", "")
                raise ApiError(code, syno_msg)
            if "text/html" in content_type:
                # DSM redirected to the login page — the session has expired.
                log.warning(
                    "Download endpoint returned HTML (content-type=%s); session may have expired",
                    content_type,
                )
                raise ApiError(
                    119,
                    "Server returned an HTML page — session expired or access denied",
                )

            empty = True
            async for chunk in resp.aiter_bytes(_DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    empty = False
                    yield chunk
            if empty:
                raise ApiError(100, "Server returned an empty response body")

    async def stream_download(
        self,
        api: str,
        method: str,
        version: int = 1,
        extra_params: dict[str, Any] | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream binary data with auto-reconnect on session errors.

        Unlike download(), the body never has to fit in memory, so a
        multi-gigabyte recording costs one chunk rather than the whole
        file (twice over, while httpx joins its chunk list).

        The re-login retry only applies before any data has been handed
        out — a stream that fails midway cannot be resumed transparently.
        """
        started = False
        try:
            async for chunk in self._raw_stream_download(api, method, version, extra_params):
                started = True
                yield chunk
        except ApiError as e:
            if started or e.code not in SESSION_ERRORS or not (self.username and self.password):
                raise
            log.info("Session error %d during download, attempting re-login", e.code)
            try:
                await login(
                    self,
                    self.username,
                    self.password,
                    device_id=self.device_id,
                    device_name=platform.node(),
                )
            except (AuthError, ApiError) as relogin_exc:
                raise SessionExpiredError(str(relogin_exc)) from e
        else:
            return

        async for chunk in self._raw_stream_download(api, method, version, extra_params):
            yield chunk

    async def download(
        self,
        api: str,
        method: str,
        version: int = 1,
        extra_params: dict[str, Any] | None = None,
    ) -> bytes:
        """Download binary data with auto-reconnect on session errors."""
        try:
            return await self._raw_download(api, method, version, extra_params)
        except ApiError as e:
            if e.code in SESSION_ERRORS and self.username and self.password:
                log.info("Session error %d during download, attempting re-login", e.code)
                try:
                    await login(
                        self,
                        self.username,
                        self.password,
                        device_id=self.device_id,
                        device_name=platform.node(),
                    )
                except (AuthError, ApiError) as relogin_exc:
                    # login() reports a rejected password as ApiError, not
                    # AuthError, so catching only the latter let a stale
                    # stored password surface as whatever the failing data
                    # call happened to be.
                    raise SessionExpiredError(str(relogin_exc)) from e
                return await self._raw_download(api, method, version, extra_params)
            raise

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
