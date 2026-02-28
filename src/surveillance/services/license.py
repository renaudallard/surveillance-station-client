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

"""License management service."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from surveillance.api.models import LicenseInfo

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI

log = logging.getLogger(__name__)


async def load_licenses(api: SurveillanceAPI) -> LicenseInfo:
    """Load license information from the NAS."""
    data = await api.request(
        api="SYNO.SurveillanceStation.License",
        method="Load",
        version=1,
    )
    return LicenseInfo.from_api(data)


async def delete_license(api: SurveillanceAPI, license_ids: list[int]) -> None:
    """Delete licenses by their IDs."""
    await api.request(
        api="SYNO.SurveillanceStation.License",
        method="DeleteKey",
        version=1,
        extra_params={"lic_list": ",".join(str(i) for i in license_ids)},
    )


async def add_license_online(api: SurveillanceAPI, license_keys: list[str]) -> None:
    """Add licenses via the NAS (online activation)."""
    await api.request(
        api="SYNO.SurveillanceStation.License",
        method="AddKey",
        version=1,
        extra_params={"licenseList": ",".join(license_keys)},
    )


async def get_device_info(api: SurveillanceAPI) -> tuple[str, str]:
    """Get NAS serial number and model via SYNO.SurveillanceStation.Info.

    Returns (serial, model).
    """
    data = await api.request(
        api="SYNO.SurveillanceStation.Info",
        method="GetInfo",
        version=1,
    )
    log.debug("Device info keys: %s", list(data.keys()))
    serial = data.get("serial", data.get("dsSerial", ""))
    model = data.get("model", data.get("dsModel", ""))
    return serial, model


def _offline_encrypt(content: str, serial: str, seed: int) -> str:
    """Encrypt content for offline license activation.

    Uses AES-CBC with a key derived from seed + serial via MD5/SHA1 chains.
    """
    seed_str = str(seed)

    # Step 1: partial = md5(seed + serial)[:12]
    partial = hashlib.md5((seed_str + serial).encode()).hexdigest()[:12]

    # Step 2: key_hex = md5(seed + partial) -> 32 hex chars
    key_hex = hashlib.md5((seed_str + partial).encode()).hexdigest()

    # Step 3: SHA1 chain to derive AES key + IV (need 48 bytes)
    accumulated = b""
    running = b""
    while len(accumulated) < 48:
        running = running + key_hex.encode()
        for _ in range(5):
            running = hashlib.sha1(running).digest()
        accumulated += running

    # Step 4: AES key = first 32 bytes, IV = bytes 32-47
    aes_key = accumulated[:32]
    iv = accumulated[32:48]

    # Step 5: AES-CBC encrypt with PKCS7 padding
    padder = PKCS7(128).padder()
    padded = padder.update(content.encode()) + padder.finalize()

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    return base64.b64encode(ciphertext).decode()


async def offline_get_timestamp(
    api: SurveillanceAPI,
    serial: str = "",
    model: str = "",
) -> int:
    """Get timestamp from Synology license server."""
    if not serial or not model:
        serial, model = await get_device_info(api)
    seed = random.randint(100000, 999999)
    payload = json.dumps({"method": "GetTimestamp"})
    cipher_text = _offline_encrypt(payload, serial, seed)

    async with httpx.AsyncClient(verify=True, timeout=30.0) as client:
        resp = await client.get(
            "https://synosurveillance.synology.com:443/license_activation.php",
            params={
                "cipherText": cipher_text,
                "dsSN": serial,
                "dsModel": model,
                "seed": str(seed),
            },
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        ts = int(result.get("timestamp", 0))
        if not ts:
            raise RuntimeError("License server returned no timestamp")
        return ts


async def offline_activate(api: SurveillanceAPI, license_keys: list[str]) -> dict[str, Any]:
    """Activate licenses offline via Synology license server."""
    serial, model = await get_device_info(api)
    seed = random.randint(100000, 999999)

    payload = json.dumps(
        {
            "method": "Add",
            "dsModel": model,
            "dsSerial": serial,
            "dsMac": "XXXXXXXXXXXX",
            "licenseList": license_keys,
            "version": 2,
            "blOffline": True,
        }
    )
    cipher_text = _offline_encrypt(payload, serial, seed)

    async with httpx.AsyncClient(verify=True, timeout=30.0) as client:
        resp = await client.get(
            "https://synosurveillance.synology.com:443/license_activation.php",
            params={
                "cipherText": cipher_text,
                "dsSN": serial,
                "dsModel": model,
                "seed": str(seed),
            },
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result


async def offline_deactivate(api: SurveillanceAPI, license_keys: list[str]) -> dict[str, Any]:
    """Deactivate licenses offline via Synology license server."""
    serial, model = await get_device_info(api)
    timestamp = await offline_get_timestamp(api, serial, model)
    seed = random.randint(100000, 999999)

    lic_list = [{"dsModel": model, "dsSerial": serial, "key": key} for key in license_keys]
    payload = json.dumps(
        {
            "method": "Delete",
            "licenseList": lic_list,
            "timestamp": timestamp,
            "blOffline": True,
        }
    )
    cipher_text = _offline_encrypt(payload, serial, seed)

    async with httpx.AsyncClient(verify=True, timeout=30.0) as client:
        resp = await client.get(
            "https://synosurveillance.synology.com:443/license_activation.php",
            params={
                "cipherText": cipher_text,
                "dsSN": serial,
                "dsModel": model,
                "seed": str(seed),
            },
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result
