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

"""Snapshot management service."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from surveillance.api.models import Snapshot

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI


async def take_snapshot(api: SurveillanceAPI, camera_id: int) -> bytes:
    """Take a live snapshot from a camera. Returns image bytes."""
    return await api.download(
        api="SYNO.SurveillanceStation.Camera",
        method="GetSnapshot",
        version=9,
        extra_params={
            "cameraId": str(camera_id),
        },
    )


async def list_snapshots(
    api: SurveillanceAPI,
    camera_id: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Snapshot], int]:
    """List saved snapshots.

    Returns (snapshots, total_count).
    """
    params: dict[str, str] = {
        "offset": str(offset),
        "limit": str(limit),
    }
    if camera_id is not None:
        params["cameraId"] = str(camera_id)

    data = await api.request(
        api="SYNO.SurveillanceStation.SnapShot",
        method="List",
        version=1,
        extra_params=params,
    )

    snapshots = [Snapshot.from_api(s) for s in data.get("data", data.get("snapshot", []))]
    total = data.get("total", len(snapshots))
    return snapshots, total


async def download_snapshot(
    api: SurveillanceAPI,
    snapshot_id: int,
    output_path: Path,
) -> Path:
    """Download a snapshot to disk."""
    data = await api.download(
        api="SYNO.SurveillanceStation.SnapShot",
        method="Download",
        version=1,
        extra_params={"id": str(snapshot_id)},
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return output_path


async def save_snapshot(api: SurveillanceAPI, camera_id: int, output_path: Path) -> Path:
    """Take and save a snapshot to disk."""
    data = await take_snapshot(api, camera_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return output_path


async def delete_snapshot(api: SurveillanceAPI, snapshot_id: int) -> None:
    """Delete a snapshot."""
    await api.request(
        api="SYNO.SurveillanceStation.SnapShot",
        method="Delete",
        version=1,
        extra_params={"idList": str(snapshot_id)},
    )
