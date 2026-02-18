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

"""Authentication management for Synology API."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI


class AuthError(Exception):
    """Authentication failure."""


class SessionExpiredError(AuthError):
    """Session has expired, needs re-login."""


async def login(api: SurveillanceAPI, username: str, password: str) -> str:
    """Login to Synology and return session ID.

    Uses SYNO.API.Auth with Surveillance Station session.
    """
    data = await api.raw_request(
        api="SYNO.API.Auth",
        method="Login",
        version=6,
        extra_params={
            "account": username,
            "passwd": password,
            "session": "SurveillanceStation",
            "format": "sid",
        },
    )

    sid = data.get("sid", "")
    if not sid:
        raise AuthError("Login succeeded but no SID returned")

    api.sid = sid
    api.username = username
    api.password = password
    return str(sid)


async def logout(api: SurveillanceAPI) -> None:
    """Logout from Synology, invalidating the session."""
    if not api.sid:
        return

    try:
        await api.raw_request(
            api="SYNO.API.Auth",
            method="Logout",
            version=6,
            extra_params={"session": "SurveillanceStation"},
        )
    except Exception:
        pass
    finally:
        api.sid = ""
