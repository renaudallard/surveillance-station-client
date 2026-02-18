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

"""Secure credential storage via keyring."""

from __future__ import annotations

import keyring

SERVICE_NAME = "surveillance-station"


def store_credentials(profile_name: str, username: str, password: str) -> None:
    """Store credentials for a connection profile."""
    keyring.set_password(SERVICE_NAME, f"{profile_name}:username", username)
    keyring.set_password(SERVICE_NAME, f"{profile_name}:password", password)


def get_credentials(profile_name: str) -> tuple[str, str] | None:
    """Retrieve credentials for a connection profile.

    Returns (username, password) or None if not found.
    """
    username = keyring.get_password(SERVICE_NAME, f"{profile_name}:username")
    password = keyring.get_password(SERVICE_NAME, f"{profile_name}:password")
    if username is None or password is None:
        return None
    return username, password


def delete_credentials(profile_name: str) -> None:
    """Delete credentials for a connection profile."""
    try:
        keyring.delete_password(SERVICE_NAME, f"{profile_name}:username")
    except keyring.errors.PasswordDeleteError:
        pass
    try:
        keyring.delete_password(SERVICE_NAME, f"{profile_name}:password")
    except keyring.errors.PasswordDeleteError:
        pass
